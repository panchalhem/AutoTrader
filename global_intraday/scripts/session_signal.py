#!/usr/bin/env python3
"""
5-minute polling script: reads today's watchlist (built by build_watchlist.py),
figures out which trading session is currently live (Asia / Europe / US,
or a manual override), and evaluates a breakout/breakdown signal for each
of that session's top instruments. Self-contained — no LLM calls, just
yfinance + pandas/numpy, same as build_watchlist.py.

Typical flow:
    1. Once a day (any time before your trading day starts):
         ./.venv/bin/python global_intraday/scripts/build_watchlist.py
    2. Then leave this running, or cron it every 5 minutes:
         ./.venv/bin/python global_intraday/scripts/session_signal.py --loop-seconds 300 --quiet --log

Signal logic (same framework as us_market/scripts/nas100_signal.py):
    LONG  when the latest intraday close breaks above the prior N-bar high,
          on above-average volume, with RSI in [rsi-long-min, rsi-long-max].
    SHORT when it breaks below the prior N-bar low, on above-average
          volume, with RSI in [rsi-short-min, rsi-short-max].
    Stop  = entry -/+ stop-atr-mult * ATR(atr-period), on the same interval.
    Target= entry +/- target-atr-mult * ATR(atr-period).

Usage:
    # one-off check of whichever session is live right now
    ./.venv/bin/python global_intraday/scripts/session_signal.py

    # force a specific session regardless of current time
    ./.venv/bin/python global_intraday/scripts/session_signal.py --session europe

    # keep checking every 5 min, only full output on a new signal
    ./.venv/bin/python global_intraday/scripts/session_signal.py --loop-seconds 300 --quiet --log

Output:
    Prints a per-symbol verdict table each check. With --log, appends one
    row per symbol per check to output/session_signal_log.csv.

Not investment advice. Futures/index tickers are a proxy for your broker's
live CFD price, not a live feed of it — cross-check before trading.
"""
import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import json

import pandas as pd

from common import OUT_DIR, WATCHLIST_LATEST, atr, current_session, fetch_history, rsi

LOG_PATH = OUT_DIR / "session_signal_log.csv"


def load_watchlist(max_age_hours):
    if not WATCHLIST_LATEST.exists():
        raise RuntimeError(
            f"No watchlist found at {WATCHLIST_LATEST}. Run build_watchlist.py first."
        )
    data = json.loads(WATCHLIST_LATEST.read_text())
    generated_at = datetime.fromisoformat(data["generated_at_utc"])
    age_hours = (datetime.now(timezone.utc) - generated_at).total_seconds() / 3600
    if age_hours > max_age_hours:
        print(f"WARNING: watchlist is {age_hours:.1f}h old (>{max_age_hours}h) — "
              f"re-run build_watchlist.py for a fresh ranking.", file=sys.stderr)
    return data


def evaluate_symbol(symbol, args):
    df = fetch_history(symbol, args.interval, args.period)
    if df is None or len(df) < max(args.lookback_bars, args.rsi_period, args.atr_period) + 2:
        return {"symbol": symbol, "error": "not enough intraday data"}

    close, vol = df["Close"], df["Volume"]
    last_close = float(close.iloc[-1])
    last_rsi = float(rsi(close, args.rsi_period).iloc[-1])
    last_atr = float(atr(df, args.atr_period).iloc[-1])

    prior_high = float(df["High"].iloc[-(args.lookback_bars + 1):-1].max())
    prior_low = float(df["Low"].iloc[-(args.lookback_bars + 1):-1].min())
    vol_avg = float(vol.iloc[-(args.lookback_bars + 1):-1].mean())
    last_vol = float(vol.iloc[-1])
    vol_confirmed = last_vol > vol_avg if vol_avg > 0 else False

    long_trigger = (
        last_close > prior_high and vol_confirmed
        and args.rsi_long_min <= last_rsi <= args.rsi_long_max
    )
    short_trigger = (
        last_close < prior_low and vol_confirmed
        and args.rsi_short_min <= last_rsi <= args.rsi_short_max
    )

    verdict, entry, stop, target, reason = "WAIT", None, None, None, ""
    if long_trigger:
        verdict, entry = "LONG", last_close
        stop = round(entry - args.stop_atr_mult * last_atr, 4)
        target = round(entry + args.target_atr_mult * last_atr, 4)
        reason = f"broke prior {args.lookback_bars}-bar high {prior_high:.2f} w/ volume"
    elif short_trigger:
        verdict, entry = "SHORT", last_close
        stop = round(entry + args.stop_atr_mult * last_atr, 4)
        target = round(entry - args.target_atr_mult * last_atr, 4)
        reason = f"broke prior {args.lookback_bars}-bar low {prior_low:.2f} w/ volume"
    elif last_close > prior_high:
        reason = "broke high, no volume/RSI confirmation"
    elif last_close < prior_low:
        reason = "broke low, no volume/RSI confirmation"
    else:
        reason = f"inside range [{prior_low:.2f}, {prior_high:.2f}]"

    return {
        "symbol": symbol, "last_close": round(last_close, 4), "rsi": round(last_rsi, 1),
        "atr": round(last_atr, 4), "prior_high": round(prior_high, 4), "prior_low": round(prior_low, 4),
        "vol_confirmed": vol_confirmed, "verdict": verdict, "entry": entry, "stop": stop,
        "target": target, "reason": reason,
    }


def print_report(session, results, quiet, checked_at):
    actionable = [r for r in results if r.get("verdict") in ("LONG", "SHORT")]
    if quiet and not actionable:
        line = " | ".join(
            f"{r['symbol']}:{r.get('last_close','-')}(rsi{r.get('rsi','-')})" for r in results
        )
        print(f"[{checked_at}] {session.upper()} WAIT-ALL  {line}")
        return

    print(f"\n=== {checked_at} | session={session.upper()} ===")
    for r in results:
        if "error" in r:
            print(f"  {r['symbol']:10s} ERROR: {r['error']}")
            continue
        tag = r["verdict"]
        line = f"  {r['symbol']:10s} {tag:5s} px={r['last_close']}  rsi={r['rsi']}  ({r['reason']})"
        print(line)
        if tag in ("LONG", "SHORT"):
            rr = abs(r["target"] - r["entry"]) / abs(r["entry"] - r["stop"])
            print(f"      entry={r['entry']}  stop={r['stop']}  target={r['target']}  R:R~{rr:.2f}:1")
    print("Not investment advice — check your broker's live price before acting.")


def log_results(session, checked_at, results):
    rows = []
    for r in results:
        row = {"checked_at_utc": checked_at, "session": session, **r}
        rows.append(row)
    df = pd.DataFrame(rows)
    header = not LOG_PATH.exists()
    df.to_csv(LOG_PATH, mode="a", header=header, index=False)


def main():
    p = argparse.ArgumentParser(description="Session-aware NAS100/global intraday signal poller")
    p.add_argument("--session", choices=["asia", "europe", "us", "auto"], default="auto",
                    help="Force a session, or 'auto' to pick based on current UTC time (default)")
    p.add_argument("--top", type=int, default=5, help="How many watchlist symbols to check (default 5)")
    p.add_argument("--interval", default="5m", help="Intraday bar interval (default 5m)")
    p.add_argument("--period", default="5d", help="History window to pull per check (default 5d)")
    p.add_argument("--lookback-bars", type=int, default=20)
    p.add_argument("--rsi-period", type=int, default=14)
    p.add_argument("--atr-period", type=int, default=14)
    p.add_argument("--rsi-long-min", type=float, default=50.0)
    p.add_argument("--rsi-long-max", type=float, default=75.0)
    p.add_argument("--rsi-short-min", type=float, default=25.0)
    p.add_argument("--rsi-short-max", type=float, default=50.0)
    p.add_argument("--stop-atr-mult", type=float, default=1.5)
    p.add_argument("--target-atr-mult", type=float, default=2.5)
    p.add_argument("--max-watchlist-age-hours", type=float, default=24.0)
    p.add_argument("--loop-seconds", type=int, default=0, help="If >0, re-check every N seconds until Ctrl+C")
    p.add_argument("--quiet", action="store_true", help="Only print a compact line when nothing is actionable")
    p.add_argument("--log", action="store_true", help="Append every check to output/session_signal_log.csv")
    args = p.parse_args()

    while True:
        try:
            watchlist = load_watchlist(args.max_watchlist_age_hours)
            now_hour = datetime.now(timezone.utc).hour
            session = args.session if args.session != "auto" else current_session(now_hour)
            picks = watchlist["sessions"].get(session, {}).get("picks", [])
            symbols = [row["symbol"] for row in picks][: args.top]
            if not symbols:
                print(f"No watchlist entries for session '{session}'.", file=sys.stderr)
            else:
                results = [evaluate_symbol(sym, args) for sym in symbols]
                checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                print_report(session, results, args.quiet, checked_at)
                if args.log:
                    log_results(session, checked_at, results)
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
