#!/usr/bin/env python3
"""
Standalone backtest of the breakout/breakdown strategy (same rules as
session_signal.py, sourced from common.STRATEGY_DEFAULTS so they can't
drift apart) against one symbol's recent intraday history. Useful for
inspecting *why* a ticker passed or failed the guardrail build_watchlist.py
applies automatically every day.

Usage:
    ./.venv/bin/python global_intraday/scripts/backtest.py --symbol NQ=F
    ./.venv/bin/python global_intraday/scripts/backtest.py --symbol ^N225 --period 60d --interval 5m
    ./.venv/bin/python global_intraday/scripts/backtest.py --symbol GC=F --show-trades

Not investment advice. Past performance of a mechanical rule set on
historical bars is not a guarantee of future results — it's a filter
against strategies with no historical edge at all, not a prediction.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import STRATEGY_DEFAULTS, fetch_history, simulate_trades, summarize_trades


def main():
    p = argparse.ArgumentParser(description="Backtest the breakout/breakdown strategy on one symbol")
    p.add_argument("--symbol", required=True)
    p.add_argument("--period", default="60d", help="History window (yfinance limits intraday intervals; 5m maxes at 60d)")
    p.add_argument("--interval", default="5m")
    p.add_argument("--show-trades", action="store_true", help="Print every simulated trade, not just the summary")
    for key, default in STRATEGY_DEFAULTS.items():
        p.add_argument(f"--{key.replace('_', '-')}", type=type(default), default=default)
    args = p.parse_args()
    params = {k: getattr(args, k) for k in STRATEGY_DEFAULTS}

    df = fetch_history(args.symbol, args.interval, args.period)
    if df is None:
        print(f"No data for {args.symbol}"); return

    trades = simulate_trades(df, params)
    stats = summarize_trades(trades)

    print(f"{args.symbol}  {args.interval} bars, {args.period} lookback  ({len(df)} bars, "
          f"{df.index.min()} -> {df.index.max()})")
    print(f"Strategy params: {params}")
    print()
    print(f"Trades: {stats['trade_count']}")
    if stats["trade_count"]:
        print(f"Win rate: {stats['win_rate']*100:.1f}%")
        print(f"Avg R per trade: {stats['avg_r']}")
        print(f"Profit factor: {stats['profit_factor']}")
        print(f"Expectancy: {stats['expectancy_r']} R/trade")
    else:
        print("No trades triggered in this window — strategy never fired, can't be evaluated.")

    if args.show_trades:
        print("\nTrades:")
        for t in trades:
            print(f"  {t['entry_time']}  {t['direction']:5s} entry={t['entry']} stop={t['stop']} "
                  f"target={t['target']}  exit={t['exit_price']} ({t['outcome']})  R={t['r_multiple']}")


if __name__ == "__main__":
    main()
