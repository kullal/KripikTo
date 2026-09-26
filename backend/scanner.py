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

# Import path database & macro sentiment
try:
    from backend.macro_sentiment import fetch_fear_and_greed_index
except ImportError:
    from macro_sentiment import fetch_fear_and_greed_index

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = str(DATA_DIR / "kripto.db")
SCAN_JSON_PATH = str(DATA_DIR / "scan_latest.json")


# Token non-kripto (saham AS ter-tokenisasi, emas, dan stablecoin/pegged) yang harus dikecualikan
NON_CRYPTO_EXCLUSIONS = {
    "UUSDT", "XAUTUSDT", "PAXGUSDT", "QQQBUSDT", "SPYBUSDT", "TQQQBUSDT",
    "AAPLBUSDT", "AMDBUSDT", "AMZNBUSDT", "ARMBUSDT", "AVGOBUSDT", "AXTIBUSDT",
    "BABABUSDT", "BMNRBUSDT", "BNCBUSDT", "CBRSBUSDT", "COINBUSDT", "CRCLBUSDT",
    "CRDOBUSDT", "CYPHBUSDT", "DRAMBUSDT", "EWYBUSDT", "FLNCBUSDT", "GOOGLBUSDT",
    "HOODBUSDT", "INTCBUSDT", "INTWBUSDT", "IRENBUSDT", "KORUBUSDT", "LITEBUSDT",
    "METABUSDT", "MSFTBUSDT", "MSTRBUSDT", "MUBUSDT", "NBISBUSDT", "NVDABUSDT",
    "ORCLBUSDT", "RKLBBUSDT", "SKHYBUSDT", "SNDKBUSDT", "SNXXBUSDT", "SOXLBUSDT",
    "SOXSBUSDT", "SPCXBUSDT", "TSLABUSDT", "AAOIBUSDT", "AGPUBUSDT"
}



def init_scanner_db(db_path: str = DB_PATH) -> None:
    """Inisialisasi tabel hasil pemindaian di SQLite."""
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
                PRIMARY KEY (scan_time, symbol)
            )
        """)
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
    return rsi.fillna(50.0)


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

    with sqlite3.connect(db_path) as conn:
        df_klines = pd.read_sql(
            "SELECT * FROM klines_history WHERE interval = ? ORDER BY symbol, open_time ASC",
            conn,
            params=(interval,)
        )
        df_summary = pd.read_sql("SELECT * FROM market_summary_24h", conn)

    if df_klines.empty or df_summary.empty:
        print(f"[!] Data klines interval [{interval}] atau market_summary_24h kosong.")
        print("[*] Jalankan data_pipeline terlebih dahulu.")
        return pd.DataFrame()

    # 1. Hitung Indikator Teknikal Bergulir per Simbol (Vectorized Groupby)
    df_klines["ma20"] = df_klines.groupby("symbol")["close"].transform(lambda x: x.rolling(20, min_periods=5).mean())
    df_klines["ma50"] = df_klines.groupby("symbol")["close"].transform(lambda x: x.rolling(50, min_periods=10).mean())
    df_klines["vol_ma20"] = df_klines.groupby("symbol")["volume"].transform(lambda x: x.rolling(20, min_periods=5).mean())
    df_klines["prev_high20"] = df_klines.groupby("symbol")["high"].transform(lambda x: x.shift(1).rolling(20, min_periods=5).max())
    df_klines["prev_low20"] = df_klines.groupby("symbol")["low"].transform(lambda x: x.shift(1).rolling(20, min_periods=5).min())
    df_klines["rsi14"] = df_klines.groupby("symbol")["close"].transform(lambda x: calculate_rsi(x, period=14))

    # Ambil baris candlestick terbaru untuk tiap koin
    latest_tech = df_klines.groupby("symbol").last().reset_index()

    # Gabungkan dengan data ringkasan pasar & Whale Ratio 24h
    merged = pd.merge(latest_tech, df_summary, on="symbol", how="inner", suffixes=("_candle", ""))

    if merged.empty:
        print("[!] Tidak ada data yang cocok antara klines dan market summary.")
        return pd.DataFrame()

    now_utc_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    results = []

    for _, row in merged.iterrows():
        sym = row["symbol"]
        if sym in NON_CRYPTO_EXCLUSIONS:
            continue

        signals = []
        score = 0

        close = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        vol = float(row["volume"])
        vol_ma20 = float(row["vol_ma20"]) if pd.notnull(row["vol_ma20"]) and row["vol_ma20"] > 0 else vol
        ma20 = float(row["ma20"]) if pd.notnull(row["ma20"]) else close
        ma50 = float(row["ma50"]) if pd.notnull(row["ma50"]) else close
        prev_high20 = float(row["prev_high20"]) if pd.notnull(row["prev_high20"]) else high
        prev_low20 = float(row["prev_low20"]) if pd.notnull(row["prev_low20"]) else low
        rsi = float(row["rsi14"]) if pd.notnull(row["rsi14"]) else 50.0

        change_24h = float(row["price_change_pct"])
        quote_vol = float(row["quote_volume"])
        taker_ratio = float(row["taker_buy_ratio"])
        quote_vol_m = quote_vol / 1_000_000.0

        vol_ratio = vol / vol_ma20 if vol_ma20 > 0 else 1.0

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

        # --- H. GENERATE TRADING PLAN OTOMATIS & ADAPTIF ---
        # Stop Loss: Di bawah MA20 atau low terdekat
        support_level = max(ma20, prev_low20) if ma20 < close else close * 0.96

        # Adaptasi Rezim: Perketat Stop Loss jika Extreme Greed
        if fgi_regime == "EXTREME_GREED":
            sl_price = round(min(close * 0.965, max(close * 0.95, support_level)), 6)
        else:
            sl_price = round(min(close * 0.96, max(close * 0.935, support_level)), 6)

        sl_pct = round(((sl_price - close) / close) * 100.0, 2)


        # Target Profit 1 & 2
        risk_dist = abs(close - sl_price)
        tp1_price = round(close + (risk_dist * 1.5), 6)
        tp1_pct = round(((tp1_price - close) / close) * 100.0, 2)

        tp2_price = round(close + (risk_dist * 2.8), 6)
        tp2_pct = round(((tp2_price - close) / close) * 100.0, 2)

        rr_ratio = f"1 : {round(abs(tp1_pct / sl_pct), 1)}" if abs(sl_pct) > 0 else "1 : 2.0"
        buy_area = f"${round(close * 0.992, 4)} - ${round(close * 1.005, 4)}"

        results.append({
            "scan_time": now_utc_str,
            "symbol": row["symbol"],
            "last_price": close,
            "price_change_pct": round(change_24h, 2),
            "quote_vol_m": round(quote_vol_m, 2),
            "taker_buy_ratio": round(taker_ratio, 2),
            "rsi14": round(rsi, 1),
            "vol_ratio": round(vol_ratio, 2),
            "score": int(score),
            "signals": ", ".join(signals),
            "buy_area": buy_area,
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
                taker_buy_ratio, rsi14, vol_ratio, score, signals,
                buy_area, stop_loss, stop_loss_pct, tp1, tp1_pct, tp2, tp2_pct, risk_reward
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        records = [
            (
                r["scan_time"], r["symbol"], r["last_price"], r["price_change_pct"], r["quote_vol_m"],
                r["taker_buy_ratio"], r["rsi14"], r["vol_ratio"], r["score"], r["signals"],
                r["buy_area"], r["stop_loss"], r["stop_loss_pct"], r["tp1"], r["tp1_pct"],
                r["tp2"], r["tp2_pct"], r["risk_reward"]
            )
            for _, r in df_top_picks.iterrows()
        ]
        cursor.executemany(insert_query, records)
        conn.commit()

    # Simpan juga ke file JSON ringkasan
    with open(SCAN_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(df_top_picks.to_dict(orient="records"), f, indent=2, ensure_ascii=False)

    return df_top_picks


def print_scan_report(df_picks: pd.DataFrame) -> None:
    """Menampilkan laporan tabel pemindaian yang rapi di terminal."""
    if df_picks.empty:
        return

    print("\n" + "=" * 115)
    print("🔥 HASIL PEMINDAIAN KUANTITATIF & ALIRAN DANA PAUS (TOP PICKS KRIPTO)")
    print("=" * 115)
    print(f"{'NO':<3} {'SIMBOL':<12} {'HARGA ($)':<12} {'CHG 24H':<9} {'TURNOVER':<11} {'WHALE %':<9} {'RSI':<6} {'SKOR':<5} {'SINYAL UTAMA'}")
    print("-" * 115)

    for i, row in df_picks.iterrows():
        chg_str = f"{row['price_change_pct']:+.2f}%"
        whale_str = f"{row['taker_buy_ratio']:.1f}%"
        price_str = f"{row['last_price']:.4f}" if row['last_price'] < 1 else f"{row['last_price']:.2f}"
        turnover_str = f"${row['quote_vol_m']:.1f}M"
        
        # Potong sinyal agar rapi di terminal
        sigs = row['signals']
        if len(sigs) > 42:
            sigs = sigs[:39] + "..."

        print(f"{i+1:<3} {row['symbol']:<12} {price_str:<12} {chg_str:<9} {turnover_str:<11} {whale_str:<9} {row['rsi14']:<6.1f} {row['score']:<5} {sigs}")

    print("=" * 115)
    print("💡 Keterangan Skor: >= 75 (Sangat Kuat / Paus Agresif) | 60 - 74 (Potensial Breakout) | 40 - 59 (Watchlist)")
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
