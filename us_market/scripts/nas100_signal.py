#!/usr/bin/env python3
"""
Standalone NAS100 intraday signal generator.

Self-contained — no LLM/API calls, just yfinance + pandas/numpy math. Pulls
Nasdaq-100 futures (NQ=F, the closest freely available proxy to a NAS100 CFD
feed) on a configurable intraday interval, computes RSI/ATR/rolling
breakout levels, and prints a WAIT / LONG / SHORT verdict with entry, stop,
and target. Every threshold is a CLI flag so the strategy can be retuned
without touching the code.

Strategy (all tunable, see --help):
    LONG  when close breaks above the prior N-bar high, on above-average
          volume, with RSI in the [rsi-long-min, rsi-long-max] band
          (filters out chasing an already-overbought spike).
    SHORT when close breaks below the prior N-bar low, on above-average
          volume, with RSI in the [rsi-short-min, rsi-short-max] band
          (filters out shorting an already-oversold move).
    Stop  = entry -/+ stop-atr-mult * ATR(atr-period)
    Target= entry +/- target-atr-mult * ATR(atr-period)

Usage:
    # one-off check
    ./.venv/bin/python us_market/scripts/nas100_signal.py

    # keep checking every 5 minutes until Ctrl+C, only printing on a new signal
    ./.venv/bin/python us_market/scripts/nas100_signal.py --loop-seconds 300 --quiet

    # retune thresholds
    ./.venv/bin/python us_market/scripts/nas100_signal.py --lookback-bars 12 --stop-atr-mult 1.0

    # run it as a cron/scheduled task yourself (every 5 min, logs each check):
    */5 * * * * cd /mnt/g/adhoc/stocktrade && ./.venv/bin/python us_market/scripts/nas100_signal.py --quiet --log

Output:
    Prints a verdict to stdout each check. With --log, appends one row per
    check to us_market/output/nas100_signal_log.csv so you have a record of
    what the script said and when, independent of this session.

Not investment advice. This is a mechanical technical read on a futures
proxy, not your broker's live CFD price — cross-check before trading.
"""
import argparse
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "output"
OUT_DIR.mkdir(exist_ok=True)
LOG_PATH = OUT_DIR / "nas100_signal_log.csv"


def rsi(series, period):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def atr(df, period):
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period).mean()


def fetch(symbol, interval, period):
    df = yf.download(symbol, period=period, interval=interval, auto_adjust=True, progress=False)
    if df.empty:
        raise RuntimeError(f"No data returned for {symbol} ({interval}, {period})")
    df.columns = df.columns.get_level_values(0)
    return df.dropna()


def evaluate(args):
    df = fetch(args.symbol, args.interval, args.period)
    if len(df) < max(args.lookback_bars, args.rsi_period, args.atr_period) + 2:
        raise RuntimeError("Not enough bars for the configured lookback/RSI/ATR periods")

    close, vol = df["Close"], df["Volume"]
    rsi_series = rsi(close, args.rsi_period)
    atr_series = atr(df, args.atr_period)

    last_close = float(close.iloc[-1])
    last_rsi = float(rsi_series.iloc[-1])
    last_atr = float(atr_series.iloc[-1])
    bar_time = df.index[-1]

    # Prior N-bar high/low, volume avg — exclude the current (still-forming) bar
    prior_high = float(df["High"].iloc[-(args.lookback_bars + 1):-1].max())
    prior_low = float(df["Low"].iloc[-(args.lookback_bars + 1):-1].min())
    vol_avg = float(vol.iloc[-(args.lookback_bars + 1):-1].mean())
    last_vol = float(vol.iloc[-1])
    vol_confirmed = last_vol > vol_avg

    long_trigger = (
        last_close > prior_high
        and vol_confirmed
        and args.rsi_long_min <= last_rsi <= args.rsi_long_max
    )
    short_trigger = (
        last_close < prior_low
        and vol_confirmed
        and args.rsi_short_min <= last_rsi <= args.rsi_short_max
    )

    verdict = "WAIT"
    entry = stop = target = None
    reason = []

    if long_trigger:
        verdict = "LONG"
        entry = last_close
        stop = round(entry - args.stop_atr_mult * last_atr, 2)
        target = round(entry + args.target_atr_mult * last_atr, 2)
        reason.append(f"close {last_close:.2f} broke above prior {args.lookback_bars}-bar high {prior_high:.2f}")
    elif short_trigger:
        verdict = "SHORT"
        entry = last_close
        stop = round(entry + args.stop_atr_mult * last_atr, 2)
        target = round(entry - args.target_atr_mult * last_atr, 2)
        reason.append(f"close {last_close:.2f} broke below prior {args.lookback_bars}-bar low {prior_low:.2f}")
    else:
        if last_close > prior_high and not vol_confirmed:
            reason.append("broke the high but volume did not confirm")
        elif last_close < prior_low and not vol_confirmed:
            reason.append("broke the low but volume did not confirm")
        elif last_close > prior_high:
            reason.append(f"broke the high but RSI {last_rsi:.1f} outside [{args.rsi_long_min},{args.rsi_long_max}]")
        elif last_close < prior_low:
            reason.append(f"broke the low but RSI {last_rsi:.1f} outside [{args.rsi_short_min},{args.rsi_short_max}]")
        else:
            reason.append(f"inside range [{prior_low:.2f}, {prior_high:.2f}]")

    return {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bar_time": str(bar_time),
        "symbol": args.symbol,
        "last_close": round(last_close, 2),
        "rsi": round(last_rsi, 1),
        "atr": round(last_atr, 2),
        "prior_high": round(prior_high, 2),
        "prior_low": round(prior_low, 2),
        "vol_last": int(last_vol),
        "vol_avg": round(vol_avg, 0),
        "vol_confirmed": vol_confirmed,
        "verdict": verdict,
        "entry": entry,
        "stop": stop,
        "target": target,
        "reason": "; ".join(reason),
    }


def print_result(r, quiet):
    if r["verdict"] == "WAIT" and quiet:
        print(f"[{r['checked_at_utc']}] WAIT  px={r['last_close']}  rsi={r['rsi']}  "
              f"range=[{r['prior_low']}, {r['prior_high']}]  ({r['reason']})")
        return

    print(f"\n=== {r['checked_at_utc']} | {r['symbol']} | bar {r['bar_time']} ===")
    print(f"Close: {r['last_close']}   RSI({r['rsi']})   ATR: {r['atr']}")
    print(f"Range: [{r['prior_low']}, {r['prior_high']}]   Vol: {r['vol_last']} vs avg {r['vol_avg']} "
          f"({'confirmed' if r['vol_confirmed'] else 'NOT confirmed'})")
    if r["verdict"] == "WAIT":
        print(f"VERDICT: WAIT — {r['reason']}")
    else:
        print(f"VERDICT: {r['verdict']}  —  {r['reason']}")
        print(f"  Entry:  {r['entry']}")
        print(f"  Stop:   {r['stop']}")
        print(f"  Target: {r['target']}")
        rr = abs(r['target'] - r['entry']) / abs(r['entry'] - r['stop'])
        print(f"  Reward:Risk ~ {rr:.2f}:1")
    print("Not investment advice — futures proxy, not your broker's live CFD price.")


def log_result(r):
    row = pd.DataFrame([r])
    header = not LOG_PATH.exists()
    row.to_csv(LOG_PATH, mode="a", header=header, index=False)


def main():
    p = argparse.ArgumentParser(description="Standalone NAS100 intraday breakout/breakdown signal")
    p.add_argument("--symbol", default="NQ=F", help="yfinance ticker (default NQ=F, Nasdaq-100 futures)")
    p.add_argument("--interval", default="5m", help="Bar interval (default 5m)")
    p.add_argument("--period", default="5d", help="History window to pull (default 5d; yfinance limits intraday range)")
    p.add_argument("--lookback-bars", type=int, default=20, help="Bars for rolling high/low/volume-avg (default 20)")
    p.add_argument("--rsi-period", type=int, default=14)
    p.add_argument("--atr-period", type=int, default=14)
    p.add_argument("--rsi-long-min", type=float, default=50.0)
    p.add_argument("--rsi-long-max", type=float, default=75.0)
    p.add_argument("--rsi-short-min", type=float, default=25.0)
    p.add_argument("--rsi-short-max", type=float, default=50.0)
    p.add_argument("--stop-atr-mult", type=float, default=1.5)
    p.add_argument("--target-atr-mult", type=float, default=2.5)
    p.add_argument("--loop-seconds", type=int, default=0, help="If >0, re-check every N seconds until Ctrl+C")
    p.add_argument("--quiet", action="store_true", help="Only print a one-line status on WAIT ticks; full report on LONG/SHORT")
    p.add_argument("--log", action="store_true", help="Append every check to output/nas100_signal_log.csv")
    args = p.parse_args()

    while True:
        try:
            r = evaluate(args)
            print_result(r, args.quiet)
            if args.log:
                log_result(r)
        except Exception as e:
            print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] ERROR: {e}", file=sys.stderr)

        if args.loop_seconds <= 0:
            break
        try:
            time.sleep(args.loop_seconds)
        except KeyboardInterrupt:
            print("\nStopped.")
            break


if __name__ == "__main__":
    main()
