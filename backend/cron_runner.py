"""
backend/cron_runner.py
Skrip eksekusi berkala (otomasi 15 menit):
1. Mengunduh data pasar terbaru & klines MTF.
2. Memindai indikator kuantitatif & pemicu taktis 15M.
3. Mengirimkan notifikasi jika ditemukan kandidat READY atau TRIGGERED.
4. Menjalankan evaluasi berkala untuk sinyal sebelumnya via outcome_tracker.
"""

import sys
import os
from pathlib import Path
from datetime import datetime, timezone

# Pastikan working directory & python path di root repo
REPO_ROOT = Path(__file__).resolve().parent.parent
os.chdir(str(REPO_ROOT))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.data_pipeline import run_pipeline, DB_PATH
from backend.scanner import run_scanner
from backend.outcome_tracker import run_outcome_tracker
from backend.notifier import broadcast_signal_alert, format_signal_message


def run_cron_cycle(top_liquid: int = 200, top_picks: int = 15, quiet: bool = True):
    log_message = lambda text: None
    # Pastikan log dicatat ke file backend/data/cron.log
    log_path = REPO_ROOT / "backend" / "data" / "cron.log"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}] Memulai siklus scan 15M...\n")

    import io, contextlib
    buffer = io.StringIO()
    scan_picks = None
    try:
        if quiet:
            with contextlib.redirect_stdout(buffer):
                run_pipeline(interval="4h", mtf=True, top_n=top_liquid, limit=100)
                scan_picks = run_scanner(interval="4h", min_score=40, top_n=top_picks, trigger_15m=True)
                run_outcome_tracker(db_path=DB_PATH)
        else:
            run_pipeline(interval="4h", mtf=True, top_n=top_liquid, limit=100)
            scan_picks = run_scanner(interval="4h", min_score=40, top_n=top_picks, trigger_15m=True)
            run_outcome_tracker(db_path=DB_PATH)
    except Exception as e:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}] ERROR: {e}\n{buffer.getvalue()}\n")
        print(f"[!] KripikTo Cron Error: {e}")
        return

    n_coins = len(scan_picks) if scan_picks is not None else 0
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}] Siklus selesai. {n_coins} koin diperiksa.\n")

    if scan_picks is None or scan_picks.empty:
        return

    # Filter kandidat READY atau TRIGGERED
    actionable = scan_picks[scan_picks["entry_status"].isin(["TRIGGERED", "READY"])]
    if not actionable.empty:
        alert_texts = []
        for _, row in actionable.iterrows():
            msg = format_signal_message(row.to_dict())
            alert_texts.append(msg)
            broadcast_signal_alert(msg)

        full_alert = "\n\n" + ("=" * 40) + "\n" + "\n\n".join(alert_texts) + "\n" + ("=" * 40)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}] ALERT SENT:\n{full_alert}\n")
        # Cetak ke stdout agar watchdog mengirimkan notifikasi ke Hermes
        print(full_alert)


if __name__ == "__main__":
    quiet_mode = "--verbose" not in sys.argv
    run_cron_cycle(quiet=quiet_mode)
