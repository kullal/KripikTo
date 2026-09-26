"""
data_pipeline.py
Modul pengunduh data pasar Kripto (Tahap 1):
1. Mengambil data pasar 24 jam seluruh pasangan USDT dari Binance Public Vision API (bypass blokir tanpa VPN).
2. Memfilter dan mengambil Top 500 koin likuid (membuang stablecoin-to-stablecoin & token leverage).
3. Menghitung metrik Whale Flow (Taker Buy Volume Ratio: persentase pembelian pasar agresif).
4. Mengunduh candlestick bergulir (Klines) untuk interval yang ditentukan (2h, 4h, 1d) secara multithreading cepat.
5. Menyimpan data terstruktur ke database SQLite lokal (kripto.db).
"""

import sys
import sqlite3
import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import requests

# Pastikan output utf-8 aman di terminal Windows
if sys.platform == "win32" and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Definisi path database
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = str(DATA_DIR / "kripto.db")

# Endpoint Binance Public Vision API (resmi, tanpa API key, tidak diblokir di Indonesia)
BINANCE_API_BASE = "https://data-api.binance.vision"

# Daftar pasangan stablecoin/wrapped yang tidak relevan untuk trading naik-turun
STABLECOIN_PAIRS = {
    "USDCUSDT", "FDUSDUSDT", "TUSDUSDT", "EURUSDT", "USDPUSDT", 
    "BUSDUSDT", "DAIUSDT", "AEURUSDT", "EURIUSDT", "WBTCUSDT", 
    "TBTCUSDT", "WBETHUSDT", "USDEUSDT", "USD1USDT", "RLUSDUSDT",
    "USDSUSDT", "PYUSDUSDT", "USD0USDT", "UUSDT", "USDTUSDT"
}

# Token non-kripto (saham AS ter-tokenisasi dan emas)
NON_CRYPTO_PAIRS = {
    "XAUTUSDT", "PAXGUSDT", "QQQBUSDT", "SPYBUSDT", "TQQQBUSDT",
    "AAPLBUSDT", "AMDBUSDT", "AMZNBUSDT", "ARMBUSDT", "AVGOBUSDT", "AXTIBUSDT",
    "BABABUSDT", "BMNRBUSDT", "BNCBUSDT", "CBRSBUSDT", "COINBUSDT", "CRCLBUSDT",
    "CRDOBUSDT", "CYPHBUSDT", "DRAMBUSDT", "EWYBUSDT", "FLNCBUSDT", "GOOGLBUSDT",
    "HOODBUSDT", "INTCBUSDT", "INTWBUSDT", "IRENBUSDT", "KORUBUSDT", "LITEBUSDT",
    "METABUSDT", "MSFTBUSDT", "MSTRBUSDT", "MUBUSDT", "NBISBUSDT", "NVDABUSDT",
    "ORCLBUSDT", "RKLBBUSDT", "SKHYBUSDT", "SNDKBUSDT", "SNXXBUSDT", "SOXLBUSDT",
    "SOXSBUSDT", "SPCXBUSDT", "TSLABUSDT", "AAOIBUSDT", "AGPUBUSDT"
}



# Pola token leverage yang harus disaring
LEVERAGED_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")


def init_db(db_path: str = DB_PATH) -> None:
    """Inisialisasi tabel SQLite untuk ringkasan 24h dan data klines."""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        
        # 1. Tabel Ringkasan Pasar 24 Jam & Whale Flow
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS market_summary_24h (
                symbol TEXT PRIMARY KEY,
                last_price REAL,
                price_change_pct REAL,
                high_price REAL,
                low_price REAL,
                volume REAL,
                quote_volume REAL,
                taker_buy_base_volume REAL,
                taker_buy_quote_volume REAL,
                taker_buy_ratio REAL,
                trades_count INTEGER,
                updated_at TEXT
            )
        """)

        # 2. Tabel Candlestick (OHLCV Klines)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS klines_history (
                symbol TEXT,
                interval TEXT,
                open_time INTEGER,
                datetime_utc TEXT,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                quote_volume REAL,
                taker_buy_volume REAL,
                PRIMARY KEY (symbol, interval, open_time)
            )
        """)

        # Indeks untuk mempercepat pembacaan data di scanner
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_klines_sym_int 
            ON klines_history (symbol, interval, open_time DESC)
        """)
        
        conn.commit()


def fetch_24h_summary(top_n: int = 500, db_path: str = DB_PATH) -> pd.DataFrame:
    """
    Mengunduh data ringkasan 24 jam seluruh koin dari Binance,
    memfilter Top N koin USDT paling likuid, dan menyimpannya ke database.
    """
    url = f"{BINANCE_API_BASE}/api/v3/ticker/24hr"
    headers = {"User-Agent": "KripikTo-Scanner/1.0"}

    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        raw_tickers = resp.json()
    except Exception as e:
        print(f"[!] Gagal menghubungi Binance API: {e}")
        return pd.DataFrame()

    filtered = []
    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    for item in raw_tickers:
        sym = item.get("symbol", "")
        # Filter hanya pasangan USDT
        if not sym.endswith("USDT"):
            continue
        # Filter stablecoins & leverage & non-crypto equities
        if sym in STABLECOIN_PAIRS or sym in NON_CRYPTO_PAIRS or sym.endswith(LEVERAGED_SUFFIXES):
            continue

        try:
            quote_vol = float(item.get("quoteVolume", 0.0))
            taker_quote = float(item.get("takerBuyQuoteAssetVolume", 0.0))
            
            # Hitung Taker Buy Ratio (Whale/Smart Money Inflow)
            taker_ratio = (taker_quote / quote_vol * 100.0) if quote_vol > 0 else 50.0

            filtered.append({
                "symbol": sym,
                "last_price": float(item.get("lastPrice", 0.0)),
                "price_change_pct": float(item.get("priceChangePercent", 0.0)),
                "high_price": float(item.get("highPrice", 0.0)),
                "low_price": float(item.get("lowPrice", 0.0)),
                "volume": float(item.get("volume", 0.0)),
                "quote_volume": quote_vol,
                "taker_buy_base_volume": float(item.get("takerBuyBaseAssetVolume", 0.0)),
                "taker_buy_quote_volume": taker_quote,
                "taker_buy_ratio": round(taker_ratio, 2),
                "trades_count": int(item.get("count", 0)),
                "updated_at": now_utc
            })
        except (ValueError, TypeError):
            continue

    if not filtered:
        print("[!] Tidak ada data koin USDT yang valid.")
        return pd.DataFrame()

    df = pd.DataFrame(filtered)
    # Urutkan berdasarkan quoteVolume (Turnover USDT harian) terbesar
    df = df.sort_values(by="quote_volume", ascending=False).reset_index(drop=True)
    df_top = df.head(top_n).copy()

    # Simpan ke SQLite
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM market_summary_24h")
        insert_query = """
            INSERT OR REPLACE INTO market_summary_24h (
                symbol, last_price, price_change_pct, high_price, low_price,
                volume, quote_volume, taker_buy_base_volume, taker_buy_quote_volume,
                taker_buy_ratio, trades_count, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        records = [
            (
                r["symbol"], r["last_price"], r["price_change_pct"], r["high_price"], r["low_price"],
                r["volume"], r["quote_volume"], r["taker_buy_base_volume"], r["taker_buy_quote_volume"],
                r["taker_buy_ratio"], r["trades_count"], r["updated_at"]
            )
            for _, r in df_top.iterrows()
        ]
        cursor.executemany(insert_query, records)
        conn.commit()

    return df_top


def _fetch_single_kline(symbol: str, interval: str, limit: int) -> Optional[List[tuple]]:
    """Helper untuk mengunduh klines sebuah simbol dari Binance."""
    url = f"{BINANCE_API_BASE}/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    headers = {"User-Agent": "KripikTo-Scanner/1.0"}

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        if resp.status_code == 200:
            raw_klines = resp.json()
            rows = []
            for k in raw_klines:
                open_time = int(k[0])
                dt_str = datetime.datetime.fromtimestamp(
                    open_time / 1000, tz=datetime.timezone.utc
                ).strftime("%Y-%m-%d %H:%M:%S")
                rows.append((
                    symbol,
                    interval,
                    open_time,
                    dt_str,
                    float(k[1]),  # Open
                    float(k[2]),  # High
                    float(k[3]),  # Low
                    float(k[4]),  # Close
                    float(k[5]),  # Volume
                    float(k[7]),  # Quote Asset Volume
                    float(k[10])  # Taker Buy Quote Asset Volume
                ))
            return rows
    except Exception:
        pass
    return None


def fetch_klines_batch(
    symbols: List[str],
    interval: str = "2h",
    limit: int = 100,
    max_workers: int = 12,
    db_path: str = DB_PATH
) -> int:
    """
    Mengunduh candlestick untuk seluruh simbol yang diminta secara paralel (multithreaded),
    lalu menyimpannya sekaligus ke database SQLite.
    """
    print(f"[*] Mengunduh {len(symbols)} koin candlestick interval [{interval}] (limit={limit})...")
    
    all_rows = []
    success_count = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_sym = {
            executor.submit(_fetch_single_kline, sym, interval, limit): sym
            for sym in symbols
        }
        for future in as_completed(future_to_sym):
            res = future.result()
            if res:
                all_rows.extend(res)
                success_count += 1

    if all_rows:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.executemany("""
                INSERT OR REPLACE INTO klines_history (
                    symbol, interval, open_time, datetime_utc,
                    open, high, low, close, volume, quote_volume, taker_buy_volume
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, all_rows)
            conn.commit()

    print(f"[✓] Berhasil mengunduh dan menyimpan {success_count}/{len(symbols)} koin ({len(all_rows)} lilin candlestick).")
    return success_count


def update_whale_flow_from_klines(interval: str = "2h", db_path: str = DB_PATH) -> None:
    """
    Menghitung Taker Buy Ratio (Whale Flow) 24 jam terakhir dan candle terakhir
    berdasarkan data candlestick klines yang telah tersimpan.
    """
    # 24 jam = 12 candle untuk 2h, 6 candle untuk 4h, 1 candle untuk 1d
    candles_in_24h = 12 if interval == "2h" else (6 if interval == "4h" else 1)
    
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        # Ambil daftar simbol unik
        cursor.execute("SELECT DISTINCT symbol FROM klines_history WHERE interval = ?", (interval,))
        symbols = [row[0] for row in cursor.fetchall()]

        for sym in symbols:
            # Ambil N candle terakhir
            cursor.execute("""
                SELECT quote_volume, taker_buy_volume
                FROM klines_history
                WHERE symbol = ? AND interval = ?
                ORDER BY open_time DESC
                LIMIT ?
            """, (sym, interval, candles_in_24h))
            rows = cursor.fetchall()
            if rows:
                total_quote = sum(r[0] for r in rows)
                total_taker = sum(r[1] for r in rows)
                ratio_24h = (total_taker / total_quote * 100.0) if total_quote > 0 else 50.0
                cursor.execute("""
                    UPDATE market_summary_24h
                    SET taker_buy_quote_volume = ?, taker_buy_ratio = ?
                    WHERE symbol = ?
                """, (round(total_taker, 2), round(ratio_24h, 2), sym))
        conn.commit()


def run_pipeline(
    interval: str = "2h",
    top_n: int = 500,
    limit: int = 100,
    db_path: str = DB_PATH
) -> pd.DataFrame:
    """
    Fungsi orkestrator Tahap 1:
    1. Inisialisasi DB
    2. Ambil ringkasan 24h Top N koin likuid
    3. Ambil data klines secara paralel
    4. Hitung agregasi Whale Flow (Taker Buy Ratio) akurat dari klines
    """
    init_db(db_path)

    print(f"[*] [Tahap 1] Mengunduh ringkasan pasar 24 jam dari Binance...")
    df_top = fetch_24h_summary(top_n=top_n, db_path=db_path)

    if df_top.empty:
        print("[!] Gagal mengunduh ringkasan pasar.")
        return df_top

    print(f"[✓] Berhasil memfilter Top {len(df_top)} koin likuid.")
    symbols = df_top["symbol"].tolist()

    fetch_klines_batch(symbols=symbols, interval=interval, limit=limit, db_path=db_path)
    
    # Hitung Whale Flow akurat dari klines
    update_whale_flow_from_klines(interval=interval, db_path=db_path)
    
    # Baca kembali ringkasan yang telah terupdate
    with sqlite3.connect(db_path) as conn:
        df_updated = pd.read_sql("SELECT * FROM market_summary_24h ORDER BY quote_volume DESC", conn)

    return df_updated


if __name__ == "__main__":
    # Test run cepat mandiri
    print("=== TEST RUN DATA PIPELINE (TAHAP 1) ===")
    df = run_pipeline(interval="2h", top_n=50, limit=100)
    print("\nContoh 5 Koin Teratas Berdasarkan Turnover & Whale Flow:")
    print(df[["symbol", "last_price", "price_change_pct", "quote_volume", "taker_buy_ratio"]].head(5))

