import unittest
from unittest.mock import patch

import pandas as pd

from backend import feature_engine


class EntryRiskTests(unittest.TestCase):
    def test_large_breakout_is_extended_even_with_healthy_rsi(self):
        closes = [100.0, 101.0] * 15
        closes[-1] = 106.0
        candles = pd.DataFrame({
            "symbol": ["TESTUSDT"] * 30,
            "open": closes,
            "close": closes,
            "high": [price + 0.5 for price in closes],
            "low": [price - 0.5 for price in closes],
            "volume": [100.0] * 29 + [400.0],
        })
        candles.loc[29, "open"] = 101.0
        trigger = feature_engine.evaluate_15m_trigger(candles)["TESTUSDT"]
        self.assertLess(trigger["rsi_15m"], 78)
        self.assertGreater(trigger["vol_ratio_15m"], 1.25)
        self.assertEqual(trigger["trigger_status"], "EXTENDED")

    def test_overextended_setup_cannot_be_ready_or_wait(self):
        for momentum in (5, 15, 25):
            with self.subTest(momentum=momentum):
                _, status, tags = feature_engine.classify_setup_and_status(
                    structure_score=20, momentum_score=momentum, flow_score=15,
                    atr_expansion=1.0, roc_1h=2.0, rs_1h=1.0,
                    is_overextended=True,
                )
                self.assertEqual(status, "EXTENDED")
                self.assertFalse(any("READY" in tag or "WAIT" in tag for tag in tags))

    def test_stop_configuration_caps_structural_risk(self):
        outputs = {}
        for maximum_loss in (-3.2, -10.0):
            with patch.object(feature_engine, "load_calibrated_config", return_value={
                "stop_loss_pct": maximum_loss,
                "sl_atr_multiplier": 1.25,
            }):
                outputs[maximum_loss] = feature_engine.calculate_dynamic_tp_sl(
                    entry_price=100.0, support_level=95.0, atr_val=2.0,
                )
        self.assertEqual(outputs[-3.2]["stop_loss"], 96.8)
        self.assertEqual(outputs[-3.2]["stop_loss_pct"], -3.2)
        self.assertEqual(outputs[-10.0]["stop_loss"], 94.05)
        self.assertEqual(outputs[-10.0]["stop_loss_pct"], -5.95)

    def test_tighter_structural_stop_is_retained(self):
        with patch.object(feature_engine, "load_calibrated_config", return_value={
            "stop_loss_pct": -10.0, "sl_atr_multiplier": 1.0,
        }):
            plan = feature_engine.calculate_dynamic_tp_sl(100.0, 99.5, 1.0)
        self.assertEqual(plan["stop_loss"], 98.505)
        self.assertGreater(plan["stop_loss"], 90.0)

    def test_rounding_does_not_exceed_risk_cap(self):
        entry = 0.004357
        with patch.object(feature_engine, "load_calibrated_config", return_value={
            "stop_loss_pct": -3.2,
        }):
            plan = feature_engine.calculate_dynamic_tp_sl(entry, 0.004, 0.0005, dec=6)
        self.assertGreaterEqual(plan["stop_loss"], entry * 0.968)
        self.assertLess(plan["stop_loss"], entry)

    def test_invalid_risk_caps_are_rejected(self):
        for cap in (0, -100, float("nan")):
            with self.subTest(cap=cap), patch.object(
                feature_engine, "load_calibrated_config", return_value={"stop_loss_pct": cap},
            ):
                with self.assertRaises(ValueError):
                    feature_engine.calculate_dynamic_tp_sl(100.0, 95.0, 2.0)

    def test_support_level_guard_caps_stop_loss_below_entry(self):
        plan = feature_engine.calculate_dynamic_tp_sl(entry_price=100.0, support_level=105.0, atr_val=2.0)
        self.assertLess(plan["stop_loss"], 100.0)
        self.assertLess(plan["stop_loss_pct"], 0)


if __name__ == "__main__":
    unittest.main()
