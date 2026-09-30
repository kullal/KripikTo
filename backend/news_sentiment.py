"""
news_sentiment.py
Modul Analisis Sentimen Berita Kripto dengan Google Gemini LLM (Tahap 3):
1. Mengambil berita kripto global terkini per koin via Google News RSS (CoinDesk, Cointelegraph, Decrypt, dll).
2. Memeriksa Crypto Risk Guard (Deteksi dini: Hack, Exploit smart contract, Tuntutan SEC, Delisting, atau Rugpull).
3. Menganalisis dampak fundamental & ekstraksi katalis menggunakan Google Gemini Flash LLM.
4. Menghitung Total Skor Gabungan (Bobot 70% Teknikal & Whale Flow + 30% Sentimen Berita AI).
5. Menyimpan rekomendasi final lengkap dengan Trading Plan ke database SQLite dan file JSON.
"""

import os
import sys
import json
import time
import datetime
import sqlite3
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Dict, Any, Optional
import pandas as pd
import requests

# Pastikan output utf-8 aman di terminal Windows
if sys.platform == "win32" and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Setup paths
BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = str(DATA_DIR / "kripto.db")
SCAN_JSON_PATH = str(DATA_DIR / "scan_latest.json")
FINAL_JSON_PATH = str(DATA_DIR / "final_recommendations.json")
ENV_FILE = PROJECT_ROOT / ".env"


def load_gemini_api_key() -> str:
    """Membaca GEMINI_API_KEY dari environment variable atau file .env."""
    key = os.environ.get("GEMINI_API_KEY", "")
    if key:
        return key

    if ENV_FILE.exists():
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("GEMINI_API_KEY="):
                    return line.strip().split("=", 1)[1]
    return ""


def init_news_db(db_path: str = DB_PATH) -> None:
    """Inisialisasi tabel analisis sentimen berita dan rekomendasi final di SQLite."""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS kripto_sentiment_analysis (
                scan_time TEXT,
                symbol TEXT,
                tech_score INTEGER,
                funding_rate REAL,
                open_interest_m REAL,
                sentiment TEXT,
                sentiment_score REAL,
                impact_level TEXT,
                catalyst TEXT,
                news_count INTEGER,
                crypto_risk INTEGER,
                final_score INTEGER,
                recommendation TEXT,
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
        # Auto-migration jika kolom belum ada
        cursor.execute("PRAGMA table_info(kripto_sentiment_analysis)")
        cols = [c[1] for c in cursor.fetchall()]
        for col, ctype in [
            ("funding_rate", "REAL"),
            ("open_interest_m", "REAL"),
            ("v2_score", "INTEGER"),
            ("setup_type", "TEXT"),
            ("entry_status", "TEXT")
        ]:
            if col not in cols:
                try:
                    cursor.execute(f"ALTER TABLE kripto_sentiment_analysis ADD COLUMN {col} {ctype}")
                except Exception:
                    pass
        conn.commit()



def fetch_news_for_crypto(symbol: str, max_items: int = 4) -> List[Dict[str, str]]:
    """
    Mengambil berita global terkini dari Google News RSS untuk koin terkait.
    Mencakup agregasi portal media kripto terkemuka (CoinDesk, Cointelegraph, Decrypt, dll).
    Menggabungkan pencarian berita umum dan pencarian khusus risiko keamanan (Hack, Exploit, Delist, SEC).
    """
    base_coin = symbol.replace("USDT", "").strip()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    def _query_rss(query_str: str, limit: int = 3) -> List[Dict[str, str]]:
        encoded = urllib.parse.quote(query_str)
        rss_url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
        try:
            resp = requests.get(rss_url, headers=headers, timeout=10)
            if resp.status_code != 200:
                return []
            root = ET.fromstring(resp.content)
            items = []
            for it in root.findall(".//item")[:limit]:
                title = it.find("title").text if it.find("title") is not None else ""
                pub_date = it.find("pubDate").text if it.find("pubDate") is not None else ""
                source = it.find("source").text if it.find("source") is not None else "Media"
                link = it.find("link").text if it.find("link") is not None else ""
                if title:
                    items.append({
                        "title": title,
                        "source": source,
                        "pub_date": pub_date,
                        "link": link
                    })
            return items
        except Exception:
            return []

    # 1. Kueri Risiko Keamanan & Regulasi (Crypto Risk Guard)
    risk_query = f'"{base_coin}" crypto (hack OR exploit OR "sec" OR lawsuit OR delist OR scam OR stolen OR insolvency)'
    risk_items = _query_rss(risk_query, limit=2)

    # 2. Kueri Berita Umum Proyek & Pergerakan Pasar
    gen_query = f'"{base_coin}" crypto (price OR partnership OR upgrade OR launch OR bull OR whale OR ETF)'
    gen_items = _query_rss(gen_query, limit=max_items)

    # Gabungkan dengan prioritas berita risiko di awal & cegah duplikasi
    seen_titles = set()
    combined_items = []
    for item in risk_items + gen_items:
        clean_title = item["title"].lower().strip()
        if clean_title not in seen_titles:
            seen_titles.add(clean_title)
            combined_items.append(item)
            if len(combined_items) >= max_items:
                break

    return combined_items


def analyze_sentiment_with_gemini(
    symbol: str,
    news_items: List[Dict[str, str]],
    api_key: str,
    fgi_context: str = ""
) -> Dict[str, Any]:
    """
    Mengirim headline berita ke Google Gemini Flash API untuk analisis sentimen & ekstraksi katalis,
    diselaraskan dengan cuaca makro Crypto Fear & Greed Index.
    """
    base_coin = symbol.replace("USDT", "")

    if not news_items:
        return {
            "sentiment": "NEUTRAL",
            "sentiment_score": 0.0,
            "catalyst": "Tidak ada berita spesifik terbaru yang signifikan; murni didorong momentum teknikal.",
            "impact_level": "RENDAH",
            "crypto_risk": False
        }

    headlines = "\n".join([f"- [{item['source']}] {item['title']}" for item in news_items])
    macro_info = f"Sentimen Makro Pasar Kripto Global: {fgi_context}\n" if fgi_context else ""

    prompt = f"""
Kamu adalah analis pasar aset kripto profesional yang objektif, teliti, dan mengutamakan manajemen risiko.
Tugasmu: Analisis kumpulan berita terkini berikut untuk koin kripto {base_coin} ({symbol}).

{macro_info}
Kumpulan Berita Global Terbaru:
{headlines}


Instruksi Analisis:
1. PERIKSA RISIKO KEAMANAN & REGULASI (CRYPTO RISK GUARD):
   Periksa secara ketat apakah ada berita terkait:
   - Peretasan / Exploit smart contract / Pencurian dana (hack / exploit / drained).
   - Tindakan hukum / tuntutan regulasi keras (gugatan SEC, CFTC, investigasi kriminal).
   - Pengumuman delisting dari bursa besar (Binance, Coinbase, dll.).
   - Masalah likuiditas, gagal bayar, atau token unlock dumping masif.
2. JIKA TERDETEKSI RISIKO KEAMANAN/REGULASI/DELISTING:
   - "sentiment" WAJIB "BEARISH".
   - "sentiment_score" WAJIB bernilai negatif antara -0.8 sampai -1.0.
   - "crypto_risk" WAJIB true.
   - "catalyst" WAJIB menjelaskan risiko tersebut dalam Bahasa Indonesia yang tegas dan jelas.
   - "impact_level" = "TINGGI".
3. JIKA TIDAK ADA RISIKO KRITIKAL:
   - "sentiment": "BULLISH" | "NEUTRAL" | "BEARISH" sesuai dampak fundamental, adopsi, upgrade, atau aksi paus.
   - "sentiment_score": float (-1.0 s.d +1.0).
   - "catalyst": 1-2 kalimat ringkas dalam Bahasa Indonesia mengenai inti pemicu / katalis berita.
   - "impact_level": "TINGGI" | "SEDANG" | "RENDAH".
   - "crypto_risk": false.

Kembalikan jawaban HANYA berupa JSON valid dengan skema berikut:
{{
  "sentiment": "BULLISH" | "NEUTRAL" | "BEARISH",
  "sentiment_score": float,
  "catalyst": "string",
  "impact_level": "TINGGI" | "SEDANG" | "RENDAH",
  "crypto_risk": boolean
}}
"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.2
        }
    }

    # Model prioritas Gemini Flash untuk fallback otomatis jika satu model sibuk
    models_to_try = ["gemini-flash-latest", "gemini-3.5-flash-lite", "gemini-3.8-flash"]

    for model_name in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
        try:
            resp = requests.post(url, json=payload, timeout=25)
            if resp.status_code == 200:
                data = resp.json()
                parts = data["candidates"][0]["content"]["parts"]
                text_resp = parts[0]["text"].strip()
                
                # Bersihkan markdown code block jika ada (```json ... ```)
                if text_resp.startswith("```"):
                    lines = text_resp.split("\n")
                    if lines[0].startswith("```"):
                        lines = lines[1:]
                    if lines and lines[-1].strip().startswith("```"):
                        lines = lines[:-1]
                    text_resp = "\n".join(lines).strip()

                parsed = json.loads(text_resp)
                return {
                    "sentiment": str(parsed.get("sentiment", "NEUTRAL")).upper(),
                    "sentiment_score": float(parsed.get("sentiment_score", 0.0)),
                    "catalyst": str(parsed.get("catalyst", "-")),
                    "impact_level": str(parsed.get("impact_level", "SEDANG")).upper(),
                    "crypto_risk": bool(parsed.get("crypto_risk", False))
                }
            elif resp.status_code in (429, 503):
                time.sleep(1.5)
                continue
            else:
                print(f"[!] Gemini ({model_name}) status {resp.status_code}: {resp.text[:60]}")
        except Exception as e:
            time.sleep(1)
            continue

    return {
        "sentiment": "NEUTRAL",
        "sentiment_score": 0.0,
        "catalyst": "Koneksi model sentimen tidak tersedia; menggunakan skor teknikal & paus.",
        "impact_level": "RENDAH",
        "crypto_risk": False
    }


def calculate_composite_score(tech_score: int, sentiment_score: float, crypto_risk: bool = False) -> int:
    """
    Menghitung skor gabungan akhir:
    - 70% Bobot Teknikal & Whale Flow (0 - 100)
    - 30% Bobot Sentimen Berita AI (-1.0 s.d +1.0 dinormalisasi ke 0 - 100)
    - Pinalti keras jika terdeteksi risiko keamanan/exploit/delisting (maksimal skor 35)
    """
    normalized_news = (sentiment_score + 1.0) / 2.0 * 100  # -1.0 -> 0, 0.0 -> 50, +1.0 -> 100
    final_score = int(round(tech_score * 0.70 + normalized_news * 0.30))
    if crypto_risk:
        final_score = min(final_score, 35)
    return max(0, min(100, final_score))


def get_recommendation_label(
    final_score: int,
    sentiment: str,
    tech_score: int,
    crypto_risk: bool = False,
    entry_status: str = "WAIT",
    setup_type: str = "NO_SETUP"
) -> str:
    """Menentukan label rekomendasi berdasarkan skor gabungan, status trigger 15M, dan pengaman risiko."""
    if crypto_risk:
        return "⚠️ AVOID (Hack / Exploit / Delisting Risk)"
    elif tech_score >= 75 and sentiment == "BEARISH":
        return "⚠️ CAUTION (Bad News Divergence)"
    elif entry_status == "TRIGGERED":
        return "🎯 TRIGGERED_BUY (15M Valid)"
    elif setup_type == "ACCUMULATION_COIL":
        return "⏳ ACCUMULATION (Paus Diam)"
    elif final_score >= 80:
        return "🚀 STRONG_BUY (Paus + Katalis Bullish)"
    elif final_score >= 65:
        return "✅ BUY_MOMENTUM (Akumulasi Sehat)"
    elif final_score >= 50:
        return "👀 WATCHLIST (Pantau Support)"
    else:
        return "⏳ NEUTRAL / WAIT"


def run_news_sentiment_pipeline(
    top_limit: int = 15,
    fgi: Optional[Dict[str, Any]] = None,
    db_path: str = DB_PATH
) -> List[Dict[str, Any]]:
    """
    Menjalankan alur lengkap analisis sentimen berita LLM untuk koin hasil scan teratas.
    """
    api_key = load_gemini_api_key()
    if not api_key:
        print("[!] GEMINI_API_KEY tidak ditemukan di environment atau .env.")
        print("[*] Masukkan GEMINI_API_KEY di file .env untuk mengaktifkan analisis berita AI.")
        return []

    init_news_db(db_path)

    fgi_context = ""
    if fgi:
        fgi_context = f"{fgi.get('classification', 'Neutral')} ({fgi.get('value', 50)}/100) - {fgi.get('advice', '')}"

    # Baca kandidat koin dari file scan_latest.json atau SQLite
    scan_candidates = []
    if Path(SCAN_JSON_PATH).exists():
        with open(SCAN_JSON_PATH, "r", encoding="utf-8") as f:
            scan_candidates = json.load(f)
    else:
        with sqlite3.connect(db_path) as conn:
            df = pd.read_sql("SELECT * FROM scan_results ORDER BY score DESC, taker_buy_ratio DESC LIMIT ?", conn, params=(top_limit,))
            scan_candidates = df.to_dict(orient="records")

    if not scan_candidates:
        print("[!] Tidak ada kandidat koin dari pemindaian Tahap 2.")
        return []

    picks_to_analyze = scan_candidates[:top_limit]
    print(f"[*] Menganalisis berita global & sentimen Gemini Flash untuk {len(picks_to_analyze)} koin terpilih...")

    final_results = []
    now_utc_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    for i, item in enumerate(picks_to_analyze):
        sym = item["symbol"]
        tech_score = int(item["score"])
        fr = float(item.get("funding_rate", 0.0))
        oi_m = float(item.get("open_interest_m", 0.0))
        print(f"    [{i+1}/{len(picks_to_analyze)}] Memeriksa berita untuk {sym} (Skor Teknikal: {tech_score}, FR: {fr:+.3f}%)...")

        # Tambahkan konteks Short Squeeze ke prompt jika Funding Rate negatif
        coin_fgi_context = fgi_context
        if fr <= -0.015:
            squeeze_note = f"\nKondisi Derivatif Koin: Funding Rate {fr:+.4f}% (Potensi SHORT SQUEEZE: Posisi short ritel over-leveraged, rawan terlikuidasi jika harga spot naik)."
            coin_fgi_context = (fgi_context + squeeze_note) if fgi_context else squeeze_note.strip()

        news_items = fetch_news_for_crypto(symbol=sym, max_items=4)
        sentiment_res = analyze_sentiment_with_gemini(
            symbol=sym,
            news_items=news_items,
            api_key=api_key,
            fgi_context=coin_fgi_context
        )

        sentiment = sentiment_res["sentiment"]
        sent_score = sentiment_res["sentiment_score"]
        impact = sentiment_res["impact_level"]
        catalyst = sentiment_res["catalyst"]
        crypto_risk = sentiment_res["crypto_risk"]

        v2_score = int(item.get("v2_score", tech_score))
        setup_type = str(item.get("setup_type", "NO_SETUP"))
        entry_status = str(item.get("entry_status", "WAIT"))

        final_score = calculate_composite_score(tech_score, sent_score, crypto_risk)
        recom = get_recommendation_label(
            final_score, sentiment, tech_score, crypto_risk,
            entry_status=entry_status, setup_type=setup_type
        )

        record = {
            "scan_time": now_utc_str,
            "symbol": sym,
            "tech_score": tech_score,
            "v2_score": v2_score,
            "setup_type": setup_type,
            "entry_status": entry_status,
            "funding_rate": fr,
            "open_interest_m": oi_m,
            "sentiment": sentiment,
            "sentiment_score": sent_score,
            "impact_level": impact,
            "catalyst": catalyst,
            "news_count": len(news_items),
            "crypto_risk": 1 if crypto_risk else 0,
            "final_score": final_score,
            "recommendation": recom,
            "buy_area": item.get("buy_area", "-"),
            "stop_loss": float(item.get("stop_loss", 0.0)),
            "stop_loss_pct": float(item.get("stop_loss_pct", 0.0)),
            "tp1": float(item.get("tp1", 0.0)),
            "tp1_pct": float(item.get("tp1_pct", 0.0)),
            "tp2": float(item.get("tp2", 0.0)),
            "tp2_pct": float(item.get("tp2_pct", 0.0)),
            "risk_reward": item.get("risk_reward", "-")
        }
        final_results.append(record)
        time.sleep(0.5)

    # Urutkan berdasarkan Skor Akhir tertinggi
    final_results.sort(key=lambda x: x["final_score"], reverse=True)

    # Simpan ke SQLite
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        insert_query = """
            INSERT OR REPLACE INTO kripto_sentiment_analysis (
                scan_time, symbol, tech_score, funding_rate, open_interest_m,
                sentiment, sentiment_score, impact_level, catalyst, news_count,
                crypto_risk, final_score, recommendation, buy_area, stop_loss,
                stop_loss_pct, tp1, tp1_pct, tp2, tp2_pct, risk_reward,
                v2_score, setup_type, entry_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        records = [
            (
                r["scan_time"], r["symbol"], r["tech_score"], r["funding_rate"], r["open_interest_m"],
                r["sentiment"], r["sentiment_score"], r["impact_level"], r["catalyst"], r["news_count"],
                r["crypto_risk"], r["final_score"], r["recommendation"], r["buy_area"], r["stop_loss"],
                r["stop_loss_pct"], r["tp1"], r["tp1_pct"], r["tp2"], r["tp2_pct"], r["risk_reward"],
                r["v2_score"], r["setup_type"], r["entry_status"]
            )
            for r in final_results
        ]
        cursor.executemany(insert_query, records)
        conn.commit()

    # Simpan ke JSON final
    with open(FINAL_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(final_results, f, indent=2, ensure_ascii=False)

    return final_results


def print_final_executive_report(final_picks: List[Dict[str, Any]]) -> None:
    """Menampilkan laporan eksekutif final gabungan Teknikal + Berita AI + Trading Plan."""
    if not final_picks:
        return

    print("\n" + "=" * 145)
    print("💎 REKOMENDASI FINAL SPOT SCALPING: GABUNGAN KRIPIKTO v2, TAKSONOMI PASAR & AI RISK GUARD")
    print("=" * 145)
    print(f"{'NO':<3} {'SIMBOL':<10} {'SETUP TYPE':<18} {'STATUS':<8} {'v2':<4} {'FUNDING':<9} {'BERITA':<9} {'AKHIR':<6} {'REKOMENDASI':<30} {'KATALIS UTAMA AI'}")
    print("-" * 145)

    for i, r in enumerate(final_picks):
        sent_badge = f"{r['sentiment']} ({r['sentiment_score']:+.1f})"
        fr_str = f"{r.get('funding_rate', 0.0):+.3f}%"
        setup_str = r.get("setup_type", "NO_SETUP")
        stat_str = r.get("entry_status", "WAIT")
        if stat_str == "READY":
            stat_str = "🚀 RDY"
        elif stat_str == "EXTENDED":
            stat_str = "⚠️ EXT"
        else:
            stat_str = "⏳ WAIT"

        catalyst_short = r['catalyst']
        if len(catalyst_short) > 42:
            catalyst_short = catalyst_short[:39] + "..."

        v2_val = r.get("v2_score", r.get("tech_score", 0))

        print(f"{i+1:<3} {r['symbol']:<10} {setup_str:<18} {stat_str:<8} {v2_val:<4} {fr_str:<9} {sent_badge:<9} {r['final_score']:<6} {r['recommendation']:<30} {catalyst_short}")

    print("=" * 145)
    print("\n📋 TRADING PLAN SPOT SCALPING (TOP 3 PICKS - TARGET PROFIT ADAPTIF):")
    print("-" * 75)
    for i, r in enumerate(final_picks[:3]):
        print(f"#{i+1} {r['symbol']} | Setup: {r.get('setup_type', '-')} | Status: {r.get('entry_status', '-')} | Rekomendasi: {r['recommendation']}")
        print(f"   Buy Area   : {r['buy_area']}")
        print(f"   Stop Loss  : ${r['stop_loss']} ({r['stop_loss_pct']}%)")
        print(f"   TP 1 (Scalp): ${r['tp1']} (+{r['tp1_pct']}%) -> Target Jual Otomatis!")
        print(f"   TP 2 (Ext) : ${r['tp2']} (+{r['tp2_pct']}%)")
        print(f"   Risk/Reward: {r['risk_reward']}")
        print(f"   Katalis AI : {r['catalyst']}\n")
    print("=" * 145)
    print(f"📁 Rekap JSON final tersimpan di: {FINAL_JSON_PATH}\n")



if __name__ == "__main__":
    print("=== TEST RUN NEWS SENTIMENT PIPELINE (TAHAP 3) ===")
    results = run_news_sentiment_pipeline(top_limit=5)
    print_final_executive_report(results)
