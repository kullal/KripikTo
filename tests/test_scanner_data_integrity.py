import contextlib
import gc
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import data_pipeline, scanner
from market_fixture import NOW_MS, candles, seed_database


class ScannerDataIntegrityTests(unittest.TestCase):
    def run_scan(self, mutation=None, interval="4h", trigger_error=False):
        with tempfile.TemporaryDirectory(prefix="kripikto-quality-") as folder:
            db = Path(folder) / "market.db"
            output = Path(folder) / "scan.json"
            seed_database(db, interval)
            output.write_text('[{"symbol":"OLD_RESULT"}]', encoding="utf-8")
            if mutation:
                with contextlib.closing(sqlite3.connect(db)) as conn:
                    conn.execute(mutation)
                    conn.commit()
            trigger = {"TESTUSDT": {
                "trigger_status": "TRIGGERED", "trigger_reason": "test trigger",
                "refined_buy_low": 105, "refined_buy_high": 106,
                "micro_support": 105.5, "tags": [],
            }}
            with (
                patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden")),
                patch.object(scanner, "fetch_derivatives_summary", return_value={}),
                patch.object(scanner, "get_derivatives_deltas", return_value={}),
                patch.object(scanner, "get_active_spot_symbols", return_value={"TESTUSDT"}),
                patch.object(scanner, "classify_setup_and_status", return_value=("MOMENTUM_RUNNER", "READY", [])),
                patch.object(scanner, "fetch_15m_trigger_batch", side_effect=RuntimeError("offline") if trigger_error else None, return_value=0) as fetch,
                patch.object(scanner, "evaluate_15m_trigger", return_value=trigger),
                patch.object(scanner, "SCAN_JSON_PATH", str(output)),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                picks = scanner.run_scanner(interval=interval, min_score=0, top_n=1,
                    fgi={"value": 50, "regime": "NEUTRAL"}, db_path=str(db), now_ms=NOW_MS)
            saved = json.loads(output.read_text(encoding="utf-8"))
            report = json.loads(output.with_name("data_quality_latest.json").read_text(encoding="utf-8"))
            with contextlib.closing(sqlite3.connect(db)) as conn:
                conn.row_factory = sqlite3.Row
                stored = [dict(row) for row in conn.execute("SELECT * FROM scan_results")]
                outcomes = [dict(row) for row in conn.execute("SELECT * FROM signal_outcomes")]
                before = conn.execute("SELECT COUNT(*) FROM klines_history").fetchone()[0]
                scanner.init_scanner_db(str(db))
                scanner.init_scanner_db(str(db))
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM klines_history").fetchone()[0], before)
            result = (picks, saved, report, stored, outcomes, fetch.call_count)
            gc.collect()
        return result

    def test_healthy_scan_persists_exact_relative_strength_and_quality(self):
        picks, saved, report, stored, outcomes, calls = self.run_scan()
        coin, btc = candles(), candles("BTCUSDT")
        expected = round((coin.close.iloc[-1] / coin.close.iloc[-2] -
                          btc.close.iloc[-1] / btc.close.iloc[-2]) * 100, 2)
        self.assertEqual(picks.iloc[0].entry_status, "TRIGGERED")
        self.assertEqual(saved[0]["rs_structure"], expected)
        self.assertEqual(stored[0]["rs_structure"], expected)
        self.assertEqual(outcomes[0]["data_quality_status"], "OK")
        self.assertEqual(stored[0]["candle_close_time_ms"], NOW_MS - 1)
        self.assertEqual(report["checks"]["trigger_15m"]["accepted_symbols"], 1)
        self.assertEqual(calls, 1)

    def test_missing_btc_is_null_and_blocks_trigger(self):
        _, saved, _, stored, outcomes, calls = self.run_scan("DELETE FROM klines_history WHERE symbol='BTCUSDT'")
        self.assertIsNone(saved[0]["rs_structure"])
        self.assertIsNone(saved[0]["rs_1h"])
        self.assertIsNone(stored[0]["btc_return_structure"])
        self.assertEqual(saved[0]["data_quality_status"], "PARTIAL")
        self.assertEqual(outcomes[0]["entry_status"], "WAIT")
        self.assertEqual(calls, 0)

    def test_invalid_momentum_cannot_score_or_trigger(self):
        _, saved, report, _, _, calls = self.run_scan("UPDATE klines_history SET fetched_at=NULL WHERE symbol='TESTUSDT' AND interval='1h'")
        self.assertEqual(saved[0]["momentum_score"], 0)
        self.assertIsNone(saved[0]["rs_1h"])
        self.assertEqual(saved[0]["entry_status"], "WAIT")
        self.assertIn("MISSING_FETCH_METADATA", report["checks"]["momentum_1h"]["rejected"]["TESTUSDT"])
        self.assertEqual(calls, 0)

    def test_invalid_structure_clears_previous_latest_output(self):
        picks, saved, report, stored, _, _ = self.run_scan("UPDATE klines_history SET high=0 WHERE symbol='TESTUSDT' AND interval='4h'")
        self.assertTrue(picks.empty)
        self.assertEqual(saved, [])
        self.assertEqual(stored, [])
        self.assertIn("INVALID_OHLCV", report["checks"]["structure_4h"]["rejected"]["TESTUSDT"])

    def test_stale_summary_blocks_signal(self):
        picks, saved, report, _, _, _ = self.run_scan("UPDATE market_summary_24h SET updated_at='2026-09-29 12:00:00'")
        self.assertTrue(picks.empty)
        self.assertEqual(saved, [])
        self.assertIn("STALE_SUMMARY", report["checks"]["market_summary"]["rejected"]["TESTUSDT"])

    def test_invalid_trigger_blocks_even_spurious_evaluator_output(self):
        _, saved, report, _, _, _ = self.run_scan("UPDATE klines_history SET fetched_at=NULL WHERE symbol='TESTUSDT' AND interval='15m'")
        self.assertEqual(saved[0]["entry_status"], "WAIT")
        self.assertEqual(saved[0]["data_quality_status"], "PARTIAL")
        self.assertIn("TRIGGER_UNAVAILABLE", saved[0]["signals"])
        self.assertIn("MISSING_FETCH_METADATA", report["checks"]["trigger_15m"]["rejected"]["TESTUSDT"])

    def test_trigger_fetch_failure_blocks_entry_with_diagnostics(self):
        _, saved, report, _, _, _ = self.run_scan(trigger_error=True)
        self.assertEqual(saved[0]["entry_status"], "WAIT")
        self.assertEqual(saved[0]["data_quality_status"], "PARTIAL")
        self.assertEqual(report["checks"]["trigger_15m"]["rejected"]["TESTUSDT"], ["TRIGGER_EVALUATION_ERROR"])

    def test_two_hour_scan_uses_two_hour_btc_without_four_hour_fallback(self):
        _, saved, _, stored, _, _ = self.run_scan(interval="2h")
        self.assertEqual(saved[0]["data_quality_status"], "OK")
        self.assertEqual(saved[0]["rs_structure_status"], "ALIGNED")
        self.assertIsNone(saved[0]["rs_4h"])
        self.assertIsNone(stored[0]["btc_return_4h"])
        self.assertEqual(stored[0]["interval"], "2h")

    def test_whale_aggregate_uses_only_complete_valid_closed_window(self):
        for mutation in (None,
                         "UPDATE klines_history SET fetched_at=NULL WHERE interval='4h'",
                         "UPDATE klines_history SET taker_buy_volume=quote_volume*2 WHERE interval='4h'"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(prefix="kripikto-flow-") as folder:
                db = Path(folder) / "market.db"
                seed_database(db)
                with contextlib.closing(sqlite3.connect(db)) as conn:
                    conn.execute("UPDATE market_summary_24h SET taker_buy_ratio=51")
                    if mutation:
                        conn.execute(mutation)
                    conn.commit()
                data_pipeline.update_whale_flow_from_klines("4h", str(db), now_ms=NOW_MS)
                with contextlib.closing(sqlite3.connect(db)) as conn:
                    ratio = conn.execute("SELECT taker_buy_ratio FROM market_summary_24h").fetchone()[0]
                self.assertAlmostEqual(ratio, 56 if mutation is None else 51)
                gc.collect()


if __name__ == "__main__":
    unittest.main()
