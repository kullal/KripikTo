"""
scanner.py
Modul Pemindai Kripto Kuantitatif & Whale Flow (Tahap 2):
1. Menghitung indikator teknikal bergulir: MA20, MA50, Volume MA20, Highest High 20, dan RSI 14.
2. Mendeteksi sinyal pasar:
   - Volume Explosion / Surge (> 1.5x - 2.0x rata-rata).
   - Breakout 20-period High.
   - Bullish Trend Alignment (Close > MA20 & Close > MA50).
   - Whale Inflow / Accumulation (Taker Buy Ratio > 52% - 55%).
   - Bullish Divergence (Paus memborong agresif saat harga konsolidasi/turun tipis).
   - RSI Momentum & Rebound filter (mencegah beli di pucuk Overbought > 80).
3. Menghitung Trading Plan otomatis:
   - Buy Area, Stop Loss, Target Profit 1 & 2, serta Risk/Reward Ratio.
4. Memberikan skor komposit (0 - 100) dan menyaring Top N koin potensial.
5. Menyimpan hasil scan ke SQLite dan file JSON ringkasan.
"""

import sys
import json
import sqlite3
import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
import pandas as pd
import numpy as np

# Pastikan output utf-8 aman di terminal Windows
if sys.platform == "win32" and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Import path database & macro sentiment & derivatives
try:
    from backend.macro_sentiment import fetch_fear_and_greed_index
    from backend.derivatives_flow import fetch_derivatives_summary, get_derivatives_deltas
except ImportError:
    from macro_sentiment import fetch_fear_and_greed_index
    from derivatives_flow import fetch_derivatives_summary, get_derivatives_deltas

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = str(DATA_DIR / "kripto.db")
SCAN_JSON_PATH = str(DATA_DIR / "scan_latest.json")



try:
    from backend.data_pipeline import (
        is_stablecoin_or_excluded, NON_CRYPTO_PAIRS, STABLECOIN_PAIRS,
        get_active_spot_symbols, get_btc_benchmark, get_btc_benchmark_time_series, fetch_15m_trigger_batch
    )
    from backend.feature_engine import (
        extract_1h_momentum_features,
        classify_setup_and_status,
        calculate_dynamic_tp_sl,
        evaluate_15m_trigger,
        DEFAULT_WEIGHTS
    )
except ImportError:
    from data_pipeline import (
        is_stablecoin_or_excluded, NON_CRYPTO_PAIRS, STABLECOIN_PAIRS,
        get_active_spot_symbols, get_btc_benchmark, get_btc_benchmark_time_series, fetch_15m_trigger_batch
    )
    from feature_engine import (
        extract_1h_momentum_features,
        classify_setup_and_status,
        calculate_dynamic_tp_sl,
        evaluate_15m_trigger,
        DEFAULT_WEIGHTS
    )



def init_scanner_db(db_path: str = DB_PATH) -> None:
    """Inisialisasi tabel hasil pemindaian dan pelacakan hasil (Outcome Tracker) di SQLite."""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS scan_results (
                scan_time TEXT,
                symbol TEXT,
                last_price REAL,
                price_change_pct REAL,
                quote_vol_m REAL,
                taker_buy_ratio REAL,
                funding_rate REAL,
                open_interest_m REAL,
                rsi14 REAL,
                vol_ratio REAL,
                score INTEGER,
                signals TEXT,
                buy_area TEXT,
                stop_loss REAL,
                stop_loss_pct REAL,
                tp1 REAL,
                tp1_pct REAL,
                tp2 REAL,
                tp2_pct REAL,
                risk_reward TEXT,
                interval TEXT,
                candle_close_time TEXT,
                buy_low REAL,
                buy_high REAL,
                entry_price REAL,
                atr14 REAL,
                v1_score INTEGER,
                PRIMARY KEY (scan_time, symbol)
            )
        """)

        # Tabel Historical Signal Outcomes untuk Outcome Tracker (MFE, MAE, Win-rate)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS signal_outcomes (
                scan_time TEXT,
                symbol TEXT,
                interval TEXT,
                entry_price REAL,
                stop_loss REAL,
                tp1 REAL,
                tp2 REAL,
                atr14 REAL,
                v1_score INTEGER,
                is_filled INTEGER DEFAULT 0,
                fill_time TEXT,
                fill_price REAL,
                result TEXT DEFAULT 'PENDING',
                result_time TEXT,
                duration_hours REAL,
                duration_candles INTEGER,
                mfe_pct REAL,
                mae_pct REAL,
                return_12h_pct REAL,
                return_24h_pct REAL,
                return_48h_pct REAL,
                evaluated_at TEXT,
                PRIMARY KEY (scan_time, symbol)
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_outcomes_result 
            ON signal_outcomes (result, scan_time DESC)
        """)

        # Migrasi kolom otomatis jika tabel scan_results lama belum memiliki kolom baru
        cursor.execute("PRAGMA table_info(scan_results)")
        cols = {c[1] for c in cursor.fetchall()}
        new_cols = [
            ("funding_rate", "REAL"),
            ("open_interest_m", "REAL"),
            ("interval", "TEXT"),
            ("candle_close_time", "TEXT"),
            ("buy_low", "REAL"),
            ("buy_high", "REAL"),
            ("entry_price", "REAL"),
            ("atr14", "REAL"),
            ("v1_score", "INTEGER"),
            ("rs_4h", "REAL"),
            ("rs_1h", "REAL"),
            ("coin_return_4h", "REAL"),
            ("coin_return_1h", "REAL"),
            ("btc_return_4h", "REAL"),
            ("btc_return_1h", "REAL"),
            ("setup_type", "TEXT"),
            ("entry_status", "TEXT"),
            ("structure_score", "INTEGER"),
            ("momentum_score", "INTEGER"),
            ("flow_score", "INTEGER"),
            ("derivative_score", "INTEGER"),
            ("macro_score", "INTEGER"),
            ("v2_score", "INTEGER"),
            ("roc_1h", "REAL"),
            ("acceleration_1h", "REAL"),
            ("atr_expansion", "REAL"),
            ("vol_ratio_1h", "REAL"),
            ("delta_oi_1h", "REAL"),
            ("delta_oi_4h", "REAL"),
            ("delta_funding_1h", "REAL")
        ]
        for col, ctype in new_cols:
            if col not in cols:
                try:
                    cursor.execute(f"ALTER TABLE scan_results ADD COLUMN {col} {ctype}")
                except Exception:
                    pass

        # Migrasi kolom signal_outcomes
        cursor.execute("PRAGMA table_info(signal_outcomes)")
        so_cols = {c[1] for c in cursor.fetchall()}
        for col, ctype in [
            ("buy_low", "REAL"), ("buy_high", "REAL"),
            ("rs_4h", "REAL"), ("rs_1h", "REAL"),
            ("btc_return_4h", "REAL"), ("btc_return_1h", "REAL"),
            ("v2_score", "INTEGER"), ("setup_type", "TEXT"),
            ("entry_status", "TEXT"), ("structure_score", "INTEGER"),
            ("momentum_score", "INTEGER"), ("flow_score", "INTEGER"),
            ("derivative_score", "INTEGER"),
            ("delta_oi_1h", "REAL"), ("delta_oi_4h", "REAL"),
            ("delta_funding_1h", "REAL")
        ]:
            if col not in so_cols:
                try:
                    cursor.execute(f"ALTER TABLE signal_outcomes ADD COLUMN {col} {ctype}")
                except Exception:
                    pass
        conn.commit()



def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Menghitung Relative Strength Index (RSI 14)."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-9)
    rsi = 100.0 - (100.0 / (1.0 + rs))
def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Menghitung Average True Range (ATR 14) untuk mengukur volatilitas nyata koin."""
    prev_close = df.groupby("symbol")["close"].shift(1)
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - prev_close).abs()
    tr3 = (df["low"] - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.groupby(df["symbol"]).transform(lambda x: x.rolling(period, min_periods=5).mean())
    return atr.fillna(df["close"] * 0.035)


def run_scanner(
    interval: str = "4h",
    min_score: int = 40,
    top_n: int = 20,
    fgi: Optional[Dict[str, Any]] = None,
    trigger_15m: bool = True,
    db_path: str = DB_PATH
) -> pd.DataFrame:
    """
    Menjalankan pemindaian kuantitatif & whale flow pada seluruh koin di database,
    diselaraskan dengan rezim sentimen makro Crypto Fear & Greed Index.
    Mengembalikan DataFrame berisi Top N koin teratas berdasarkan skor sinyal.
    """
    init_scanner_db(db_path)

    if fgi is None:
        fgi = fetch_fear_and_greed_index(db_path)

    fgi_val = fgi.get("value", 50)
    fgi_regime = fgi.get("regime", "NEUTRAL")

    # Ambil data derivatif (Funding Rate & Open Interest Binance Futures)
    deriv_map = fetch_derivatives_summary(db_path)
    active_spot = get_active_spot_symbols()

    # Ambil benchmark pasar Bitcoin (BTCUSDT) untuk Relative Strength (RS)
    btc_bench = get_btc_benchmark(db_path)
    btc_ret_4h = float(btc_bench.get("btc_return_4h", 0.0))
    btc_ret_1h = float(btc_bench.get("btc_return_1h", 0.0))
    btc_series_4h = get_btc_benchmark_time_series(interval="4h", db_path=db_path)
    btc_series_1h = get_btc_benchmark_time_series(interval="1h", db_path=db_path)

    with sqlite3.connect(db_path) as conn:
        df_klines = pd.read_sql(
            "SELECT * FROM klines_history WHERE interval = ? AND (is_closed = 1 OR is_closed IS NULL) ORDER BY symbol, open_time ASC",
            conn,
            params=(interval,)
        )
        df_summary = pd.read_sql("SELECT * FROM market_summary_24h", conn)
        
        # Ambil data lilin 1H dari database untuk kalkulasi Momentum Kinetik 1H & RS 1H
        df_1h = pd.read_sql(
            "SELECT symbol, open_time, open, high, low, close, volume FROM klines_history WHERE interval = '1h' AND (is_closed = 1 OR is_closed IS NULL) ORDER BY symbol, open_time ASC",
            conn
        )

    if df_klines.empty or df_summary.empty:
        print(f"[!] Data klines interval [{interval}] atau market_summary_24h kosong.")
        print("[*] Jalankan data_pipeline terlebih dahulu.")
        return pd.DataFrame()

    # Ekstraksi fitur momentum kinetik 1H (KripikTo v2)
    feat_1h_map = extract_1h_momentum_features(df_1h, btc_ret_1h=btc_ret_1h) if not df_1h.empty else {}

    # Hitung return 1H per simbol sebagai cadangan
    ret_1h_map = {}
    if not df_1h.empty:
        last_two_1h = df_1h.groupby("symbol").tail(2)
        for s, grp in last_two_1h.groupby("symbol"):
            if len(grp) >= 2:
                c_now = float(grp.iloc[-1]["close"])
                c_prev = float(grp.iloc[-2]["close"])
                if c_prev > 0:
                    ret_1h_map[s] = round(((c_now - c_prev) / c_prev) * 100.0, 2)

    # 1. Hitung Indikator Teknikal Bergulir per Simbol (Vectorized Groupby pada CLOSED candles)
    df_klines["prev_close"] = df_klines.groupby("symbol")["close"].shift(1)
    df_klines["ret_candle"] = ((df_klines["close"] - df_klines["prev_close"]) / df_klines["prev_close"].replace(0, np.nan)) * 100.0
    df_klines["ma20"] = df_klines.groupby("symbol")["close"].transform(lambda x: x.rolling(20, min_periods=5).mean())
    df_klines["ma50"] = df_klines.groupby("symbol")["close"].transform(lambda x: x.rolling(50, min_periods=10).mean())
    df_klines["vol_ma20"] = df_klines.groupby("symbol")["volume"].transform(lambda x: x.rolling(20, min_periods=5).mean())
    df_klines["prev_high20"] = df_klines.groupby("symbol")["high"].transform(lambda x: x.shift(1).rolling(20, min_periods=5).max())
    df_klines["prev_low20"] = df_klines.groupby("symbol")["low"].transform(lambda x: x.shift(1).rolling(20, min_periods=5).min())
    df_klines["rsi14"] = df_klines.groupby("symbol")["close"].transform(lambda x: calculate_rsi(x, period=14))
    df_klines["atr14"] = calculate_atr(df_klines, period=14)

    # Ambil baris candlestick tertutup terbaru (Latest Closed Candle) untuk tiap koin
    latest_tech = df_klines.groupby("symbol").last().reset_index()

    # Gabungkan dengan data ringkasan pasar 24h & Whale Ratio 24h (Pisahkan volume lilin vs volume 24 jam)
    merged = pd.merge(latest_tech, df_summary, on="symbol", how="inner", suffixes=("_candle", "_24h"))

    if merged.empty:
        print("[!] Tidak ada data yang cocok antara klines dan market summary.")
        return pd.DataFrame()

    now_utc_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    results = []

    for _, row in merged.iterrows():
        sym = row["symbol"]
        close = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])

        # Pastikan koin berstatus aktif TRADING di Spot Binance (bukan BREAK/HALT/DELISTED)
        if active_spot and sym not in active_spot:
            continue

        # Filter komprehensif: stablecoin, peg dolar dinamis, token saham & leverage
        if is_stablecoin_or_excluded(sym, last_price=close, high_price=high, low_price=low):
            continue

        signals = []
        v1_score = 0

        # PERBAIKAN BUG VOLUME: Bandingkan volume lilin tertutup tunggal dengan MA20 lilin tertutup
        vol_candle = float(row["volume_candle"])
        vol_ma20 = float(row["vol_ma20"]) if pd.notnull(row["vol_ma20"]) and row["vol_ma20"] > 0 else vol_candle
        vol_ratio = vol_candle / vol_ma20 if vol_ma20 > 0 else 1.0

        ma20 = float(row["ma20"]) if pd.notnull(row["ma20"]) else close
        ma50 = float(row["ma50"]) if pd.notnull(row["ma50"]) else close
        prev_high20 = float(row["prev_high20"]) if pd.notnull(row["prev_high20"]) else high
        prev_low20 = float(row["prev_low20"]) if pd.notnull(row["prev_low20"]) else low
        rsi = float(row["rsi14"]) if pd.notnull(row["rsi14"]) else 50.0

        change_24h = float(row["price_change_pct"])
        quote_vol_24h = float(row["quote_volume_24h"])
        taker_ratio = float(row["taker_buy_ratio"])
        quote_vol_m = quote_vol_24h / 1_000_000.0
        candle_close_time = str(row.get("datetime_utc", ""))

        # Data derivatif & time-series deltas (Fase 4)
        deriv = deriv_map.get(sym, {})
        deltas = get_derivatives_deltas(sym, db_path)
        funding_rate = float(deriv.get("funding_rate", 0.0))
        oi_m = float(deriv.get("open_interest_m", 0.0))
        delta_oi_1h = float(deltas.get("delta_oi_1h", 0.0))
        delta_oi_4h = float(deltas.get("delta_oi_4h", 0.0))
        delta_funding_1h = float(deltas.get("delta_funding_1h", 0.0))

        # Relative Strength 4H vs BTC dengan verifikasi keselarasan timestamp (Fase 2)
        coin_open_time = int(row.get("open_time", 0))
        coin_ret_4h = round(float(row["ret_candle"]), 2) if pd.notnull(row.get("ret_candle")) else 0.0
        btc_matched_ret_4h = btc_series_4h.get(coin_open_time, btc_ret_4h)
        rs_4h = round(coin_ret_4h - btc_matched_ret_4h, 2)

        # Fitur kinetik 1H dari feature_engine
        feat_1h = feat_1h_map.get(sym, {
            "close_1h": close,
            "roc_1h": 0.0,
            "acceleration_1h": 0.0,
            "atr_1h": float(row["atr14"]) if pd.notnull(row.get("atr14")) else (close * 0.035),
            "atr_expansion_ratio": 1.0,
            "vol_ratio_1h": 1.0,
            "rs_1h": 0.0,
            "open_time_1h": 0
        })
        roc_1h = float(feat_1h.get("roc_1h", 0.0))
        accel_1h = float(feat_1h.get("acceleration_1h", 0.0))
        atr_exp = float(feat_1h.get("atr_expansion_ratio", 1.0))
        vol_r_1h = float(feat_1h.get("vol_ratio_1h", 1.0))
        atr_1h = float(feat_1h.get("atr_1h") or row["atr14"] or (close * 0.035))
        coin_ret_1h = roc_1h if roc_1h != 0.0 else ret_1h_map.get(sym, 0.0)

        # RS 1H terverifikasi timestamp
        open_time_1h = int(feat_1h.get("open_time_1h", 0))
        btc_matched_ret_1h = btc_series_1h.get(open_time_1h, btc_ret_1h)
        rs_1h = round(coin_ret_1h - btc_matched_ret_1h, 2)

        # =========================================================================
        # 1. PERHITUNGAN SKOR V1 (LEGACY COMPOSITE - Disimpan untuk Validasi A/B)
        # =========================================================================
        if taker_ratio >= 55.0:
            v1_score += 25
        elif taker_ratio >= 52.0:
            v1_score += 15
        elif taker_ratio < 45.0:
            v1_score -= 15

        if -4.0 <= change_24h <= 2.0 and taker_ratio >= 53.0:
            v1_score += 20

        if funding_rate <= -0.10:
            v1_score += 25
        elif funding_rate <= -0.02:
            v1_score += 15
        elif funding_rate >= +0.05:
            v1_score -= 15

        if oi_m >= 10.0 and funding_rate < -0.01:
            v1_score += 10

        if close > prev_high20 and prev_high20 > 0:
            v1_score += 25
        elif close >= (prev_high20 * 0.985):
            v1_score += 12

        if vol_ratio >= 2.0:
            v1_score += 20
        elif vol_ratio >= 1.4:
            v1_score += 10

        if close > ma20 and close > ma50:
            v1_score += 15 if ma20 > ma50 else 10
        elif close > ma20:
            v1_score += 5
        elif close < ma20 and close < ma50:
            v1_score -= 10

        if 52.0 <= rsi <= 68.0:
            v1_score += 12
        elif rsi < 35.0:
            v1_score += 12
        elif rsi >= 80.0:
            v1_score -= 15

        if change_24h > 35.0:
            v1_score -= 20

        if rs_4h >= 3.0 and rs_1h >= 1.0:
            v1_score += 15
        elif rs_4h >= 1.5:
            v1_score += 8
        elif rs_4h < -2.0 and rs_1h < -1.0:
            v1_score -= 10

        v1_score = max(0, min(100, v1_score))

        # =========================================================================
        # 2. SISTEM SKORING 5 PILAR MODULAR KRIPIKTO V2 (Max 100 Poin)
        # =========================================================================

        # --- PILAR 1: STRUKTUR TREN 4H (Maksimal 25 Poin) ---
        structure_score = 0
        if close > ma20 and close > ma50:
            if ma20 > ma50:
                structure_score += 10
                signals.append("SUPER_BULLISH_TREND")
            else:
                structure_score += 7
                signals.append("BULLISH_CROSS_UP")
        elif close > ma20:
            structure_score += 4
            signals.append("ABOVE_MA20")
        elif close < ma20 and close < ma50:
            structure_score -= 5

        if close > prev_high20 and prev_high20 > 0:
            structure_score += 10
            signals.append("BREAKOUT_20_BAR_HIGH")
        elif close >= (prev_high20 * 0.985):
            structure_score += 5
            signals.append("MENDEKATI_BREAKOUT")

        if rs_4h >= 3.0:
            structure_score += 5
            signals.append(f"OUTPERFORM_BTC_4H (RS:{rs_4h:+.1f}%)")
        elif rs_4h >= 1.5:
            structure_score += 3
        elif rs_4h < -2.0:
            structure_score -= 5
            signals.append(f"UNDERPERFORM_BTC_4H (RS:{rs_4h:+.1f}%)")

        structure_score = max(0, min(DEFAULT_WEIGHTS["structure"], structure_score))

        # --- PILAR 2: KINETIK MOMENTUM 1H (Maksimal 30 Poin) ---
        momentum_score = 0
        # Kecepatan ROC 1H
        if roc_1h >= 2.5:
            momentum_score += 8
            signals.append(f"ROC_1H_STRONG ({roc_1h:+.1f}%)")
        elif roc_1h >= 1.0:
            momentum_score += 5
            signals.append(f"ROC_1H_POSITIVE ({roc_1h:+.1f}%)")
        elif roc_1h < -1.5:
            momentum_score -= 5

        # Akselerasi Harga 1H (Delta ROC / Turunan Kecepatan)
        if accel_1h >= 1.5:
            momentum_score += 7
            signals.append(f"ACCEL_1H_HIGH ({accel_1h:+.1f}%)")
        elif accel_1h >= 0.5:
            momentum_score += 4
        elif accel_1h < -1.5:
            momentum_score -= 4

        # Volatility Expansion Ratio (ATR 1H vs SMA20 ATR 1H)
        if atr_exp >= 1.25:
            momentum_score += 5
            signals.append(f"VOLATILITY_EXPANSION ({atr_exp:.1f}x)")
        elif 0.85 <= atr_exp < 1.25:
            momentum_score += 2

        # Lonjakan Volume 1H
        if vol_r_1h >= 2.0:
            momentum_score += 5
            signals.append(f"VOL_SURGE_1H ({vol_r_1h:.1f}x)")
        elif vol_r_1h >= 1.4:
            momentum_score += 3

        # Relative Strength 1H vs BTC
        if rs_1h >= 1.5:
            momentum_score += 5
            signals.append(f"RS_1H_BEATING_BTC ({rs_1h:+.1f}%)")
        elif rs_1h >= 0.5:
            momentum_score += 3
        elif rs_1h < -1.0:
            momentum_score -= 4

        # RSI filter sehat
        if 52.0 <= rsi <= 68.0:
            momentum_score += 3
        elif rsi < 35.0:
            momentum_score += 3
            signals.append("RSI_OVERSOLD_REBOUND")

        momentum_score = max(0, min(DEFAULT_WEIGHTS["momentum"], momentum_score))

        # --- PILAR 3: WHALE FLOW (ALIRAN DANA PAUS - Maksimal 20 Poin) ---
        flow_score = 0
        if taker_ratio >= 55.0:
            flow_score += 15
            signals.append("PAUS_AKUMULASI_KUAT")
        elif taker_ratio >= 52.0:
            flow_score += 10
            signals.append("PAUS_INFLOW")
        elif taker_ratio < 45.0:
            flow_score -= 10
            signals.append("TEKANAN_JUAL_BESAR")

        if -4.0 <= change_24h <= 2.0 and taker_ratio >= 53.0:
            flow_score += 5
            signals.append("WHALE_DIVERGENCE (Nyicil Diam-diam)")

        flow_score = max(0, min(DEFAULT_WEIGHTS["flow"], flow_score))

        # --- PILAR 4: DERIVATIF & DINAMIKA OI (Maksimal 15 Poin) ---
        derivative_score = 0
        if funding_rate <= -0.05 or (funding_rate <= -0.015 and delta_oi_1h > 0):
            derivative_score += 12
            signals.append(f"EXTREME_SHORT_SQUEEZE (FR:{funding_rate:+.3f}%, ΔOI:+${delta_oi_1h}M)")
        elif funding_rate <= -0.015:
            derivative_score += 8
            signals.append(f"POTENSI_SHORT_SQUEEZE (FR:{funding_rate:+.3f}%)")
        elif delta_funding_1h < -0.003 and delta_oi_1h > 0:
            derivative_score += 6
            signals.append(f"SHORT_BUILDUP (ΔFR:{delta_funding_1h:+.3f}%, ΔOI:+${delta_oi_1h}M)")
        elif delta_oi_1h > 0.5 and roc_1h > 0.5:
            derivative_score += 5
            signals.append(f"LONG_INFLOW (ΔOI:+${delta_oi_1h}M)")
        elif funding_rate >= +0.05 or (funding_rate >= +0.03 and delta_oi_1h < -0.5):
            derivative_score -= 8
            signals.append("RISIKO_LONG_DUMP (Hindari Beli Pucuk)")

        if oi_m >= 10.0 and funding_rate < -0.01:
            derivative_score += 3
            signals.append(f"BIG_OI_SHORT_POOL (${oi_m}M)")

        derivative_score = max(0, min(DEFAULT_WEIGHTS["derivatives"], derivative_score))

        # --- PILAR 5: MAKRO BAROMETER (Maksimal 10 Poin) ---
        macro_score = 5 # Baseline
        if fgi_regime == "GREED":
            if "BREAKOUT_20_BAR_HIGH" in signals and vol_ratio >= 1.4:
                macro_score += 3
                signals.append("ORDER_FLOW_PRESSURE_BOOST")
            else:
                macro_score += 2
        elif fgi_regime == "EXTREME_FEAR":
            if "WHALE_DIVERGENCE (Nyicil Diam-diam)" in signals:
                macro_score += 4
                signals.append("SMART_MONEY_BOTTOM_BOOSTER")
            if "BREAKOUT_20_BAR_HIGH" in signals:
                macro_score -= 3
                signals.append("PENALTI_LIQUIDITY_DEPLETION")
        elif fgi_regime == "EXTREME_GREED":
            macro_score -= 2
            signals.append("⚠️ WASPADA_TAIL_RISK")

        macro_score = max(0, min(DEFAULT_WEIGHTS["news"], macro_score))

        # --- SKOR TOTAL V2 KOMPOSIT ---
        v2_score = structure_score + momentum_score + flow_score + derivative_score + macro_score

        # Penalti risiko pompaan ekstrem
        if change_24h > 35.0:
            v2_score -= 15
            signals.append("RISIKO_PUCUK (Naik >35%)")
        if rsi >= 80.0:
            v2_score -= 10
            signals.append("RSI_OVERBOUGHT_EXTREME")

        v2_score = max(0, min(100, v2_score))

        # Filter minimum score berdasarkan v2_score
        if v2_score < min_score:
            continue

        # =========================================================================
        # 3. KLASIFIKASI TAKSONOMI PASAR & STATUS ENTRI (KRIPIKTO V2)
        # =========================================================================
        is_overextended = (close > ma20 * 1.035) or (rsi >= 68.0) or (prev_high20 > 0 and close > prev_high20 * 1.025)

        setup_type, entry_status, status_tags = classify_setup_and_status(
            structure_score=structure_score,
            momentum_score=momentum_score,
            flow_score=flow_score,
            atr_expansion=atr_exp,
            roc_1h=roc_1h,
            rs_1h=rs_1h,
            vol_ratio_4h=vol_ratio,
            is_overextended=is_overextended
        )
        for stg in status_tags:
            if stg not in signals:
                signals.append(stg)

        # =========================================================================
        # 4. TRADING PLAN & TARGET PROFIT / STOP LOSS DINAMIS (BERBASIS ATR 1H)
        # =========================================================================
        if "BREAKOUT_20_BAR_HIGH" in signals or "MENDEKATI_BREAKOUT" in signals:
            support_level = max(ma20, prev_high20) if prev_high20 > 0 else ma20
        else:
            support_level = ma20 if ma20 < close else max(prev_low20, close * 0.96)

        dec = 8 if close < 0.01 else (6 if close < 1.0 else 4)

        if is_overextended:
            signals.append("⏳ TUNGGU_RETEST (Antre Diskon di Support)")
            buy_low = round(max(support_level * 0.992, close * 0.93), dec)
            buy_high = round(min(support_level * 1.015, close * 0.978), dec)
            if buy_low >= buy_high:
                buy_low = round(close * 0.95, dec)
                buy_high = round(close * 0.975, dec)
        else:
            signals.append("🎯 AREA_BUY_SEHAT (Dekat Support)")
            buy_low = round(max(support_level * 0.995, close * 0.982), dec)
            buy_high = round(close * 0.995, dec)
            if buy_low >= buy_high:
                buy_low = round(close * 0.98, dec)
                buy_high = round(close * 0.995, dec)

        entry_price = (buy_low + buy_high) / 2.0
        if entry_price <= 0:
            entry_price = max(close, 1e-8)

        # Target Profit & Stop Loss Dinamis via feature_engine (MFE +4.5% & MAE -2.93% compliant)
        target_atr = atr_1h if atr_1h > 0 else (float(row["atr14"]) if pd.notnull(row.get("atr14")) and row["atr14"] > 0 else (entry_price * 0.035))
        tp_sl = calculate_dynamic_tp_sl(entry_price=entry_price, support_level=support_level, atr_val=target_atr, dec=dec)
        
        sl_price = tp_sl["stop_loss"]
        sl_pct = tp_sl["stop_loss_pct"]
        tp1_price = tp_sl["tp1"]
        tp1_pct = tp_sl["tp1_pct"]
        tp2_price = tp_sl["tp2"]
        tp2_pct = tp_sl["tp2_pct"]
        rr_ratio = tp_sl["risk_reward"]
        buy_area = f"${buy_low} - ${buy_high}"

        results.append({
            "scan_time": now_utc_str,
            "symbol": row["symbol"],
            "interval": interval,
            "candle_close_time": candle_close_time,
            "last_price": close,
            "price_change_pct": round(change_24h, 2),
            "quote_vol_m": round(quote_vol_m, 2),
            "taker_buy_ratio": round(taker_ratio, 2),
            "funding_rate": funding_rate,
            "open_interest_m": oi_m,
            "rsi14": round(rsi, 1),
            "vol_ratio": round(vol_ratio, 2),
            "score": int(v2_score),
            "v1_score": int(v1_score),
            "v2_score": int(v2_score),
            "setup_type": setup_type,
            "entry_status": entry_status,
            "structure_score": int(structure_score),
            "momentum_score": int(momentum_score),
            "flow_score": int(flow_score),
            "derivative_score": int(derivative_score),
            "macro_score": int(macro_score),
            "delta_oi_1h": delta_oi_1h,
            "delta_oi_4h": delta_oi_4h,
            "delta_funding_1h": delta_funding_1h,
            "roc_1h": round(roc_1h, 2),
            "acceleration_1h": round(accel_1h, 2),
            "atr_expansion": round(atr_exp, 2),
            "vol_ratio_1h": round(vol_r_1h, 2),
            "signals": ", ".join(signals),
            "buy_area": buy_area,
            "buy_low": buy_low,
            "buy_high": buy_high,
            "entry_price": entry_price,
            "atr14": round(target_atr, dec),
            "rs_4h": rs_4h,
            "rs_1h": rs_1h,
            "coin_return_4h": coin_ret_4h,
            "coin_return_1h": coin_ret_1h,
            "btc_return_4h": btc_ret_4h,
            "btc_return_1h": btc_ret_1h,
            "stop_loss": sl_price,
            "stop_loss_pct": sl_pct,
            "tp1": tp1_price,
            "tp1_pct": tp1_pct,
            "tp2": tp2_price,
            "tp2_pct": tp2_pct,
            "risk_reward": rr_ratio
        })

    if not results:
        print("[!] Tidak ada koin yang memenuhi kriteria minimum score.")
        return pd.DataFrame()

    df_results = pd.DataFrame(results)
    # Urutkan berdasarkan Skor v2 tertinggi, lalu Whale Ratio tertinggi
    df_results = df_results.sort_values(
        by=["v2_score", "taker_buy_ratio", "quote_vol_m"],
        ascending=[False, False, False]
    ).reset_index(drop=True)

    df_top_picks = df_results.head(top_n).copy()

    # =========================================================================
    # 5. TAHAP 15M TACTICAL TRIGGER CONFIRMATION (KRIPIKTO v2 FASE 5)
    # =========================================================================
    if trigger_15m and not df_top_picks.empty:
        candidate_syms = df_top_picks[
            (df_top_picks["v2_score"] >= 40) &
            (df_top_picks["setup_type"].isin(["MOMENTUM_RUNNER", "STRUCTURE_BULLISH", "ACCUMULATION_COIL"]))
        ]["symbol"].tolist()

        if candidate_syms:
            print(f"[*] [Fase 5] Mengunduh & memvalidasi pemicu taktis 15M untuk {len(candidate_syms)} koin kandidat...")
            try:
                fetch_15m_trigger_batch(symbols=candidate_syms, limit=60, db_path=db_path)
                with sqlite3.connect(db_path) as conn:
                    placeholders = ",".join(["?"] * len(candidate_syms))
                    df_15m = pd.read_sql(
                        f"""
                        SELECT symbol, open_time, open, high, low, close, volume
                        FROM klines_history
                        WHERE interval = '15m' AND symbol IN ({placeholders}) AND (is_closed = 1 OR is_closed IS NULL)
                        ORDER BY symbol, open_time ASC
                        """,
                        conn,
                        params=candidate_syms
                    )

                trigger_15m_map = evaluate_15m_trigger(df_15m)

                for idx, r in df_top_picks.iterrows():
                    sym = r["symbol"]
                    t_info = trigger_15m_map.get(sym)
                    if t_info:
                        t_status = t_info["trigger_status"]
                        t_reason = t_info["trigger_reason"]
                        t_tags = t_info.get("tags", [])

                        # Jika setup mendapatkan konfirmasi 15M (Breakout vol spike atau retest bounce)
                        if t_status == "TRIGGERED":
                            df_top_picks.at[idx, "entry_status"] = "TRIGGERED"
                            # Perketat Buy Area dengan mikro-support 15M
                            ref_low = t_info["refined_buy_low"]
                            ref_high = t_info["refined_buy_high"]
                            ref_entry = (ref_low + ref_high) / 2.0
                            df_top_picks.at[idx, "buy_low"] = ref_low
                            df_top_picks.at[idx, "buy_high"] = ref_high
                            df_top_picks.at[idx, "buy_area"] = f"${ref_low} - ${ref_high}"
                            df_top_picks.at[idx, "entry_price"] = ref_entry

                            # Hitung ulang TP/SL dinamis dengan entry price presisi 15M
                            dec = 8 if r["last_price"] < 0.01 else (6 if r["last_price"] < 1.0 else 4)
                            new_targets = calculate_dynamic_tp_sl(
                                entry_price=ref_entry,
                                support_level=t_info["micro_support"],
                                atr_val=float(r["atr14"]),
                                dec=dec
                            )
                            df_top_picks.at[idx, "stop_loss"] = new_targets["stop_loss"]
                            df_top_picks.at[idx, "stop_loss_pct"] = new_targets["stop_loss_pct"]
                            df_top_picks.at[idx, "tp1"] = new_targets["tp1"]
                            df_top_picks.at[idx, "tp1_pct"] = new_targets["tp1_pct"]
                            df_top_picks.at[idx, "tp2"] = new_targets["tp2"]
                            df_top_picks.at[idx, "tp2_pct"] = new_targets["tp2_pct"]
                            df_top_picks.at[idx, "risk_reward"] = new_targets["risk_reward"]

                        elif t_status == "EXTENDED":
                            if r["entry_status"] != "WAIT":
                                df_top_picks.at[idx, "entry_status"] = "EXTENDED"
                        elif t_status == "FAILED":
                            if r["setup_type"] != "ACCUMULATION_COIL":
                                df_top_picks.at[idx, "entry_status"] = "FAILED"

                        # Tambahkan sinyal & alasan 15M
                        cur_signals = r["signals"]
                        all_sigs = [s.strip() for s in cur_signals.split(",") if s.strip()] if cur_signals else []
                        if t_reason not in all_sigs:
                            all_sigs.insert(0, t_reason)
                        for tg in t_tags:
                            if tg not in all_sigs:
                                all_sigs.append(tg)
                        df_top_picks.at[idx, "signals"] = ", ".join(all_sigs)
            except Exception as e:
                print(f"[!] Evaluasi trigger 15M dilewati karena kendala: {e}")

    # Simpan hasil scan ke SQLite
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        insert_query = """
            INSERT OR REPLACE INTO scan_results (
                scan_time, symbol, last_price, price_change_pct, quote_vol_m,
                taker_buy_ratio, funding_rate, open_interest_m, rsi14, vol_ratio, score, signals,
                buy_area, stop_loss, stop_loss_pct, tp1, tp1_pct, tp2, tp2_pct, risk_reward,
                interval, candle_close_time, buy_low, buy_high, entry_price, atr14, v1_score,
                rs_4h, rs_1h, coin_return_4h, coin_return_1h, btc_return_4h, btc_return_1h,
                setup_type, entry_status, structure_score, momentum_score, flow_score,
                derivative_score, macro_score, v2_score, roc_1h, acceleration_1h,
                atr_expansion, vol_ratio_1h
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        records = [
            (
                r["scan_time"], r["symbol"], r["last_price"], r["price_change_pct"], r["quote_vol_m"],
                r["taker_buy_ratio"], r["funding_rate"], r["open_interest_m"], r["rsi14"], r["vol_ratio"],
                r["score"], r["signals"], r["buy_area"], r["stop_loss"], r["stop_loss_pct"],
                r["tp1"], r["tp1_pct"], r["tp2"], r["tp2_pct"], r["risk_reward"],
                r["interval"], r["candle_close_time"], r["buy_low"], r["buy_high"], r["entry_price"],
                r["atr14"], r["v1_score"],
                r["rs_4h"], r["rs_1h"], r["coin_return_4h"], r["coin_return_1h"], r["btc_return_4h"], r["btc_return_1h"],
                r["setup_type"], r["entry_status"], r["structure_score"], r["momentum_score"], r["flow_score"],
                r["derivative_score"], r["macro_score"], r["v2_score"], r["roc_1h"], r["acceleration_1h"],
                r["atr_expansion"], r["vol_ratio_1h"]
            )
            for _, r in df_top_picks.iterrows()
        ]
        cursor.executemany(insert_query, records)

        # Masukkan juga draft ke signal_outcomes untuk Outcome Tracker
        outcomes_query = """
            INSERT OR IGNORE INTO signal_outcomes (
                scan_time, symbol, interval, buy_low, buy_high, entry_price,
                stop_loss, tp1, tp2, atr14, v1_score, v2_score, setup_type, entry_status,
                structure_score, momentum_score, flow_score, derivative_score,
                rs_4h, rs_1h, btc_return_4h, btc_return_1h, result
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
        """
        outcomes_records = [
            (
                r["scan_time"], r["symbol"], r["interval"], r["buy_low"], r["buy_high"], r["entry_price"],
                r["stop_loss"], r["tp1"], r["tp2"], r["atr14"], r["v1_score"], r["v2_score"],
                r["setup_type"], r["entry_status"],
                r["structure_score"], r["momentum_score"], r["flow_score"], r["derivative_score"],
                r["rs_4h"], r["rs_1h"], r["btc_return_4h"], r["btc_return_1h"]
            )
            for _, r in df_top_picks.iterrows()
        ]
        cursor.executemany(outcomes_query, outcomes_records)
        conn.commit()

    # Simpan juga ke file JSON ringkasan
    with open(SCAN_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(df_top_picks.to_dict(orient="records"), f, indent=2, ensure_ascii=False)

    return df_top_picks


def print_scan_report(df_picks: pd.DataFrame) -> None:
    """Menampilkan laporan tabel pemindaian KripikTo v2 di terminal dengan taksonomi sinyal & target dinamis."""
    if df_picks.empty:
        return

    print("\n" + "=" * 145)
    print("🔥 HASIL PEMINDAIAN SPOT SCALPING KRIPTO (KRIPIKTO v2: MTF + TAKSONOMI + DYNAMIC ATR)")
    print("=" * 145)
    print(f"{'NO':<3} {'SIMBOL':<10} {'HARGA ($)':<10} {'24H %':<8} {'SETUP TYPE':<18} {'STATUS':<9} {'SKOR':<5} {'STR':<4} {'MOM':<4} {'FLW':<4} {'RS 4H':<7} {'RS 1H':<7} {'TP1 %':<7} {'SL %':<7} {'SINYAL'}")
    print("-" * 145)

    for i, row in df_picks.iterrows():
        chg_str = f"{row['price_change_pct']:+.1f}%"
        setup_str = row.get("setup_type", "NO_SETUP")
        status_str = row.get("entry_status", "WAIT")
        if status_str == "TRIGGERED":
            status_str = "🎯 TRIGGER"
        elif status_str == "READY":
            status_str = "🚀 READY"
        elif status_str == "EXTENDED":
            status_str = "⚠️ EXT"
        elif status_str == "FAILED":
            status_str = "❌ FAIL"
        else:
            status_str = "⏳ WAIT"

        v2_s = row.get("v2_score", row.get("score", 0))
        str_s = row.get("structure_score", 0)
        mom_s = row.get("momentum_score", 0)
        flw_s = row.get("flow_score", 0)

        rs4_val = row.get('rs_4h', 0.0)
        rs4_str = f"{rs4_val:+.1f}%" if pd.notnull(rs4_val) else "0.0%"
        rs1_val = row.get('rs_1h', 0.0)
        rs1_str = f"{rs1_val:+.1f}%" if pd.notnull(rs1_val) else "0.0%"

        tp1_p = f"+{row['tp1_pct']:.1f}%" if pd.notnull(row.get('tp1_pct')) else "+4.5%"
        sl_p = f"{row['stop_loss_pct']:.1f}%" if pd.notnull(row.get('stop_loss_pct')) else "-4.0%"
        price_str = f"{row['last_price']:.4f}" if row['last_price'] < 1 else f"{row['last_price']:.2f}"

        sigs = row['signals']
        if len(sigs) > 28:
            sigs = sigs[:25] + "..."

        print(f"{i+1:<3} {row['symbol']:<10} {price_str:<10} {chg_str:<8} {setup_str:<18} {status_str:<9} {v2_s:<5} {str_s:<4} {mom_s:<4} {flw_s:<4} {rs4_str:<7} {rs1_str:<7} {tp1_p:<7} {sl_p:<7} {sigs}")

    print("=" * 145)
    print("💡 Taksonomi: TRIGGER = Konfirmasi 15M valid (Eksekusi Sekarang) | READY = Momentum runner (Menunggu 15M) | WAIT = Akumulasi/Squeeze")
    print("🎯 Target Dinamis: TP1 & SL dikalkulasi adaptif via ATR 1H & Support struktural (Validasi Empiris Outcome Tracker)")
    print(f"📁 Rekap JSON tersimpan di: {SCAN_JSON_PATH}\n")



if __name__ == "__main__":
    print("=== TEST RUN SCANNER (KRIPIKTO v2) ===")
    picks = run_scanner(interval="4h", min_score=40, top_n=15)
    print_scan_report(picks)
    if not picks.empty:
        print("Contoh Trading Plan Koin Teratas (#1):")
        top_coin = picks.iloc[0]
        print(f"Coin       : {top_coin['symbol']}")
        print(f"Buy Area   : {top_coin['buy_area']}")
        print(f"Stop Loss  : ${top_coin['stop_loss']} ({top_coin['stop_loss_pct']}%)")
        print(f"TP 1       : ${top_coin['tp1']} (+{top_coin['tp1_pct']}%)")
        print(f"TP 2       : ${top_coin['tp2']} (+{top_coin['tp2_pct']}%)")
        print(f"Risk/Reward: {top_coin['risk_reward']}")
