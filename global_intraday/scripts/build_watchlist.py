#!/usr/bin/env python3
"""
Daily research script: ranks the instrument universe (indices, commodities,
US mega-cap stocks), backtests the exact live strategy against each one's
recent intraday history, and picks the top N per trading session (Asia /
Europe / US) for intraday trading — but ONLY from instruments whose
backtest actually cleared a minimum bar for historical profitability.
Writes output/watchlist_latest.json, which session_signal.py reads before
every 5-minute check.

Run this once a day (recommended: shortly before the Asia session opens,
~22:00 UTC / before 09:00 JST, so the day's watchlist is ready before the
first window starts).

Two-stage guardrail, in order:

  STAGE 1 — Backtest gate (run first, hard filter). For every instrument,
  common.simulate_trades() replays the SAME breakout/breakdown + volume +
  RSI rule session_signal.py trades live, bar-by-bar, over the last
  --backtest-period of --backtest-interval bars. An instrument is dropped
  from consideration entirely — regardless of how it scores below — unless
  ALL of:
      trade_count      >= --min-trades          (default 8: enough sample
                                                   to mean something)
      expectancy_r      >= --min-expectancy      (default 0.0: strategy must
                                                   not have lost money on
                                                   average per trade)
      profit_factor     >= --min-profit-factor   (default 1.1: gross wins
                                                   must beat gross losses by
                                                   a real margin, not by luck)
  This is what "maximum chance of profit, confirmed by backdated strategy"
  means concretely — an instrument only reaches stage 2 if the exact rules
  session_signal.py will use today would have made money on it recently.

  STAGE 2 — Ranking (only among instruments that passed stage 1). Composite
  score = weighted percentile rank, per-session, of:
      Volatility:  ATR(14) % of price       (--w-vol,       default 0.40)
      Momentum:    |RSI(14) - 50|            (--w-momentum,  default 0.35)
      Liquidity:   20-day average volume     (--w-liquidity, default 0.25)
  Top N per session by this score becomes the day's watchlist.

Usage:
    ./.venv/bin/python global_intraday/scripts/build_watchlist.py
    ./.venv/bin/python global_intraday/scripts/build_watchlist.py --top 5
    ./.venv/bin/python global_intraday/scripts/build_watchlist.py --min-trades 15 --min-profit-factor 1.3
    ./.venv/bin/python global_intraday/scripts/build_watchlist.py --skip-backtest   # rank only, no gate (fast, for debugging)

Output:
    global_intraday/output/watchlist_latest.json   always overwritten
    global_intraday/output/watchlist_<date>.json    dated snapshot, kept
    Each session's list includes every candidate considered — both the
    picks and the ones the backtest gate rejected, with the reason why —
    so the filtering is auditable, not a black box.

Not investment advice. A historical backtest passing this gate means the
mechanical rule set had a positive edge on that instrument recently — not
that it will today. Markets change regime; re-run this daily and treat a
pass as "worth watching," not a guarantee.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pandas as pd

from common import (
    INSTRUMENTS,
    OUT_DIR,
    STRATEGY_DEFAULTS,
    atr,
    candidates_for_session,
    fetch_history,
    rsi,
    simulate_trades,
    summarize_trades,
)

DAILY_PERIOD = "3mo"
DAILY_INTERVAL = "1d"


def compute_daily_metrics(symbol, rsi_period, atr_period):
    df = fetch_history(symbol, DAILY_INTERVAL, DAILY_PERIOD)
    if df is None or len(df) < max(rsi_period, atr_period, 20) + 2:
        return None
    close, vol = df["Close"], df["Volume"]
    last_close = float(close.iloc[-1])
    last_rsi = float(rsi(close, rsi_period).iloc[-1])
    last_atr = float(atr(df, atr_period).iloc[-1])
    atr_pct = (last_atr / last_close) * 100 if last_close else None
    vol_avg20 = float(vol.rolling(20).mean().iloc[-1])
    sma20 = float(close.rolling(20).mean().iloc[-1])
    ret_5d = (close.iloc[-1] / close.iloc[-6] - 1) * 100 if len(close) > 6 else None
    if pd.isna(last_rsi) or pd.isna(atr_pct) or pd.isna(vol_avg20):
        return None
    return {
        "symbol": symbol,
        "last_close": round(last_close, 4),
        "atr_pct": round(atr_pct, 3),
        "rsi14": round(last_rsi, 1),
        "vol_avg20": round(vol_avg20, 0),
        "ret_5d_pct": round(ret_5d, 2) if ret_5d is not None and pd.notna(ret_5d) else None,
        "trend_up": bool(last_close > sma20),
        "last_date": str(df.index[-1].date()),
    }


def _clean_pf(pf):
    """JSON has no Infinity token; cap an all-winners profit factor instead of emitting one."""
    if pf is None:
        return None
    if pf == float("inf"):
        return 999.99
    return pf


def run_backtest(symbol, interval, period, strategy_params, min_trades, min_profit_factor, min_expectancy):
    df = fetch_history(symbol, interval, period)
    min_bars = strategy_params["lookback_bars"] + strategy_params["max_hold_bars"] + max(
        strategy_params["rsi_period"], strategy_params["atr_period"]
    ) + 5
    if df is None or len(df) < min_bars:
        return {
            "bt_trade_count": 0, "bt_win_rate": None, "bt_profit_factor": None, "bt_expectancy_r": None,
            "bt_pass": False, "bt_reason": "insufficient intraday history to backtest",
        }

    stats = summarize_trades(simulate_trades(df, strategy_params))
    pf = _clean_pf(stats["profit_factor"])

    passed, reason = True, None
    if stats["trade_count"] < min_trades:
        passed, reason = False, f"only {stats['trade_count']} historical trades (<{min_trades} required)"
    elif stats["expectancy_r"] is None or stats["expectancy_r"] < min_expectancy:
        passed, reason = False, f"expectancy {stats['expectancy_r']}R below required {min_expectancy}R"
    elif pf is None or pf < min_profit_factor:
        passed, reason = False, f"profit factor {pf} below required {min_profit_factor}"

    return {
        "bt_trade_count": stats["trade_count"],
        "bt_win_rate": stats["win_rate"],
        "bt_profit_factor": pf,
        "bt_expectancy_r": stats["expectancy_r"],
        "bt_pass": passed,
        "bt_reason": reason,
    }


def rank_session(session, metrics_by_symbol, top_n, weights):
    syms = candidates_for_session(session)
    all_rows = [metrics_by_symbol[s] for s in syms if metrics_by_symbol.get(s) is not None]
    passing_rows = [r for r in all_rows if r.get("bt_pass", True)]
    rejected = [
        {"symbol": r["symbol"], "name": INSTRUMENTS[r["symbol"]]["name"], "reason": r.get("bt_reason")}
        for r in all_rows if not r.get("bt_pass", True)
    ]

    if not passing_rows:
        return [], rejected

    df = pd.DataFrame(passing_rows)
    df["momentum_signal"] = (df["rsi14"] - 50).abs()
    df["vol_rank"] = df["atr_pct"].rank(pct=True)
    df["momentum_rank"] = df["momentum_signal"].rank(pct=True)
    df["liquidity_rank"] = df["vol_avg20"].rank(pct=True)
    df["score"] = (
        weights["vol"] * df["vol_rank"]
        + weights["momentum"] * df["momentum_rank"]
        + weights["liquidity"] * df["liquidity_rank"]
    )
    df["bias"] = df["rsi14"].apply(lambda r: "bullish" if r >= 55 else ("bearish" if r <= 45 else "neutral"))
    df = df.sort_values("score", ascending=False).head(top_n)

    out = []
    for r in df.itertuples():
        meta = INSTRUMENTS[r.symbol]
        out.append({
            "symbol": r.symbol, "name": meta["name"], "class": meta["class"],
            "score": round(r.score, 4), "bias": r.bias, "last_close": r.last_close,
            "atr_pct": r.atr_pct, "rsi14": r.rsi14, "ret_5d_pct": r.ret_5d_pct,
            "vol_avg20": r.vol_avg20, "trend_up": r.trend_up,
            "backtest": {
                "trade_count": r.bt_trade_count, "win_rate": r.bt_win_rate,
                "profit_factor": r.bt_profit_factor, "expectancy_r": r.bt_expectancy_r,
            },
        })
    return out, rejected


def main():
    p = argparse.ArgumentParser(description="Daily intraday watchlist builder with a backtest profitability gate")
    p.add_argument("--top", type=int, default=5, help="How many instruments to keep per session (default 5)")
    p.add_argument("--rsi-period", type=int, default=14)
    p.add_argument("--atr-period", type=int, default=14)
    p.add_argument("--w-vol", type=float, default=0.40, help="Weight on volatility percentile rank")
    p.add_argument("--w-momentum", type=float, default=0.35, help="Weight on momentum (|RSI-50|) percentile rank")
    p.add_argument("--w-liquidity", type=float, default=0.25, help="Weight on liquidity (volume) percentile rank")

    p.add_argument("--skip-backtest", action="store_true", help="Skip the backtest gate entirely (rank only)")
    p.add_argument("--backtest-period", default="60d", help="Intraday history window for the backtest (default 60d, yfinance's max for 5m bars)")
    p.add_argument("--backtest-interval", default="5m", help="Bar interval for the backtest — should match session_signal.py's --interval")
    p.add_argument("--min-trades", type=int, default=8, help="Minimum historical trades required to trust the stats (default 8)")
    p.add_argument("--min-profit-factor", type=float, default=1.1, help="Minimum gross-win/gross-loss ratio required (default 1.1)")
    p.add_argument("--min-expectancy", type=float, default=0.0, help="Minimum average R-multiple per trade required (default 0.0 = must not lose)")
    for key, default in STRATEGY_DEFAULTS.items():
        if key in ("rsi_period", "atr_period"):
            continue  # already exposed above, shared with the daily-metrics RSI/ATR
        p.add_argument(f"--strategy-{key.replace('_', '-')}", type=type(default), default=default,
                        help=f"Strategy param passed to the backtest simulation (default {default})")

    args = p.parse_args()
    weights = {"vol": args.w_vol, "momentum": args.w_momentum, "liquidity": args.w_liquidity}
    strategy_params = {**STRATEGY_DEFAULTS, "rsi_period": args.rsi_period, "atr_period": args.atr_period}
    for key in STRATEGY_DEFAULTS:
        if key in ("rsi_period", "atr_period"):
            continue
        strategy_params[key] = getattr(args, f"strategy_{key}")

    all_symbols = sorted(INSTRUMENTS.keys())
    print(f"Fetching daily bars for {len(all_symbols)} instruments...")
    metrics_by_symbol = {}
    for sym in all_symbols:
        try:
            metrics_by_symbol[sym] = compute_daily_metrics(sym, args.rsi_period, args.atr_period)
        except Exception as e:
            print(f"  {sym}: daily fetch failed ({e})")
            metrics_by_symbol[sym] = None

    if not args.skip_backtest:
        print(f"\nBacktesting {len(all_symbols)} instruments on {args.backtest_interval} bars "
              f"over {args.backtest_period} (min {args.min_trades} trades, "
              f"PF>={args.min_profit_factor}, expectancy>={args.min_expectancy}R)...")
        for sym in all_symbols:
            if metrics_by_symbol.get(sym) is None:
                continue
            try:
                bt = run_backtest(
                    sym, args.backtest_interval, args.backtest_period, strategy_params,
                    args.min_trades, args.min_profit_factor, args.min_expectancy,
                )
            except Exception as e:
                bt = {"bt_trade_count": 0, "bt_win_rate": None, "bt_profit_factor": None,
                      "bt_expectancy_r": None, "bt_pass": False, "bt_reason": f"backtest error: {e}"}
            metrics_by_symbol[sym].update(bt)
            status = "PASS" if bt["bt_pass"] else f"FAIL ({bt['bt_reason']})"
            print(f"  {sym:10s} trades={bt['bt_trade_count']:<4} "
                  f"win%={bt['bt_win_rate']}  PF={bt['bt_profit_factor']}  "
                  f"exp={bt['bt_expectancy_r']}R  -> {status}")
    else:
        for sym in all_symbols:
            if metrics_by_symbol.get(sym) is not None:
                metrics_by_symbol[sym]["bt_pass"] = True

    generated_at = datetime.now(timezone.utc)
    watchlist = {
        "generated_at_utc": generated_at.isoformat(timespec="seconds"),
        "as_of_date": generated_at.strftime("%Y-%m-%d"),
        "params": {
            "top": args.top, "weights": weights,
            "backtest_gate": None if args.skip_backtest else {
                "period": args.backtest_period, "interval": args.backtest_interval,
                "min_trades": args.min_trades, "min_profit_factor": args.min_profit_factor,
                "min_expectancy": args.min_expectancy, "strategy_params": strategy_params,
            },
        },
        "sessions": {},
    }

    for session in ["asia", "europe", "us"]:
        top, rejected = rank_session(session, metrics_by_symbol, args.top, weights)
        watchlist["sessions"][session] = {"picks": top, "rejected_by_backtest": rejected}
        print(f"\n=== {session.upper()} — top {len(top)} (of {len(top) + len(rejected)} candidates) ===")
        if not top:
            print("  (none passed the backtest gate)")
        for row in top:
            bt = row["backtest"]
            print(f"  {row['symbol']:10s} {row['name']:16s} score={row['score']:.3f}  "
                  f"bias={row['bias']:8s} ATR%={row['atr_pct']:.2f}  RSI={row['rsi14']:.1f}  "
                  f"| bt: {bt['trade_count']}tr win{bt['win_rate']} PF{bt['profit_factor']} exp{bt['expectancy_r']}R")
        if rejected:
            print(f"  rejected: {', '.join(r['symbol'] for r in rejected)}")

    latest_path = OUT_DIR / "watchlist_latest.json"
    dated_path = OUT_DIR / f"watchlist_{watchlist['as_of_date']}.json"
    latest_path.write_text(json.dumps(watchlist, indent=2))
    dated_path.write_text(json.dumps(watchlist, indent=2))
    print(f"\nWrote {latest_path} and {dated_path}")


if __name__ == "__main__":
    main()
