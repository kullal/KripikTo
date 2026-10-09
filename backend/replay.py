"""Deterministic long-only OHLC replay. No network, fabricated path, or portfolio claims."""
from dataclasses import dataclass
from datetime import datetime, timezone
import math

REPLAY_VERSION = "v3-ohlc-costs"
INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}
COMPLETED = {"TP1_HIT", "SL_HIT", "TIMEOUT"}


def timestamp_ms(value):
    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def utc_string(value):
    return datetime.fromtimestamp(value / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class Costs:
    # Experimental assumptions, not an exchange fee schedule. Basis points per side.
    fee_bps: float = 10.0
    slippage_bps: float = 5.0

    def __post_init__(self):
        if any(not math.isfinite(v) or not 0 <= v < 10000 for v in (self.fee_bps, self.slippage_bps)):
            raise ValueError("Costs must be finite, in [0, 10000) bps")


def validate_candles(candles, interval_ms):
    """Require supplied order, unique contiguous timestamps, and valid closed OHLC."""
    previous = None
    for c in candles:
        t = int(c["open_time"])
        values = [float(c[k]) for k in ("open", "high", "low", "close")]
        o, h, l, close = values
        if (t % interval_ms != 0 or not all(math.isfinite(v) and v > 0 for v in values)
                or not l <= min(o, close) <= max(o, close) <= h
                or int(c["close_time"]) != t + interval_ms - 1
                or c.get("is_closed", 1) != 1
                or (previous is not None and t != previous + interval_ms)):
            raise ValueError("Invalid, duplicate, unordered or missing replay candle")
        previous = t


def replay_trade(signal, candles, *, interval="15m", fill_timeout_hours=2.0,
                 trade_timeout_hours=6.0, costs=None, entry_mode="limit"):
    costs = costs or Costs()
    step = INTERVAL_MS[interval]
    if entry_mode not in ("limit", "market"):
        raise ValueError("entry_mode must be limit or market")
    if any(not math.isfinite(v) or v <= 0 for v in (fill_timeout_hours, trade_timeout_hours)):
        raise ValueError("Timeouts must be positive and finite")
    start = timestamp_ms(signal["scan_time"])
    first_open = ((start + step - 1) // step) * step
    rows = [dict(c) for c in candles if int(c["open_time"]) >= first_open]
    result = dict(signal)
    result.update(is_filled=0, fill_time="", fill_price=0.0, result="PENDING", result_time="",
                  duration_hours=0.0, duration_candles=0, mfe_pct=0.0, mae_pct=0.0,
                  exit_price=None, gross_return_pct=None, net_return_pct=None,
                  return_12h_pct=None, return_24h_pct=None, return_48h_pct=None,
                  replay_version=REPLAY_VERSION, replay_interval=interval,
                  fee_bps=costs.fee_bps, slippage_bps=costs.slippage_bps,
                  fill_timeout_hours=fill_timeout_hours, trade_timeout_hours=trade_timeout_hours,
                  replay_note="", evaluated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    try:
        validate_candles(rows, step)
        if rows and rows[0]["open_time"] != first_open:
            raise ValueError("Missing first post-signal candle")
    except (ValueError, KeyError, TypeError) as exc:
        result.update(result="DATA_UNAVAILABLE", replay_note=str(exc))
        return result
    entry = float(signal.get("entry_price") or 0)
    if entry <= 0:
        entry = (float(signal.get("buy_low") or 0) + float(signal.get("buy_high") or 0)) / 2
    sl, tp = float(signal.get("stop_loss") or 0), float(signal.get("tp1") or 0)
    if not all(math.isfinite(v) for v in (entry, sl, tp)) or not 0 < sl < entry < tp:
        result.update(result="INVALID_PLAN")
        return result
    fill_at = None
    fill_index = None
    deadline = None
    expiry = start + fill_timeout_hours * 3_600_000

    def finish(status, when, price=None, index=0):
        result.update(result=status, result_time=utc_string(when))
        if fill_at is not None:
            result["duration_hours"] = round((when - fill_at) / 3_600_000, 4)
            result["duration_candles"] = index - fill_index
        if price is not None:
            # TP is a limit exit; stop/timeout are market exits.
            exit_price = price if status == "TP1_HIT" else price * (1 - costs.slippage_bps / 10000)
            fill = result["fill_price"]
            fee = costs.fee_bps / 10000
            result.update(exit_price=exit_price,
                          gross_return_pct=(exit_price / fill - 1) * 100,
                          net_return_pct=(exit_price * (1 - fee) / (fill * (1 + fee)) - 1) * 100)

    for index, c in enumerate(rows):
        t, o, h, l = c["open_time"], c["open"], c["high"], c["low"]
        if fill_at is None:
            if t >= expiry:
                finish("UNFILLED", t)
                break
            if entry_mode == "limit" and not l <= entry <= h:
                continue
            if entry_mode == "limit" and t + step > expiry:
                finish("AMBIGUOUS", t)
                result["replay_note"] = "Entry touch may be after intrabar order expiry"
                break
            price = o if entry_mode == "market" else entry
            if entry_mode == "market" and not sl < price < tp:
                finish("ENTRY_INVALIDATED", t)
                break
            fill_at, fill_index = t, index
            deadline = t + trade_timeout_hours * 3_600_000
            result.update(is_filled=1, fill_time=utc_string(t),
                          fill_price=price * (1 + costs.slippage_bps / 10000) if entry_mode == "market" else price)
            # Limit touch time is unknown, including order of any exit touch.
            if entry_mode == "limit":
                if l <= sl or h >= tp:
                    finish("AMBIGUOUS", t, index=index)
                    break
                continue
        if t >= deadline:
            finish("TIMEOUT", t, o, index)
            break
        if t + step > deadline:
            finish("AMBIGUOUS", t, index=index)
            result["replay_note"] = "Timeout inside candle; finer data required"
            break
        fill = result["fill_price"]
        result["mfe_pct"] = max(result["mfe_pct"], round((h / fill - 1) * 100, 2))
        result["mae_pct"] = min(result["mae_pct"], round((l / fill - 1) * 100, 2))
        # A gap exit at open precedes all subsequent high/low observations.
        if o <= sl:
            finish("SL_HIT", t, o, index)
            break
        if o >= tp:
            finish("TP1_HIT", t, tp, index)
            break
        if h >= tp and l <= sl:
            finish("AMBIGUOUS", t, index=index)
            break
        if l <= sl:
            finish("SL_HIT", t, sl, index)
            break
        if h >= tp:
            finish("TP1_HIT", t, tp, index)
            break
    # Forward marks are observations, independent of when the simulated position exited.
    if fill_at is not None:
        by_end = {c["close_time"] + 1: c for c in rows}
        for hours in (12, 24, 48):
            c = by_end.get(fill_at + hours * 3_600_000)
            if c:
                result[f"return_{hours}h_pct"] = (c["close"] / result["fill_price"] - 1) * 100
    return result


def summarize(results):
    completed = [r for r in results if r["result"] in COMPLETED and r.get("net_return_pct") is not None]
    returns = [r["net_return_pct"] for r in completed]
    gains, losses = sum(max(0, x) for x in returns), -sum(min(0, x) for x in returns)
    return {"signals": len(results), "completed": len(completed),
            "filled": sum(bool(r.get("is_filled")) for r in results),
            "ambiguous": sum(r["result"] == "AMBIGUOUS" for r in results),
            "pending": sum(r["result"] == "PENDING" for r in results),
            "data_unavailable": sum(r["result"] == "DATA_UNAVAILABLE" for r in results),
            "unfilled": sum(r["result"] == "UNFILLED" for r in results),
            "win_rate": 100 * sum(x > 0 for x in returns) / len(returns) if returns else None,
            "profit_factor": gains / losses if losses else None,
            "expectancy_pct": sum(returns) / len(returns) if returns else None,
            "sum_trade_return_pct": sum(returns) if returns else None}
