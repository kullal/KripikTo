import unittest

import pandas as pd
from pandas.testing import assert_series_equal

from backend import feature_engine, scanner


class RsiRegressionTests(unittest.TestCase):
    def test_scanner_returns_known_rsi_and_preserves_index(self):
        prices = pd.Series([10.0, 12.0, 11.0, 14.0], index=[5, 8, 9, 12])
        result = scanner.calculate_rsi(prices, period=3)
        self.assertIsInstance(result, pd.Series)
        self.assertEqual(result.index.tolist(), prices.index.tolist())
        self.assertEqual(result.iloc[:3].tolist(), [50.0, 50.0, 50.0])
        # Three changes: gains 2 + 0 + 3, losses 0 + 1 + 0.
        self.assertAlmostEqual(result.iloc[-1], 100.0 * 5.0 / 6.0)

    def test_rising_falling_and_flat_prices(self):
        for prices, expected in [
            (list(range(1, 31)), 100.0),
            (list(range(30, 0, -1)), 0.0),
            ([10.0] * 30, 50.0),
        ]:
            with self.subTest(expected=expected):
                result = scanner.calculate_rsi(pd.Series(prices, dtype=float))
                self.assertTrue((result.iloc[14:] == expected).all())

    def test_rsi_is_invariant_to_price_scale(self):
        prices = pd.Series([10.0, 12.0, 11.0, 14.0, 13.0, 15.0])
        reference = feature_engine.calculate_rsi(prices, period=3)
        tiny_price_rsi = scanner.calculate_rsi(prices * 1e-12, period=3)
        assert_series_equal(reference, tiny_price_rsi)

    def test_grouped_scanner_indicators_do_not_mix_symbols(self):
        candles = pd.DataFrame({
            "symbol": ["RISEUSDT"] * 30 + ["FALLUSDT"] * 30,
            "close": list(range(1, 31)) + list(range(30, 0, -1)),
        })
        rsi = candles.groupby("symbol")["close"].transform(scanner.calculate_rsi)
        self.assertEqual(rsi.iloc[29], 100.0)
        self.assertEqual(rsi.iloc[59], 0.0)
        self.assertEqual(rsi.iloc[30], 50.0)

    def test_15m_trigger_uses_non_neutral_rsi(self):
        candles = pd.DataFrame({
            "symbol": ["RISEUSDT"] * 30,
            "open": [value - 0.1 for value in range(100, 130)],
            "high": [value + 0.5 for value in range(100, 130)],
            "low": [value - 0.5 for value in range(100, 130)],
            "close": list(range(100, 130)),
            "volume": [100.0] * 30,
        })
        trigger = feature_engine.evaluate_15m_trigger(candles)["RISEUSDT"]
        self.assertEqual(trigger["rsi_15m"], 100.0)
        self.assertEqual(trigger["trigger_status"], "EXTENDED")


if __name__ == "__main__":
    unittest.main()
