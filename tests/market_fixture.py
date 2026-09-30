"""Deterministic, closed market candles for offline integration tests."""

import contextlib
import datetime
import sqlite3

import pandas as pd

from backend import data_pipeline


NOW = datetime.datetime(2026, 9, 30, 12, tzinfo=datetime.timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)
MINUTES = {"15m": 15, "1h": 60, "2h": 120, "4h": 240, "1d": 1440}


def candles(symbol="TESTUSDT", interval="4h", count=60):
    duration = MINUTES[interval] * 60_000
    boundary = (NOW_MS // duration) * duration
    records = []
    for index in range(count):
        open_time = boundary - (count - index) * duration
        price = (60000.0 + index * 300.0) if symbol == "BTCUSDT" else (100.0 + index * 0.3)
        width = 500.0 if symbol == "BTCUSDT" else 1.0
        close = price + (100.0 if symbol == "BTCUSDT" else 0.1)
        records.append({
            "symbol": symbol, "interval": interval, "open_time": open_time,
            "close_time": open_time + duration - 1,
            "datetime_utc": datetime.datetime.fromtimestamp(open_time / 1000, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "open": price, "high": price + width, "low": price - width,
            "close": close, "volume": 100.0, "quote_volume": close * 100.0,
            "taker_buy_volume": close * 56.0, "is_closed": 1,
            "fetched_at": NOW.strftime("%Y-%m-%d %H:%M:%S"),
        })
    return pd.DataFrame(records)


def seed_database(db_path, structure_interval="4h"):
    data_pipeline.init_db(str(db_path))
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        conn.execute("""
            INSERT INTO market_summary_24h (
                symbol,last_price,price_change_pct,high_price,low_price,
                volume,quote_volume,taker_buy_ratio,updated_at
            ) VALUES ('TESTUSDT',118,1,119,99,1000,1000000,56,?)
        """, (NOW.strftime("%Y-%m-%d %H:%M:%S"),))
        for interval in dict.fromkeys([structure_interval, "1h", "15m"]):
            for symbol in ("TESTUSDT", "BTCUSDT"):
                frame = candles(symbol, interval)
                columns = list(frame.columns)
                conn.executemany(
                    "INSERT INTO klines_history (" + ",".join(columns) + ") VALUES (" +
                    ",".join(["?"] * len(columns)) + ")",
                    frame.itertuples(index=False, name=None),
                )
        conn.commit()
