"""
news_sentiment.py
Modul Analisis Sentimen Berita Kripto dengan Google Gemini LLM (Tahap 3):
1. Mengambil berita kripto global terkini per koin via Google News RSS (CoinDesk, Cointelegraph, Decrypt, dll).
2. Memeriksa Crypto Risk Guard (Deteksi dini: Hack, Exploit smart contract, Tuntutan SEC, Delisting, atau Rugpull).
3. Menganalisis dampak fundamental & ekstraksi katalis menggunakan Google Gemini Flash LLM.
4. Menghitung Total Skor Gabungan (Bobot 70% Teknikal & Whale Flow + 30% Sentimen Berita AI).
5. Menyimpan rekomendasi final lengkap dengan Trading Plan ke database SQLite dan file JSON.
"""

import hashlib
import math
import re
from email.utils import parsedate_to_datetime
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

try:
    from backend.feature_engine import BASE_WEIGHTS, SCORING_VERSION, compose_quant_score, load_calibrated_config
    from backend.research_utils import database, signal_id, utc_time
except ImportError:
    from feature_engine import BASE_WEIGHTS, SCORING_VERSION, compose_quant_score, load_calibrated_config
    from research_utils import database, signal_id, utc_time

ASSET_NAMES = {"BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana",
               "BNB": "BNB", "XRP": "XRP", "ADA": "Cardano", "DOGE": "Dogecoin",
               "AVAX": "Avalanche", "LINK": "Chainlink", "DOT": "Polkadot"}


class NewsItems(list):
    def __init__(self, items=(), status="AVAILABLE"):
        super().__init__(items)
        self.status = status


def unavailable_sentiment(status, message):
    return {"sentiment": "UNKNOWN", "sentiment_score": None,
            "catalyst": message, "impact_level": "UNKNOWN", "crypto_risk": None,
            "news_status": status, "model": None, "prompt_hash": None}


def validate_sentiment_response(parsed):
    if not isinstance(parsed, dict):
        raise ValueError("Expected a JSON object.")
    sentiment = parsed.get("sentiment")
    impact = parsed.get("impact_level")
    score = parsed.get("sentiment_score")
    risk = parsed.get("crypto_risk")
    catalyst = parsed.get("catalyst")
    if (sentiment not in {"BULLISH", "NEUTRAL", "BEARISH"}
            or impact not in {"TINGGI", "SEDANG", "RENDAH"}
            or type(risk) is not bool or type(score) not in (float, int)
            or not math.isfinite(score) or not -1 <= score <= 1
            or not isinstance(catalyst, str) or not catalyst.strip()):
        raise ValueError("Invalid sentiment schema.")
    if risk and (sentiment != "BEARISH" or not -1 <= score <= -0.8 or impact != "TINGGI"):
        raise ValueError("Risk Guard response is inconsistent.")
    return dict(sentiment=sentiment, sentiment_score=float(score), crypto_risk=risk,
                impact_level=impact, catalyst=catalyst)


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
    with database(db_path) as conn:
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
            ("entry_status", "TEXT"),
            ("signal_id", "TEXT"), ("news_analyzed_at", "TEXT"), ("news_status", "TEXT"),
            ("evidence_json", "TEXT"), ("model", "TEXT"), ("prompt_hash", "TEXT"),
            ("execution_eligible", "INTEGER"), ("scoring_version", "TEXT")
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
    base_coin = symbol.removesuffix("USDT").strip()
    asset_name = ASSET_NAMES.get(base_coin, base_coin)
    fetch_failed = False
    now = datetime.datetime.now(datetime.timezone.utc)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    def _query_rss(query_str: str, limit: int = 3) -> List[Dict[str, str]]:
        nonlocal fetch_failed
        encoded = urllib.parse.quote(query_str)
        rss_url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
        try:
            resp = requests.get(rss_url, headers=headers, timeout=10)
            if resp.status_code != 200:
                fetch_failed = True
                return []
            root = ET.fromstring(resp.content)
            items = []
            for it in root.findall(".//item")[:limit]:
                title = it.find("title").text if it.find("title") is not None else ""
                pub_date = it.find("pubDate").text if it.find("pubDate") is not None else ""
                source = it.find("source").text if it.find("source") is not None else "Media"
                link = it.find("link").text if it.find("link") is not None else ""
                if title:
                    try:
                        published = parsedate_to_datetime(pub_date).astimezone(datetime.timezone.utc)
                    except (TypeError, ValueError):
                        continue
                    if not 0 <= (now - published).total_seconds() <= 72 * 3600:
                        continue
                    if not (re.search(r"(?<![A-Za-z0-9])" + re.escape(base_coin) + r"(?![A-Za-z0-9])", title, re.I)
                            or asset_name.lower() in title.lower()):
                        continue
                    items.append({
                        "title": title,
                        "source": source,
                        "pub_date": pub_date,
                        "link": link
                    })
            return items
        except (requests.RequestException, ET.ParseError):
            fetch_failed = True
            return []

    # 1. Kueri Risiko Keamanan & Regulasi (Crypto Risk Guard)
    risk_query = f'"{asset_name}" crypto (hack OR exploit OR "sec" OR lawsuit OR delist OR scam OR stolen OR insolvency)'
    risk_items = _query_rss(risk_query, limit=2)

    # 2. Kueri Berita Umum Proyek & Pergerakan Pasar
    gen_query = f'"{asset_name}" crypto (price OR partnership OR upgrade OR launch OR bull OR whale OR ETF)'
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

    return NewsItems(combined_items, "FETCH_FAILED" if fetch_failed else ("AVAILABLE" if combined_items else "NO_NEWS"))


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

    if getattr(news_items, "status", None) == "FETCH_FAILED":
        return unavailable_sentiment("FETCH_FAILED", "Pengambilan berita tidak lengkap; Risk Guard belum terverifikasi.")
    if not news_items:
        return unavailable_sentiment("NO_NEWS", "Tidak ada headline relevan dalam 72 jam; Risk Guard belum terverifikasi.")

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

    # Use the configured model and retain a safe diagnostic on failure.
    models_to_try = [os.environ.get("GEMINI_MODEL", "gemini-flash-latest")]
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    failure_reason = "UNKNOWN_ERROR"

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

                parsed = validate_sentiment_response(json.loads(text_resp))
                return dict(parsed, news_status="AVAILABLE", model=model_name, prompt_hash=prompt_hash)
            elif resp.status_code in (429, 503):
                failure_reason = f"HTTP_{resp.status_code}"
                time.sleep(1.5)
                continue
            else:
                failure_reason = f"HTTP_{resp.status_code}"
        except Exception as e:
            # Exception messages may contain request URLs with the API key.
            prefix = "REQUEST_FAILED" if isinstance(e, requests.RequestException) else "INVALID_RESPONSE"
            failure_reason = f"{prefix}:{type(e).__name__}"
            time.sleep(1)
            continue

    print(f"[!] Gemini gagal untuk {symbol}: {failure_reason}")
    result = unavailable_sentiment("MODEL_FAILED", f"Gemini gagal ({failure_reason}); Risk Guard belum terverifikasi.")
    result["model"] = model_name
    result["prompt_hash"] = prompt_hash
    return result


def calculate_news_pillar_score(sentiment_score: float) -> int:
    """
    Menghitung Skor Pilar ke-5 (AI News Sentiment - Maksimal 10 Poin):
    Sesuai arsitektur 5 pilar KripikTo v2:
    Structure: 25 | Momentum: 30 | Flow: 20 | Derivatives: 15 | AI News: 10 = Total 100
    - sentiment_score rentang -1.0 s.d +1.0:
      * >= +0.6 (Strong Bullish) -> 10 poin
      * +0.2 s.d +0.5 (Bullish)  -> 8 poin
      * -0.1 s.d +0.1 (Neutral)  -> 5 poin
      * -0.5 s.d -0.2 (Bearish)  -> 2 poin
      * < -0.5 (Strong Bearish)  -> 0 poin
    """
    if sentiment_score is None:
        return 0
    if not math.isfinite(sentiment_score) or not -1 <= sentiment_score <= 1:
        raise ValueError("Sentiment score must be finite in [-1, 1].")
    if sentiment_score >= 0.6:
        return 10
    elif sentiment_score >= 0.2:
        return 8
    elif sentiment_score >= -0.1:
        return 5
    elif sentiment_score >= -0.5:
        return 2
    else:
        return 0


def calculate_unified_v2_final_score(
    v2_score: int,
    sentiment_score: float,
    crypto_risk: bool = False,
    news_weight: float = 10,
) -> float:
    """
    Menyatukan skor kuantitatif v2 (maksimal 90 dari Pilar 1-4 + Makro baseline)
    dengan Pilar ke-5 AI News (0 - 10 poin) ke dalam basis 100 poin tunggal yang konsisten.
    Crypto Risk Guard: Hard safety override membatasi maksimal 35 jika ada risiko fatal.
    """
    if not math.isfinite(news_weight) or not 0 < news_weight < 100:
        raise ValueError("Invalid news weight.")
    news_pts = calculate_news_pillar_score(sentiment_score) / 10 * news_weight
    final_score = min(100, v2_score + news_pts)
    if crypto_risk:
        final_score = min(final_score, 35)
    return max(0, min(100, final_score))


def get_recommendation_label(
    final_score: int,
    sentiment: str,
    v2_score: int,
    crypto_risk: bool = False,
    entry_status: str = "WAIT",
    setup_type: str = "NO_SETUP"
) -> str:
    """
    Menentukan label rekomendasi berdasarkan gerbang taksonomi status entri (Fase 5):
    PRINSIP UTAMA:
    - WAIT != BUY
    - READY != BUY
    - EXTENDED != BUY
    Hanya status 'TRIGGERED' yang boleh menghasilkan sinyal beli (BUY)!
    Skor tinggi tanpa pemicu taktis 15M hanya masuk status READY/WATCHLIST.
    """
    # 1. Hard Safety Override (Crypto Risk Guard)
    if crypto_risk:
        return "⚠️ AVOID (Hack / Exploit / Delisting Risk)"

    # 2. Bad News Divergence
    if v2_score >= 70 and sentiment == "BEARISH":
        return "⚠️ CAUTION (Bad News Divergence)"

    # 3. Gerbang Eksekusi 15M (Triggered)
    if entry_status == "TRIGGERED":
        if setup_type == "MOMENTUM_RUNNER":
            return "🚀 STRONG_BUY (15M Breakout Triggered)"
        else:
            return "🎯 TRIGGERED_BUY (15M Trigger Valid)"

    # 4. Peringatan Pucuk (Extended)
    if entry_status == "EXTENDED":
        return "⚠️ OVEREXTENDED (Tunggu Pullback / Jangan Beli Pucuk)"

    # 5. Setup Batal (Failed)
    if entry_status == "FAILED":
        return "❌ SETUP_FAILED (Breakdown di Bawah Support)"

    # 6. Momentum Runner Siap Pemicu (Ready)
    if entry_status == "READY":
        return "👀 READY_WATCHLIST (Menunggu 15M Trigger - Jangan Beli Dulu)"

    # 7. Status Konsolidasi / Akumulasi (Wait)
    if setup_type == "ACCUMULATION_COIL":
        return "⏳ ACCUMULATION (Paus Diam - Masuk Watchlist)"
    elif setup_type == "VOLATILITY_SQUEEZE":
        return "⏳ SQUEEZE_WAIT (Tunggu Ekspansi Volatilitas)"
    elif final_score >= 60:
        return "👀 WATCHLIST (Pantau Support)"
    else:
        return "⏳ NEUTRAL / WAIT"


def run_news_sentiment_pipeline(
    top_limit: int = 15,
    fgi: Optional[Dict[str, Any]] = None,
    db_path: str = DB_PATH,
    candidates: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """
    Menjalankan alur lengkap analisis sentimen berita LLM untuk koin hasil scan teratas.
    """
    api_key = load_gemini_api_key()
    if not api_key:
        Path(FINAL_JSON_PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(FINAL_JSON_PATH).write_text("[]", encoding="utf-8")
        print("[!] GEMINI_API_KEY tidak ditemukan di environment atau .env.")
        print("[*] Masukkan GEMINI_API_KEY di file .env untuk mengaktifkan analisis berita AI.")
        return []

    init_news_db(db_path)

    fgi_context = ""
    if fgi:
        fgi_context = f"{fgi.get('classification', 'Neutral')} ({fgi.get('value', 50)}/100) - {fgi.get('advice', '')}"

    # Baca kandidat koin dari file scan_latest.json atau SQLite
    scan_candidates = [] if candidates is None else candidates
    if candidates is not None:
        pass
    elif Path(SCAN_JSON_PATH).exists() and db_path == DB_PATH:
        with open(SCAN_JSON_PATH, "r", encoding="utf-8") as f:
            scan_candidates = json.load(f)
    else:
        with database(db_path) as conn:
            df = pd.read_sql("SELECT * FROM scan_results WHERE scan_time=(SELECT MAX(scan_time) FROM scan_results) ORDER BY CASE entry_status WHEN 'TRIGGERED' THEN 4 WHEN 'READY' THEN 3 WHEN 'WAIT' THEN 2 WHEN 'EXTENDED' THEN 1 ELSE 0 END DESC, score DESC, taker_buy_ratio DESC LIMIT ?", conn, params=(top_limit,))
            scan_candidates = df.to_dict(orient="records")

    if not scan_candidates:
        Path(FINAL_JSON_PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(FINAL_JSON_PATH).write_text("[]", encoding="utf-8")
        print("[!] Tidak ada kandidat koin dari pemindaian Tahap 2.")
        return []

    picks_to_analyze = scan_candidates[:top_limit]
    print(f"[*] Menganalisis berita global & sentimen Gemini Flash untuk {len(picks_to_analyze)} koin terpilih...")

    final_results = []
    for i, item in enumerate(picks_to_analyze):
        if item.get("scoring_version") != SCORING_VERSION:
            continue
        sym = item["symbol"]
        tech_score = int(item["score"])
        fr = float(item.get("funding_rate", 0.0))
        oi_m = float(item.get("open_interest_m", 0.0))
        print(f"    [{i+1}/{len(picks_to_analyze)}] Memeriksa berita untuk {sym} (Skor Teknikal: {tech_score}, FR: {fr:+.3f}%)...")

        # Tambahkan konteks Short Squeeze ke prompt jika Funding Rate negatif
        coin_fgi_context = fgi_context
        if fr <= -0.015:
            squeeze_note = f"\nKondisi Derivatif Koin: Funding Rate {fr:+.4f}% (Bias pendanaan negatif; funding saja tidak membuktikan short squeeze)."
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

        weights = json.loads(item.get("weights_json") or json.dumps(BASE_WEIGHTS))
        if item.get("scoring_version") != SCORING_VERSION:
            continue  # Legacy scores cannot safely be mixed with normalized scores.
        v2_score = compose_quant_score({"structure": item["structure_score"],
            "momentum": item["momentum_score"], "flow": item["flow_score"],
            "derivatives": item["derivative_score"]}, weights, item.get("score_penalty", 0))
        setup_type = str(item.get("setup_type", "NO_SETUP"))
        entry_status = str(item.get("entry_status", "WAIT"))

        final_score = calculate_unified_v2_final_score(v2_score, sent_score, crypto_risk, weights["news"])
        recom = get_recommendation_label(
            final_score, sentiment, v2_score, crypto_risk,
            entry_status=entry_status, setup_type=setup_type
        )

        news_status = sentiment_res.get("news_status", "MODEL_FAILED")
        analyzed_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        signal_age = (utc_time(analyzed_at) - utc_time(item["scan_time"])).total_seconds()
        if news_status != "AVAILABLE":
            recom = "NEWS_UNVERIFIED (Risk Guard belum tersedia)"
        elif not crypto_risk and not 0 <= signal_age <= 900:
            recom = "SIGNAL_EXPIRED (Perlu scan baru)"
        record = {
            "scan_time": item["scan_time"],
            "signal_id": signal_id(item["scan_time"], sym),
            "news_analyzed_at": analyzed_at, "news_status": news_status,
            "model": sentiment_res.get("model"), "prompt_hash": sentiment_res.get("prompt_hash"),
            "evidence_json": json.dumps(news_items, ensure_ascii=False),
            "scoring_version": SCORING_VERSION,
            "execution_eligible": int("BUY" in recom and item.get("data_quality_status") == "OK"),
            "entry_price": item.get("entry_price"),
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
            "crypto_risk": None if crypto_risk is None else int(crypto_risk),
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
    with database(db_path) as conn:
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
        cursor.executemany("""UPDATE kripto_sentiment_analysis SET signal_id=?, news_analyzed_at=?,
            news_status=?, evidence_json=?, model=?, prompt_hash=?, execution_eligible=?, scoring_version=?
            WHERE scan_time=? AND symbol=?""",
            [(r["signal_id"], r["news_analyzed_at"], r["news_status"], r["evidence_json"], r["model"],
              r["prompt_hash"], r["execution_eligible"], r["scoring_version"], r["scan_time"], r["symbol"]) for r in final_results])
        conn.commit()

    # Simpan ke JSON final
    Path(FINAL_JSON_PATH).parent.mkdir(parents=True, exist_ok=True)
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
        sentiment_score = r.get("sentiment_score")
        score_text = f"{sentiment_score:+.1f}" if sentiment_score is not None else "N/A"
        sent_badge = f"{r['sentiment']} ({score_text})"
        funding_rate = r.get("funding_rate")
        fr_str = f"{funding_rate:+.3f}%" if funding_rate is not None else "N/A"
        setup_str = r.get("setup_type", "NO_SETUP")
        stat_str = r.get("entry_status", "WAIT")
        if stat_str == "TRIGGERED":
            stat_str = "🎯 TRIG"
        elif stat_str == "FAILED":
            stat_str = "❌ FAIL"
        elif stat_str == "READY":
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
        print(f"   TP 1 (Scalp): ${r['tp1']} (+{r['tp1_pct']}%)")
        print(f"   TP 2 (Ext) : ${r['tp2']} (+{r['tp2_pct']}%)")
        print(f"   Risk/Reward: {r['risk_reward']}")
        print(f"   Katalis AI : {r['catalyst']}\n")
    print("=" * 145)
    print(f"📁 Rekap JSON final tersimpan di: {FINAL_JSON_PATH}\n")



if __name__ == "__main__":
    print("=== TEST RUN NEWS SENTIMENT PIPELINE (TAHAP 3) ===")
    results = run_news_sentiment_pipeline(top_limit=5)
    print_final_executive_report(results)
