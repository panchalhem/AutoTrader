#!/usr/bin/env python3
"""
weekly_research.py — the slower, once-a-week companion to trade_monitor.py's
daily backtest gate. Nothing here replaces the daily gate (it stays running
every day — see trade_monitor.py's own docstring for why that cadence
matters: regime drift and boundary flips both get caught same-day by it,
and a weekly-only gate would miss both). This script does the things that
genuinely benefit from an unhurried, once-a-week pass instead:

  1. WALK-FORWARD VALIDATION of today's swing gate. Every symbol that
     currently passes the swing gate (2y daily bars, see
     trade_monitor.SWING_STRATEGY_DEFAULTS / SWING_MIN_*) is re-tested on
     two disjoint slices of that same 2y history: an earlier "train" slice
     and a later, held-out "test" slice (--holdout-days trading days, default
     126 ~= 6 months). A symbol only "confirms" if it independently clears
     the gate on BOTH slices — passing only because of one unusually good
     stretch is exactly what this catches. (The intraday gate can't get the
     same treatment: yfinance caps 5-minute history at 60 days total, so
     there's no earlier disjoint slice to hold out.)

  2. UNIVERSE CURATION notes: which asset classes are pulling their weight
     (gate pass rate) vs. dead weight, and a reminder to review one-off
     manual universe additions (see trade_monitor.py's `extra_symbols`)
     periodically rather than letting them sit forever un-reconsidered.

Run this on Sundays (markets closed, no rush, and it front-runs Monday's
first daily gate rebuild). It does NOT touch output/watchlist_latest.json —
it's read-only research, written to its own dated report file.

Usage:
    ./.venv/bin/python automation/weekly_research.py
    ./.venv/bin/python automation/weekly_research.py --holdout-days 90

Not wired into cron by default — add it yourself once you've reviewed a
couple of its reports, e.g.:
    0 21 * * 0 cd /mnt/g/adhoc/stocktrade && ./.venv/bin/python automation/weekly_research.py --quiet
(21:00 UTC Sunday ~= Sunday evening in most western-hemisphere zones and
well before Monday's Asia session open; adjust to taste.)
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import trade_monitor as tm  # noqa: E402  (single source of truth for the universe/strategy/backtest math)

REPORT_DIR = tm.OUT_DIR


def walk_forward_one(symbol, df, params, min_trades, min_pf, min_exp, holdout_days):
    min_bars = params["lookback_bars"] + params["max_hold_bars"] + max(params["rsi_period"], params["atr_period"]) + 5
    if df is None or len(df) < holdout_days + min_bars * 2:
        return {"status": "insufficient history for a train/test split"}

    train_df, test_df = df.iloc[:-holdout_days], df.iloc[-holdout_days:]

    def stats_and_pass(part_df):
        if len(part_df) < min_bars:
            return {"trade_count": 0, "win_rate": None, "profit_factor": None, "expectancy_r": None}, False
        s = tm.summarize_trades(tm.simulate_trades(part_df, params))
        passed = (
            s["trade_count"] >= min_trades
            and s["expectancy_r"] is not None and s["expectancy_r"] >= min_exp
            and s["profit_factor"] is not None and s["profit_factor"] >= min_pf
        )
        return s, passed

    train_stats, train_pass = stats_and_pass(train_df)
    test_stats, test_pass = stats_and_pass(test_df)
    return {
        "status": "ok",
        "train": train_stats, "train_pass": train_pass,
        "test": test_stats, "test_pass": test_pass,
        "walk_forward_pass": train_pass and test_pass,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--holdout-days", type=int, default=126, help="trading days held out as the out-of-sample test slice (default 126 ~= 6 months)")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    if not tm.WATCHLIST_LATEST.exists():
        print("No output/watchlist_latest.json yet — run trade_monitor.py at least once first.", file=sys.stderr)
        sys.exit(1)
    watchlist = json.loads(tm.WATCHLIST_LATEST.read_text())
    swing_gate = watchlist.get("swing_gate_results") or {}
    swing_passing = sorted(s for s, g in swing_gate.items() if g.get("bt_pass"))
    if not swing_passing:
        print("No symbols currently pass the swing gate — nothing to walk-forward validate.", file=sys.stderr)
        sys.exit(0)

    print(f"[weekly] walk-forward validating {len(swing_passing)} swing-gate passers "
          f"({args.holdout_days}-trading-day held-out test slice)...")
    data = tm.fetch_batch(swing_passing, tm.SWING_BACKTEST_INTERVAL, tm.SWING_BACKTEST_PERIOD)

    results = {}
    for sym in swing_passing:
        r = walk_forward_one(sym, data.get(sym), tm.SWING_STRATEGY_DEFAULTS,
                              tm.SWING_MIN_TRADES, tm.SWING_MIN_PROFIT_FACTOR, tm.SWING_MIN_EXPECTANCY,
                              args.holdout_days)
        results[sym] = r
        if not args.quiet:
            if r["status"] != "ok":
                print(f"  {sym:12s} SKIP ({r['status']})")
            else:
                verdict = "CONFIRMED" if r["walk_forward_pass"] else "did not hold up"
                print(f"  {sym:12s} train={'PASS' if r['train_pass'] else 'fail':5s} "
                      f"test={'PASS' if r['test_pass'] else 'fail':5s} -> {verdict}")

    confirmed = sorted(s for s, r in results.items() if r.get("walk_forward_pass"))
    not_confirmed = sorted(s for s, r in results.items() if r.get("status") == "ok" and not r["walk_forward_pass"])
    skipped = sorted(s for s, r in results.items() if r.get("status") != "ok")

    by_class = {}
    for s in swing_passing:
        cls = tm.INSTRUMENTS.get(s, {}).get("class", "unknown")
        by_class.setdefault(cls, {"total": 0, "confirmed": 0})
        by_class[cls]["total"] += 1
        if s in confirmed:
            by_class[cls]["confirmed"] += 1

    generated_at = datetime.now(timezone.utc)
    report = {
        "generated_at_utc": generated_at.isoformat(timespec="seconds"),
        "holdout_days": args.holdout_days,
        "swing_gate_passing_count": len(swing_passing),
        "confirmed": confirmed,
        "not_confirmed": not_confirmed,
        "skipped_insufficient_history": skipped,
        "by_class": by_class,
        "results": results,
        "note": (
            "This does NOT modify watchlist_latest.json or today's live gate — "
            "it's a research report only. A symbol failing here is a candidate "
            "for a closer look (or exclusion), not an automatic removal."
        ),
    }
    out_path = REPORT_DIR / f"weekly_research_{generated_at:%Y-%m-%d}.json"
    out_path.write_text(json.dumps(report, indent=2))
    (REPORT_DIR / "weekly_research_latest.json").write_text(json.dumps(report, indent=2))

    print(f"\n[weekly] {len(confirmed)}/{len(swing_passing)} swing-gate passers confirmed walk-forward "
          f"(held up on both the train and held-out test slice).")
    if not_confirmed:
        print(f"[weekly] did not hold up out-of-sample: {', '.join(not_confirmed)}")
    if skipped:
        print(f"[weekly] skipped (insufficient history for a clean split): {', '.join(skipped)}")
    print("\n[weekly] pass rate by class (of today's swing-gate passers):")
    for cls, c in sorted(by_class.items()):
        print(f"  {cls:10s} {c['confirmed']}/{c['total']} confirmed")
    print(f"\n[weekly] wrote {out_path}")


if __name__ == "__main__":
    main()
