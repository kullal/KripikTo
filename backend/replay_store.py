"""Closed forward candles retained independently of truncated trade outcomes."""
import sqlite3
from contextlib import closing


def init_replay_store(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS replay_candles (
        symbol TEXT, interval TEXT, open_time INTEGER, close_time INTEGER,
        open REAL, high REAL, low REAL, close REAL, volume REAL,
        PRIMARY KEY(symbol, interval, open_time))""")


def save_candles(db_path, symbol, interval, candles):
    with closing(sqlite3.connect(db_path)) as conn:
        init_replay_store(conn)
        conn.executemany("INSERT OR REPLACE INTO replay_candles VALUES (?,?,?,?,?,?,?,?,?)", [
            (symbol, interval, c["open_time"], c["close_time"], c["open"], c["high"],
             c["low"], c["close"], c.get("volume", 0)) for c in candles])
        conn.commit()


def load_candles(conn, symbol, start_ms, end_ms, interval="15m"):
    # No network and no migration when reading experiments.
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='replay_candles'").fetchone()
    if not exists:
        return []
    rows = conn.execute("""SELECT open_time, close_time, open, high, low, close, volume
        FROM replay_candles WHERE symbol=? AND interval=? AND open_time>=? AND open_time<=?
        ORDER BY open_time""", (symbol, interval, start_ms, end_ms)).fetchall()
    keys = ("open_time", "close_time", "open", "high", "low", "close", "volume")
    return [dict(zip(keys, row)) for row in rows]
