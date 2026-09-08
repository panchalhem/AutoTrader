#!/usr/bin/env python3
"""
weekend_discovery.py — grows the live universe instead of leaving it fixed.

trade_monitor.py's daily gate only ever re-evaluates the ~150 instruments
hand-curated into build_universe() (Nifty 50 + a handful of manual NSE
additions, S&P/global indices, commodities, ~50 US mega-caps, crypto
majors). That's a deliberately small, backtest-daily-affordable set — but it
means the pipeline can only ever surface an opportunity that happens to live
inside it. This script is the other half: it searches much bigger pools
(thousands of NSE-listed stocks, the full S&P 400/500/600) for instruments
worth adding permanently, so there's a growing, ever-improving set of
candidates for the daily gate to review — not the same ~150 forever.

WHAT IT DOES
  1. Builds a candidate pool (--source nse | us | both) of every symbol in
     that pool NOT already in trade_monitor.INSTRUMENTS, and not already
     tried within --retry-after-days (default 90 — an edge can appear where
     there wasn't one before; markets aren't static, so a rejection isn't
     forever, just not worth re-checking every single week).
  2. Takes the next --batch-size (default 300) untried-or-due-for-retry
     candidates, cheapest filter first:
       a. Liquidity/price floor (20-day avg volume >= --min-avg-volume,
          last close >= --min-price-nse / --min-price-us) — matches
          scripts/trade_screener.py's own established floor, cuts out
          illiquid names before spending a backtest on them.
       b. The SAME swing gate trade_monitor.py runs daily (2-year daily
          bars, SWING_MIN_TRADES/PROFIT_FACTOR/EXPECTANCY, same cost_bps
          cost model) — swing only, not intraday: yfinance caps 5-minute
          history at 60 days, so an intraday backtest run here would tell
          you nothing an intraday backtest run again tomorrow, once the
          symbol's live, won't already tell you — not worth doubling this
          job's runtime for.
  3. A symbol that clears the gate is added to
     automation/data/discovered_universe.json, which
     trade_monitor.build_universe() merges into the live universe on its
     next run — automatically part of tomorrow's daily gate, no manual
     edit. Every candidate tried (pass or fail) is recorded in
     automation/output/discovery_log.json so the next run doesn't
     immediately re-fetch it.

WHY THIS DOESN'T RUN DAILY: a full NSE sweep is ~10,000 symbols — batched at
300/run that's roughly 34 runs to cover once. That's fine at weekend cadence
(markets closed, no urgency) but would either be far too slow to keep up
with daily, or would need a batch size large enough to hammer Yahoo/NSE's
rate limits daily. The daily gate re-tests the smaller live universe fresh
every day regardless of how a symbol entered it — this script's only job is
finding new candidates, not re-validating ones already promoted (a promoted
symbol whose edge decays will simply stop passing the daily gate on its
own, same as any hand-curated one).

USAGE
    ./.venv/bin/python automation/weekend_discovery.py
    ./.venv/bin/python automation/weekend_discovery.py --source nse --batch-size 500
    ./.venv/bin/python automation/weekend_discovery.py --source us

Not wired into cron by default. Suggested, once you've reviewed a couple of
runs by hand (splits the two pools across the two weekend days rather than
both fighting for the same evening):
    0 22 * * 6 cd /mnt/g/adhoc/stocktrade && ./.venv/bin/python automation/weekend_discovery.py --source nse --quiet
    0 22 * * 0 cd /mnt/g/adhoc/stocktrade && ./.venv/bin/python automation/weekend_discovery.py --source us --quiet
(automation/weekly_research.py, run separately, walk-forward-validates
whatever's currently promoted — a different job, see its own docstring.)

Not investment advice. Clearing this gate means the mechanical rule had a
historical edge on that instrument's last 2 years — not a guarantee, and not
yet walk-forward-validated (weekly_research.py does that afterward, on
whatever's currently promoted, this script's additions included).
"""
import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pandas as pd

import trade_monitor as tm  # noqa: E402  (single source of truth for universe/strategy/backtest math)

REPO_ROOT = Path(__file__).resolve().parent.parent
US_MARKET_DATA = REPO_ROOT / "us_market" / "data"
DISCOVERY_LOG_FILE = tm.OUT_DIR / "discovery_log.json"

DEFAULT_MIN_AVG_VOLUME = 200_000
DEFAULT_MIN_PRICE_NSE = 50.0
DEFAULT_MIN_PRICE_US = 5.0

# kite_instruments.csv tags government securities / T-bills / NCDs as
# instrument_type "EQ" alongside real equities (NSE's own instrument master,
# not a Zerodha quirk) — ~58% of the raw NSE-EQ pool on a 2026-08-24 check.
# Not a perfect filter (some NCD suffix codes slip through), but it cuts the
# bulk of debt paper before spending a fetch+backtest on something that was
# never going to have a breakout signal in the first place.
_NSE_NON_EQUITY_SUFFIX_RE = re.compile(r"-(SG|GS|TB|GB|N\d+)$")
_NSE_BOND_LIKE_RE = re.compile(r"^\d+[A-Z]{2}\d")  # e.g. 656KA30-SG, 66RJ30-SG


def _looks_like_nse_debt(tradingsymbol):
    return bool(_NSE_NON_EQUITY_SUFFIX_RE.search(tradingsymbol)) or bool(_NSE_BOND_LIKE_RE.match(tradingsymbol))


def load_discovery_log():
    if not DISCOVERY_LOG_FILE.exists():
        return {}
    try:
        return json.loads(DISCOVERY_LOG_FILE.read_text())
    except Exception:
        return {}


def save_discovery_log(log):
    DISCOVERY_LOG_FILE.write_text(json.dumps(log, indent=2))


def load_discovered_universe():
    if not tm.DISCOVERED_UNIVERSE_FILE.exists():
        return {}
    try:
        return json.loads(tm.DISCOVERED_UNIVERSE_FILE.read_text())
    except Exception:
        return {}


def save_discovered_universe(u):
    tm.DISCOVERED_UNIVERSE_FILE.write_text(json.dumps(u, indent=2))


def nse_candidate_pool(already_have):
    """Every Zerodha-tradable NSE-EQ symbol not already in the live universe."""
    tm.fetch_zerodha_tradable_symbols()  # ensures/refreshes automation/data/kite_instruments.csv as a side effect
    if not tm.KITE_INSTRUMENTS_CACHE.exists():
        return {}
    df = pd.read_csv(tm.KITE_INSTRUMENTS_CACHE)
    nse_eq = df[(df["segment"] == "NSE") & (df["instrument_type"] == "EQ")]
    pool = {}
    for _, row in nse_eq.iterrows():
        bare = str(row["tradingsymbol"])
        if _looks_like_nse_debt(bare):
            continue
        sym = f"{bare}.NS"
        if sym in already_have:
            continue
        pool[sym] = {"name": tm._clean_company_name(row.get("name", row["tradingsymbol"])), "class": "stock",
                     "broker": "zerodha", "buckets": ["zerodha"]}
    return pool


def us_candidate_pool(already_have):
    """S&P 400 (mid) + 500 + 600 (small) constituents not already in the live universe."""
    pool = {}
    for fname in ("sp400_list.csv", "sp500_list.csv", "sp600_list.csv"):
        path = US_MARKET_DATA / fname
        if not path.exists():
            continue
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                raw = (row.get("Symbol") or row.get("symbol") or "").strip()
                if not raw:
                    continue
                sym = raw.replace(".", "-")  # BRK.B -> BRK-B, Yahoo's convention
                if sym in already_have:
                    continue
                pool[sym] = {"name": sym, "class": "stock", "broker": "capitalcom", "buckets": ["us"]}
    return pool


def due_candidates(pool, log, retry_after_days):
    now = datetime.now(timezone.utc)
    due = {}
    for sym, meta in pool.items():
        prev = log.get(sym)
        if prev is None:
            due[sym] = meta
            continue
        tried_at = prev.get("last_tried_utc")
        if not tried_at:
            due[sym] = meta
            continue
        age_days = (now - datetime.fromisoformat(tried_at)).total_seconds() / 86400
        if age_days >= retry_after_days:
            due[sym] = meta
    return due


def evaluate_batch(symbols, pool, min_avg_volume, min_price_nse, min_price_us, quiet):
    daily_data = tm.fetch_batch(symbols, tm.DAILY_INTERVAL, tm.DAILY_PERIOD)
    results = {}
    liquid = []
    for sym in symbols:
        df = daily_data.get(sym)
        if df is None or len(df) < 20:
            results[sym] = {"result": "rejected", "reason": "no/insufficient daily data"}
            continue
        last_close = float(df["Close"].iloc[-1])
        vol_avg20 = float(df["Volume"].rolling(20).mean().iloc[-1])
        min_price = min_price_nse if pool[sym]["broker"] == "zerodha" else min_price_us
        if pd.isna(vol_avg20) or vol_avg20 < min_avg_volume:
            results[sym] = {"result": "rejected", "reason": f"avg volume {vol_avg20:.0f} below floor {min_avg_volume:.0f}"}
            continue
        if pd.isna(last_close) or last_close < min_price:
            results[sym] = {"result": "rejected", "reason": f"price {last_close:.2f} below floor {min_price:.2f}"}
            continue
        liquid.append(sym)

    if not quiet:
        print(f"  {len(liquid)}/{len(symbols)} cleared the liquidity/price floor, backtesting those on the swing gate...")

    swing_data = tm.fetch_batch(liquid, tm.SWING_BACKTEST_INTERVAL, tm.SWING_BACKTEST_PERIOD)
    for sym in liquid:
        try:
            # min_win_rate must match the live daily gate (tm.SWING_MIN_WIN_RATE,
            # 0.80 as of 2026-08-29) — otherwise this would mark a symbol
            # "added" here on the older PF/expectancy-only bar, merge it into
            # the live universe, and then have it silently fail the real
            # daily gate every day, which is confusing and pointless.
            bt = tm.run_backtest(sym, swing_data.get(sym), tm.SWING_STRATEGY_DEFAULTS,
                                  tm.SWING_MIN_TRADES, tm.SWING_MIN_PROFIT_FACTOR, tm.SWING_MIN_EXPECTANCY,
                                  min_win_rate=tm.SWING_MIN_WIN_RATE)
        except Exception as e:
            bt = {"bt_pass": False, "bt_reason": f"error: {e}"}
        if bt["bt_pass"]:
            results[sym] = {"result": "added", "stats": bt}
        else:
            results[sym] = {"result": "rejected", "reason": bt.get("bt_reason") or "failed swing gate", "stats": bt}
    return results


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", choices=["nse", "us", "both"], default="both")
    p.add_argument("--batch-size", type=int, default=300, help="max NEW candidates to evaluate per pool this run (default 300)")
    p.add_argument("--retry-after-days", type=int, default=90, help="re-try a previously-rejected symbol after this many days (default 90)")
    p.add_argument("--min-avg-volume", type=float, default=DEFAULT_MIN_AVG_VOLUME)
    p.add_argument("--min-price-nse", type=float, default=DEFAULT_MIN_PRICE_NSE)
    p.add_argument("--min-price-us", type=float, default=DEFAULT_MIN_PRICE_US)
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    already_have = set(tm.INSTRUMENTS.keys())
    log = load_discovery_log()
    discovered = load_discovered_universe()

    pool = {}
    if args.source in ("nse", "both"):
        pool.update(nse_candidate_pool(already_have))
    if args.source in ("us", "both"):
        pool.update(us_candidate_pool(already_have))
    print(f"[discovery] candidate pool ({args.source}): {len(pool)} symbols not already in the live universe")

    due = due_candidates(pool, log, args.retry_after_days)
    print(f"[discovery] {len(due)} of those are new or due for retry (>= {args.retry_after_days}d since last try)")

    batch = list(due.items())[: args.batch_size]
    if not batch:
        print("[discovery] nothing to evaluate this run.")
        return
    symbols = [s for s, _ in batch]
    batch_pool = dict(batch)
    print(f"[discovery] evaluating {len(symbols)} of them this run...")

    results = evaluate_batch(symbols, batch_pool, args.min_avg_volume, args.min_price_nse, args.min_price_us, args.quiet)

    now = datetime.now(timezone.utc)
    added, rejected = [], []
    for sym, r in results.items():
        log[sym] = {"last_tried_utc": now.isoformat(timespec="seconds"), "result": r["result"],
                     "reason": r.get("reason"), "stats": r.get("stats")}
        if r["result"] == "added":
            added.append(sym)
            meta = dict(batch_pool[sym])
            meta["discovered_utc"] = now.isoformat(timespec="seconds")
            meta["discovery_stats"] = r.get("stats")
            discovered[sym] = meta
            if not args.quiet:
                s = r["stats"]
                print(f"  ADDED   {sym:14s} trades={s['bt_trade_count']:<4} win%={s['bt_win_rate']} "
                      f"PF={s['bt_profit_factor']} exp={s['bt_expectancy_r']}R")
        else:
            rejected.append(sym)

    save_discovery_log(log)
    save_discovered_universe(discovered)

    print(f"\n[discovery] {len(added)} added to the live universe, {len(rejected)} rejected (recorded, retried in "
          f"{args.retry_after_days}d), {len(pool) - len(due)} skipped (tried recently).")
    if added:
        print(f"[discovery] newly added: {', '.join(added)}")
    print(f"[discovery] discovered_universe.json now holds {len(discovered)} symbols total "
          f"(all of them part of trade_monitor.py's live universe from its next run).")
    print(f"[discovery] wrote {tm.DISCOVERED_UNIVERSE_FILE} and {DISCOVERY_LOG_FILE}")


if __name__ == "__main__":
    main()
