"""Closed-candle validation and exact-period relative strength."""

import datetime
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd


INTERVAL_MS = {"15m": 900_000, "1h": 3_600_000, "2h": 7_200_000,
               "4h": 14_400_000, "1d": 86_400_000}
STRUCTURE_MIN_CANDLES = 51  # Full MA50 plus a previous close.
MOMENTUM_MIN_CANDLES = 35  # Full ATR14 and 20 ATR observations.
TRIGGER_MIN_CANDLES = 21  # Previous 20-bar high plus the trigger candle.
FRESHNESS_GRACE_MS = 60_000
ReturnKey = Tuple[int, int, int, int]


def utc_now_ms() -> int:
    return int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)


def validate_market_summary(
    summary: pd.DataFrame, now_ms: Optional[int] = None,
) -> Tuple[pd.DataFrame, Dict[str, list]]:
    """The 24h ticker snapshot must itself be recent (at most one hour old)."""
    now_ms = utc_now_ms() if now_ms is None else now_ms
    now = pd.Timestamp(now_ms, unit="ms", tz="UTC")
    accepted, rejected = [], {}
    for index, row in summary.iterrows():
        reasons = []
        updated = pd.to_datetime(row.get("updated_at"), utc=True, errors="coerce")
        if pd.isna(updated):
            reasons.append("MISSING_SUMMARY_TIMESTAMP")
        elif updated > now:
            reasons.append("FUTURE_SUMMARY")
        elif (now - updated).total_seconds() * 1000 > INTERVAL_MS["1h"] + FRESHNESS_GRACE_MS:
            reasons.append("STALE_SUMMARY")
        values = pd.to_numeric(pd.Series({name: row.get(name) for name in (
            "last_price", "price_change_pct", "high_price", "low_price",
            "volume", "quote_volume", "taker_buy_ratio",
        )}), errors="coerce")
        if not np.isfinite(values.to_numpy(dtype=float)).all():
            reasons.append("INVALID_SUMMARY_NUMERIC_DATA")
        elif (values["last_price"] <= 0 or values["low_price"] <= 0
              or values["high_price"] < values["low_price"] or values["volume"] < 0
              or values["quote_volume"] <= 0 or not 0 <= values["taker_buy_ratio"] <= 100):
            reasons.append("INVALID_SUMMARY_VALUES")
        if reasons:
            rejected[str(row["symbol"])] = reasons
        else:
            accepted.append(index)
    return summary.loc[accepted].copy(), rejected


def validate_closed_candles(
    candles: pd.DataFrame,
    interval: str,
    min_candles: int,
    now_ms: Optional[int] = None,
) -> Tuple[pd.DataFrame, Dict[str, list]]:
    """Validate the most recent required window per symbol; preserve DB history.

    A closed candle and its fetch must be no older than one interval plus a
    one-minute operational grace. Forming/unknown candles are never indicators.
    """
    duration = INTERVAL_MS[interval]
    now_ms = utc_now_ms() if now_ms is None else now_ms
    now = pd.Timestamp(now_ms, unit="ms", tz="UTC")
    required = {"symbol", "interval", "open_time", "close_time", "is_closed",
                "fetched_at", "open", "high", "low", "close", "volume"}
    missing = required - set(candles.columns)
    if missing:
        symbols = candles["symbol"].dropna().unique() if "symbol" in candles else ["UNKNOWN"]
        return candles.iloc[:0].copy(), {
            str(symbol): ["MISSING_COLUMNS:" + ",".join(sorted(missing))] for symbol in symbols
        }
    numeric_columns = ["open_time", "close_time", "open", "high", "low", "close", "volume"]
    accepted, rejected = [], {}
    for symbol, history in candles.groupby("symbol", sort=False):
        closed = history[(history["is_closed"] == 1) & (history["interval"] == interval)].copy()
        closed["open_time"] = pd.to_numeric(closed["open_time"], errors="coerce")
        window = closed.sort_values("open_time").tail(min_candles).copy()
        reasons = []
        if len(window) < min_candles:
            reasons.append(f"INSUFFICIENT_CANDLES:{len(window)}/{min_candles}")
        if window.empty:
            rejected[str(symbol)] = reasons
            continue
        numeric = window[numeric_columns].apply(pd.to_numeric, errors="coerce")
        if not np.isfinite(numeric.to_numpy(dtype=float)).all():
            reasons.append("INVALID_NUMERIC_DATA")
        else:
            window[numeric_columns] = numeric
            if ((numeric[["open", "high", "low", "close"]] <= 0).any().any()
                    or (numeric["volume"] < 0).any()
                    or (numeric["high"] < numeric[["open", "close", "low"]].max(axis=1)).any()
                    or (numeric["low"] > numeric[["open", "close", "high"]].min(axis=1)).any()):
                reasons.append("INVALID_OHLCV")
            if ((numeric["open_time"] % duration != 0).any()
                    or (numeric["close_time"] != numeric["open_time"] + duration - 1).any()):
                reasons.append("INVALID_CANDLE_TIMESTAMPS")
            if numeric["open_time"].duplicated().any():
                reasons.append("DUPLICATE_CANDLES")
            if (numeric["open_time"].diff().dropna() != duration).any():
                reasons.append("CANDLE_GAP")
            if (numeric["close_time"] > now_ms).any():
                reasons.append("CANDLE_NOT_CLOSED")
            if now_ms - numeric["close_time"].iloc[-1] > duration + FRESHNESS_GRACE_MS:
                reasons.append("STALE_CANDLES")
        fetched = pd.to_datetime(window["fetched_at"], utc=True, errors="coerce", format="mixed")
        if fetched.isna().any():
            reasons.append("MISSING_FETCH_METADATA")
        elif (fetched > now).any():
            reasons.append("FUTURE_FETCH_METADATA")
        else:
            if (now - fetched.iloc[-1]).total_seconds() * 1000 > duration + FRESHNESS_GRACE_MS:
                reasons.append("STALE_FETCH")
            if "INVALID_NUMERIC_DATA" not in reasons:
                close_times = pd.to_datetime(numeric["close_time"], unit="ms", utc=True)
                if ((fetched + pd.Timedelta(seconds=1)) < close_times).any():
                    reasons.append("FETCH_BEFORE_CLOSE")
        if reasons:
            rejected[str(symbol)] = list(dict.fromkeys(reasons))
        else:
            accepted.append(window)
    clean = pd.concat(accepted, ignore_index=True) if accepted else candles.iloc[:0].copy()
    return clean, rejected


def build_return_series(candles: pd.DataFrame, interval: str) -> Dict[ReturnKey, float]:
    """Returns keyed by both current AND previous candle boundaries."""
    result = {}
    duration = INTERVAL_MS[interval]
    for _, history in candles.groupby("symbol"):
        rows = history.sort_values("open_time").to_dict(orient="records")
        for previous, current in zip(rows, rows[1:]):
            key = (int(current["open_time"]), int(current["close_time"]),
                   int(previous["open_time"]), int(previous["close_time"]))
            if key[0] - key[2] != duration or float(previous["close"]) <= 0:
                continue
            result[key] = ((float(current["close"]) / float(previous["close"])) - 1) * 100
    return result


def aligned_relative_strength(
    row: pd.Series, benchmark: Dict[ReturnKey, float], coin_return: float,
) -> Tuple[Optional[float], Optional[float], str]:
    fields = [row.get(name) for name in
              ("open_time", "close_time", "prev_open_time", "prev_close_time")]
    if any(pd.isna(value) for value in fields) or not np.isfinite(coin_return):
        return None, None, "COIN_RETURN_UNAVAILABLE"
    key = tuple(int(value) for value in fields)
    matched = benchmark.get(key)
    if matched is None:
        return None, None, "BTC_ALIGNMENT_MISSING"
    return round(coin_return - matched, 2), round(matched, 2), "ALIGNED"
