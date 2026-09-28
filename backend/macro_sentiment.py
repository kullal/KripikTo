"""
macro_sentiment.py
Modul Barometer Sentimen Makro Kripto (Crypto Fear & Greed Index):
1. Mengambil data indeks emosi pasar dari Alternative.me API secara publik & gratis.
2. Mengklasifikasikan rezim pasar berdasarkan temuan riset akademik (Finance Research Letters / JBEF):
   - Extreme Fear (0-24): Penipisan likuiditas (Liquidity Depletion), rawan False Breakout, prioritaskan Whale Divergence.
   - Fear (25-44): Pasar berhati-hati, selektif pada koin berfundamental/katalis kuat.
   - Neutral (45-54): Pasar seimbang, patuh pada level teknikal.
   - Greed (55-74): Sweet spot, momentum & breakout memiliki probabilitas tembus (win rate) tertinggi.
   - Extreme Greed (75-100): Asymmetric tail risk (risiko flash dump tiba-tiba), wajib perketat Stop Loss.
3. Menyimpan riwayat sentimen ke database SQLite (kripto.db).
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
FNG_API_URL = "https://api.alternative.me/fng/?limit=1"


def init_macro_db(db_path: str = DB_PATH) -> None:
    """Inisialisasi tabel macro_sentiment di SQLite."""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS macro_sentiment (
                date TEXT PRIMARY KEY,
                value INTEGER,
                classification TEXT,
                regime TEXT,
                updated_at TEXT
            )
        """)
        conn.commit()


def fetch_fear_and_greed_index(db_path: str = DB_PATH) -> Dict[str, Any]:
    """
    Mengambil data Crypto Fear & Greed Index terkini.
    Mengembalikan dictionary dengan nilai numerik, klasifikasi, dan arahan rezim.
    """
    init_macro_db(db_path)
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")

    val = 50
    classification = "Neutral"

    try:
        resp = requests.get(FNG_API_URL, headers={"User-Agent": "KripikTo-Scanner/1.0"}, timeout=10)
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            if data:
                val = int(data[0].get("value", 50))
                classification = data[0].get("value_classification", "Neutral")
    except Exception as e:
        print(f"[!] Gagal mengambil Fear & Greed Index online ({e}). Menggunakan data fallback/lokal.")
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value, classification FROM macro_sentiment ORDER BY date DESC LIMIT 1")
            row = cursor.fetchone()
            if row:
                val, classification = row[0], row[1]

    # Analisis Rezim Pasar Berdasarkan Literatur Akademik
    if val <= 24:
        regime = "EXTREME_FEAR"
        advice = "Likuiditas tipis (False Breakout tinggi). Hati-hati mengejar harga, cari koin dengan Akumulasi Paus di harga bawah."
    elif 25 <= val <= 44:
        regime = "FEAR"
        advice = "Pasar selektif. Fokus hanya pada koin dengan katalis berita positif dan aliran dana paus agresif."
    elif 45 <= val <= 54:
        regime = "NEUTRAL"
        advice = "Pasar seimbang. Pergerakan harga disiplin pada level teknikal (MA20/MA50 dan Support/Resistance)."
    elif 55 <= val <= 74:
        regime = "GREED"
        advice = "Rezim Sweet Spot. Likuiditas melimpah, momentum & breakout memiliki akurasi tembus (win rate) tertinggi."
    else:
        regime = "EXTREME_GREED"
        advice = "Pasar jenuh / euforia tinggi. Waspada flash dump tiba-tiba (tail risk); wajib perketat Stop Loss (Trailing Stop)."

    # Simpan ke SQLite
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO macro_sentiment (date, value, classification, regime, updated_at)
            VALUES (?, ?, ?, ?, ?)
        """, (today_str, val, classification, regime, now_utc.strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()

    return {
        "value": val,
        "classification": classification,
        "regime": regime,
        "advice": advice,
        "date": today_str
    }


def print_macro_banner(fgi: Dict[str, Any]) -> None:
    """Mencetak banner cuaca pasar makro yang informatif di terminal."""
    val = fgi.get("value", 50)
    cls = fgi.get("classification", "Neutral")
    advice = fgi.get("advice", "")

    icon = "🔥" if val >= 55 else ("❄️" if val <= 44 else "⚖️")
    print("\n" + "=" * 115)
    print(f"{icon} KONDISI PASAR MAKRO (CRYPTO FEAR & GREED INDEX): {cls.upper()} ({val}/100)")
    print(f"📌 Implikasi Akademik: {advice}")
    print("=" * 115)


if __name__ == "__main__":
    print("=== TEST RUN MACRO SENTIMENT ===")
    fgi = fetch_fear_and_greed_index()
    print_macro_banner(fgi)
