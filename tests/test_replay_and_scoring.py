import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from backend import calibration_lab, feature_engine, outcome_tracker, scanner
from backend.replay import Costs, REPLAY_VERSION, replay_trade, summarize, timestamp_ms
from backend.replay_store import save_candles, load_candles
from backend.scoring import BASE_WEIGHTS, SCORING_VERSION, score_snapshot
from backend.strategy_comparison import compare_signal, run_comparison

START = "2026-09-01 00:00:00"
START_MS = timestamp_ms(START)


def candle(index, o=100, h=101, l=99, c=100):
    t = START_MS + index * 900000
    return dict(open_time=t, close_time=t + 899999, open=o, high=h, low=l, close=c, volume=100)


def signal(**updates):
    return dict(dict(scan_time=START, symbol="TESTUSDT", entry_price=100, stop_loss=95, tp1=105,
                     tp2=110, entry_status="TRIGGERED", data_quality_status="OK",
                     scoring_version=SCORING_VERSION, trigger_type="BREAKOUT", breakout_level=101,
                     atr14=2, support_level=98, structure_score=20, momentum_score=24,
                     flow_score=16, derivative_score=12, macro_score=5, score_penalty=0), **updates)


class ReplayTests(unittest.TestCase):
    def test_two_sided_fees_and_market_slippage(self):
        rows = [candle(i) for i in range(25)]
        result = replay_trade(signal(), rows, entry_mode="market", costs=Costs(10, 5))
        self.assertEqual(result["result"], "TIMEOUT")
        self.assertAlmostEqual(result["fill_price"], 100.05)
        self.assertAlmostEqual(result["exit_price"], 99.95)
        expected = (99.95 * .999 / (100.05 * 1.001) - 1) * 100
        self.assertAlmostEqual(result["net_return_pct"], expected)

    def test_limit_never_fills_above_limit(self):
        r = replay_trade(signal(), [candle(0), candle(1, h=106)], costs=Costs(10, 5))
        self.assertEqual(r["fill_price"], 100)
        self.assertEqual(r["exit_price"], 105)

    def test_gap_stop_uses_open_not_stop_price(self):
        r = replay_trade(signal(), [candle(0), candle(1, o=90, l=89, h=92, c=91)], costs=Costs(0, 0))
        self.assertEqual(r["result"], "SL_HIT")
        self.assertEqual(r["exit_price"], 90)

    def test_both_exit_touches_have_no_invented_pnl(self):
        r = replay_trade(signal(), [candle(0), candle(1, h=106, l=94)])
        self.assertEqual(r["result"], "AMBIGUOUS")
        self.assertIsNone(r["exit_price"])
        self.assertIsNone(r["net_return_pct"])

    def test_limit_fill_and_exit_same_bar_is_ambiguous(self):
        self.assertEqual(replay_trade(signal(), [candle(0, h=106)])["result"], "AMBIGUOUS")

    def test_market_entry_at_open_can_exit_in_same_candle(self):
        self.assertEqual(replay_trade(signal(), [candle(0, h=106)], entry_mode="market")["result"], "TP1_HIT")

    def test_missing_duplicate_unordered_or_invalid_candles_fail_closed(self):
        variants = [[candle(1)], [candle(0), candle(2)], [candle(0), candle(0)],
                    [candle(1), candle(0)], [candle(0, l=102)],
                    [dict(candle(0), is_closed=0)]]
        for rows in variants:
            with self.subTest(rows=rows):
                r = replay_trade(signal(), rows)
                self.assertEqual(r["result"], "DATA_UNAVAILABLE")
                self.assertIsNone(r["net_return_pct"])

    def test_timeout_at_open_ignores_later_candle_spike(self):
        rows = [candle(i) for i in range(24)] + [candle(24, o=102, h=200, l=90, c=100)]
        r = replay_trade(signal(), rows, costs=Costs(0, 0))
        self.assertEqual(r["result"], "TIMEOUT")
        self.assertEqual(r["exit_price"], 102)

    def test_limit_expiry_precedes_fill(self):
        rows = [candle(i, o=102, l=101, h=103, c=102) for i in range(8)] + [candle(8)]
        self.assertEqual(replay_trade(signal(), rows)["result"], "UNFILLED")

    def test_intrabar_expiry_does_not_invent_fill_before_deadline(self):
        rows = [candle(i, o=102, l=101, h=103, c=102) for i in range(1, 8)] + [candle(8)]
        r = replay_trade(signal(scan_time="2026-09-01 00:01:00"), rows)
        self.assertEqual(r["result"], "AMBIGUOUS")
        self.assertEqual(r["is_filled"], 0)

    def test_forward_returns_are_independent_of_early_exit(self):
        rows = [candle(i) for i in range(48)]
        rows[1] = candle(1, h=106)
        rows[-1] = candle(47, o=109, h=111, l=108, c=110)
        r = replay_trade(signal(), rows, costs=Costs(0, 0))
        self.assertEqual(r["result"], "TP1_HIT")
        self.assertAlmostEqual(r["return_12h_pct"], 10)

    def test_timezone_offsets_are_converted(self):
        self.assertEqual(timestamp_ms("2026-09-01T07:00:00+07:00"), START_MS)

    def test_net_win_rate_includes_profitable_timeout(self):
        r = summarize([dict(result="TIMEOUT", is_filled=1, net_return_pct=1),
                       dict(result="AMBIGUOUS", is_filled=1, net_return_pct=None)])
        self.assertEqual(r["completed"], 1)
        self.assertEqual(r["win_rate"], 100)
        self.assertIsNone(r["profit_factor"])

    def test_invalid_costs_and_timeouts_rejected(self):
        for value in (-1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                Costs(value, 0)
        with self.assertRaises(ValueError):
            replay_trade(signal(), [], trade_timeout_hours=0)


class ScoringCalibrationTests(unittest.TestCase):
    def test_weight_scales_contribution_instead_of_clipping_raw_score(self):
        weights = {**BASE_WEIGHTS, "derivatives": 20, "flow": 15}
        original = signal()
        total, components = score_snapshot(original, weights)
        self.assertEqual(components["derivatives"], 16)
        self.assertAlmostEqual(total, 72)
        self.assertEqual(original["derivative_score"], 12)

    def test_news_budget_is_reserved_and_modifiers_are_explicit(self):
        full = signal(structure_score=25, momentum_score=30, flow_score=20, derivative_score=15)
        self.assertEqual(score_snapshot(full)[0], 90)
        self.assertEqual(score_snapshot(dict(full, score_penalty=15))[0], 75)
        with self.assertRaises(ValueError):
            score_snapshot(full, {**BASE_WEIGHTS, "structure": 100})

    def test_calibration_uses_same_score_and_net_results(self):
        df = pd.DataFrame([signal(net_return_pct=2, result="TP1_HIT"),
                           signal(net_return_pct=-1, result="SL_HIT")])
        metrics = calibration_lab._evaluate_weight_set(df, BASE_WEIGHTS)
        self.assertEqual(metrics["cutoff"], score_snapshot(signal())[0])
        self.assertEqual(metrics["objective"], .5)

    def test_calibration_replays_order_instead_of_mfe_mae(self):
        up_first = [candle(0), candle(1, h=105), candle(2, l=96)]
        down_first = [candle(0), candle(1, l=96), candle(2, h=105)]
        wins = calibration_lab.evaluate_configuration_dynamic_atr(
            pd.DataFrame([signal(forward_candles=up_first)]), 1.6, 1)
        losses = calibration_lab.evaluate_configuration_dynamic_atr(
            pd.DataFrame([signal(forward_candles=down_first)]), 1.6, 1)
        self.assertGreater(wins["expectancy"], 0)
        self.assertLess(losses["expectancy"], 0)
        with self.assertRaises(ValueError):
            calibration_lab.simulate_trade_outcome(5, -3.3, 4, -3)

    def test_split_groups_timestamps_and_purges_label_overlap(self):
        times = pd.date_range(START, periods=50, freq="12h")
        df = pd.DataFrame([dict(scan_time=str(t), symbol=s) for t in times for s in ("A", "B")])
        train, test = calibration_lab.split_train_test(df)
        self.assertFalse(train.empty)
        self.assertTrue(set(train.scan_time).isdisjoint(test.scan_time))
        self.assertLess(pd.Timestamp(train.scan_time.max()) + pd.Timedelta(hours=8.25), pd.Timestamp(test.scan_time.min()))

    def test_unvalidated_legacy_config_is_not_loaded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps({"weights": {"structure": 999}}))
            with patch.object(feature_engine, "CONFIG_PATH", path):
                self.assertEqual(feature_engine.load_calibrated_config(), {})

    def test_failed_validation_cannot_overwrite_config(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text("sentinel")
            with self.assertRaises(ValueError):
                calibration_lab.save_calibrated_config({}, BASE_WEIGHTS,
                    {"replay_version": REPLAY_VERSION, "total_trades": 30, "profit_factor": .76, "expectancy": -.18}, {}, str(path))
            self.assertEqual(path.read_text(), "sentinel")


class StrategyComparisonTests(unittest.TestCase):
    def path(self):
        return [candle(i, o=102, h=103, l=101.5, c=102) for i in range(33)]

    def test_retest_requires_confirmation_and_enters_next_open(self):
        rows = self.path()
        rows[1] = candle(1, o=101.3, h=102, l=100.8, c=101.8)
        rows[2] = candle(2, o=102, h=106, l=101.5, c=105)
        arms = compare_signal(signal(), rows, Costs(0, 0))
        self.assertEqual(arms["BREAKOUT"]["fill_time"], START)
        self.assertEqual(arms["BREAKOUT_RETEST"]["fill_time"], "2026-09-01 00:30:00")
        self.assertEqual(arms["BREAKOUT_RETEST"]["fill_price"], 102)

    def test_no_retest_counts_missed_entry_in_same_cohort(self):
        arms = compare_signal(signal(), self.path())
        self.assertEqual(arms["BREAKOUT"]["result"], "TIMEOUT")
        self.assertEqual(arms["BREAKOUT_RETEST"]["result"], "UNFILLED")

    def test_failed_level_cannot_later_be_called_successful_retest(self):
        rows = self.path()
        rows[1] = candle(1, o=102, h=103, l=99, c=100)
        arms = compare_signal(signal(), rows)
        self.assertEqual(arms["BREAKOUT_RETEST"]["result"], "ENTRY_INVALIDATED")

    def test_incomplete_window_excludes_both_arms(self):
        arms = compare_signal(signal(), self.path()[:-1])
        self.assertTrue(all(r["result"] == "DATA_UNAVAILABLE" for r in arms.values()))

    def test_near_ma_alone_is_not_retest(self):
        rows = pd.DataFrame([dict(symbol="X", open=100, high=101, low=99, close=100, volume=100) for _ in range(40)])
        result = feature_engine.evaluate_15m_trigger(rows)["X"]
        self.assertEqual(result["trigger_status"], "WAIT")
        self.assertEqual(result["trigger_type"], "NONE")

    def test_scanner_detects_actual_breakout_then_retest(self):
        rows = [dict(symbol="X", open=p, high=p+.5, low=p-.5, close=p, volume=100)
                for p in [99, 100] * 20]
        rows[-2].update(open=100, high=101.5, low=99.9, close=101.2, volume=400)
        rows[-1].update(open=100.7, high=101.2, low=100.4, close=101)
        result = feature_engine.evaluate_15m_trigger(pd.DataFrame(rows))["X"]
        self.assertEqual(result["trigger_type"], "BREAKOUT_RETEST")
        self.assertEqual(result["breakout_level"], 100.5)
        rows[-1].update(open=100.7, high=101.2, low=99.8, close=100)
        rows.append(dict(symbol="X", open=100.7, high=101.2, low=100.4, close=101, volume=100))
        failed = feature_engine.evaluate_15m_trigger(pd.DataFrame(rows))["X"]
        self.assertEqual(failed["trigger_type"], "NONE")


class PersistenceTests(unittest.TestCase):
    def test_tracker_persists_costs_exit_and_refreshes_early_completed_path(self):
        with tempfile.TemporaryDirectory() as folder:
            db = str(Path(folder) / "test.db")
            scanner.init_scanner_db(db)
            current = signal()
            with contextlib.closing(sqlite3.connect(db)) as conn:
                keys = list(current)
                conn.execute("INSERT INTO scan_results (" + ",".join(keys) + ") VALUES (" + ",".join("?" for _ in keys) + ")", list(current.values()))
                conn.commit()
            early = [candle(0), candle(1, h=106)]
            later = early + [candle(i) for i in range(2, 200)]
            with patch.object(outcome_tracker, "fetch_subsequent_klines", side_effect=[early, later]) as fetch, contextlib.redirect_stdout(io.StringIO()):
                first = outcome_tracker.run_outcome_tracker(db_path=db)
                second = outcome_tracker.run_outcome_tracker(db_path=db)
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(first.iloc[0]["result"], "TP1_HIT")
            self.assertEqual(second.iloc[0]["replay_version"], REPLAY_VERSION)
            self.assertIsNotNone(second.iloc[0]["return_48h_pct"])
            self.assertGreater(second.iloc[0]["net_return_pct"], 0)
            with contextlib.closing(sqlite3.connect(db)) as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM replay_candles").fetchone()[0], 200)

    def test_cache_and_comparison_are_read_only_and_versioned(self):
        with tempfile.TemporaryDirectory() as folder:
            db = str(Path(folder) / "test.db")
            scanner.init_scanner_db(db)
            current = signal()
            with contextlib.closing(sqlite3.connect(db)) as conn:
                keys = list(current)
                conn.execute("INSERT INTO scan_results (" + ",".join(keys) + ") VALUES (" + ",".join("?" for _ in keys) + ")", list(current.values()))
                conn.commit()
            save_candles(db, "TESTUSDT", "15m", StrategyComparisonTests().path())
            before = Path(db).read_bytes()
            report = run_comparison(db)
            self.assertEqual(report["sample_pairs"], 1)
            self.assertEqual(report["summary"]["BREAKOUT"]["completed"], 1)
            self.assertEqual(Path(db).read_bytes(), before)
            dataset = calibration_lab.load_dataset(db)
            self.assertEqual(len(dataset), 1)
            self.assertEqual(len(dataset.iloc[0].forward_candles), 33)

    def test_tracker_excludes_wait_extended_partial_and_explicit_blocks(self):
        for updates in ({"entry_status": "WAIT"}, {"entry_status": "EXTENDED"},
                        {"data_quality_status": "PARTIAL"}, {"execution_eligible": 0}):
            self.assertFalse(outcome_tracker.is_trade_eligible(signal(**updates)))
        self.assertTrue(outcome_tracker.is_trade_eligible(signal()))


if __name__ == "__main__":
    unittest.main()
