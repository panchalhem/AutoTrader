import os
import sys
import unittest
from pathlib import Path
import pandas as pd
import numpy as np

# Add scripts directory to sys.path so we can import trade_screener
ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / "scripts"))

import trade_screener


class TestTradeScreener(unittest.TestCase):
    def test_stop_target_buy(self):
        # Buy stop target:
        # stop = close * (1 - 1.5 * atr_pct / 100)
        # target = close * (1 + 2.5 * atr_pct / 100)
        close = 100.0
        atr_pct = 2.0  # 2% ATR
        stop, target = trade_screener.stop_target(close, atr_pct, "buy")
        self.assertEqual(stop, 97.0)  # 100 * (1 - 1.5 * 2/100) = 100 * 0.97 = 97.0
        self.assertEqual(target, 105.0)  # 100 * (1 + 2.5 * 2/100) = 100 * 1.05 = 105.0

    def test_stop_target_sell(self):
        # Sell stop target:
        # stop = close * (1 + 1.5 * atr_pct / 100)
        # target = close * (1 - 2.5 * atr_pct / 100)
        close = 100.0
        atr_pct = 2.0  # 2% ATR
        stop, target = trade_screener.stop_target(close, atr_pct, "sell")
        self.assertEqual(stop, 103.0)  # 100 * (1 + 1.5 * 2/100) = 100 * 1.03 = 103.0
        self.assertEqual(target, 95.0)  # 100 * (1 - 2.5 * 2/100) = 100 * 0.95 = 95.0

    def test_classify_empty(self):
        # Test classify with empty DataFrame - should not raise unpacking errors and return empty dataframes
        cols = [
            "symbol", "cap_segment", "last_date", "last_close", "atr_pct", "vol_surge_x",
            "vol_today", "vol_avg20", "rsi14", "trend_up", "trend_down", "ret_5d_pct",
            "ret_20d_pct", "dist_to_20d_high_pct", "breakout_up_today", "breakout_down_today",
            "near_breakout"
        ]
        empty_master = pd.DataFrame(columns=cols)
        buy_core, buy_caution, sell_core, sell_extended, watch = trade_screener.classify(empty_master)
        
        self.assertTrue(buy_core.empty)
        self.assertTrue(buy_caution.empty)
        self.assertTrue(sell_core.empty)
        self.assertTrue(sell_extended.empty)
        self.assertTrue(watch.empty)

        # Columns should be created even if empty
        self.assertIn("stop_loss", buy_core.columns)
        self.assertIn("target", buy_core.columns)
        self.assertIn("stop_loss", sell_core.columns)
        self.assertIn("target", sell_core.columns)

    def test_classify_normal(self):
        # Construct a small realistic mock DataFrame
        data = {
            "symbol": ["AAPL", "MSFT", "GOOG"],
            "cap_segment": ["Large", "Large", "Large"],
            "last_date": ["2026-08-14", "2026-08-14", "2026-08-14"],
            "last_close": [150.0, 300.0, 2800.0],
            "atr_pct": [2.5, 1.8, 1.2],
            "vol_surge_x": [3.5, 1.1, 0.8],
            "vol_today": [1500000, 900000, 100000],
            "vol_avg20": [400000, 300000, 150000],  # Liquid: AAPL, MSFT. Illiquid: GOOG (150k < 200k)
            "rsi14": [60.0, 80.0, 45.0],
            "trend_up": [True, True, False],
            "trend_down": [False, False, True],
            "ret_5d_pct": [3.2, 5.5, -4.0],
            "ret_20d_pct": [12.0, 15.0, -8.0],
            "dist_to_20d_high_pct": [0.0, 0.0, 5.0],
            "breakout_up_today": [True, True, False],
            "breakout_down_today": [False, False, False],
            "near_breakout": [True, True, False]
        }
        master = pd.DataFrame(data)
        buy_core, buy_caution, sell_core, sell_extended, watch = trade_screener.classify(master)

        # AAPL: liquid, breakout_up_today=True, rsi=60 (<75) -> buy_core
        self.assertIn("AAPL", buy_core["symbol"].values)
        self.assertNotIn("AAPL", buy_caution["symbol"].values)

        # MSFT: liquid, breakout_up_today=True, rsi=80 (>=75) -> buy_caution
        self.assertIn("MSFT", buy_caution["symbol"].values)
        self.assertNotIn("MSFT", buy_core["symbol"].values)

        # GOOG: volume_avg20 = 150,000 < LIQUIDITY_FLOOR (200,000) -> should be excluded from liquid analysis
        self.assertNotIn("GOOG", buy_core["symbol"].values)
        self.assertNotIn("GOOG", buy_caution["symbol"].values)
        self.assertNotIn("GOOG", sell_core["symbol"].values)
        self.assertNotIn("GOOG", watch["symbol"].values)


if __name__ == "__main__":
    unittest.main()
