"""Paired breakout experiment on frozen snapshots and the same cached forward window.

Run: python -m backend.strategy_comparison --db backend/data/kripto.db
Both arms use identical absolute stop/target levels, fees, slippage, and a six-hour
time stop. Market entries execute at the NEXT candle open after confirmation.
This is a trade-level experiment, not a portfolio equity backtest.
"""
import argparse
import json
import math
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

from backend.replay import Costs, INTERVAL_MS, replay_trade, summarize, timestamp_ms, utc_string, validate_candles
from backend.replay_store import load_candles
from backend.scoring import SCORING_VERSION


def compare_signal(signal, candles, costs=None, wait_hours=2.0, hold_hours=6.0):
    if any(not math.isfinite(v) or v <= 0 for v in (wait_hours, hold_hours)):
        raise ValueError("Experiment windows must be positive and finite")
    step = INTERVAL_MS["15m"]
    start = timestamp_ms(signal["scan_time"])
    first = ((start + step - 1) // step) * step
    end = first + (wait_hours + hold_hours) * 3_600_000
    rows = [c for c in candles if first <= c["open_time"] <= end]
    def blank(status):
        return {"result": status, "is_filled": 0, "net_return_pct": None}
    try:
        validate_candles(rows, step)
        if not rows or rows[0]["open_time"] != first or rows[-1]["open_time"] < end:
            raise ValueError("Incomplete shared forward window")
        level = float(signal["breakout_level"])
        if not math.isfinite(level) or level <= 0:
            raise ValueError("Missing frozen resistance")
    except (KeyError, TypeError, ValueError):
        return {name: blank("DATA_UNAVAILABLE") for name in ("BREAKOUT", "BREAKOUT_RETEST")}
    common = dict(interval="15m", trade_timeout_hours=hold_hours, costs=costs, entry_mode="market")
    direct = replay_trade(signal, rows, **common)
    retest = blank("UNFILLED")
    for c in rows:
        # Confirmation requires a full later candle. Entry uses its successor open.
        if c["close_time"] + 1 >= first + wait_hours * 3_600_000:
            break
        if c["close"] < level:
            retest = blank("ENTRY_INVALIDATED")
            break
        if c["low"] <= level <= c["high"] and c["close"] > level and c["close"] >= c["open"]:
            pending = dict(signal, scan_time=utc_string(c["close_time"] + 1))
            retest = replay_trade(pending, rows, **common)
            break
    return {"BREAKOUT": direct, "BREAKOUT_RETEST": retest}


def run_comparison(db_path, *, costs=None, wait_hours=2.0, hold_hours=6.0):
    costs = costs or Costs()
    records, skipped = [], Counter()
    # Read-only: comparing strategies never alters the live database.
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute("SELECT * FROM scan_results ORDER BY scan_time, symbol"):
            signal = dict(row)
            if signal.get("scoring_version") != SCORING_VERSION:
                skipped["legacy_scoring"] += 1
                continue
            if (signal.get("entry_status") != "TRIGGERED" or signal.get("data_quality_status") != "OK"
                    or signal.get("trigger_type") != "BREAKOUT" or signal.get("execution_eligible") == 0):
                skipped["not_eligible_breakout"] += 1
                continue
            start = timestamp_ms(signal["scan_time"])
            rows = load_candles(conn, signal["symbol"], start,
                start + (wait_hours + hold_hours + 0.25) * 3_600_000)
            arms = compare_signal(signal, rows, costs, wait_hours, hold_hours)
            records.append({"scan_time": signal["scan_time"], "symbol": signal["symbol"], "arms": arms})
    summaries = {name: summarize([r["arms"][name] for r in records])
                 for name in ("BREAKOUT", "BREAKOUT_RETEST")}
    return {"experiment": "breakout-vs-confirmed-retest-v1", "scoring_version": SCORING_VERSION,
            "fee_bps_per_side": costs.fee_bps, "slippage_bps_per_side": costs.slippage_bps,
            "wait_hours": wait_hours, "hold_hours": hold_hours,
            "sample_pairs": len(records), "skipped": dict(skipped), "summary": summaries,
            "cohort": "Technical triggered breakout snapshots; optional news is not part of this experiment",
            "note": "Trade-level returns; overlapping signals are not a portfolio. No automatic winner selection.",
            "records": records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="backend/data/kripto.db")
    parser.add_argument("--output", default="backend/data/strategy_comparison.json")
    parser.add_argument("--fee-bps", type=float, default=10)
    parser.add_argument("--slippage-bps", type=float, default=5)
    parser.add_argument("--wait-hours", type=float, default=2)
    parser.add_argument("--hold-hours", type=float, default=6)
    args = parser.parse_args()
    report = run_comparison(args.db, costs=Costs(args.fee_bps, args.slippage_bps),
                            wait_hours=args.wait_hours, hold_hours=args.hold_hours)
    Path(args.output).write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "records"}, indent=2))


if __name__ == "__main__":
    main()
