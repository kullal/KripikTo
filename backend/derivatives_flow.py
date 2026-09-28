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
    """Inisialisasi tabel derivatives_summary di SQLite."""
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
        conn.commit()


def fetch_derivatives_summary(db_path: str = DB_PATH) -> Dict[str, Dict[str, Any]]:
    """
    Mengunduh data Funding Rate dan Open Interest Binance Futures.
    Mengembalikan mapping {symbol: {'funding_rate': float, 'open_interest_m': float}}.
    """
    init_derivatives_db(db_path)
    now_utc_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    results_map = {}
    records_to_insert = []

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

    # Simpan ke SQLite
    if records_to_insert:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.executemany("""
                INSERT OR REPLACE INTO derivatives_summary (
                    symbol, funding_rate, open_interest, open_interest_m, updated_at
                ) VALUES (?, ?, ?, ?, ?)
            """, records_to_insert)
            conn.commit()

    return results_map


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
