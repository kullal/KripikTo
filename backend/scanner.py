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
    from backend.derivatives_flow import fetch_derivatives_summary
except ImportError:
    from macro_sentiment import fetch_fear_and_greed_index
    from derivatives_flow import fetch_derivatives_summary

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = str(DATA_DIR / "kripto.db")
SCAN_JSON_PATH = str(DATA_DIR / "scan_latest.json")



try:
    from backend.data_pipeline import (
        is_stablecoin_or_excluded, NON_CRYPTO_PAIRS, STABLECOIN_PAIRS,
        get_active_spot_symbols, get_btc_benchmark
    )
except ImportError:
    from data_pipeline import (
        is_stablecoin_or_excluded, NON_CRYPTO_PAIRS, STABLECOIN_PAIRS,
        get_active_spot_symbols, get_btc_benchmark
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
            ("btc_return_1h", "REAL")
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
            ("rs_4h", "REAL"), ("rs_1h", "REAL"),
            ("btc_return_4h", "REAL"), ("btc_return_1h", "REAL")
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
    interval: str = "2h",
    min_score: int = 40,
    top_n: int = 20,
    fgi: Optional[Dict[str, Any]] = None,
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

    with sqlite3.connect(db_path) as conn:
        df_klines = pd.read_sql(
            "SELECT * FROM klines_history WHERE interval = ? AND (is_closed = 1 OR is_closed IS NULL) ORDER BY symbol, open_time ASC",
            conn,
            params=(interval,)
        )
        df_summary = pd.read_sql("SELECT * FROM market_summary_24h", conn)
        
        # Ambil return 1H dari database untuk kalkulasi Relative Strength 1H
        df_1h = pd.read_sql(
            "SELECT symbol, close, open_time FROM klines_history WHERE interval = '1h' AND (is_closed = 1 OR is_closed IS NULL) ORDER BY symbol, open_time ASC",
            conn
        )

    if df_klines.empty or df_summary.empty:
        print(f"[!] Data klines interval [{interval}] atau market_summary_24h kosong.")
        print("[*] Jalankan data_pipeline terlebih dahulu.")
        return pd.DataFrame()

    # Hitung return 1H per simbol
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
        score = 0

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

        # --- A. DETEKSI WHALE FLOW (ALIRAN DANA PAUS) ---
        if taker_ratio >= 55.0:
            score += 25
            signals.append("PAUS_AKUMULASI_KUAT")
        elif taker_ratio >= 52.0:
            score += 15
            signals.append("PAUS_INFLOW")
        elif taker_ratio < 45.0:
            score -= 15
            signals.append("TEKANAN_JUAL_BESAR")

        # Divergensi Paus: Harga flat/koreksi tipis tapi Paus borong agresif
        if -4.0 <= change_24h <= 2.0 and taker_ratio >= 53.0:
            score += 20
            signals.append("WHALE_DIVERGENCE (Nyicil Diam-diam)")

        # Ambil data derivatif koin jika ada di Binance Futures
        deriv = deriv_map.get(sym, {})
        funding_rate = float(deriv.get("funding_rate", 0.0))
        oi_m = float(deriv.get("open_interest_m", 0.0))

        # --- A2. DETEKSI ANOMALI DERIVATIF (POTENSI SHORT SQUEEZE SPOT SCALPING) ---
        if funding_rate <= -0.10:
            score += 25
            signals.append(f"EXTREME_SHORT_SQUEEZE (FR: {funding_rate:+.3f}%)")
        elif funding_rate <= -0.02:
            score += 15
            signals.append(f"POTENSI_SHORT_SQUEEZE (FR: {funding_rate:+.3f}%)")
        elif funding_rate >= +0.05:
            score -= 15
            signals.append("RISIKO_LONG_DUMP (Hindari Beli Pucuk)")

        if oi_m >= 10.0 and funding_rate < -0.01:
            score += 10
            signals.append(f"BIG_OI_SHORT_POOL (${oi_m}M)")

        # --- B. DETEKSI AKSI HARGA & BREAKOUT ---

        if close > prev_high20 and prev_high20 > 0:
            score += 25
            signals.append("BREAKOUT_20_BAR_HIGH")
        elif close >= (prev_high20 * 0.985):
            score += 12
            signals.append("MENDEKATI_BREAKOUT")

        # --- C. DETEKSI LONJAKAN VOLUME (VOLUME SURGE) ---
        if vol_ratio >= 2.0:
            score += 20
            signals.append("VOLUME_MELEDAK (>2x)")
        elif vol_ratio >= 1.4:
            score += 10
            signals.append("VOLUME_SURGE (>1.4x)")

        # --- D. DETEKSI STRUKTUR TREN (MA20 & MA50) ---
        if close > ma20 and close > ma50:
            if ma20 > ma50:
                score += 15
                signals.append("SUPER_BULLISH_TREND")
            else:
                score += 10
                signals.append("BULLISH_CROSS_UP")
        elif close > ma20:
            score += 5
            signals.append("ABOVE_MA20")
        elif close < ma20 and close < ma50:
            score -= 10

        # --- E. DETEKSI MOMENTUM RSI 14 ---
        if 52.0 <= rsi <= 68.0:
            score += 12
            signals.append("RSI_HEALTHY_MOMENTUM")
        elif rsi < 35.0:
            # Rebound oversold
            score += 12
            signals.append("RSI_OVERSOLD_REBOUND")
        elif rsi >= 80.0:
            # Bahaya pucuk / overbought ekstrem
            score -= 15
            signals.append("RSI_OVERBOUGHT_EXTREME")

        # --- F. FILTER PENALTI POMPAAN EKSTREM ---
        if change_24h > 35.0:
            score -= 20
            signals.append("RISIKO_PUCUK (Naik >35%)")

        # --- F2. RELATIVE STRENGTH (RS) VS BITCOIN BENCHMARK (RS_4H & RS_1H) ---
        coin_ret_4h = round(float(row["ret_candle"]), 2) if pd.notnull(row.get("ret_candle")) else 0.0
        coin_ret_1h = ret_1h_map.get(sym, 0.0)
        rs_4h = round(coin_ret_4h - btc_ret_4h, 2)
        rs_1h = round(coin_ret_1h - btc_ret_1h, 2)

        if rs_4h >= 3.0 and rs_1h >= 1.0:
            score += 15
            signals.append(f"OUTPERFORM_BTC_STRONG (RS4h:{rs_4h:+.1f}%, RS1h:{rs_1h:+.1f}%)")
        elif rs_4h >= 1.5:
            score += 8
            signals.append(f"OUTPERFORM_BTC_4H (RS:{rs_4h:+.1f}%)")
        elif rs_4h < -2.0 and rs_1h < -1.0:
            score -= 10
            signals.append(f"UNDERPERFORM_BTC (RS4h:{rs_4h:+.1f}%)")

        # --- G. PENYESUAIAN REZIM MAKRO AKADEMIK (FEAR & GREED INDEX) ---
        if fgi_regime == "EXTREME_FEAR":
            # Riset: Penipisan likuiditas -> False breakout tinggi
            if "BREAKOUT_20_BAR_HIGH" in signals:
                score -= 8
                signals.append("PENALTI_LIQUIDITY_DEPLETION")
            # Riset: Smart money accumulation di harga diskon
            if "WHALE_DIVERGENCE (Nyicil Diam-diam)" in signals:
                score += 10
                signals.append("SMART_MONEY_BOTTOM_BOOSTER")
        elif fgi_regime == "GREED":
            # Riset: Order-flow pressure searah -> Breakout win rate tinggi
            if "BREAKOUT_20_BAR_HIGH" in signals and vol_ratio >= 1.4:
                score += 5
                signals.append("ORDER_FLOW_PRESSURE_BOOST")
        elif fgi_regime == "EXTREME_GREED":
            # Riset JBEF: Asymmetric tail risk (risiko flash dump tiba-tiba)
            signals.append("⚠️ WASPADA_TAIL_RISK")

        score = max(0, min(100, score))

        if score < min_score:
            continue

        # --- H. TRADING PLAN CERDAS: STRATEGI RETEST & ANTI-BELI PUCUK ---
        # 1. Deteksi Overextension (Jarak harga terhadap MA20 & RSI)
        is_overextended = (close > ma20 * 1.035) or (rsi >= 68.0) or (prev_high20 > 0 and close > prev_high20 * 1.025)
        
        # 2. Penentuan Support Kunci untuk Titik Beli (Pullback / Retest)
        # Jika breakout terjadi, support utama adalah batas atas breakout sebelumnya (retest level) atau MA20
        if "BREAKOUT_20_BAR_HIGH" in signals or "MENDEKATI_BREAKOUT" in signals:
            support_level = max(ma20, prev_high20) if prev_high20 > 0 else ma20
        else:
            support_level = ma20 if ma20 < close else max(prev_low20, close * 0.96)

        dec = 8 if close < 0.01 else (6 if close < 1.0 else 4)

        # 3. Hitung Buy Area Diskon (Bukan Beli di Harga Running Close):
        if is_overextended:
            signals.append("⏳ TUNGGU_RETEST (Antre Diskon di Support)")
            score = max(0, score - 8) # Kurangi skor FOMO beli di pucuk
            # Buy area diarahkan ke area retest support, bukan di pucuk candle
            buy_low = round(max(support_level * 0.992, close * 0.93), dec)
            buy_high = round(min(support_level * 1.015, close * 0.978), dec)
            if buy_low >= buy_high:
                buy_low = round(close * 0.95, dec)
                buy_high = round(close * 0.975, dec)
        else:
            # Jika harga masih dekat support / konsolidasi sehat:
            signals.append("🎯 AREA_BUY_SEHAT (Dekat Support)")
            buy_low = round(max(support_level * 0.995, close * 0.982), dec)
            buy_high = round(close * 0.995, dec) # Antre limit sedikit di bawah running
            if buy_low >= buy_high:
                buy_low = round(close * 0.98, dec)
                buy_high = round(close * 0.995, dec)

        # Titik acuan entri harga antrean (midpoint buy area)
        entry_price = (buy_low + buy_high) / 2.0
        if entry_price <= 0:
            entry_price = max(close, 1e-8)

        # 4. Stop Loss Adaptif berbasis ATR & Support Struktural:
        atr_val = float(row["atr14"]) if pd.notnull(row.get("atr14")) and row["atr14"] > 0 else (entry_price * 0.035)
        structural_sl = min(support_level * 0.985, entry_price - (1.2 * atr_val))
        
        # Batasi SL antara -4.0% s.d -5.8% dari harga beli antrean (mencegah SL tersapu noise wajar kripto)
        sl_price = round(min(entry_price * 0.96, max(entry_price * 0.942, structural_sl)), dec)
        sl_pct = round(((sl_price - entry_price) / entry_price) * 100.0, 2) if entry_price > 0 else -4.5

        # 5. Take Profit (Dihitung dari entry_price, bukan dari harga pucuk):
        tp1_pct = 6.0
        tp1_price = round(entry_price * 1.06, dec)
        tp2_pct = 12.0
        tp2_price = round(entry_price * 1.12, dec)

        rr_ratio = f"1 : {round(abs(tp1_pct / sl_pct), 1)}" if abs(sl_pct) > 0 else "1 : 1.5"
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
            "score": int(score),
            "v1_score": int(score),
            "signals": ", ".join(signals),
            "buy_area": buy_area,
            "buy_low": buy_low,
            "buy_high": buy_high,
            "entry_price": entry_price,
            "atr14": round(atr_val, dec),
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
    # Urutkan berdasarkan Skor tertinggi, lalu Whale Ratio tertinggi
    df_results = df_results.sort_values(
        by=["score", "taker_buy_ratio", "quote_vol_m"],
        ascending=[False, False, False]
    ).reset_index(drop=True)

    df_top_picks = df_results.head(top_n).copy()

    # Simpan hasil scan ke SQLite
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        insert_query = """
            INSERT OR REPLACE INTO scan_results (
                scan_time, symbol, last_price, price_change_pct, quote_vol_m,
                taker_buy_ratio, funding_rate, open_interest_m, rsi14, vol_ratio, score, signals,
                buy_area, stop_loss, stop_loss_pct, tp1, tp1_pct, tp2, tp2_pct, risk_reward,
                interval, candle_close_time, buy_low, buy_high, entry_price, atr14, v1_score,
                rs_4h, rs_1h, coin_return_4h, coin_return_1h, btc_return_4h, btc_return_1h
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        records = [
            (
                r["scan_time"], r["symbol"], r["last_price"], r["price_change_pct"], r["quote_vol_m"],
                r["taker_buy_ratio"], r["funding_rate"], r["open_interest_m"], r["rsi14"], r["vol_ratio"],
                r["score"], r["signals"], r["buy_area"], r["stop_loss"], r["stop_loss_pct"],
                r["tp1"], r["tp1_pct"], r["tp2"], r["tp2_pct"], r["risk_reward"],
                r["interval"], r["candle_close_time"], r["buy_low"], r["buy_high"], r["entry_price"],
                r["atr14"], r["v1_score"],
                r["rs_4h"], r["rs_1h"], r["coin_return_4h"], r["coin_return_1h"], r["btc_return_4h"], r["btc_return_1h"]
            )
            for _, r in df_top_picks.iterrows()
        ]
        cursor.executemany(insert_query, records)

        # Masukkan juga draft ke signal_outcomes untuk Outcome Tracker
        outcomes_query = """
            INSERT OR IGNORE INTO signal_outcomes (
                scan_time, symbol, interval, entry_price, stop_loss, tp1, tp2, atr14, v1_score,
                rs_4h, rs_1h, btc_return_4h, btc_return_1h, result
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
        """
        outcomes_records = [
            (
                r["scan_time"], r["symbol"], r["interval"], r["entry_price"],
                r["stop_loss"], r["tp1"], r["tp2"], r["atr14"], r["v1_score"],
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
    """Menampilkan laporan tabel pemindaian yang rapi di terminal dengan indikator Relative Strength vs BTC."""
    if df_picks.empty:
        return

    print("\n" + "=" * 135)
    print("🔥 HASIL PEMINDAIAN SPOT SCALPING & POTENSI SHORT SQUEEZE (TOP PICKS KRIPTO)")
    print("=" * 135)
    print(f"{'NO':<3} {'SIMBOL':<11} {'HARGA ($)':<11} {'CHG 24H':<9} {'RS 4H':<8} {'RS 1H':<8} {'TURNOVER':<10} {'WHALE %':<8} {'FUNDING %':<11} {'RSI':<6} {'SKOR':<5} {'SINYAL UTAMA'}")
    print("-" * 135)

    for i, row in df_picks.iterrows():
        chg_str = f"{row['price_change_pct']:+.2f}%"
        rs4_val = row.get('rs_4h', 0.0)
        rs4_str = f"{rs4_val:+.1f}%" if pd.notnull(rs4_val) else "0.0%"
        rs1_val = row.get('rs_1h', 0.0)
        rs1_str = f"{rs1_val:+.1f}%" if pd.notnull(rs1_val) else "0.0%"
        whale_str = f"{row['taker_buy_ratio']:.1f}%"
        fr_val = row.get('funding_rate', 0.0)
        fr_str = f"{fr_val:+.3f}%" if pd.notnull(fr_val) else "0.000%"
        price_str = f"{row['last_price']:.4f}" if row['last_price'] < 1 else f"{row['last_price']:.2f}"
        turnover_str = f"${row['quote_vol_m']:.1f}M"

        # Potong sinyal agar rapi di terminal
        sigs = row['signals']
        if len(sigs) > 35:
            sigs = sigs[:32] + "..."

        print(f"{i+1:<3} {row['symbol']:<11} {price_str:<11} {chg_str:<9} {rs4_str:<8} {rs1_str:<8} {turnover_str:<10} {whale_str:<8} {fr_str:<11} {row['rsi14']:<6.1f} {row['score']:<5} {sigs}")

    print("=" * 135)
    print("🎯 Strategi Spot Scalping: Target Profit Utama +6.0% langsung kunci keuntungan | Stop Loss ketat ~3.8%")
    print(f"📁 Rekap JSON tersimpan di: {SCAN_JSON_PATH}\n")



if __name__ == "__main__":
    print("=== TEST RUN SCANNER (TAHAP 2) ===")
    picks = run_scanner(interval="2h", min_score=40, top_n=15)
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
