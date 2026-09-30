import contextlib
import gc
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from backend import data_integrity, data_pipeline, feature_engine
from market_fixture import NOW_MS, candles, seed_database


class ClosedCandleTests(unittest.TestCase):
    def validate(self, frame, minimum=51):
        return data_integrity.validate_closed_candles(frame, "4h", minimum, now_ms=NOW_MS)

    def test_complete_closed_window_is_accepted(self):
        clean, issues = self.validate(candles())
        self.assertEqual(len(clean), 51)
        self.assertEqual(issues, {})

    def test_forming_and_unknown_flags_are_excluded(self):
        frame = candles()
        extra = frame.tail(1).copy()
        extra["open_time"] += 14_400_000
        extra["close_time"] += 14_400_000
        for flag in (0, None):
            with self.subTest(flag=flag):
                extra["is_closed"] = flag
                clean, issues = self.validate(pd.concat([frame, extra], ignore_index=True))
                self.assertEqual(issues, {})
                self.assertLess(clean["close_time"].max(), NOW_MS)

    def test_false_closed_flag_cannot_admit_future_candle(self):
        frame = candles()
        frame.loc[59, "open_time"] += 14_400_000
        frame.loc[59, "close_time"] += 14_400_000
        clean, issues = self.validate(frame)
        self.assertTrue(clean.empty)
        self.assertIn("CANDLE_NOT_CLOSED", issues["TESTUSDT"])

    def test_insufficient_history_is_rejected(self):
        clean, issues = self.validate(candles(count=20))
        self.assertTrue(clean.empty)
        self.assertIn("INSUFFICIENT_CANDLES:20/51", issues["TESTUSDT"])

    def test_gap_inside_indicator_window_is_rejected(self):
        clean, issues = self.validate(candles().drop(index=40))
        self.assertTrue(clean.empty)
        self.assertIn("CANDLE_GAP", issues["TESTUSDT"])

    def test_gap_outside_indicator_window_does_not_discard_recent_data(self):
        clean, issues = self.validate(candles().drop(index=0))
        self.assertEqual(len(clean), 51)
        self.assertEqual(issues, {})

    def test_stale_closed_window_is_rejected(self):
        frame = candles()
        frame["open_time"] -= 28_800_000
        frame["close_time"] -= 28_800_000
        clean, issues = self.validate(frame)
        self.assertTrue(clean.empty)
        self.assertIn("STALE_CANDLES", issues["TESTUSDT"])

    def test_legacy_metadata_is_not_assumed_valid(self):
        frame = candles()
        frame.loc[59, "close_time"] = None
        frame.loc[59, "fetched_at"] = None
        clean, issues = self.validate(frame)
        self.assertTrue(clean.empty)
        self.assertIn("INVALID_NUMERIC_DATA", issues["TESTUSDT"])
        self.assertIn("MISSING_FETCH_METADATA", issues["TESTUSDT"])

    def test_bad_ohlcv_and_duplicate_candles_are_rejected(self):
        frame = candles()
        frame.loc[59, "high"] = frame.loc[59, "low"] - 1
        clean, issues = self.validate(frame)
        self.assertTrue(clean.empty)
        self.assertIn("INVALID_OHLCV", issues["TESTUSDT"])
        duplicate = pd.concat([candles(), candles().tail(1)], ignore_index=True)
        _, issues = self.validate(duplicate)
        self.assertIn("DUPLICATE_CANDLES", issues["TESTUSDT"])

    def test_wrong_close_boundary_is_rejected(self):
        frame = candles()
        frame.loc[59, "close_time"] -= 1
        clean, issues = self.validate(frame)
        self.assertTrue(clean.empty)
        self.assertIn("INVALID_CANDLE_TIMESTAMPS", issues["TESTUSDT"])

    def test_stale_and_future_fetch_metadata_are_rejected(self):
        for fetched_at, expected in [
            ("2026-09-29 12:00:00", "STALE_FETCH"),
            ("2026-09-30 12:01:00", "FUTURE_FETCH_METADATA"),
        ]:
            with self.subTest(expected=expected):
                frame = candles()
                frame["fetched_at"] = fetched_at
                clean, issues = self.validate(frame)
                self.assertTrue(clean.empty)
                self.assertIn(expected, issues["TESTUSDT"])


class BenchmarkAlignmentTests(unittest.TestCase):
    def test_exact_current_and_previous_candles_are_matched(self):
        frame = candles("BTCUSDT", "1h", count=2)
        returns = data_integrity.build_return_series(frame, "1h")
        latest = frame.iloc[-1].copy()
        latest["prev_open_time"] = frame.iloc[-2]["open_time"]
        latest["prev_close_time"] = frame.iloc[-2]["close_time"]
        rs, btc, status = data_integrity.aligned_relative_strength(latest, returns, 2.0)
        expected_btc = (frame.iloc[-1]["close"] / frame.iloc[-2]["close"] - 1) * 100
        self.assertEqual(status, "ALIGNED")
        self.assertEqual(btc, round(expected_btc, 2))
        self.assertEqual(rs, round(2.0 - expected_btc, 2))

    def test_matching_current_timestamp_with_wrong_previous_period_is_unavailable(self):
        frame = candles("BTCUSDT", "1h", count=2)
        latest = frame.iloc[-1].copy()
        latest["prev_open_time"] = frame.iloc[-2]["open_time"] - 3_600_000
        latest["prev_close_time"] = frame.iloc[-2]["close_time"] - 3_600_000
        result = data_integrity.aligned_relative_strength(
            latest, data_integrity.build_return_series(frame, "1h"), 2.0,
        )
        self.assertEqual(result, (None, None, "BTC_ALIGNMENT_MISSING"))

    def test_momentum_features_do_not_assume_zero_btc_return(self):
        features = feature_engine.extract_1h_momentum_features(candles(interval="1h"))
        self.assertIsNone(features["TESTUSDT"]["rs_1h"])
        self.assertEqual(features["TESTUSDT"]["rs_1h_status"], "BTC_ALIGNMENT_MISSING")

    def test_stale_btc_produces_no_return_and_keeps_diagnostics(self):
        with tempfile.TemporaryDirectory(prefix="kripikto-btc-test-") as folder:
            db = Path(folder) / "test.db"
            seed_database(db)
            issues = {}
            returns = data_pipeline.get_btc_benchmark_time_series(
                "4h", str(db), now_ms=NOW_MS + 28_800_000, quality_issues=issues,
            )
            self.assertEqual(returns, {})
            self.assertIn("STALE_CANDLES", issues["BTCUSDT"])
            benchmark = data_pipeline.get_btc_benchmark(str(db), now_ms=NOW_MS + 28_800_000)
            self.assertIsNone(benchmark["btc_return_4h"])
            gc.collect()

    def test_legacy_database_migration_preserves_unknown_history(self):
        with tempfile.TemporaryDirectory(prefix="kripikto-legacy-test-") as folder:
            db = Path(folder) / "test.db"
            with contextlib.closing(sqlite3.connect(db)) as conn:
                conn.execute("CREATE TABLE klines_history (symbol TEXT, interval TEXT, open_time INTEGER)")
                conn.execute("INSERT INTO klines_history VALUES ('OLDUSDT','4h',0)")
                conn.commit()
            data_pipeline.init_db(str(db))
            data_pipeline.init_db(str(db))
            with contextlib.closing(sqlite3.connect(db)) as conn:
                self.assertEqual(conn.execute(
                    "SELECT symbol,is_closed,close_time,fetched_at FROM klines_history"
                ).fetchone(), ("OLDUSDT", 0, None, None))
            gc.collect()


if __name__ == "__main__":
    unittest.main()
