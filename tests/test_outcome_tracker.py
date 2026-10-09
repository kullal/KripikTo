import datetime
import inspect
import unittest
from unittest.mock import patch

from backend import outcome_tracker


def candle(hour, low=99.0, high=102.0, close=101.0):
    timestamp = datetime.datetime(2026, 9, 30, tzinfo=datetime.timezone.utc)
    timestamp += datetime.timedelta(hours=hour)
    open_time = int(timestamp.timestamp() * 1000)
    return {
        "open_time": open_time, "close_time": open_time + 3_600_000 - 1,
        "datetime_utc": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        "open": close, "close": close, "low": low, "high": high, "volume": 100.0,
    }


class OutcomeTrackerTests(unittest.TestCase):
    def evaluate(self, candles, overrides=None, **kwargs):
        signal = {
            "scan_time": "2026-09-30 01:00:00", "symbol": "TESTUSDT",
            "buy_low": 99.0, "buy_high": 101.0, "entry_price": 100.0,
            "stop_loss": 95.0, "tp1": 110.0, "tp2": 120.0,
        }
        signal.update(overrides or {})
        with patch.object(outcome_tracker, "fetch_subsequent_klines", return_value=candles):
            return outcome_tracker.evaluate_single_signal(signal, sim_interval="1h",
                costs=outcome_tracker.Costs(0, 0), fill_timeout_hours=24, **kwargs)

    def test_touching_buy_area_does_not_fill_unreached_entry(self):
        result = self.evaluate([candle(1, low=100.8, high=103.0)])
        self.assertEqual(result["is_filled"], 0)
        self.assertEqual(result["fill_price"], 0.0)
        self.assertEqual(result["result"], "PENDING")

    def test_gap_below_entry_does_not_invent_fill_at_entry(self):
        result = self.evaluate([candle(1, low=90.0, high=94.0, close=92.0)])
        self.assertEqual(result["is_filled"], 0)

    def test_entry_touch_records_declared_price(self):
        result = self.evaluate([candle(1)])
        self.assertEqual(result["is_filled"], 1)
        self.assertEqual(result["fill_price"], 100.0)

    def test_legacy_missing_entry_uses_declared_area_midpoint(self):
        result = self.evaluate([candle(1)], {"entry_price": None})
        self.assertEqual(result["fill_price"], 100.0)

    def test_fill_and_any_exit_touch_same_candle_is_ambiguous(self):
        for low, high in [(99.0, 111.0), (94.0, 102.0), (94.0, 111.0)]:
            with self.subTest(low=low, high=high):
                result = self.evaluate([candle(1, low=low, high=high)])
                self.assertEqual(result["result"], "AMBIGUOUS")

    def test_pending_fill_is_replayed_without_pre_fill_price_moves(self):
        result = self.evaluate([
            candle(1, low=101.5, high=130.0, close=110.0),
            candle(2),
            candle(3, high=111.0, close=110.0),
        ], {
            "is_filled": 1, "fill_time": "2026-09-30 02:00:00",
            "fill_price": 100.0, "mfe_pct": 999.0, "mae_pct": -99.0,
        })
        self.assertEqual(result["result"], "TP1_HIT")
        self.assertEqual(result["fill_time"], "2026-09-30 02:00:00")
        self.assertEqual(result["mfe_pct"], 11.0)
        self.assertEqual(result["mae_pct"], -1.0)

    def test_stop_after_fill_is_recorded(self):
        result = self.evaluate([candle(1), candle(2, low=94.0, close=96.0)])
        self.assertEqual(result["result"], "SL_HIT")

    def test_timeout_excludes_price_moves_after_six_hour_deadline(self):
        result = self.evaluate([candle(1)] + [candle(h, low=100, high=100, close=100) for h in range(2, 7)]
                               + [candle(7, high=121.0)])
        self.assertEqual(result["result"], "TIMEOUT")
        self.assertEqual(result["duration_hours"], 6.0)
        self.assertEqual(result["mfe_pct"], 0.0)

    def test_limit_expiry_is_checked_before_accepting_fill(self):
        result = self.evaluate([candle(h, low=101, high=103, close=102) for h in range(1, 25)] + [candle(25)])
        self.assertEqual(result["result"], "UNFILLED")
        self.assertEqual(result["is_filled"], 0)

    def test_candle_before_scan_cannot_fill_order(self):
        result = self.evaluate([candle(0)], {"scan_time": "2026-09-30 00:30:00"})
        self.assertEqual(result["is_filled"], 0)

    def test_runner_uses_six_hour_default(self):
        parameter = inspect.signature(outcome_tracker.run_outcome_tracker).parameters["timeout_hours"]
        self.assertEqual(parameter.default, 6.0)


if __name__ == "__main__":
    unittest.main()
