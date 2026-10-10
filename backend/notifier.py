"""
backend/notifier.py
Modul notifikasi ringan untuk mengirim sinyal READY dan TRIGGERED.
Mendukung: CallMeBot (WA Personal Gratis), Fonnte (WA Gateway), dan Telegram.
"""

import json
import logging
from pathlib import Path
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

CONFIG_PATH = Path(__file__).resolve().parent / "data" / "notifier_config.json"
logger = logging.getLogger("notifier")


def load_notifier_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "enabled": False,
        "provider": "none",
        "callmebot": {"phone": "", "apikey": ""},
        "fonnte": {"token": "", "target": ""},
        "telegram": {"bot_token": "", "chat_id": ""}
    }


def send_whatsapp_callmebot(phone: str, apikey: str, text: str) -> bool:
    """Kirim WA via CallMeBot (gratis untuk pesan personal)."""
    if not phone or not apikey:
        return False
    url = f"https://api.callmebot.com/whatsapp.php?phone={phone}&text={quote_plus(text)}&apikey={apikey}"
    try:
        req = Request(url, headers={"User-Agent": "KripikTo/1.0"})
        with urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        logger.error(f"[Notifier] CallMeBot error: {e}")
        return False


def send_whatsapp_fonnte(token: str, target: str, text: str) -> bool:
    """Kirim WA via Fonnte Gateway (fonnte.com)."""
    if not token or not target:
        return False
    url = "https://api.fonnte.com/send"
    data = json.dumps({"target": target, "message": text}).encode("utf-8")
    try:
        req = Request(url, data=data, headers={
            "Authorization": token,
            "Content-Type": "application/json",
            "User-Agent": "KripikTo/1.0"
        })
        with urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        logger.error(f"[Notifier] Fonnte error: {e}")
        return False


def send_telegram(bot_token: str, chat_id: str, text: str) -> bool:
    """Kirim notifikasi via Telegram Bot."""
    if not bot_token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode("utf-8")
    try:
        req = Request(url, data=data, headers={
            "Content-Type": "application/json",
            "User-Agent": "KripikTo/1.0"
        })
        with urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        logger.error(f"[Notifier] Telegram error: {e}")
        return False


def broadcast_signal_alert(message: str) -> bool:
    """Dispatches alert to configured provider."""
    cfg = load_notifier_config()
    if not cfg.get("enabled"):
        return False

    provider = cfg.get("provider", "none").lower()
    if provider == "callmebot":
        c = cfg.get("callmebot", {})
        return send_whatsapp_callmebot(c.get("phone", ""), c.get("apikey", ""), message)
    elif provider == "fonnte":
        f = cfg.get("fonnte", {})
        return send_whatsapp_fonnte(f.get("token", ""), f.get("target", ""), message)
    elif provider == "telegram":
        t = cfg.get("telegram", {})
        return send_telegram(t.get("bot_token", ""), t.get("chat_id", ""), message)
    return False


def format_signal_message(row: dict) -> str:
    """Format row kandidat scanner menjadi pesan ringkas untuk WA/Telegram."""
    status = row.get("entry_status", "-")
    icon = "🚀" if status == "TRIGGERED" else "👀"
    return (
        f"{icon} *[KRIPIKTO SIGNAL: {status}]*\n"
        f"Pair: *{row.get('symbol')}* (Skor: {row.get('v2_score', 0):.0f})\n"
        f"Setup: {row.get('setup_type', '-')}\n"
        f"Harga: ${row.get('last_price', 0)}\n"
        f"Buy Area: {row.get('buy_area', '-')}\n"
        f"Stop Loss: ${row.get('stop_loss', 0)} ({row.get('stop_loss_pct', 0)}%)\n"
        f"TP1: ${row.get('tp1', 0)} ({row.get('tp1_pct', 0)}%)\n"
        f"Sinyal: {row.get('signals', '-')[:120]}..."
    )
