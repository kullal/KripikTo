"""
main.py
Orkestrator Utama Sistem Analisis Kripto Kuantitatif & Berita AI (KripikTo):
Alur Eksekusi Berurutan:
1. Unduh Data: Ringkasan Pasar 24 Jam & Candlestick (Klines) Top 500 Koin Likuid via Binance Vision API.
2. Pemindai Kuantitatif: Hitung MA20, MA50, RSI 14, Breakout, Lonjakan Volume, dan Aliran Paus (Taker Buy Ratio).
3. Analisis Sentimen Berita AI: Scraping Berita Global + Analisis Katalis & Crypto Risk Guard via Google Gemini Flash.
4. Laporan Eksekutif & Trading Plan: Menampilkan rekomendasi gabungan dan menyimpan data ke database SQLite & JSON.
"""

import sys
import argparse
from pathlib import Path

# Pastikan output utf-8 aman di terminal Windows
if sys.platform == "win32" and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Import modul backend
from backend.data_pipeline import run_pipeline, DB_PATH
from backend.scanner import run_scanner, print_scan_report
from backend.news_sentiment import run_news_sentiment_pipeline, print_final_executive_report


def run_all(
    interval: str = "2h",
    top_picks_limit: int = 15,
    top_liquid_limit: int = 500,
    skip_download: bool = False
) -> None:
    """Menjalankan seluruh tahapan analisis kripto secara terpadu."""
    print("\n" + "=" * 100)
    print(f"🚀 MEMULAI SISTEM ANALISIS KRIPTO (KRIPIKTO) | TIMEFRAME: [{interval.upper()}]")
    print("=" * 100)

    # -------------------------------------------------------------
    # TAHAP 1: DATA PIPELINE (DOWNLOAD DATA BINANCE PUBLIC VISION)
    # -------------------------------------------------------------
    if not skip_download:
        print(f"\n[LANGKAH 1/3] Mengunduh ringkasan pasar & candlestick Top {top_liquid_limit} koin likuid...")
        try:
            run_pipeline(interval=interval, top_n=top_liquid_limit, limit=100)
        except Exception as e:
            print(f"[!] Terjadi kendala saat mengunduh data Binance ({e}).")
            print("[*] Mencoba melanjutkan analisis menggunakan data lokal yang tersedia di kripto.db...")
    else:
        print("\n[LANGKAH 1/3] Melewati unduhan data (--skip-download aktif). Menggunakan database yang ada.")

    # -------------------------------------------------------------
    # TAHAP 2: QUANTITATIVE & WHALE SCANNER
    # -------------------------------------------------------------
    print(f"\n[LANGKAH 2/3] Memindai indikator teknikal & aliran dana paus untuk Top {top_picks_limit} koin...")
    scan_picks = run_scanner(interval=interval, min_score=40, top_n=top_picks_limit)
    if scan_picks.empty:
        print("[!] Tidak ada koin yang memenuhi kriteria scanner saat ini.")
        return

    print_scan_report(scan_picks)

    # -------------------------------------------------------------
    # TAHAP 3: GLOBAL NEWS SCRAPING + GEMINI LLM SENTIMENT & RISK GUARD
    # -------------------------------------------------------------
    print(f"\n[LANGKAH 3/3] Mengambil berita global & menganalisis sentimen katalis dengan Google Gemini...")
    final_picks = run_news_sentiment_pipeline(top_limit=top_picks_limit)

    if final_picks:
        print_final_executive_report(final_picks)
        print("✅ Alur eksekusi selesai! Seluruh rekomendasi & Trading Plan tersimpan rapi di database SQLite.")
    else:
        print("[!] Gagal menyelesaikan analisis sentimen berita.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KripikTo: Quantitative Crypto Scanner & AI Sentiment System")
    parser.add_argument(
        "--interval",
        type=str,
        default="2h",
        choices=["1h", "2h", "4h", "1d"],
        help="Interval candlestick untuk analisis (default: 2h, pilihan: 1h, 2h, 4h, 1d)"
    )
    parser.add_argument(
        "--top",
        type=int,
        default=15,
        help="Jumlah koin teratas hasil scan untuk dianalisis beritanya dengan Gemini AI (default: 15)"
    )
    parser.add_argument(
        "--liquid",
        type=int,
        default=500,
        help="Jumlah koin likuid yang dipantau dari Binance (default: 500)"
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Lewati unduhan data jika database kripto.db sudah diperbarui baru-baru ini"
    )

    args = parser.parse_args()
    run_all(
        interval=args.interval,
        top_picks_limit=args.top,
        top_liquid_limit=args.liquid,
        skip_download=args.skip_download
    )
