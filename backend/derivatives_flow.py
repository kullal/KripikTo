"""
derivatives_flow.py
Modul Aliran Pasar Derivatif (Funding Rate & Open Interest untuk Spot Scalping):
1. Mengambil data Funding Rate & Open Interest (OI) Binance Futures via CoinGecko Derivatives API publik (bebas akses tanpa VPN).
2. Mendeteksi anomali pasar derivatif untuk strategi SPOT SCALPING (+6% Quick Profit):
   - Funding Rate Negatif Ekstrem (< -0.02% s.d < -0.10%): Terjadi penumpukan posisi Short ritel yang over-leveraged.
     Paus kerap memborong koin di pasar SPOT untuk memicu Short Squeeze, menghasilkan ledakan harga instan +6% s.d +15%!
   - Funding Rate Positif Ekstrem (> +0.05%): Pasar spot rawan long liquidation dump (hindari beli di pucuk).
3. Menyimpan data ke database SQLite lokal (kripto.db).
"""

import sys
import sqlite3
import datetime
from pathlib import Path
from typing import Dict, Any, Optional
import requests

# Pastikan output utf-8 aman di terminal Windows
if sys.platform == "win32" and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = str(DATA_DIR / "kripto.db")
DERIVATIVES_API_URL = "https://api.coingecko.com/api/v3/derivatives"


def init_derivatives_db(db_path: str = DB_PATH) -> None:
    """Inisialisasi tabel derivatives_summary dan derivatives_history di SQLite."""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS derivatives_summary (
                symbol TEXT PRIMARY KEY,
                funding_rate REAL,
                open_interest REAL,
                open_interest_m REAL,
                updated_at TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS derivatives_history (
                symbol TEXT,
                timestamp_ms INTEGER,
                recorded_at TEXT,
                funding_rate REAL,
                open_interest REAL,
                open_interest_m REAL,
                PRIMARY KEY (symbol, timestamp_ms)
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_derivatives_hist 
            ON derivatives_history(symbol, timestamp_ms DESC)
        """)
        conn.commit()


def migrate_history_from_scans(db_path: str = DB_PATH) -> int:
    """Memindahkan riwayat funding rate & OI dari scan_results ke derivatives_history."""
    init_derivatives_db(db_path)
    count = 0
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT symbol, scan_time, funding_rate, open_interest_m 
            FROM scan_results 
            WHERE funding_rate IS NOT NULL AND open_interest_m IS NOT NULL
        """)
        rows = cursor.fetchall()
        for sym, scan_t, fr, oi_m in rows:
            try:
                dt = datetime.datetime.strptime(scan_t, "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone.utc)
                ts_ms = int(dt.timestamp() * 1000)
                oi_raw = float(oi_m) * 1_000_000.0
                cursor.execute("""
                    INSERT OR IGNORE INTO derivatives_history (
                        symbol, timestamp_ms, recorded_at, funding_rate, open_interest, open_interest_m
                    ) VALUES (?, ?, ?, ?, ?, ?)
                """, (sym, ts_ms, scan_t, float(fr), oi_raw, float(oi_m)))
                if cursor.rowcount > 0:
                    count += 1
            except Exception:
                continue
        conn.commit()
    return count


def fetch_derivatives_summary(db_path: str = DB_PATH) -> Dict[str, Dict[str, Any]]:
    """
    Mengunduh data Funding Rate dan Open Interest Binance Futures.
    Mengembalikan mapping {symbol: {'funding_rate': float, 'open_interest_m': float, ...}}.
    Juga menyimpan deret waktu ke derivatives_history untuk mengukur Delta OI & Delta Funding.
    """
    init_derivatives_db(db_path)
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    now_utc_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    now_ts_ms = int(now_dt.timestamp() * 1000)

    results_map = {}
    records_to_insert = []
    history_to_insert = []

    try:
        headers = {"User-Agent": "KripikTo-Scanner/1.0"}
        resp = requests.get(DERIVATIVES_API_URL, headers=headers, timeout=12)
        if resp.status_code == 200:
            contracts = resp.json()
            for c in contracts:
                # Fokus pada kontrak Binance Futures
                if c.get("market") == "Binance (Futures)":
                    sym = c.get("symbol", "").strip()
                    if not sym.endswith("USDT"):
                        continue

                    try:
                        fr = float(c.get("funding_rate") or 0.0)
                        oi = float(c.get("open_interest") or 0.0)
                        oi_m = round(oi / 1_000_000.0, 2)
                        
                        data_point = {
                            "symbol": sym,
                            "funding_rate": round(fr, 4),
                            "open_interest": oi,
                            "open_interest_m": oi_m,
                            "updated_at": now_utc_str
                        }
                        results_map[sym] = data_point
                        records_to_insert.append((sym, round(fr, 4), oi, oi_m, now_utc_str))
                        history_to_insert.append((sym, now_ts_ms, now_utc_str, round(fr, 4), oi, oi_m))
                    except (ValueError, TypeError):
                        continue
    except Exception as e:
        print(f"[!] Gagal mengambil data derivatif online ({e}). Menggunakan data lokal di database.")
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT symbol, funding_rate, open_interest, open_interest_m, updated_at FROM derivatives_summary")
            for row in cursor.fetchall():
                results_map[row[0]] = {
                    "symbol": row[0],
                    "funding_rate": row[1],
                    "open_interest": row[2],
                    "open_interest_m": row[3],
                    "updated_at": row[4]
                }
        return results_map

    # Simpan ke SQLite (summary dan history time series)
    if records_to_insert:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.executemany("""
                INSERT OR REPLACE INTO derivatives_summary (
                    symbol, funding_rate, open_interest, open_interest_m, updated_at
                ) VALUES (?, ?, ?, ?, ?)
            """, records_to_insert)
            cursor.executemany("""
                INSERT OR REPLACE INTO derivatives_history (
                    symbol, timestamp_ms, recorded_at, funding_rate, open_interest, open_interest_m
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, history_to_insert)
            conn.commit()

    return results_map


def get_derivatives_deltas(symbol: str, db_path: str = DB_PATH) -> Dict[str, float]:
    """
    Menghitung perubahan dinamis pasar derivatif (Fase 4):
    - delta_oi_1h : Perubahan Open Interest dalam 1 jam (dalam Juta USD)
    - delta_oi_4h : Perubahan Open Interest dalam 4 jam (dalam Juta USD)
    - delta_funding_1h : Perubahan Funding Rate dalam 1 jam
    """
    init_derivatives_db(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT timestamp_ms, funding_rate, open_interest_m 
            FROM derivatives_history 
            WHERE symbol = ? 
            ORDER BY timestamp_ms DESC 
            LIMIT 20
        """, (symbol,))
        rows = cursor.fetchall()

    if not rows:
        return {
            "delta_oi_1h": 0.0,
            "delta_oi_4h": 0.0,
            "delta_funding_1h": 0.0,
            "funding_rate": 0.0,
            "open_interest_m": 0.0
        }

    latest_ts, latest_fr, latest_oi = rows[0]
    one_hour_ms = 3600 * 1000
    four_hours_ms = 4 * 3600 * 1000

    row_1h = None
    row_4h = None

    for ts, fr, oi in rows[1:]:
        age = latest_ts - ts
        # Cari snapshot yang mendekati 1 jam (antara 30 menit s.d 2 jam)
        if 1800 * 1000 <= age <= 7200 * 1000 and row_1h is None:
            row_1h = (ts, fr, oi)
        # Cari snapshot yang mendekati 4 jam (antara 3 jam s.d 8 jam)
        if 3 * 3600 * 1000 <= age <= 8 * 3600 * 1000 and row_4h is None:
            row_4h = (ts, fr, oi)

    # Jika tidak ada yang tepat 1h, ambil snapshot kedua terakhir jika usianya masuk akal
    if row_1h is None and len(rows) > 1:
        row_1h = rows[1]

    d_oi_1h = round(latest_oi - row_1h[2], 2) if row_1h else 0.0
    d_oi_4h = round(latest_oi - row_4h[2], 2) if row_4h else d_oi_1h
    d_fr_1h = round(latest_fr - row_1h[1], 4) if row_1h else 0.0

    return {
        "delta_oi_1h": d_oi_1h,
        "delta_oi_4h": d_oi_4h,
        "delta_funding_1h": d_fr_1h,
        "funding_rate": latest_fr,
        "open_interest_m": latest_oi
    }


if __name__ == "__main__":
    print("=== TEST RUN DERIVATIVES FLOW ===")
    d_map = fetch_derivatives_summary()
    print(f"Total kontrak Binance Futures tersimpan: {len(d_map)}")
    
    # Koin dengan potensi Short Squeeze (Funding Rate < -0.01%)
    short_squeeze = [v for v in d_map.values() if v["funding_rate"] < -0.01]
    short_squeeze.sort(key=lambda x: x["funding_rate"])
    print(f"\nKandidat Potensi Short Squeeze (Funding Rate Negatif untuk Spot Scalping): {len(short_squeeze)}")
    for x in short_squeeze[:5]:
        print(f"  {x['symbol']:<12} Funding Rate: {x['funding_rate']:+.4f}% | OI: ${x['open_interest_m']}M")
