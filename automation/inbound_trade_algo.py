#!/usr/bin/env python3
"""
inbound_trade_algo.py — INBOUNDTRADEALGO.

trade_monitor.py's core rule (BREAKOUT_INTRADAY / BREAKOUT_SWING, see that
file's module docstring for the full strategy list) only ever fires when
price crosses OUTSIDE its own recent range. Between breakouts, a genuinely
gate-passing, liquid instrument spends most of its time sitting in "inside
range [low, high]" WAIT — visible in the dashboard, tracked every cycle, and
never traded. INBOUNDTRADEALGO is the other half: trade the range itself —
buy small near the BOTTOM of it, short small near the TOP of it, targeting a
modest reversion back across the range rather than a breakout continuation.
This is a DIFFERENT thesis from breakout (mean-reversion vs. continuation),
so it is backtested and gated entirely separately — passing the breakout
gate says nothing about whether this rule works on that same instrument.

--------------------------------------------------------------------------
FEASIBILITY CHECK (done 2026-08-25, BEFORE this was wired into the live
pipeline — read this before trusting it)
--------------------------------------------------------------------------
Backtested this exact rule against the live instrument universe before
writing anything into the daily gate:

  INTRADAY (60-day / 5-minute bars): NOT feasible, and deliberately NOT
  offered on this timeframe. On a 60-symbol sample: aggregate profit factor
  0.59-0.62, expectancy -0.24R to -0.31R across ~32,000-44,000 simulated
  trades — a clearly negative edge. ZERO of the 60 symbols individually
  cleared even a loose PF>=1.1/expectancy>=0.0R bar under either parameter
  set tried. A 5-minute dip into an established range gets run over by
  continuation far more often than it reverts — this is what "it doesn't
  work" looks like when actually measured, not assumed.

  SWING (2-year / daily bars): feasible for a real subset. Pooled across
  the full 169-instrument universe the aggregate is still ~breakeven
  (profit factor 0.96-0.98, expectancy -0.01 to -0.03R depending on
  parameters) — so "buy every dip" doesn't work here either — but
  per-symbol, with the parameters below, 29 of 169 (17.2%) individually
  clear PF>=1.3/expectancy>=0.05R (the same cost-aware bar
  BREAKOUT_SWING uses), including liquid, unremarkable-to-overfit names:
  NVDA, SPY, TCS.NS, COIN, KOTAKBANK.NS, INDIGO.NS. That is the real
  opportunity here — not "trade every WAIT instrument," a specific,
  individually-backtested ~17% of them, re-verified daily like everything
  else in this pipeline.

Conclusion: INBOUNDTRADEALGO runs on the SWING (daily-bar) timeframe only.
It reuses the exact same 2-year daily data trade_monitor.py's swing gate
already fetches — no extra API calls for the backtest.

--------------------------------------------------------------------------
THE RULE
--------------------------------------------------------------------------
Range = the same prior_high/prior_low (rolling lookback_bars high/low) the
breakout strategies use, just read the opposite way:
    LONG  when close is still inside the range AND within band_pct of the
          range's bottom AND RSI is in [rsi_long_min, rsi_long_max] — a
          moderate pullback, not a capitulating breakdown (that's what
          band_pct + the RSI floor are for: this is not "catch the falling
          knife").
    SHORT the mirror image at the range's top.
Stop/target are ATR multiples like every other strategy here, just smaller
(this is meant to bank a small profit, not ride a trend): stop just beyond
the range edge, target a modest move back toward the middle — see
run_backtest()'s R:R note if you want the exact numbers.

--------------------------------------------------------------------------
ENABLE / DISABLE
--------------------------------------------------------------------------
Delegates to strategy_config.py — the same on/off switch shared by all
three strategies (BREAKOUT_INTRADAY, BREAKOUT_SWING, INBOUNDTRADEALGO), one
file: automation/data/strategy_config.json, e.g.
    {"INBOUNDTRADEALGO": {"enabled": false}}
Checked before this strategy's daily gate, its live signal loop, AND live
order execution (auto_trader.py / zerodha_trader.py) — disabling it pulls
it out of all three, no other code changes. Default: enabled — the
feasibility check above is the reason it defaults on, not an oversight.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import strategy_config
import trading_settings as ts

STRATEGY_NAME = "INBOUNDTRADEALGO"

# Chosen from a small grid search across the live universe (2026-08-25) —
# best PF>=1.3/exp>=0.05R pass count (29/169) among the combinations tried,
# with a healthy mix of liquid names rather than a handful of illiquid
# overfits. Not claimed to be optimal, just evidence-picked over guessed.
DEFAULTS = {
    "lookback_bars": 10,       # same window BREAKOUT_SWING uses for its own range
    "rsi_period": 14,
    "atr_period": 14,
    "band_pct": 0.10,          # "near the edge" = within the bottom/top 10% of the range
    "rsi_long_min": 35.0, "rsi_long_max": 50.0,   # pulled back, not capitulating
    "rsi_short_min": 50.0, "rsi_short_max": 65.0,  # pushed up, not blowing off
    "stop_atr_mult": 0.6,      # tight — invalidated fast if the range genuinely breaks
    "target_atr_mult": 1.0,    # small, deliberately: a reversion trade, not a trend ride
    "max_hold_bars": 7,        # same as BREAKOUT_SWING — ~1 trading week
    "cost_bps": 5.0,           # same round-trip cost assumption as every other gate here
    "breakeven_trigger_r": 1.0,  # see trade_monitor.SWING_STRATEGY_DEFAULTS for rationale
    "breakeven_lock_r": 0.0,
}
BACKTEST_PERIOD, BACKTEST_INTERVAL = "2y", "1d"  # matches BREAKOUT_SWING — reuses its fetch
# All four gate thresholds moved into trading_settings.py (2026-09-03, user
# request) — dashboard-editable, shared with trade_monitor.SWING_MIN_* for
# consistency (same swing_* keys) since both strategies are gated to the
# same bar by design. Read fresh at import time; a change takes effect on
# the next process start — see trading_settings.py's docstring.
MIN_TRADES = ts.get("swing_min_trades")
MIN_PROFIT_FACTOR = ts.get("swing_min_profit_factor")
MIN_EXPECTANCY = ts.get("swing_min_expectancy")
MIN_WIN_RATE = ts.get("swing_min_win_rate")


def is_enabled():
    return strategy_config.is_enabled(STRATEGY_NAME)


def set_enabled(enabled):
    strategy_config.set_enabled(STRATEGY_NAME, enabled)


def signals(close, high, low, rsi_series, params=None):
    """(long_sig, short_sig) boolean Series — factored out so the backtest
    simulator and the live evaluator run the identical rule, never two
    versions that can drift apart."""
    p = params or DEFAULTS
    lb = p["lookback_bars"]
    prior_high = high.rolling(lb).max().shift(1)
    prior_low = low.rolling(lb).min().shift(1)
    band = p["band_pct"] * (prior_high - prior_low)
    still_inside = (close > prior_low) & (close < prior_high)
    long_sig = still_inside & (close <= prior_low + band) & rsi_series.between(p["rsi_long_min"], p["rsi_long_max"])
    short_sig = still_inside & (close >= prior_high - band) & rsi_series.between(p["rsi_short_min"], p["rsi_short_max"])
    return long_sig, short_sig, prior_low, prior_high
