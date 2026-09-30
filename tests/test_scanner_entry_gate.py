import contextlib
import datetime
import gc
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import data_pipeline, scanner


class ScannerEntryGateTests(unittest.TestCase):
    def run_scan(self, initial_status, trigger_status, setup="MOMENTUM_RUNNER"):
        with tempfile.TemporaryDirectory(prefix="kripikto-entry-test-") as folder:
            db_path = str(Path(folder) / "test.db")
            json_path = str(Path(folder) / "scan.json")
            data_pipeline.init_db(db_path)
            with contextlib.closing(sqlite3.connect(db_path)) as conn:
                conn.execute("""
                    INSERT INTO market_summary_24h (
                        symbol, last_price, price_change_pct, high_price, low_price,
                        volume, quote_volume, taker_buy_ratio, updated_at
                    ) VALUES ('TESTUSDT', 118, 1, 119, 99, 40000, 4000000, 56, '2026-09-30 00:00:00')
                """)
                now = datetime.datetime(2026, 9, 30, tzinfo=datetime.timezone.utc)
                for interval, hours in [("4h", 4), ("1h", 1), ("15m", 0.25)]:
                    start = now - datetime.timedelta(hours=hours * 60)
                    for symbol in ("TESTUSDT", "BTCUSDT"):
                        for index in range(60):
                            timestamp = start + datetime.timedelta(hours=hours * index)
                            open_time = int(timestamp.timestamp() * 1000)
                            price = 100.0 + index * 0.3
                            conn.execute("""
                            INSERT INTO klines_history (
                                symbol, interval, open_time, close_time, datetime_utc,
                                open, high, low, close, volume, quote_volume,
                                taker_buy_volume, is_closed, fetched_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                        """, (
                            symbol, interval, open_time, int(open_time + hours * 3_600_000 - 1),
                            timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                            price, price + 1.0, price - 1.0, price + 0.1,
                            100.0, 10000.0, 5600.0, "2026-09-30 00:00:00",
                        ))
                conn.commit()
            trigger = {
                "TESTUSDT": {
                    "trigger_status": trigger_status, "trigger_reason": "15M test",
                    "refined_buy_low": 105.0, "refined_buy_high": 106.0,
                    "micro_support": 105.5, "tags": ["15M_TEST"],
                }
            }
            with (
                patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden")),
                patch.object(scanner, "fetch_derivatives_summary", return_value={}),
                patch.object(scanner, "get_derivatives_deltas", return_value={}),
                patch.object(scanner, "get_active_spot_symbols", return_value={"TESTUSDT"}),
                patch.object(scanner, "classify_setup_and_status", return_value=(setup, initial_status, [])),
                patch.object(scanner, "fetch_15m_trigger_batch", return_value=1),
                patch.object(scanner, "evaluate_15m_trigger", return_value=trigger),
                patch.object(scanner, "SCAN_JSON_PATH", json_path),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                picks = scanner.run_scanner(
                    min_score=0, top_n=1, fgi={"value": 50, "regime": "NEUTRAL"},
                    db_path=db_path,
                    now_ms=int(now.timestamp() * 1000),
                )
            self.assertEqual(len(picks), 1)
            result = picks.iloc[0].to_dict()
            saved = json.loads(Path(json_path).read_text(encoding="utf-8"))[0]
            self.assertEqual(saved["entry_status"], result["entry_status"])
            with contextlib.closing(sqlite3.connect(db_path)) as conn:
                self.assertEqual(conn.execute(
                    "SELECT entry_status FROM scan_results"
                ).fetchone()[0], result["entry_status"])
            gc.collect()
        return result

    def test_extended_and_failed_cannot_be_promoted_by_micro_trigger(self):
        for status in ("EXTENDED", "FAILED"):
            with self.subTest(status=status):
                pick = self.run_scan(status, "TRIGGERED")
                self.assertEqual(pick["entry_status"], status)
                self.assertNotEqual(pick["entry_price"], 105.5)
                self.assertIn("HIGHER_TIMEFRAME_ENTRY_BLOCKED", pick["signals"])

    def test_eligible_setup_accepts_micro_trigger(self):
        pick = self.run_scan("READY", "TRIGGERED")
        self.assertEqual(pick["entry_status"], "TRIGGERED")
        self.assertEqual(pick["entry_price"], 105.5)

    def test_overextension_blocks_waiting_setup(self):
        pick = self.run_scan("WAIT", "EXTENDED")
        self.assertEqual(pick["entry_status"], "EXTENDED")

    def test_breakdown_invalidates_accumulation(self):
        pick = self.run_scan("WAIT", "FAILED", setup="ACCUMULATION_COIL")
        self.assertEqual(pick["entry_status"], "FAILED")


if __name__ == "__main__":
    unittest.main()
