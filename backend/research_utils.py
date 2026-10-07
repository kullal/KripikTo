"""Shared identity, execution-cost and finite-capital research utilities."""

import datetime
import hashlib
import math
import sqlite3
from contextlib import contextmanager

UTC = datetime.timezone.utc
FORWARD_HOURS = 31  # 24h limit expiry + 6h position + deadline candle.
DEFAULT_COSTS = {"fee_pct": 0.10, "slippage_pct": 0.05, "spread_pct": 0.02}


@contextmanager
def database(db_path):
    """Commit/rollback transactions and close the connection on every path."""
    connection = sqlite3.connect(db_path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def utc_time(value):
    parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def signal_id(scan_time, symbol):
    key = f"{utc_time(scan_time).isoformat()}|{symbol}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def net_return(entry, exit_price, costs=None):
    costs = DEFAULT_COSTS if costs is None else costs
    values = {key: float(costs.get(key, 0.0)) for key in DEFAULT_COSTS}
    if any(not math.isfinite(v) or not 0 <= v < 100 for v in values.values()):
        raise ValueError("Execution costs must be finite percentages in [0, 100).")
    fee = values["fee_pct"] / 100
    friction = (values["slippage_pct"] + values["spread_pct"] / 2) / 100
    if friction >= 1 or not 0 < entry or not 0 < exit_price:
        raise ValueError("Invalid execution prices/costs.")
    return (exit_price * (1 - friction) * (1 - fee) /
            (entry * (1 + friction) * (1 + fee)) - 1) * 100


def init_forward_db(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS signal_forward_candles (
        scan_time TEXT, symbol TEXT, open_time INTEGER, close_time INTEGER,
        datetime_utc TEXT, open REAL, high REAL, low REAL, close REAL,
        volume REAL, PRIMARY KEY (scan_time, symbol, open_time))""")


def cache_forward_candles(db_path, signal, candles, horizon_hours=FORWARD_HOURS):
    """Persist a fixed observation horizon, including prices after the old exit."""
    scan = utc_time(signal["scan_time"])
    end = scan + datetime.timedelta(hours=horizon_hours)
    rows = []
    for c in candles:
        timestamp = utc_time(c["datetime_utc"])
        if scan <= timestamp <= end:
            open_ms = int(timestamp.timestamp() * 1000)
            rows.append((signal["scan_time"], signal["symbol"], open_ms,
                         int(c.get("close_time", open_ms + 3_600_000 - 1)),
                         c["datetime_utc"], c["open"], c["high"], c["low"],
                         c["close"], c.get("volume", 0)))
    with database(db_path) as conn:
        init_forward_db(conn)
        conn.executemany("INSERT OR REPLACE INTO signal_forward_candles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)


def read_forward_candles(conn, scan_time, symbol):
    cursor = conn.execute("""SELECT open_time, close_time, datetime_utc, open,
        high, low, close, volume FROM signal_forward_candles
        WHERE scan_time=? AND symbol=? ORDER BY open_time""", (scan_time, symbol))
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def complete_forward_window(scan_time, candles, require_complete=True, horizon_hours=FORWARD_HOURS):
    """Require an uninterrupted, valid, closed 1H window through the deadline."""
    if not candles:
        return False
    scan_ms = int(utc_time(scan_time).timestamp() * 1000)
    first = ((scan_ms + 3_599_999) // 3_600_000) * 3_600_000
    last = ((scan_ms + horizon_hours * 3_600_000) // 3_600_000) * 3_600_000
    if not require_complete:
        last = min(last, max(c["open_time"] for c in candles))
    if last < first:
        return False
    required = list(range(first, last + 1, 3_600_000))
    selected = [c for c in candles if first <= c["open_time"] <= last]
    if [c["open_time"] for c in selected] != required:
        return False
    now_ms = int(datetime.datetime.now(UTC).timestamp() * 1000)
    for c in selected:
        prices = [float(c[k]) for k in ("open", "high", "low", "close")]
        volume = float(c.get("volume", 0))
        if (not all(math.isfinite(p) and p > 0 for p in prices)
                or not math.isfinite(volume) or volume < 0
                or c["close_time"] != c["open_time"] + 3_600_000 - 1
                or c["close_time"] > now_ms
                or c["high"] < max(c["open"], c["close"], c["low"])
                or c["low"] > min(c["open"], c["close"], c["high"])):
            return False
    return True


def portfolio_metrics(trades, max_positions=5, allocation_pct=20.0):
    """Reserve finite capital at signal time; ambiguous executions remain unknown.

    Trades must have chronological signal and result times and net_return_pct.
    No compounding assumes sequential independent trades or unlimited capital.
    """
    if max_positions < 1 or not 0 < allocation_pct <= 100:
        raise ValueError("Invalid portfolio capacity.")
    cash = 1.0
    active = []
    settled = []
    skipped = 0
    peak = 1.0
    drawdown = 0.0

    def settle(until):
        nonlocal cash, peak, drawdown
        for position in sorted(active[:], key=lambda p: p[0]):
            if position[0] > until:
                continue
            _, principal, ret, filled = position
            cash += principal * (1 + ret / 100)
            if filled:
                settled.append(ret)
            active.remove(position)
            equity = cash + sum(p[1] for p in active)
            peak = max(peak, equity)
            drawdown = max(drawdown, (peak - equity) / peak * 100)

    uncertain = False
    for trade in sorted(trades, key=lambda t: (utc_time(t["scan_time"]), -float(t.get("selection_score", 0)), t["symbol"])):
        start = utc_time(trade["scan_time"])
        settle(start)
        if uncertain:
            skipped += 1
            continue
        equity = cash + sum(p[1] for p in active)
        principal = equity * allocation_pct / 100
        if len(active) >= max_positions or cash + 1e-12 < principal:
            skipped += 1
            continue
        if trade.get("net_return_pct") is None:
            # Unknown P&L prevents a determinate portfolio equity path.
            uncertain = True
            continue
        finish = utc_time(trade["result_time"])
        cash -= principal
        active.append((finish, principal, float(trade["net_return_pct"]), bool(trade.get("is_filled", 1))))
    settle(datetime.datetime.max.replace(tzinfo=UTC))
    gains = sum(r for r in settled if r > 0)
    losses = -sum(r for r in settled if r < 0)
    return {
        "portfolio_return_pct": None if uncertain else (cash - 1) * 100,
        "realized_equity_drawdown_pct": None if uncertain else drawdown,
        "profit_factor": gains / losses if losses else None,
        "expectancy": sum(settled) / len(settled) if settled else 0.0,
        "executed_trades": len(settled), "capacity_skipped": skipped,
        "portfolio_complete": not uncertain,
    }
