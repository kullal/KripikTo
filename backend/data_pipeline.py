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
    "USDSUSDT", "PYUSDUSDT", "USD0USDT", "UUSDT", "USDTUSDT",
    "XUSDUSDT", "AUSDUSDT", "CUSDUSDT", "SUSDUSDT", "USDYUSDT",
    "USDXUSDT", "FRAXUSDT", "LUSDUSDT", "USTCUSDT", "GUSDUSDT",
    "USDDUSDT", "VAIUSDT", "DOLAUSDT", "OUSDUSDT", "TRYUSDT", "BRLUSDT"
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


def is_stablecoin_or_excluded(
    symbol: str,
    last_price: float = 0.0,
    high_price: float = 0.0,
    low_price: float = 0.0
) -> bool:
    """
    Penyaringan komprehensif token non-kripto, saham, fiat, dan stablecoin:
    1. Blacklist simbol eksplisit & token leverage (UP/DOWN/BULL/BEAR).
    2. Deteksi pola nama: aset berakhiran/berawalan USD, EUR, TRY, BRL, GBP.
    3. Deteksi dinamis harga peg: harga mendekati ~$1.00 dengan volatilitas 24h sempit (< 3.5%).
    """
    if not symbol.endswith("USDT"):
        return True

    if symbol in STABLECOIN_PAIRS or symbol in NON_CRYPTO_PAIRS or symbol.endswith(LEVERAGED_SUFFIXES):
        return True

    base = symbol[:-4].upper()

    # Pola nama stablecoin/fiat peg (misal XUSD, USDE, USDS, USD0, USD1, EUR, BRL, dsb)
    if (
        base.endswith("USD")
        or base.startswith("USD")
        or base.endswith("EUR")
        or base.endswith("TRY")
        or base.endswith("BRL")
    ):
        return True

    # Token saham AS ter-tokenisasi tambahan (akhiran BUSDT)
    if symbol.endswith("BUSDT"):
        return True

    # Deteksi dinamis aset berharga ~$1.00 dengan fluktuasi sangat sempit (peg dolar sintetis)
    if last_price > 0:
        if 0.96 <= last_price <= 1.04:
            if high_price > 0 and low_price > 0:
                spread = (high_price - low_price) / last_price
                if spread < 0.035:
                    return True
            else:
                return True

    return False


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

        # 2. Tabel Candlestick (OHLCV Klines) dengan Data Integrity Layer (is_closed & fetched_at)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS klines_history (
                symbol TEXT,
                interval TEXT,
                open_time INTEGER,
                close_time INTEGER,
                datetime_utc TEXT,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                quote_volume REAL,
                taker_buy_volume REAL,
                is_closed INTEGER DEFAULT 1,
                fetched_at TEXT,
                PRIMARY KEY (symbol, interval, open_time)
            )
        """)

        # Migrasi kolom otomatis jika tabel database lama belum memiliki kolom baru
        cursor.execute("PRAGMA table_info(klines_history)")
        existing_cols = {col[1] for col in cursor.fetchall()}
        for col_name, col_def in [
            ("close_time", "INTEGER"),
            ("is_closed", "INTEGER DEFAULT 1"),
            ("fetched_at", "TEXT")
        ]:
            if col_name not in existing_cols:
                try:
                    cursor.execute(f"ALTER TABLE klines_history ADD COLUMN {col_name} {col_def}")
                except Exception:
                    pass

        # Indeks untuk mempercepat pembacaan data di scanner & outcome tracker
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_klines_sym_int 
            ON klines_history (symbol, interval, open_time DESC)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_klines_closed 
            ON klines_history (symbol, interval, is_closed, open_time DESC)
        """)
        
        conn.commit()


def get_active_spot_symbols() -> set:
    """Mengambil daftar simbol pasangan USDT yang berstatus aktif TRADING di pasar SPOT Binance."""
    url = f"{BINANCE_API_BASE}/api/v3/exchangeInfo"
    try:
        resp = requests.get(url, timeout=12)
        if resp.status_code == 200:
            data = resp.json()
            return {
                s["symbol"]
                for s in data.get("symbols", [])
                if s.get("status") == "TRADING"
                and s.get("isSpotTradingAllowed", True)
                and s.get("symbol", "").endswith("USDT")
            }
    except Exception as e:
        print(f"[!] Gagal mengambil status exchangeInfo: {e}")
    return set()


def fetch_24h_summary(top_n: int = 500, db_path: str = DB_PATH) -> pd.DataFrame:
    """
    Mengunduh data ringkasan 24 jam seluruh koin dari Binance,
    memfilter Top N koin USDT paling likuid yang aktif diperdagangkan di pasar Spot,
    dan menyimpannya ke database.
    """
    active_symbols = get_active_spot_symbols()
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
        # Pastikan simbol aktif diperdagangkan di Spot Binance (bukan status BREAK/HALT/DELISTED)
        if active_symbols and sym not in active_symbols:
            continue

        # Filter hanya pasangan USDT
        last_p = float(item.get("lastPrice", 0.0))
        high_p = float(item.get("highPrice", 0.0))
        low_p = float(item.get("lowPrice", 0.0))

        # Filter komprehensif: stablecoins, peg dolar dinamis, leverage, & saham AS
        if is_stablecoin_or_excluded(sym, last_price=last_p, high_price=high_p, low_price=low_p):
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
    """Helper untuk mengunduh klines sebuah simbol dari Binance dengan penandaan candle tertutup (is_closed)."""
    url = f"{BINANCE_API_BASE}/api/v3/klines"
    # Meminta limit + 1 candle agar dipastikan mendapatkan setidaknya `limit` candle yang sudah selesai ditutup
    params = {"symbol": symbol, "interval": interval, "limit": min(limit + 1, 1000)}
    headers = {"User-Agent": "KripikTo-Scanner/1.0"}

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        if resp.status_code == 200:
            raw_klines = resp.json()
            rows = []
            now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
            now_utc_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            for k in raw_klines:
                open_time = int(k[0])
                close_time = int(k[6])
                # Candle resmi tertutup jika close_time telah terlewati oleh waktu sekarang
                is_closed = 1 if close_time <= now_ms else 0
                dt_str = datetime.datetime.fromtimestamp(
                    open_time / 1000, tz=datetime.timezone.utc
                ).strftime("%Y-%m-%d %H:%M:%S")
                rows.append((
                    symbol,
                    interval,
                    open_time,
                    close_time,
                    dt_str,
                    float(k[1]),  # Open
                    float(k[2]),  # High
                    float(k[3]),  # Low
                    float(k[4]),  # Close
                    float(k[5]),  # Volume
                    float(k[7]),  # Quote Asset Volume
                    float(k[10]), # Taker Buy Quote Asset Volume
                    is_closed,
                    now_utc_str
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
                    symbol, interval, open_time, close_time, datetime_utc,
                    open, high, low, close, volume, quote_volume, taker_buy_volume,
                    is_closed, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, all_rows)
            conn.commit()

    print(f"[✓] Berhasil mengunduh dan menyimpan {success_count}/{len(symbols)} koin ({len(all_rows)} lilin candlestick).")
    return success_count


def fetch_btc_benchmark(
    intervals: List[str] = ("4h", "1h", "15m"),
    db_path: str = DB_PATH
) -> Dict[str, Any]:
    """
    Mengunduh candlestick benchmark pasar Bitcoin (BTCUSDT) untuk seluruh interval kunci.
    Menyimpannya ke klines_history untuk kalkulasi Relative Strength (RS vs BTC).
    """
    print("[*] Mengunduh benchmark pasar Bitcoin (BTCUSDT)...")
    for iv in intervals:
        _rows = _fetch_single_kline("BTCUSDT", iv, limit=50)
        if _rows:
            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()
                cursor.executemany("""
                    INSERT OR REPLACE INTO klines_history (
                        symbol, interval, open_time, close_time, datetime_utc,
                        open, high, low, close, volume, quote_volume, taker_buy_volume,
                        is_closed, fetched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, _rows)
                conn.commit()

    return get_btc_benchmark(db_path)


def get_btc_benchmark(db_path: str = DB_PATH) -> Dict[str, Any]:
    """
    Membaca data lilin tertutup terakhir BTCUSDT untuk menghitung return 4h dan 1h BTC.
    Digunakan untuk mengukur Relative Strength (RS_4H dan RS_1H) terhadap altcoin.
    """
    res = {
        "btc_price": 0.0,
        "btc_return_4h": 0.0,
        "btc_return_1h": 0.0,
        "btc_candle_time_4h": "",
        "btc_candle_time_1h": ""
    }
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        for iv, key_ret, key_time in [
            ("4h", "btc_return_4h", "btc_candle_time_4h"),
            ("1h", "btc_return_1h", "btc_candle_time_1h")
        ]:
            cursor.execute("""
                SELECT close, datetime_utc
                FROM klines_history
                WHERE symbol = 'BTCUSDT' AND interval = ? AND (is_closed = 1 OR is_closed IS NULL)
                ORDER BY open_time DESC
                LIMIT 2
            """, (iv,))
            rows = cursor.fetchall()
            if len(rows) >= 2:
                latest_close = float(rows[0][0])
                prev_close = float(rows[1][0])
                res["btc_price"] = latest_close
                res[key_time] = rows[0][1]
                if prev_close > 0:
                    res[key_ret] = round(((latest_close - prev_close) / prev_close) * 100.0, 2)
            elif len(rows) == 1:
                res["btc_price"] = float(rows[0][0])
                res[key_time] = rows[0][1]

    return res


def fetch_mtf_klines_batch(
    symbols: List[str],
    intervals: List[str] = ("4h", "1h"),
    limit: int = 100,
    max_workers: int = 16,
    db_path: str = DB_PATH
) -> Dict[str, int]:
    """
    Mengunduh candlestick untuk seluruh simbol pada multi-timeframe (misal 4h & 1h) secara paralel.
    Menghasilkan request tasks (symbol, interval) yang dieksekusi serentak.
    """
    tasks = [(sym, iv) for sym in symbols for iv in intervals]
    print(f"[*] Mengunduh {len(symbols)} koin pada MTF {intervals} (Total {len(tasks)} tasks, limit={limit})...")
    
    all_rows = []
    success_counts = {iv: 0 for iv in intervals}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_task = {
            executor.submit(_fetch_single_kline, sym, iv, limit): (sym, iv)
            for sym, iv in tasks
        }
        for future in as_completed(future_to_task):
            sym, iv = future_to_task[future]
            try:
                res = future.result()
                if res:
                    all_rows.extend(res)
                    success_counts[iv] += 1
            except Exception:
                pass

    if all_rows:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.executemany("""
                INSERT OR REPLACE INTO klines_history (
                    symbol, interval, open_time, close_time, datetime_utc,
                    open, high, low, close, volume, quote_volume, taker_buy_volume,
                    is_closed, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, all_rows)
            conn.commit()

    print(f"[✓] MTF Download selesai: {success_counts} koin tersimpan ({len(all_rows)} lilin candlestick).")
    return success_counts


def fetch_15m_trigger_batch(
    symbols: List[str],
    limit: int = 60,
    max_workers: int = 10,
    db_path: str = DB_PATH
) -> int:
    """
    Mengunduh lilin 15M hanya untuk koin kandidat terpilih (lolos saringan 4H/1H)
    sebagai Tactical Entry Trigger konfirmasi breakout & retest.
    """
    print(f"[*] Mengunduh 15M trigger klines untuk {len(symbols)} koin kandidat...")
    return fetch_klines_batch(symbols=symbols, interval="15m", limit=limit, max_workers=max_workers, db_path=db_path)


def update_whale_flow_from_klines(interval: str = "2h", db_path: str = DB_PATH) -> None:
    """
    Menghitung Taker Buy Ratio (Whale Flow) 24 jam terakhir dan candle terakhir
    berdasarkan data candlestick klines yang telah tersimpan.
    """
    # 24 jam = 96 candle untuk 15m, 24 candle untuk 1h, 12 candle untuk 2h, 6 candle untuk 4h, 1 candle untuk 1d
    if interval == "15m":
        candles_in_24h = 96
    elif interval == "1h":
        candles_in_24h = 24
    elif interval == "2h":
        candles_in_24h = 12
    elif interval == "4h":
        candles_in_24h = 6
    else:
        candles_in_24h = 1
    
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
                WHERE symbol = ? AND interval = ? AND (is_closed = 1 OR is_closed IS NULL)
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
    interval: str = "4h",
    mtf: bool = True,
    top_n: int = 500,
    limit: int = 100,
    db_path: str = DB_PATH
) -> pd.DataFrame:
    """
    Fungsi orkestrator Tahap 1:
    1. Inisialisasi DB
    2. Unduh Benchmark Bitcoin (BTCUSDT) untuk 4h, 1h, 15m
    3. Ambil ringkasan 24h Top N koin likuid
    4. Ambil data klines multi-timeframe (4h & 1h) secara paralel
    5. Hitung agregasi Whale Flow (Taker Buy Ratio) akurat dari klines
    """
    init_db(db_path)

    # 1. Unduh Benchmark Bitcoin terlebih dahulu
    btc_bench = fetch_btc_benchmark(intervals=["4h", "1h", "15m"], db_path=db_path)
    btc_price = btc_bench.get("btc_price", 0.0)
    btc_4h = btc_bench.get("btc_return_4h", 0.0)
    btc_1h = btc_bench.get("btc_return_1h", 0.0)
    print(f"[✓] Benchmark BTC: ${btc_price:,.2f} | 4H Return: {btc_4h:+.2f}% | 1H Return: {btc_1h:+.2f}%")

    print(f"[*] [Tahap 1] Mengunduh ringkasan pasar 24 jam dari Binance...")
    df_top = fetch_24h_summary(top_n=top_n, db_path=db_path)

    if df_top.empty:
        print("[!] Gagal mengunduh ringkasan pasar.")
        return df_top

    print(f"[✓] Berhasil memfilter Top {len(df_top)} koin likuid.")
    symbols = df_top["symbol"].tolist()

    # Pastikan BTCUSDT ada di daftar simbol
    if "BTCUSDT" not in symbols:
        symbols.append("BTCUSDT")

    if mtf:
        # Unduh 4h dan 1h sekaligus untuk analisis Struktur (4H) dan Momentum (1H)
        intervals_to_fetch = ["4h", "1h"]
        if interval not in intervals_to_fetch:
            intervals_to_fetch.insert(0, interval)
        fetch_mtf_klines_batch(symbols=symbols, intervals=intervals_to_fetch, limit=limit, db_path=db_path)
        for iv in intervals_to_fetch:
            update_whale_flow_from_klines(interval=iv, db_path=db_path)
    else:
        fetch_klines_batch(symbols=symbols, interval=interval, limit=limit, db_path=db_path)
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

