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
import math
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
import requests
import pandas as pd
import numpy as np

try:
    from backend.research_utils import database, signal_id, net_return, cache_forward_candles, read_forward_candles, complete_forward_window, init_forward_db, DEFAULT_COSTS, utc_time
except ImportError:
    from research_utils import database, signal_id, net_return, cache_forward_candles, read_forward_candles, complete_forward_window, init_forward_db, DEFAULT_COSTS, utc_time

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
    with database(db_path) as conn:
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
            ("btc_return_1h", "REAL"),
            ("signal_id", "TEXT"), ("exit_price", "REAL"), ("exit_time", "TEXT"),
            ("gross_return_pct", "REAL"), ("net_return_pct", "REAL"),
            ("execution_costs_json", "TEXT"), ("forward_complete", "INTEGER DEFAULT 0"),
            ("replay_status", "TEXT")
        ]
        for col_name, col_type in required_cols:
            if col_name not in existing_cols:
                try:
                    cursor.execute(f"ALTER TABLE signal_outcomes ADD COLUMN {col_name} {col_type}")
                except Exception:
                    pass

        init_forward_db(conn)
        for scan_time, symbol in cursor.execute("SELECT scan_time, symbol FROM signal_outcomes").fetchall():
            cursor.execute("UPDATE signal_outcomes SET signal_id=? WHERE scan_time=? AND symbol=?",
                           (signal_id(scan_time, symbol), scan_time, symbol))

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


def import_signals_from_history(db_path: str = DB_PATH) -> int:
    """
    Mengimpor sinyal dari scan_results dan final_recommendations.json ke signal_outcomes
    sehingga data masa lalu (termasuk kasus PARTI) dapat dievaluasi secara otomatis.
    """
    init_outcome_tracker_db(db_path)
    imported_count = 0

    with database(db_path) as conn:
        cursor = conn.cursor()

        # 1. Impor dari scan_results SQLite
        try:
            cursor.execute("""
                SELECT scan_time, symbol, interval, buy_area, buy_low, buy_high, entry_price,
                       stop_loss, tp1, tp2, atr14, v1_score, v2_score, setup_type, entry_status,
                       structure_score, momentum_score, flow_score, derivative_score,
                       rs_4h, rs_1h, btc_return_4h, btc_return_1h
                FROM scan_results
            """)
            rows = cursor.fetchall()
            for r in rows:
                (scan_t, sym, iv, area, b_low, b_high, ep, sl, tp1, tp2, atr,
                 v1_s, v2_s, stype, estatus, str_s, mom_s, flw_s, der_s, rs4, rs1, btc4, btc1) = r
                if not b_low or not b_high or b_low <= 0:
                    b_low, b_high = parse_buy_area_str(area or "")
                if not ep or ep <= 0:
                    ep = (b_low + b_high) / 2.0 if (b_low and b_high) else 0.0

                # Masukkan sinyal baru jika belum ada
                cursor.execute("""
                    INSERT OR IGNORE INTO signal_outcomes (
                        scan_time, symbol, interval, buy_low, buy_high, entry_price,
                        stop_loss, tp1, tp2, atr14, v1_score, v2_score, setup_type, entry_status,
                        structure_score, momentum_score, flow_score, derivative_score,
                        rs_4h, rs_1h, btc_return_4h, btc_return_1h, result
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
                """, (scan_t, sym, iv or "4h", b_low, b_high, ep, sl, tp1, tp2, atr,
                      v1_s, v2_s, stype, estatus, str_s, mom_s, flw_s, der_s, rs4, rs1, btc4, btc1))
                if cursor.rowcount > 0:
                    imported_count += 1
                else:
                    # Perbarui metadata v2 tanpa menimpa hasil evaluasi empiris yang sudah ada
                    cursor.execute("""
                        UPDATE signal_outcomes
                        SET v2_score = COALESCE(?, v2_score),
                            setup_type = COALESCE(?, setup_type),
                            entry_status = COALESCE(?, entry_status),
                            structure_score = COALESCE(?, structure_score),
                            momentum_score = COALESCE(?, momentum_score),
                            flow_score = COALESCE(?, flow_score),
                            derivative_score = COALESCE(?, derivative_score)
                        WHERE scan_time = ? AND symbol = ?
                    """, (v2_s, stype, estatus, str_s, mom_s, flw_s, der_s, scan_t, sym))
            conn.commit()
        except Exception:
            pass

    # The scan table owns order identity. News never creates a second signal.
    # Legacy final JSON uses analysis timestamps and cannot safely be imported.
    with database(db_path) as conn:
        for scan_t, sym in conn.execute("SELECT scan_time, symbol FROM signal_outcomes").fetchall():
            conn.execute("UPDATE signal_outcomes SET signal_id=? WHERE scan_time=? AND symbol=?",
                         (signal_id(scan_t, sym), scan_t, sym))

    return imported_count


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
    sim_interval: str = "1h",
    fill_timeout_hours: int = 24,
    trade_timeout_hours: float = TIME_STOP_HOURS,
    candles: Optional[List[Dict[str, Any]]] = None,
    costs: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """
    Merekonstruksi pergerakan harga historis sejak waktu scan:
    1. Fase Limit Order: Entry eksplisit harus berada di rentang low/high candle.
    2. Fase Aktif: Hitung MFE, MAE, deteksi TP1/TP2 vs SL (kronologis lilin per lilin).
    3. Fase Ambiguous: Jika TP dan SL tersentuh di lilin yang sama -> AMBIGUOUS (tanpa mengarang urutan).
    4. Fase Timeout: Keluar jika melebihi Time-Stop 6 jam (mencegah modal tersandera koin sideways).
    """
    scan_t_str = signal["scan_time"]
    sym = signal["symbol"]
    buy_low = float(signal.get("buy_low") or 0.0)
    buy_high = float(signal.get("buy_high") or 0.0)
    entry_p = float(signal.get("entry_price") or 0.0)
    sl = float(signal.get("stop_loss") or 0.0)
    tp1 = float(signal.get("tp1") or 0.0)
    tp2 = float(signal.get("tp2") or 0.0)

    # Konversi waktu scan ke milidetik UTC
    dt_scan = utc_time(scan_t_str)
    start_ms = int(dt_scan.timestamp() * 1000)

    klines = fetch_subsequent_klines(sym, start_ms, interval=sim_interval, limit=500) if candles is None else candles
    if not klines:
        return signal

    # Replay from scan time on every evaluation. Previous partial results must
    # not treat candles before the fill as an already active position.
    is_filled = 0
    fill_time_str = ""
    fill_price = 0.0
    fill_dt = None
    mfe_pct = 0.0
    mae_pct = 0.0
    result = "PENDING"
    result_time = ""
    dur_hours = 0.0
    dur_candles = 0

    exit_price = None
    exit_time = None
    ret_12h = None
    ret_24h = None
    ret_48h = None

    # Legacy signals may omit entry_price; use the declared buy area only.
    if entry_p <= 0:
        if 0 < buy_low <= buy_high:
            entry_p = (buy_low + buy_high) / 2.0
        else:
            entry_p = buy_high if buy_high > 0 else buy_low
    if entry_p <= 0:
        return signal

    fill_candle_idx = -1

    for idx, c in enumerate(klines):
        c_dt = utc_time(c["datetime_utc"])
        c_high = c["high"]
        c_low = c["low"]
        c_close = c["close"]
        c_open = c["open"]
        if c_dt < dt_scan:
            continue

        # --- TAHAP 1: MENUNGGU LIMIT ORDER TERISI (FILL) ---
        if not is_filled:
            elapsed_since_scan = (c_dt - dt_scan).total_seconds() / 3600.0
            if elapsed_since_scan >= fill_timeout_hours:
                result = "UNFILLED"
                result_time = c["datetime_utc"]
                break
            # Require evidence that the declared limit price was touched.
            # OHLC alone does not prove a fill at that price across a gap.
            if c_low <= entry_p <= c_high:
                is_filled = 1
                # Use the scanner's declared entry price. Do not infer an intrabar fill price.
                fill_price = entry_p
                fill_time_str = c["datetime_utc"]
                fill_dt = c_dt
                fill_candle_idx = idx

                # OHLC cannot reveal whether fill happened before TP/SL on this candle.
                # Mark the event ambiguous instead of inventing the intrabar order.
                same_candle_tp = (tp1 > 0 and c_high >= tp1) or (tp1 <= 0 and tp2 > 0 and c_high >= tp2)
                same_candle_sl = sl > 0 and c_low <= sl
                if same_candle_tp or same_candle_sl:
                    result = "AMBIGUOUS"
                    result_time = c["datetime_utc"]
                    dur_hours = 0.0
                    dur_candles = 0
                    break

                # This candle only proves that the order was touched.
                # MFE/MAE/TP/SL evaluation starts on the next closed candle.
                continue
            else:
                continue

        # --- TAHAP 2: TRADE AKTIF SETELAH TERISI (IN POSITION) ---
        if is_filled and fill_price > 0:
            elapsed_from_fill = (c_dt - fill_dt).total_seconds() / 3600.0 if fill_dt else 0.0
            # A candle starting at the deadline is outside the trade window.
            # Its later high/low cannot count as a TP/SL within that window.
            if elapsed_from_fill >= trade_timeout_hours:
                result = "TIMEOUT"
                result_time = c["datetime_utc"]
                exit_price = float(c_open)
                exit_time = result_time
                dur_hours = round(elapsed_from_fill, 1)
                dur_candles = idx - (fill_candle_idx if fill_candle_idx >= 0 else 0)
                break
            
            # Hitung MFE (% Keuntungan Maksimal yang sempat tersentuh)
            cur_gain = ((c_high - fill_price) / fill_price) * 100.0
            if cur_gain > mfe_pct:
                mfe_pct = round(cur_gain, 2)

            # Hitung MAE (% Drawdown Terburuk yang dialami)
            cur_drawdown = ((c_low - fill_price) / fill_price) * 100.0
            if cur_drawdown < mae_pct:
                mae_pct = round(cur_drawdown, 2)

            # Catat return periodik (12h, 24h, 48h)
            if ret_12h is None and elapsed_from_fill >= 12.0:
                ret_12h = round(((c_close - fill_price) / fill_price) * 100.0, 2)
            if ret_24h is None and elapsed_from_fill >= 24.0:
                ret_24h = round(((c_close - fill_price) / fill_price) * 100.0, 2)
            if ret_48h is None and elapsed_from_fill >= 48.0:
                ret_48h = round(((c_close - fill_price) / fill_price) * 100.0, 2)

            # Cek Pemicu TP dan SL:
            hit_tp1 = (tp1 > 0 and c_high >= tp1)
            hit_tp2 = (tp1 <= 0 and tp2 > 0 and c_high >= tp2)
            hit_sl = (sl > 0 and c_low <= sl)

            # Jika terjadi flash spike dua arah di lilin yang sama (Spesifikasi 3.5: AMBIGUOUS, jangan mengarang urutan)
            if (hit_tp1 or hit_tp2) and hit_sl:
                result = "AMBIGUOUS"
                result_time = c["datetime_utc"]
                dur_hours = round(elapsed_from_fill, 1)
                dur_candles = idx - (fill_candle_idx if fill_candle_idx >= 0 else 0)
                break
            elif hit_tp2:
                result = "TP2_HIT"
                exit_price = tp2
                exit_time = c["datetime_utc"]
                result_time = c["datetime_utc"]
                dur_hours = round(elapsed_from_fill, 1)
                dur_candles = idx - (fill_candle_idx if fill_candle_idx >= 0 else 0)
                break
            elif hit_tp1:
                result = "TP1_HIT"
                exit_price = tp1
                exit_time = c["datetime_utc"]
                result_time = c["datetime_utc"]
                dur_hours = round(elapsed_from_fill, 1)
                dur_candles = idx - (fill_candle_idx if fill_candle_idx >= 0 else 0)
                break
            elif hit_sl:
                result = "SL_HIT"
                exit_price = min(sl, c_open)
                exit_time = c["datetime_utc"]
                result_time = c["datetime_utc"]
                dur_hours = round(elapsed_from_fill, 1)
                dur_candles = idx - (fill_candle_idx if fill_candle_idx >= 0 else 0)
                break

    now_utc_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # Update objek sinyal
    signal["is_filled"] = is_filled
    signal["fill_time"] = fill_time_str
    signal["fill_price"] = fill_price
    signal["result"] = result
    signal["result_time"] = result_time
    signal["duration_hours"] = dur_hours
    signal["duration_candles"] = dur_candles
    signal["mfe_pct"] = mfe_pct
    signal["mae_pct"] = mae_pct
    signal["return_12h_pct"] = ret_12h
    signal["return_24h_pct"] = ret_24h
    signal["return_48h_pct"] = ret_48h
    signal["evaluated_at"] = now_utc_str
    signal["signal_id"] = signal_id(scan_t_str, sym)
    signal["exit_price"] = exit_price
    signal["exit_time"] = exit_time
    signal["gross_return_pct"] = ((exit_price / fill_price - 1) * 100) if exit_price is not None else None
    signal["net_return_pct"] = net_return(fill_price, exit_price, costs) if exit_price is not None else (0.0 if result == "UNFILLED" else None)
    signal["execution_costs_json"] = json.dumps(DEFAULT_COSTS if costs is None else costs, sort_keys=True)

    return signal


def run_outcome_tracker(
    timeout_hours: float = TIME_STOP_HOURS,
    force_recheck: bool = False,
    db_path: str = DB_PATH,
    costs: Optional[Dict[str, float]] = None,
) -> pd.DataFrame:
    """
    Menjalankan evaluasi penuh seluruh sinyal historis yang masih PENDING.
    Memperbarui database SQLite dan mencetak Scoreboard Performa Empiris.
    """
    if not math.isfinite(timeout_hours) or timeout_hours <= 0:
        raise ValueError("timeout_hours must be finite and positive.")
    horizon_hours = max(31, 24 + math.ceil(timeout_hours) + 1)
    init_outcome_tracker_db(db_path)
    import_signals_from_history(db_path)

    with database(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        if force_recheck:
            cursor.execute("SELECT * FROM signal_outcomes WHERE entry_status = 'TRIGGERED' ORDER BY scan_time ASC")
        else:
            cursor.execute("SELECT * FROM signal_outcomes WHERE entry_status = 'TRIGGERED' AND (result = 'PENDING' OR COALESCE(forward_complete, 0) = 0) ORDER BY scan_time ASC")
        rows = cursor.fetchall()
        signals = [dict(r) for r in rows]

    if not signals:
        print("[*] Tidak ada sinyal yang perlu dievaluasi saat ini.")
        # Baca seluruh outcomes yang sudah selesai untuk dicetak scoreboarnya
        with database(db_path) as conn:
            df_all = pd.read_sql("SELECT * FROM signal_outcomes WHERE entry_status = 'TRIGGERED' AND replay_status='VALID' ORDER BY scan_time DESC", conn)
        print_outcome_scoreboard(df_all)
        return df_all

    print(f"\n[*] Menjalankan evaluasi empiris pada {len(signals)} sinyal dari Binance API...")
    updated_records = []

    for s in signals:
        start_ms = int(utc_time(s["scan_time"]).timestamp() * 1000)
        batch_limit = min(1000, max(500, horizon_hours + 2))
        candles = fetch_subsequent_klines(s["symbol"], start_ms, interval="1h", limit=batch_limit)
        end_ms = start_ms + horizon_hours * 3_600_000
        while candles and candles[-1]["open_time"] < end_ms and len(candles) >= batch_limit:
            next_start = candles[-1]["open_time"] + 3_600_000
            batch = fetch_subsequent_klines(s["symbol"], next_start, interval="1h", limit=1000)
            if not batch or batch[0]["open_time"] < next_start:
                break
            candles.extend(batch)
            if len(batch) < 1000:
                break
        if not candles:
            continue
        cache_forward_candles(db_path, s, candles, horizon_hours=horizon_hours)
        with database(db_path) as conn:
            forward = read_forward_candles(conn, s["scan_time"], s["symbol"])
            if not complete_forward_window(s["scan_time"], forward, require_complete=False, horizon_hours=horizon_hours):
                conn.execute("""UPDATE signal_outcomes SET replay_status='INVALID', forward_complete=0,
                    net_return_pct=NULL, gross_return_pct=NULL, exit_price=NULL, exit_time=NULL
                    WHERE scan_time=? AND symbol=?""", (s["scan_time"], s["symbol"]))
                continue
        evaluated = evaluate_single_signal(s, sim_interval="1h", trade_timeout_hours=timeout_hours, candles=forward, costs=costs)
        evaluated["forward_complete"] = int(complete_forward_window(s["scan_time"], forward, horizon_hours=horizon_hours))
        updated_records.append(evaluated)

    # Simpan kembali hasil evaluasi ke database
    with database(db_path) as conn:
        cursor = conn.cursor()
        update_query = """
            UPDATE signal_outcomes
            SET is_filled = ?, fill_time = ?, fill_price = ?, result = ?, result_time = ?,
                duration_hours = ?, duration_candles = ?, mfe_pct = ?, mae_pct = ?,
                return_12h_pct = ?, return_24h_pct = ?, return_48h_pct = ?, evaluated_at = ?,
                exit_price=?, exit_time=?, gross_return_pct=?, net_return_pct=?, execution_costs_json=?, forward_complete=?, replay_status=\'VALID\'
            WHERE scan_time = ? AND symbol = ?
        """
        for r in updated_records:
            cursor.execute(update_query, (
                r["is_filled"], r["fill_time"], r["fill_price"], r["result"], r["result_time"],
                r["duration_hours"], r["duration_candles"], r["mfe_pct"], r["mae_pct"],
                r["return_12h_pct"], r["return_24h_pct"], r["return_48h_pct"], r["evaluated_at"],
                r["exit_price"], r["exit_time"], r["gross_return_pct"], r["net_return_pct"],
                r["execution_costs_json"], r["forward_complete"], r["scan_time"], r["symbol"]
            ))
        conn.commit()

    with database(db_path) as conn:
        df_all = pd.read_sql("SELECT * FROM signal_outcomes WHERE entry_status = 'TRIGGERED' AND replay_status='VALID' ORDER BY scan_time DESC", conn)

    print_outcome_scoreboard(df_all)
    return df_all


def print_outcome_scoreboard(df: pd.DataFrame) -> None:
    """Mencetak Scoreboard Kuantitatif yang komprehensif di terminal."""
    if df.empty:
        print("[!] Belum ada rekaman sinyal di Outcome Tracker.")
        return

    total_signals = len(df)
    filled_df = df[df["is_filled"] == 1]
    total_filled = len(filled_df)
    fill_rate = (total_filled / total_signals * 100.0) if total_signals > 0 else 0.0

    tp1_hits = len(df[df["result"].isin(["TP1_HIT", "TP2_HIT"])])
    tp2_hits = len(df[df["result"] == "TP2_HIT"])
    sl_hits = len(df[df["result"] == "SL_HIT"])
    timeouts = len(df[df["result"] == "TIMEOUT"])
    unfilled = len(df[df["result"] == "UNFILLED"])
    pending = len(df[df["result"] == "PENDING"])

    # Win rate dihitung dari order yang BENAR-BENAR TERISI (bukan unfilled)
    completed_filled = tp1_hits + sl_hits + timeouts
    win_rate = (tp1_hits / completed_filled * 100.0) if completed_filled > 0 else 0.0
    loss_rate = (sl_hits / completed_filled * 100.0) if completed_filled > 0 else 0.0
    timeout_rate = (timeouts / completed_filled * 100.0) if completed_filled > 0 else 0.0

    avg_mfe = filled_df["mfe_pct"].mean() if not filled_df.empty else 0.0
    avg_mae = filled_df["mae_pct"].mean() if not filled_df.empty else 0.0

    dur_hits = filled_df[filled_df["duration_hours"] > 0]["duration_hours"]
    avg_dur = dur_hits.mean() if not dur_hits.empty else 0.0

    print("\n" + "=" * 115)
    print("📊 KRIPIKTO EMPIRICAL OUTCOME TRACKER SCOREBOARD (PENGUKURAN VALIDITAS SISTEM)")
    print("=" * 115)
    print(f"Total Sinyal Terlacak : {total_signals:<6} | Terisi (Filled) : {total_filled:<5} ({fill_rate:.1f}%) | Belum Terisi (Unfilled) : {unfilled}")
    print(f"Win Rate (Hit TP1)    : {win_rate:.1f}% ({tp1_hits} trades) [TP2 Hit: {tp2_hits}]")
    print(f"Loss Rate (Hit SL)    : {loss_rate:.1f}% ({sl_hits} trades)")
    print(f"Timeout (Sideways)    : {timeout_rate:.1f}% ({timeouts} trades)")
    print(f"Trade Berjalan (Live) : {pending} trades")
    print("-" * 115)
    print(f"Rata-rata MFE (Max Profit Terbuka) : {avg_mfe:+.2f}%  (Seberapa jauh harga sempat naik)")
    print(f"Rata-rata MAE (Max Drawdown Alami)  : {avg_mae:+.2f}%  (Uji apakah SL terlalu ketat)")
    print(f"Rata-rata Durasi Trade Selesai     : {avg_dur:.1f} jam")
    print("=" * 115)

    # Breakdown performa berdasarkan TAKSONOMI SETUP KripikTo v2
    if "setup_type" in df.columns:
        valid_setups = df[df["setup_type"].notnull() & (df["setup_type"] != "")]
        if not valid_setups.empty:
            print("\n🔍 EVALUASI EMPIRIS BERDASARKAN TAKSONOMI SETUP (v2):")
            print(f"{'SETUP TYPE':<22} {'TOTAL':<7} {'FILLED':<8} {'WIN RATE':<10} {'AVG MFE':<10} {'AVG MAE':<10}")
            print("-" * 75)
            for stype, grp in valid_setups.groupby("setup_type"):
                st_tot = len(grp)
                st_filled = grp[grp["is_filled"] == 1]
                st_fill_cnt = len(st_filled)
                st_wins = len(st_filled[st_filled["result"].isin(["TP1_HIT", "TP2_HIT"])])
                st_losses = len(st_filled[st_filled["result"] == "SL_HIT"])
                st_finished = st_wins + st_losses + len(st_filled[st_filled["result"] == "TIMEOUT"])
                st_wr = (st_wins / st_finished * 100.0) if st_finished > 0 else 0.0
                st_mfe = st_filled["mfe_pct"].mean() if not st_filled.empty else 0.0
                st_mae = st_filled["mae_pct"].mean() if not st_filled.empty else 0.0
                print(f"{stype:<22} {st_tot:<7} {st_fill_cnt:<8} {st_wr:>5.1f}%     {st_mfe:>+5.2f}%     {st_mae:>+5.2f}%")
            print("-" * 75)

    print("\n📋 RINCIAN TRACKING INDIVIDUAL (SAMPLE TOP SINYAL HISTORIS):")
    print(f"{'WAKTU SCAN':<19} {'SIMBOL':<10} {'FILL PRICE':<11} {'TP1':<10} {'SL':<10} {'STATUS':<10} {'MFE %':<8} {'MAE %':<8} {'DURASI':<8}")
    print("-" * 115)

    for _, row in df.head(15).iterrows():
        st_time = row["scan_time"][:16]
        fp_val = row.get('fill_price')
        if pd.notnull(fp_val) and fp_val > 0:
            fp_str = f"${fp_val:.4f}" if fp_val < 1 else f"${fp_val:.2f}"
        else:
            fp_str = "-"
        tp_str = f"${row['tp1']:.4f}" if pd.notnull(row['tp1']) and row['tp1'] < 1 else f"${row['tp1']:.2f}"
        sl_str = f"${row['stop_loss']:.4f}" if pd.notnull(row['stop_loss']) and row['stop_loss'] < 1 else f"${row['stop_loss']:.2f}"
        mfe_str = f"{row['mfe_pct']:+.1f}%" if pd.notnull(row['mfe_pct']) else "0.0%"
        mae_str = f"{row['mae_pct']:+.1f}%" if pd.notnull(row['mae_pct']) else "0.0%"
        dur_str = f"{row['duration_hours']:.1f}h" if row['duration_hours'] > 0 else "-"
        res_str = row["result"]

        if res_str in ["TP1_HIT", "TP2_HIT"]:
            res_str = f"✅ {res_str}"
        elif res_str == "SL_HIT":
            res_str = f"❌ {res_str}"
        elif res_str == "TIMEOUT":
            res_str = f"⏳ {res_str}"
        elif res_str == "UNFILLED":
            res_str = f"🚫 {res_str}"
        else:
            res_str = f"🔄 {res_str}"

        print(f"{st_time:<19} {row['symbol']:<10} {fp_str:<11} {tp_str:<10} {sl_str:<10} {res_str:<10} {mfe_str:<8} {mae_str:<8} {dur_str:<8}")

    print("=" * 115 + "\n")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="KripikTo Outcome Tracker: Empirical Performance Engine")
    parser.add_argument("--timeout", type=float, default=TIME_STOP_HOURS, help="Batas jam timeout posisi sideways (default: 6 jam)")
    parser.add_argument("--recheck", action="store_true", help="Evaluasi ulang seluruh riwayat sinyal dari nol")
    parser.add_argument("--fee-pct", type=float, default=DEFAULT_COSTS["fee_pct"])
    parser.add_argument("--slippage-pct", type=float, default=DEFAULT_COSTS["slippage_pct"])
    parser.add_argument("--spread-pct", type=float, default=DEFAULT_COSTS["spread_pct"])
    args = parser.parse_args()

    run_outcome_tracker(timeout_hours=args.timeout, force_recheck=args.recheck,
        costs={"fee_pct": args.fee_pct, "slippage_pct": args.slippage_pct, "spread_pct": args.spread_pct})
