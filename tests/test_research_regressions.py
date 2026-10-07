import contextlib
import datetime
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import pandas as pd

from backend import calibration_lab as lab
from backend import derivatives_flow as derivatives
from backend import feature_engine as features
from backend import news_sentiment as news
from backend import outcome_tracker as tracker
from backend import scanner, data_pipeline
from backend.research_utils import (signal_id, net_return, database, cache_forward_candles,
    read_forward_candles, complete_forward_window, portfolio_metrics)

UTC = datetime.timezone.utc
START = datetime.datetime(2026, 6, 1, tzinfo=UTC)


def path(scan_time=START, events=None):
    result = []
    for hour in range(32):
        timestamp = scan_time + datetime.timedelta(hours=hour)
        candle = dict(open_time=int(timestamp.timestamp() * 1000),
            close_time=int(timestamp.timestamp() * 1000) + 3_599_999,
            datetime_utc=timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            open=100.0, high=101.0, low=99.0, close=100.0, volume=100)
        candle.update((events or {}).get(hour, {}))
        result.append(candle)
    return result


def candidate(scan_time=START, symbol="TESTUSDT", events=None):
    return dict(scan_time=scan_time.strftime("%Y-%m-%d %H:%M:%S"), symbol=symbol,
        entry_price=100.0, support_level=100.0, last_price=100.0, atr14=4.0,
        buy_low=99.0, buy_high=101.0, buy_area="$99 - $101",
        stop_loss=95.2, tp1=106.4, tp2=112.0, stop_loss_pct=-4.8,
        tp1_pct=6.4, tp2_pct=12.0, risk_reward="1 : 1.3", score=90.0,
        structure_score=25, momentum_score=30, flow_score=20, derivative_score=15,
        v1_score=90, v2_score=90, score_penalty=0, taker_buy_ratio=56,
        quote_vol_m=10, funding_rate=0, open_interest_m=10,
        entry_status="TRIGGERED", setup_type="MOMENTUM_RUNNER", data_quality_status="OK",
        scoring_version=features.SCORING_VERSION,
        weights_json=json.dumps(features.BASE_WEIGHTS), forward_candles=path(scan_time, events),
        label_end=scan_time + datetime.timedelta(hours=32))


class ExecutionResearchTests(unittest.TestCase):
    def test_counterfactual_replays_past_the_original_exit(self):
        row = candidate(events={1: {"high": 107}, 3: {"high": 111}})
        old = tracker.evaluate_single_signal(dict(row), candles=row["forward_candles"])
        self.assertEqual(old["result_time"], "2026-06-01 01:00:00")
        new = lab.evaluate_configuration_dynamic_atr(pd.DataFrame([row]), 2.5, 1.25)
        expected = net_return(100, 110) * 0.20
        self.assertAlmostEqual(new["portfolio_return_pct"], expected)

    def test_identical_extrema_with_different_order_have_different_outcomes(self):
        tp_first = candidate(events={1: {"high": 107}, 2: {"low": 94}})
        sl_first = candidate(events={1: {"low": 94}, 2: {"high": 107}})
        a = lab.evaluate_configuration_dynamic_atr(pd.DataFrame([tp_first]), 1.6, 1.25)
        b = lab.evaluate_configuration_dynamic_atr(pd.DataFrame([sl_first]), 1.6, 1.25)
        self.assertGreater(a["portfolio_return_pct"], 0)
        self.assertLess(b["portfolio_return_pct"], 0)

    def test_extrema_only_simulation_is_rejected(self):
        with self.assertRaises(ValueError):
            lab.simulate_trade_outcome(6, -3, 5, -2.8)

    def test_ambiguous_has_no_invented_return(self):
        row = candidate(events={1: {"high": 107, "low": 94}})
        result = tracker.evaluate_single_signal(dict(row), candles=row["forward_candles"])
        self.assertEqual(result["result"], "AMBIGUOUS")
        self.assertIsNone(result["exit_price"])
        self.assertIsNone(result["net_return_pct"])
        metrics = lab.evaluate_configuration_dynamic_atr(pd.DataFrame([row]), 1.6, 1.25)
        self.assertFalse(metrics["eligible_for_selection"])
        self.assertIsNone(metrics["portfolio_return_pct"])

    def test_timeout_uses_deadline_open_and_accounts_for_costs(self):
        row = candidate(events={6: {"open": 102, "high": 200, "low": 1, "close": 150}})
        outcome = tracker.evaluate_single_signal(row, candles=row["forward_candles"])
        self.assertEqual(outcome["result"], "TIMEOUT")
        self.assertEqual(outcome["exit_price"], 102)
        self.assertAlmostEqual(outcome["net_return_pct"], net_return(100, 102))
        self.assertEqual(outcome["mfe_pct"], 1)

    def test_stop_gap_uses_worse_open(self):
        row = candidate(events={1: {"open": 90, "high": 92, "low": 89, "close": 91}})
        outcome = tracker.evaluate_single_signal(row, candles=row["forward_candles"])
        self.assertEqual(outcome["exit_price"], 90)

    def test_custom_timeout_extends_cache_beyond_research_horizon(self):
        row = candidate()
        candles = path() + path(START + datetime.timedelta(hours=32))[:2]
        for c in candles[:23]:
            c.update(open=102, high=103, low=101, close=102)
        with tempfile.TemporaryDirectory() as folder:
            db = str(Path(folder) / "test.db")
            cache_forward_candles(db, row, candles, horizon_hours=33)
            with database(db) as conn:
                cached = read_forward_candles(conn, row["scan_time"], row["symbol"])
            self.assertEqual(len(cached), 34)
            self.assertTrue(complete_forward_window(row["scan_time"], cached, horizon_hours=33))
            result = tracker.evaluate_single_signal(row, trade_timeout_hours=10, candles=cached)
            self.assertEqual(result["fill_time"], "2026-06-01 23:00:00")
            self.assertEqual(result["result_time"], "2026-06-02 09:00:00")
            self.assertEqual(result["result"], "TIMEOUT")

    def test_calibrated_tp2_cannot_fall_below_tp1(self):
        plan = features.calculate_dynamic_tp_sl(100, 100, 4, config={"tp_atr_multiplier": 4})
        self.assertGreaterEqual(plan["tp2"], plan["tp1"])

    def test_full_exit_at_tp1_cannot_claim_tp2_from_the_same_high(self):
        row = candidate(events={1: {"high": 130}})
        outcome = tracker.evaluate_single_signal(row, candles=row["forward_candles"])
        self.assertEqual(outcome["result"], "TP1_HIT")
        self.assertEqual(outcome["exit_price"], 106.4)

    def test_invalid_costs_are_rejected(self):
        for cost in (-1, float("nan"), 100):
            with self.assertRaises(ValueError):
                net_return(100, 101, {"fee_pct": cost})

    def test_complete_cache_retains_prices_after_original_exit_and_is_idempotent(self):
        row = candidate(events={1: {"high": 107}, 20: {"high": 120}})
        with tempfile.TemporaryDirectory() as folder:
            db = str(Path(folder) / "test.db")
            for _ in range(2):
                cache_forward_candles(db, row, row["forward_candles"])
            with database(db) as conn:
                candles = read_forward_candles(conn, row["scan_time"], row["symbol"])
            self.assertEqual(len(candles), 32)
            self.assertEqual(candles[20]["high"], 120)
            self.assertTrue(complete_forward_window(row["scan_time"], candles))
            self.assertFalse(complete_forward_window(row["scan_time"], candles[:20]))
            self.assertFalse(complete_forward_window(row["scan_time"], candles[:10] + candles[11:]))

    def test_finite_capital_skips_overlapping_orders(self):
        row = dict(scan_time="2026-06-01 00:00:00", result_time="2026-06-01 06:00:00",
                   is_filled=1, net_return_pct=10, selection_score=90)
        metrics = portfolio_metrics([dict(row, symbol=str(i)) for i in range(6)])
        self.assertEqual(metrics["executed_trades"], 5)
        self.assertEqual(metrics["capacity_skipped"], 1)
        self.assertAlmostEqual(metrics["portfolio_return_pct"], 10)

    def test_unfilled_releases_capital_without_becoming_a_completed_trade(self):
        row = dict(scan_time="2026-06-01 00:00:00", result_time="2026-06-02 00:00:00",
                   symbol="TEST", is_filled=0, net_return_pct=0)
        result = portfolio_metrics([row])
        self.assertEqual(result["executed_trades"], 0)
        self.assertEqual(result["portfolio_return_pct"], 0)


class CalibrationConsistencyTests(unittest.TestCase):
    def test_weights_rescale_scores_without_changing_setup_units(self):
        weights = dict(structure=35, momentum=30, flow=10, derivatives=15, news=10)
        score = features.compose_quant_score(dict(structure=25, momentum=30, flow=20, derivatives=15), weights)
        self.assertEqual(score, 90)
        setup, status, _ = features.classify_setup_and_status(25, 30, 20, 1.3, 3, 2)
        self.assertEqual((setup, status), ("MOMENTUM_RUNNER", "READY"))

    def test_news_is_a_single_weighted_pillar_and_unknown_adds_nothing(self):
        self.assertEqual(news.calculate_unified_v2_final_score(90, 1), 100)
        self.assertEqual(news.calculate_unified_v2_final_score(90, None), 90)
        self.assertEqual(news.calculate_unified_v2_final_score(80, 1, news_weight=20), 100)
        self.assertEqual(news.calculate_unified_v2_final_score(90, 1, True), 35)

    def test_invalid_weights_fail_explicitly(self):
        with self.assertRaises(ValueError):
            features.compose_quant_score({}, dict(features.BASE_WEIGHTS, news=11))

    def test_timestamp_groups_stay_together_and_overlapping_labels_are_purged(self):
        rows = [candidate(START + datetime.timedelta(hours=h), str(symbol))
                for h in range(80) for symbol in range(2)]
        train, test = lab.split_train_test(pd.DataFrame(rows))
        self.assertFalse(train.empty)
        self.assertTrue(set(train.scan_time).isdisjoint(set(test.scan_time)))
        self.assertLess(train.label_end.max(), pd.to_datetime(test.scan_time, utc=True).min())

    def test_train_validation_test_and_walk_forward_are_disjoint(self):
        rows = [candidate(START + datetime.timedelta(days=d)) for d in range(90)]
        df = pd.DataFrame(rows)
        train, validation, test = lab.split_train_validation_test(df)
        self.assertLess(train.label_end.max(), pd.to_datetime(validation.scan_time, utc=True).min())
        self.assertLess(validation.label_end.max(), pd.to_datetime(test.scan_time, utc=True).min())
        self.assertEqual(len(lab.walk_forward_splits(df)), 3)

    def test_missing_forward_path_cannot_be_calibrated(self):
        row = candidate()
        row.pop("forward_candles")
        with self.assertRaises(ValueError):
            lab.evaluate_configuration_dynamic_atr(pd.DataFrame([row]), 1.6, 1.25)

    def test_saved_plan_is_exactly_the_evaluated_plan(self):
        row = candidate(events={1: {"high": 110}})
        metrics = lab.evaluate_configuration_dynamic_atr(pd.DataFrame([row]), 1.8, 1.4)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "config.json"
            lab.save_calibrated_config(metrics, features.BASE_WEIGHTS, metrics, metrics, str(output))
            config = json.loads(output.read_text())
        for key, value in metrics["plan"].items():
            self.assertEqual(config[key], value)
        self.assertEqual(config["target_profit_1_pct"], 4.5)
        self.assertEqual(config["stop_loss_pct"], -4.8)

    def test_unknown_out_of_sample_result_cannot_be_applied(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "config.json"
            with self.assertRaises(ValueError):
                lab.save_calibrated_config({}, features.BASE_WEIGHTS, {}, {}, str(output))
            self.assertFalse(output.exists())

    def test_calibration_smoke_freezes_plan_and_weights_before_test(self):
        rows = [candidate(START + datetime.timedelta(days=d), events={1: {"high": 115}})
                for d in range(90)]
        with patch.object(lab, "load_dataset", return_value=pd.DataFrame(rows)), contextlib.redirect_stdout(io.StringIO()):
            report = lab.run_calibration_lab()
        self.assertTrue(report["test"]["eligible_for_selection"])
        self.assertEqual(report["test"]["plan"], report["plan"])
        self.assertEqual(set(report["ab_same_candidate_pool"]), {"v1", "v2"})
        self.assertEqual(len(report["walk_forward"]), 3)

    def test_dataset_requires_complete_new_version_canonical_scan(self):
        row = candidate(events={1: {"high": 110}})
        with tempfile.TemporaryDirectory() as folder:
            db = str(Path(folder) / "scans.db")
            data_pipeline.init_db(db)
            scanner.init_scanner_db(db)
            tracker.init_outcome_tracker_db(db)
            with database(db) as conn:
                columns = {r[1] for r in conn.execute("PRAGMA table_info(scan_results)")}
                fields = {k: v for k, v in row.items() if k in columns}
                names = ",".join(fields)
                placeholders = ",".join("?" for _ in fields)
                conn.execute(f"INSERT INTO scan_results ({names}) VALUES ({placeholders})", tuple(fields.values()))
                conn.execute("INSERT INTO signal_outcomes (scan_time,symbol,entry_status) VALUES (?,?,?)",
                             (row["scan_time"], row["symbol"], "TRIGGERED"))
            self.assertTrue(lab.load_dataset(db).empty)
            cache_forward_candles(db, row, row["forward_candles"])
            self.assertEqual(len(lab.load_dataset(db)), 1)
            with database(db) as conn:
                conn.execute("UPDATE scan_results SET scoring_version=NULL")
            self.assertTrue(lab.load_dataset(db).empty)

    def test_tracker_migrates_caches_fixed_window_and_skips_watchlist(self):
        row = candidate(events={1: {"high": 110}})
        with tempfile.TemporaryDirectory() as folder:
            db = str(Path(folder) / "scans.db")
            data_pipeline.init_db(db)
            scanner.init_scanner_db(db)
            tracker.init_outcome_tracker_db(db)
            with database(db) as conn:
                columns = {r[1] for r in conn.execute("PRAGMA table_info(scan_results)")}
                fields = {k: v for k, v in row.items() if k in columns}
                for symbol, status in (("TESTUSDT", "TRIGGERED"), ("WATCHUSDT", "WAIT")):
                    fields.update(symbol=symbol, entry_status=status)
                    conn.execute(f"INSERT INTO scan_results ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", tuple(fields.values()))
            with patch.object(tracker, "fetch_subsequent_klines", return_value=row["forward_candles"]) as fetch, contextlib.redirect_stdout(io.StringIO()):
                result = tracker.run_outcome_tracker(db_path=db)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(len(result), 1)
            self.assertEqual(result.iloc[0].forward_complete, 1)
            self.assertIsNotNone(result.iloc[0].net_return_pct)
            self.assertEqual(result.iloc[0].replay_status, "VALID")
            with database(db) as conn:
                self.assertEqual(len(read_forward_candles(conn, row["scan_time"], row["symbol"])), 32)

    def test_legacy_news_json_cannot_create_duplicate_order(self):
        with tempfile.TemporaryDirectory() as folder:
            db = str(Path(folder) / "scans.db")
            output = Path(folder) / "final.json"
            data_pipeline.init_db(db)
            scanner.init_scanner_db(db)
            output.write_text(json.dumps([dict(scan_time="2026-06-01 01:00:00", symbol="TESTUSDT", buy_area="$99 - $101")]))
            with patch.object(tracker, "RECOMMENDATIONS_JSON", str(output)):
                self.assertEqual(tracker.import_signals_from_history(db), 0)
            with database(db) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM signal_outcomes").fetchone()[0], 0)


class NewsIntegrityTests(unittest.TestCase):
    def test_final_report_handles_unknown_news_and_missing_funding(self):
        row = dict(candidate(), **news.unavailable_sentiment("NO_NEWS", "Tidak ada berita relevan."),
                   funding_rate=None, final_score=90,
                   recommendation="NEWS_UNVERIFIED (Risk Guard belum tersedia)")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            news.print_final_executive_report([row])
        self.assertIn("UNKNOWN (N/A)", output.getvalue())
        self.assertIn("NEWS_UNVERIFIED", output.getvalue())
        self.assertNotIn("Target Jual Otomatis", output.getvalue())

    def test_final_report_preserves_valid_neutral_score(self):
        row = dict(candidate(), **self.valid(), final_score=95, recommendation="TRIGGERED_BUY")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            news.print_final_executive_report([row])
        self.assertIn("NEUTRAL (+0.0)", output.getvalue())

    def valid(self):
        return dict(sentiment="NEUTRAL", sentiment_score=0.0, impact_level="RENDAH",
                    catalyst="Tidak ada katalis kuat.", crypto_risk=False)

    def test_boolean_string_and_nonfinite_score_are_rejected(self):
        for override in ({"crypto_risk": "false"}, {"sentiment_score": float("nan")},
                         {"sentiment_score": 2}, {"sentiment": "UNKNOWN"}):
            with self.assertRaises(ValueError):
                news.validate_sentiment_response(dict(self.valid(), **override))

    def test_inconsistent_risk_is_rejected(self):
        with self.assertRaises(ValueError):
            news.validate_sentiment_response(dict(self.valid(), crypto_risk=True))

    def test_no_news_and_fetch_failure_are_distinct_unknown_states(self):
        self.assertEqual(news.analyze_sentiment_with_gemini("TESTUSDT", [], "key")["news_status"], "NO_NEWS")
        value = news.analyze_sentiment_with_gemini("TESTUSDT", news.NewsItems(status="FETCH_FAILED"), "key")
        self.assertEqual(value["news_status"], "FETCH_FAILED")
        self.assertIsNone(value["crypto_risk"])
        self.assertIsNone(value["sentiment_score"])

    def test_invalid_model_schema_is_failure_not_neutral(self):
        response = Mock(status_code=200)
        response.json.return_value = {"candidates": [{"content": {"parts": [{"text": json.dumps(dict(self.valid(), crypto_risk="false"))}]}}]}
        items = [{"source": "Media", "title": "TEST upgrade", "pub_date": "today"}]
        with patch.object(news.requests, "post", return_value=response), patch.object(news.time, "sleep"):
            value = news.analyze_sentiment_with_gemini("TESTUSDT", items, "key")
        self.assertEqual(value["news_status"], "MODEL_FAILED")
        self.assertIsNone(value["sentiment_score"])

    def test_model_quota_failure_has_a_safe_diagnostic(self):
        items = [{"source": "Media", "title": "TEST upgrade", "pub_date": "today"}]
        output = io.StringIO()
        with patch.object(news.requests, "post", return_value=Mock(status_code=429)), patch.object(news.time, "sleep"), contextlib.redirect_stdout(output):
            value = news.analyze_sentiment_with_gemini("TESTUSDT", items, "private-test-key")
        self.assertIn("HTTP_429", value["catalyst"])
        self.assertIn("HTTP_429", output.getvalue())
        self.assertNotIn("private-test-key", output.getvalue())

    def test_request_error_diagnostic_does_not_expose_key_in_url(self):
        items = [{"source": "Media", "title": "TEST upgrade", "pub_date": "today"}]
        output = io.StringIO()
        error = news.requests.ConnectionError("https://example.com?key=private-test-key")
        with patch.object(news.requests, "post", side_effect=error), patch.object(news.time, "sleep"), contextlib.redirect_stdout(output):
            value = news.analyze_sentiment_with_gemini("TESTUSDT", items, "private-test-key")
        self.assertIn("REQUEST_FAILED:ConnectionError", value["catalyst"])
        self.assertNotIn("private-test-key", output.getvalue() + value["catalyst"])

    def test_news_keeps_original_identity_and_unknown_cannot_emit_buy(self):
        row = candidate(datetime.datetime.now(UTC).replace(microsecond=0))
        row.pop("forward_candles")
        row.pop("label_end")
        with tempfile.TemporaryDirectory() as folder:
            db = str(Path(folder) / "news.db")
            output = str(Path(folder) / "final.json")
            with patch.object(news, "load_gemini_api_key", return_value="test-key"), patch.object(news, "fetch_news_for_crypto", return_value=news.NewsItems(status="NO_NEWS")), patch.object(news, "FINAL_JSON_PATH", output), patch.object(news.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
                for _ in range(2):
                    result = news.run_news_sentiment_pipeline(db_path=db, candidates=[row])
            self.assertEqual(result[0]["scan_time"], row["scan_time"])
            self.assertEqual(result[0]["signal_id"], signal_id(row["scan_time"], row["symbol"]))
            self.assertEqual(result[0]["final_score"], 90)
            self.assertEqual(result[0]["execution_eligible"], 0)
            with database(db) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM kripto_sentiment_analysis").fetchone()[0], 1)

    def test_old_and_unrelated_headlines_are_not_accepted(self):
        from email.utils import format_datetime
        recent = format_datetime(datetime.datetime.now(UTC) - datetime.timedelta(hours=1))
        old = format_datetime(datetime.datetime.now(UTC) - datetime.timedelta(days=4))
        xml = f"<rss><channel><item><title>TEST upgrade</title><pubDate>{recent}</pubDate></item><item><title>TEST hack</title><pubDate>{old}</pubDate></item><item><title>OTHER hack</title><pubDate>{recent}</pubDate></item></channel></rss>"
        with patch.object(news.requests, "get", return_value=Mock(status_code=200, content=xml.encode())):
            items = news.fetch_news_for_crypto("TESTUSDT")
        self.assertEqual([r["title"] for r in items], ["TEST upgrade"])


class DerivativeIntegrityTests(unittest.TestCase):
    def test_stale_missing_or_invalid_snapshot_does_not_score(self):
        now = int(START.timestamp() * 1000)
        self.assertEqual(derivatives.validate_derivative_snapshot({}, now), "UNAVAILABLE")
        snapshot = dict(updated_at="2026-05-31 00:00:00", funding_rate=-0.1, open_interest_m=10)
        self.assertEqual(derivatives.validate_derivative_snapshot(snapshot, now), "STALE")
        snapshot.update(updated_at="2026-06-01 00:00:00", funding_rate=float("nan"))
        self.assertEqual(derivatives.validate_derivative_snapshot(snapshot, now), "INVALID")

    def test_missing_one_hour_and_four_hour_snapshots_remain_null(self):
        now = int(START.timestamp() * 1000)
        with tempfile.TemporaryDirectory() as folder:
            db = str(Path(folder) / "derivatives.db")
            derivatives.init_derivatives_db(db)
            with database(db) as conn:
                for offset, oi in ((0, 12), (2 * 3_600_000, 10)):
                    conn.execute("INSERT INTO derivatives_history VALUES (?, ?, ?, ?, ?, ?)",
                        ("TESTUSDT", now-offset, "2026-06-01 00:00:00", -0.02, oi*1e6, oi))
            result = derivatives.get_derivatives_deltas("TESTUSDT", db, now)
        self.assertIsNone(result["delta_oi_1h"])
        self.assertIsNone(result["delta_oi_4h"])

    def test_aligned_one_hour_does_not_fill_missing_four_hour(self):
        now = int(START.timestamp() * 1000)
        with tempfile.TemporaryDirectory() as folder:
            db = str(Path(folder) / "derivatives.db")
            derivatives.init_derivatives_db(db)
            with database(db) as conn:
                for offset, oi in ((0, 12), (3_600_000, 10)):
                    conn.execute("INSERT INTO derivatives_history VALUES (?, ?, ?, ?, ?, ?)",
                        ("TESTUSDT", now-offset, "2026-06-01 00:00:00", -0.02, oi*1e6, oi))
            result = derivatives.get_derivatives_deltas("TESTUSDT", db, now)
        self.assertEqual(result["delta_oi_1h"], 2)
        self.assertEqual(result["delta_1h_status"], "ALIGNED")
        self.assertIsNone(result["delta_oi_4h"])


if __name__ == "__main__":
    unittest.main()
