"""
backend/outcome_tracker.py
Modul Pelacakan Hasil & Evaluasi Empiris (Outcome Tracker):
Prinsip: "Measurement before optimization"

Fungsi Utama:
1. Membaca riwayat sinyal dari SQLite (signal_outcomes & scan_results) dan final_recommendations.json.
2. Mengunduh lilin historis Binance sejak scan_time untuk merekonstruksi jalannya harga secara presisi.
3. Mengukur metrik empiris kuantitatif:
   - is_filled: Apakah harga pasar pernah masuk/menyentuh rentang buy_area (Limit Order filled)?
   - MFE (Maximum Favorable Excursion): Puncak keuntungan (% profit tertinggi yang sempat disentuh).
   - MAE (Maximum Adverse Excursion): Penurunan terdalam (% drawdown yang dialami posisi).
   - Result: TP1_HIT, TP2_HIT, SL_HIT, TIMEOUT, UNFILLED, atau PENDING (masih berjalan).
   - Return 12h, 24h, 48h untuk mengukur apakah koin meledak atau sideways.
   - Durasi waktu (jam & jumlah lilin) hingga exit.
4. Menghasilkan Scoreboard Kuantitatif:
   - Real Win Rate %, Loss Rate %, Timeout Rate %, Fill Rate %.
   - Profit Factor & Rasio R:R Terealisasi vs R:R Teoretis.
   - Evaluasi A/B: Korelasi Skor v1 dan Relative Strength (RS vs BTC) terhadap keberhasilan trade.
"""

import sys
import re
import json
import sqlite3
import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
import requests
import pandas as pd
import numpy as np
from contextlib import closing

try:
    from backend.replay import Costs, REPLAY_VERSION, replay_trade, timestamp_ms, summarize
    from backend.replay_store import save_candles
except ImportError:
    from replay import Costs, REPLAY_VERSION, replay_trade, timestamp_ms, summarize
    from replay_store import save_candles

# Pastikan output utf-8 aman di terminal Windows
if sys.platform == "win32" and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = str(DATA_DIR / "kripto.db")
RECOMMENDATIONS_JSON = str(DATA_DIR / "final_recommendations.json")
SCAN_JSON_PATH = str(DATA_DIR / "scan_latest.json")
BINANCE_API_BASE = "https://data-api.binance.vision"


def init_outcome_tracker_db(db_path: str = DB_PATH) -> None:
    """Inisialisasi dan migrasi tabel signal_outcomes di SQLite."""
    with closing(sqlite3.connect(db_path)) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS signal_outcomes (
                scan_time TEXT,
                symbol TEXT,
                interval TEXT,
                buy_low REAL,
                buy_high REAL,
                entry_price REAL,
                stop_loss REAL,
                tp1 REAL,
                tp2 REAL,
                atr14 REAL,
                v1_score INTEGER,
                v2_score INTEGER,
                setup_type TEXT,
                rs_4h REAL,
                rs_1h REAL,
                btc_return_4h REAL,
                btc_return_1h REAL,
                is_filled INTEGER DEFAULT 0,
                fill_time TEXT,
                fill_price REAL,
                result TEXT DEFAULT 'PENDING',
                result_time TEXT,
                duration_hours REAL,
                duration_candles INTEGER,
                mfe_pct REAL,
                mae_pct REAL,
                return_12h_pct REAL,
                return_24h_pct REAL,
                return_48h_pct REAL,
                evaluated_at TEXT,
                PRIMARY KEY (scan_time, symbol)
            )
        """)

        # Migrasi kolom jika belum ada
        cursor.execute("PRAGMA table_info(signal_outcomes)")
        existing_cols = {col[1] for col in cursor.fetchall()}
        required_cols = [
            ("exit_price", "REAL"), ("net_return_pct", "REAL"), ("gross_return_pct", "REAL"),
            ("replay_version", "TEXT"), ("replay_interval", "TEXT"),
            ("fee_bps", "REAL"), ("slippage_bps", "REAL"), ("replay_note", "TEXT"),
            ("fill_timeout_hours", "REAL"), ("trade_timeout_hours", "REAL"),
            ("execution_eligible", "INTEGER"), ("data_quality_status", "TEXT"),
            ("buy_low", "REAL"),
            ("buy_high", "REAL"),
            ("v2_score", "INTEGER"),
            ("setup_type", "TEXT"),
            ("entry_status", "TEXT"),
            ("structure_score", "INTEGER"),
            ("momentum_score", "INTEGER"),
            ("flow_score", "INTEGER"),
            ("derivative_score", "INTEGER"),
            ("rs_4h", "REAL"),
            ("rs_1h", "REAL"),
            ("btc_return_4h", "REAL"),
            ("btc_return_1h", "REAL")
        ]
        for col_name, col_type in required_cols:
            if col_name not in existing_cols:
                try:
                    cursor.execute(f"ALTER TABLE signal_outcomes ADD COLUMN {col_name} {col_type}")
                except Exception:
                    pass

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_outcomes_res 
            ON signal_outcomes (result, scan_time DESC)
        """)
        conn.commit()


def parse_buy_area_str(buy_area: str) -> Tuple[float, float]:
    """Ekstraksi angka rentang buy area dari format string '$0.026586 - $0.026699'."""
    nums = re.findall(r"[\d\.]+(?:e-?\d+)?", buy_area.replace("$", "").strip())
    if len(nums) >= 2:
        try:
            return float(nums[0]), float(nums[1])
        except ValueError:
            pass
    elif len(nums) == 1:
        try:
            val = float(nums[0])
            return val * 0.995, val * 1.005
        except ValueError:
            pass
    return 0.0, 0.0


def is_trade_eligible(signal):
    """Explicit final blocks win; technical-only entry requires a valid triggered snapshot."""
    return (signal.get("entry_status") == "TRIGGERED"
            and signal.get("data_quality_status") == "OK"
            and signal.get("execution_eligible") != 0)


def import_signals_from_history(db_path: str = DB_PATH) -> int:
    init_outcome_tracker_db(db_path)
    count = 0
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        columns = {r[1] for r in conn.execute("PRAGMA table_info(signal_outcomes)")}
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='scan_results'").fetchone():
            for row in conn.execute("SELECT * FROM scan_results").fetchall():
                record = dict(row)
                # Snapshot metadata only; never overwrite recorded outcomes.
                fields = [k for k in record if k in columns and k not in
                          ("result", "is_filled", "fill_price", "fill_time", "evaluated_at")]
                sql = "INSERT OR IGNORE INTO signal_outcomes (" + ",".join(fields) + ") VALUES (" + ",".join("?" for _ in fields) + ")"
                count += conn.execute(sql, [record[k] for k in fields]).rowcount
                metadata = [k for k in ("entry_status", "data_quality_status", "execution_eligible") if k in record]
                if metadata:
                    conn.execute("UPDATE signal_outcomes SET " + ",".join(k + "=?" for k in metadata)
                                 + " WHERE scan_time=? AND symbol=?",
                                 [record[k] for k in metadata] + [record["scan_time"], record["symbol"]])
        # Do not mix the production JSON into experiments using a different database.
        if str(Path(db_path).resolve()) == str(Path(DB_PATH).resolve()) and Path(RECOMMENDATIONS_JSON).exists():
            recs = json.loads(Path(RECOMMENDATIONS_JSON).read_text(encoding="utf-8"))
            for record in recs:
                if "execution_eligible" in record:
                    conn.execute("UPDATE signal_outcomes SET execution_eligible=? WHERE scan_time=? AND symbol=?",
                                 (record["execution_eligible"], record.get("scan_time"), record.get("symbol")))
        conn.commit()
    return count


def fetch_subsequent_klines(
    symbol: str,
    start_time_ms: int,
    interval: str = "1h",
    limit: int = 500
) -> List[Dict[str, Any]]:
    """
    Mengunduh candlestick Binance publik sejak waktu start_time_ms.
    Mengembalikan data lilin dalam format list of dict.
    """
    url = f"{BINANCE_API_BASE}/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "startTime": start_time_ms, "limit": limit}
    headers = {"User-Agent": "KripikTo-OutcomeTracker/1.0"}

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=12)
        if resp.status_code == 200:
            raw = resp.json()
            rows = []
            now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
            for k in raw:
                op_t = int(k[0])
                cl_t = int(k[6])
                # Only closed candles are valid for historical outcome evaluation.
                if cl_t > now_ms:
                    continue
                dt_str = datetime.datetime.fromtimestamp(
                    op_t / 1000, tz=datetime.timezone.utc
                ).strftime("%Y-%m-%d %H:%M:%S")
                rows.append({
                    "open_time": op_t,
                    "close_time": cl_t,
                    "datetime_utc": dt_str,
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5])
                })
            return rows
    except Exception:
        pass
    return []


TIME_STOP_HOURS = 6.0  # Sesuai spesifikasi Fase 3.6 (Momentum Scalping Time Stop)


def evaluate_single_signal(
    signal: Dict[str, Any],
    sim_interval: str = "15m",
    fill_timeout_hours: float = 2.0,
    trade_timeout_hours: float = TIME_STOP_HOURS,
    costs=None,
    db_path=None,
) -> Dict[str, Any]:
    """Replay closed candles; retain the complete forward path for later experiments."""
    start_ms = timestamp_ms(signal["scan_time"])
    candles = fetch_subsequent_klines(signal["symbol"], start_ms, interval=sim_interval, limit=500)
    if not candles and signal.get("replay_version") == REPLAY_VERSION:
        return dict(signal, replay_note="No closed candles fetched; previous replay preserved")
    if db_path and candles:
        save_candles(db_path, signal["symbol"], sim_interval, candles)
    return replay_trade(signal, candles, interval=sim_interval,
                        fill_timeout_hours=fill_timeout_hours,
                        trade_timeout_hours=trade_timeout_hours, costs=costs)


def run_outcome_tracker(
    timeout_hours: float = TIME_STOP_HOURS,
    force_recheck: bool = False,
    db_path: str = DB_PATH,
    fee_bps: float = 10.0,
    slippage_bps: float = 5.0,
    fill_timeout_hours: float = 2.0,
) -> pd.DataFrame:
    """
    Menjalankan evaluasi penuh seluruh sinyal historis yang masih PENDING.
    Memperbarui database SQLite dan mencetak Scoreboard Performa Empiris.
    """
    init_outcome_tracker_db(db_path)
    import_signals_from_history(db_path)

    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        if force_recheck:
            cursor.execute("SELECT * FROM signal_outcomes ORDER BY scan_time ASC")
        else:
            cursor.execute("SELECT * FROM signal_outcomes WHERE result IN ('PENDING', 'DATA_UNAVAILABLE') OR replay_version=? ORDER BY scan_time ASC", (REPLAY_VERSION,))
        rows = cursor.fetchall()
        signals = [dict(r) for r in rows]

    if not signals:
        print("[*] Tidak ada sinyal yang perlu dievaluasi saat ini.")
        # Baca seluruh outcomes yang sudah selesai untuk dicetak scoreboarnya
        with closing(sqlite3.connect(db_path)) as conn:
            df_all = pd.read_sql("SELECT * FROM signal_outcomes ORDER BY scan_time DESC", conn)
        print_outcome_scoreboard(df_all)
        return df_all

    print(f"\n[*] Menjalankan evaluasi empiris pada {len(signals)} sinyal dari Binance API...")
    updated_records = []

    costs = Costs(fee_bps, slippage_bps)
    for s in signals:
        if not is_trade_eligible(s):
            continue
        # A trade can finish before the experiment/forward-mark window. Keep
        # collecting that path on later runs rather than losing early winners.
        if not force_recheck and s.get("replay_version") == REPLAY_VERSION and s["result"] not in ("PENDING", "DATA_UNAVAILABLE"):
            with closing(sqlite3.connect(db_path)) as conn:
                cached = conn.execute("SELECT 1 FROM sqlite_master WHERE name='replay_candles'").fetchone()
                last = conn.execute("SELECT MAX(open_time) FROM replay_candles WHERE symbol=? AND interval='15m'", (s["symbol"],)).fetchone()[0] if cached else None
            same_costs = s.get("fee_bps") == fee_bps and s.get("slippage_bps") == slippage_bps
            same_window = s.get("fill_timeout_hours") == fill_timeout_hours and s.get("trade_timeout_hours") == timeout_hours
            if last and last >= timestamp_ms(s["scan_time"]) + 50.25 * 3_600_000 and same_costs and same_window:
                continue
        evaluated = evaluate_single_signal(s, sim_interval="15m", trade_timeout_hours=timeout_hours,
            fill_timeout_hours=fill_timeout_hours, costs=costs, db_path=db_path)
        updated_records.append(evaluated)

    # Simpan kembali hasil evaluasi ke database
    with closing(sqlite3.connect(db_path)) as conn:
        cursor = conn.cursor()
        update_query = """
            UPDATE signal_outcomes
            SET is_filled = ?, fill_time = ?, fill_price = ?, result = ?, result_time = ?,
                duration_hours = ?, duration_candles = ?, mfe_pct = ?, mae_pct = ?,
                return_12h_pct = ?, return_24h_pct = ?, return_48h_pct = ?, evaluated_at = ?
            WHERE scan_time = ? AND symbol = ?
        """
        for r in updated_records:
            cursor.execute(update_query, (
                r["is_filled"], r["fill_time"], r["fill_price"], r["result"], r["result_time"],
                r["duration_hours"], r["duration_candles"], r["mfe_pct"], r["mae_pct"],
                r["return_12h_pct"], r["return_24h_pct"], r["return_48h_pct"], r["evaluated_at"],
                r["scan_time"], r["symbol"]
            ))
            extras = ["exit_price", "net_return_pct", "gross_return_pct", "replay_version",
                      "replay_interval", "fee_bps", "slippage_bps", "replay_note",
                      "fill_timeout_hours", "trade_timeout_hours"]
            cursor.execute("UPDATE signal_outcomes SET " + ", ".join(f"{k}=?" for k in extras)
                + " WHERE scan_time=? AND symbol=?", [r.get(k) for k in extras] + [r["scan_time"], r["symbol"]])
        conn.commit()

    with closing(sqlite3.connect(db_path)) as conn:
        df_all = pd.read_sql("SELECT * FROM signal_outcomes ORDER BY scan_time DESC", conn)

    print_outcome_scoreboard(df_all)
    return df_all


def print_outcome_scoreboard(df: pd.DataFrame) -> None:
    """Report only current eligible replay cohorts with identical cost assumptions."""
    if df.empty:
        print("[*] Belum ada rekaman outcome.")
        return
    eligible = df.apply(lambda row: is_trade_eligible(row.to_dict()), axis=1)
    current = df.get("replay_version", pd.Series(None, index=df.index)).eq(REPLAY_VERSION)
    print(f"[*] Observasi/legacy di luar statistik transaksi: {int((~(eligible & current)).sum())}")
    df = df[eligible & current].copy()
    if df.empty:
        print("[*] Belum ada replay versi terbaru untuk sinyal entry yang memenuhi syarat.")
        return
    cohorts = ["replay_interval", "fee_bps", "slippage_bps", "fill_timeout_hours", "trade_timeout_hours"]
    for assumptions, group in df.groupby(cohorts, dropna=False):
        print("\nAsumsi replay:", dict(zip(cohorts, assumptions)))
        records = group.astype(object).where(pd.notna(group), None).to_dict(orient="records")
        print(json.dumps(summarize(records), ensure_ascii=False, indent=2, allow_nan=False))
    print("\nHasil per trade setelah biaya (bukan return portofolio):")
    print(df[["scan_time", "symbol", "result", "fill_price", "exit_price", "net_return_pct"]].head(15).to_string(index=False))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="KripikTo Outcome Tracker: Empirical Performance Engine")
    parser.add_argument("--timeout", type=float, default=TIME_STOP_HOURS, help="Batas jam timeout posisi sideways (default: 6 jam)")
    parser.add_argument("--recheck", action="store_true", help="Evaluasi ulang seluruh riwayat sinyal dari nol")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--fee-bps", type=float, default=10.0, help="Asumsi fee per sisi, basis points")
    parser.add_argument("--slippage-bps", type=float, default=5.0, help="Asumsi slippage per sisi, basis points")
    parser.add_argument("--fill-timeout", type=float, default=2.0, help="Masa berlaku entry limit dalam jam")
    args = parser.parse_args()

    run_outcome_tracker(timeout_hours=args.timeout, force_recheck=args.recheck, db_path=args.db,
                        fee_bps=args.fee_bps, slippage_bps=args.slippage_bps,
                        fill_timeout_hours=args.fill_timeout)
