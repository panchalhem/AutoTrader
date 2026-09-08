#!/usr/bin/env python3
"""
auto_trader.py — places REAL orders on Capital.com from trade_monitor.py's
live signals, with hard risk caps enforced in code. Zerodha is NOT wired in
here yet (needs a paid Kite Connect subscription + daily OAuth login — a
separate, later phase by design).

################################################################################
# THIS SCRIPT CAN PLACE REAL ORDERS WITH REAL MONEY.
# It defaults to dry-run: it computes and logs everything it WOULD do and
# calls no order-placing endpoint. Live order placement requires BOTH
# --live AND --confirm "I UNDERSTAND" (exact string) at the same time.
################################################################################

Hard risk caps — deliberately NOT CLI flags, so a typo can't loosen them.
Edit the constants below directly if you want different numbers:
    PER_TRADE_RISK_PCT = 0.005   0.5% of account equity: the most this
                                  script will risk on a single trade if its
                                  stop-loss is hit.
    DAILY_LOSS_CAP_PCT  = 0.04   4% of account equity: if today's equity
                                  drop (realized + unrealized) reaches this,
                                  ALL new orders stop for the rest of the day.

Safety mechanisms:
    - Every order gets a stop-loss AND take-profit attached AT PLACEMENT —
      Capital.com's own platform enforces the exit, not this script, so a
      crash or network drop after the order goes in doesn't leave a
      position unprotected.
    - Daily circuit breaker compares current account equity to the equity
      recorded at the start of today's local trading day (output/
      daily_risk_state.json). Breach -> output/TRADING_HALTED is created
      and no new orders are placed until you delete that file yourself,
      after reviewing what happened. Existing open positions are left
      alone — they already carry their own stop/take-profit.
    - Manual kill switch: create output/TRADING_HALTED yourself at any
      time (e.g. `touch automation/output/TRADING_HALTED`) to immediately
      block all new orders; delete it to resume.
    - If risk-capped position sizing would fall below the instrument's
      broker-reported minimum deal size, the trade is SKIPPED — never
      rounded up past the risk cap to meet the minimum.
    - Every decision (placed / skipped / dry-run / error) is logged to
      output/auto_trader_log.csv with the reason, separate from
      trade_monitor.py's own read-only signal log.
    - Reuses trade_monitor.py's own evaluate_symbol()/watchlist directly
      (imported, not reimplemented) so there is exactly one definition of
      "what counts as a signal" — this script and the plain monitor can
      never silently disagree.

Usage — ALWAYS run dry-run first, repeatedly, before ever using --live:
    ./.venv/bin/python automation/auto_trader.py --once            # dry-run by default
    ./.venv/bin/python automation/auto_trader.py --once --refresh-epics

    # only once you've reviewed dry-run output and trust it:
    ./.venv/bin/python automation/auto_trader.py --live --confirm "I UNDERSTAND" --loop-seconds 300

IMPORTANT: this is UNTESTED against Capital.com's real API — there were no
credentials available to verify it against in the environment that wrote
this. Field names in account/market-details responses are coded defensively
with clear error messages, but may still need a small fix once you run it
against your real account. Start on CAPITAL_ENV=demo (see automation/.env)
and read --dry-run output carefully before ever passing --live.
"""
import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from capital_api import CapitalSession, credentials as capital_credentials
import trade_monitor as tm
import strategy_config
import trading_settings as ts

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "output"
DATA_DIR = ROOT / "data"
OUT_DIR.mkdir(exist_ok=True)

HALT_FILE = OUT_DIR / "TRADING_HALTED"
DAILY_STATE_FILE = OUT_DIR / "daily_risk_state.json"
EPIC_MAP_FILE = DATA_DIR / "capital_epic_map.json"
TRADE_LOG = OUT_DIR / "auto_trader_log.csv"
SWING_POSITIONS_FILE = OUT_DIR / "capital_swing_positions.json"
SWING_TRADE_LOG = OUT_DIR / "capital_swing_trader_log.csv"

# PER_TRADE_RISK_PCT, DAILY_LOSS_CAP_PCT, and INTRADAY_CAPITAL_PCT moved into
# trading_settings.py (2026-09-03, user request) — dashboard-editable, no
# code change/restart needed. Read live at point of use (ts.get(...) below,
# inside size_position()/check_daily_loss_cap-equivalent) rather than cached
# at import, so a Control Panel settings change takes effect within one
# 5-minute loop tick. See trading_settings.py's module docstring for the
# full reload-semantics table across every setting, not just these three.
MIN_RISK_PER_TRADE_USD = 10.0  # user-requested floor (2026-08-25) — on this small an account,
                                # 0.5% of equity is only ~$3-5/trade, which does NOT mean stops are
                                # too tight: INBOUNDTRADEALGO's stop_atr_mult=0.6 is deliberate (many
                                # small losers offset by fewer larger wins, backtested that way) and
                                # widening the ATR distance itself would invalidate that edge. This
                                # floor instead raises position SIZE for the same stop distance, so a
                                # stop-out costs a more meaningful ~$10 rather than a few dollars —
                                # the per-instrument ATR stop distance (and therefore how often it
                                # gets hit) is untouched. Still fully bounded by the margin cap in
                                # size_position() below, so it can never oversize past what the
                                # broker's leverage actually allows.


# Fixed column set — see trade_monitor.SIGNAL_LOG_COLUMNS for why this
# can't just be list(row.keys()): an append-only CSV's header is written
# once, ever, so any future drift in which keys a row happens to carry
# silently corrupts the file for every later reader.
TRADE_LOG_COLUMNS = ["checked_at", "env", "symbol", "name", "verdict", "entry", "stop", "target",
                     "epic", "size", "mode", "outcome", "reason", "strategy"]
SWING_TRADE_LOG_COLUMNS = TRADE_LOG_COLUMNS + ["opened_at", "held_trading_days", "deal_id"]


def log_decision(row):
    header = not TRADE_LOG.exists()
    with open(TRADE_LOG, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_LOG_COLUMNS, extrasaction="ignore")
        if header:
            w.writeheader()
        w.writerow({k: row.get(k) for k in TRADE_LOG_COLUMNS})


def log_swing_decision(row):
    header = not SWING_TRADE_LOG.exists()
    with open(SWING_TRADE_LOG, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SWING_TRADE_LOG_COLUMNS, extrasaction="ignore")
        if header:
            w.writeheader()
        w.writerow({k: row.get(k) for k in SWING_TRADE_LOG_COLUMNS})


def load_swing_positions():
    if SWING_POSITIONS_FILE.exists():
        return json.loads(SWING_POSITIONS_FILE.read_text())
    return {}


def save_swing_positions(state):
    SWING_POSITIONS_FILE.write_text(json.dumps(state, indent=2))


def trading_days_between(start_date, end_date):
    """Weekday count between two date objects — an approximation of trading
    days (doesn't account for exchange holidays, only weekends; close
    enough for a max-hold check, not used for anything price-sensitive)."""
    days, d = 0, start_date
    while d < end_date:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


def strategy_max_hold_bars(strategy):
    if strategy == "INBOUNDTRADEALGO":
        return tm.inbound_trade_algo.DEFAULTS["max_hold_bars"]
    return tm.SWING_STRATEGY_DEFAULTS["max_hold_bars"]  # BREAKOUT_SWING


_CLASS_TO_CAPITAL_TYPE = {
    "stock": "SHARES", "crypto": "CRYPTOCURRENCIES", "commodity": "COMMODITIES",
    "index": "INDICES", "forex": "CURRENCIES",
}


def _strip(s):
    return "".join(ch for ch in str(s).upper() if ch.isalnum())


def _pick_market_result(sym, name, results):
    """Picks the right Capital.com market from a name search — NOT just the
    first result or a naive exact-name match. Confirmed in practice both of
    those are unsafe: search("Tron") ranks the unrelated stock Tronox (TROX)
    above the actual TRON/USD crypto market, and search("IREN") exact-matches
    the small Italian utility 'Iren' (whose name happens to equal our search
    term literally) over the real 'IREN Limited' we mean. A wrong pick here
    means a live order gets placed on the wrong instrument entirely.

    Preference order, each tier restricted to the expected instrumentType
    when we know it (from trade_monitor.INSTRUMENTS' class):
      1. epic (alnum-stripped) exactly equals our symbol (alnum-stripped) —
         the strongest possible signal, since Capital.com epics are usually
         the ticker itself.
      2. instrumentName startswith our search name, case-insensitive.
      3. instrumentName exactly equals our search name, case-insensitive.
      4. instrumentName contains our search name.
      5. first result in the type-filtered pool (loudly flagged — verify
         before trusting it for a live order).
    """
    expected_type = _CLASS_TO_CAPITAL_TYPE.get(tm.INSTRUMENTS.get(sym, {}).get("class"))
    pool = [r for r in results if r.get("instrumentType") == expected_type] if expected_type else []
    if not pool:
        pool = results

    sym_key = _strip(sym)
    by_epic = [r for r in pool if _strip(r.get("epic", "")) == sym_key]
    if by_epic:
        return by_epic[0], True

    name_l = name.lower()
    starts = [r for r in pool if str(r.get("instrumentName", "")).lower().startswith(name_l)]
    if starts:
        return starts[0], True

    exact = [r for r in pool if str(r.get("instrumentName", "")).lower() == name_l]
    if exact:
        return exact[0], True

    contains = [r for r in pool if name_l in str(r.get("instrumentName", "")).lower()]
    if contains:
        return contains[0], True

    return pool[0], False


def resolve_epic_map(sess, symbols_and_names, refresh=False):
    """Maps our internal proxy symbols (GC=F, ^N225, AAPL, ...) to real
    Capital.com epic codes via their market search, cached locally so this
    doesn't re-search every run."""
    cache = {}
    if EPIC_MAP_FILE.exists() and not refresh:
        cache = json.loads(EPIC_MAP_FILE.read_text())

    changed = False
    for sym, name in symbols_and_names:
        if sym in cache:
            continue
        try:
            results = sess.search_markets(name)
        except Exception as e:
            print(f"[epic-map] search failed for {sym} ({name}): {e}", file=sys.stderr)
            continue
        if not results:
            print(f"[epic-map] WARNING: no Capital.com market found for {sym} ({name}) — skipping", file=sys.stderr)
            continue
        chosen, confident = _pick_market_result(sym, name, results)
        if not confident:
            # Never cache (or trade) a guess we're not sure about — a wrong
            # epic here means a live order on the wrong instrument entirely.
            # Skip it this cycle; the run's "no epic resolved" path handles
            # the rest safely. Re-checked next resolve_epic_map() call since
            # it's deliberately left out of the cache.
            print(f"[epic-map] WARNING: no confident Capital.com match for {sym} ({name}) — best candidate "
                  f"was '{chosen.get('instrumentName')}' ({chosen.get('epic')}), NOT trusted. Leaving "
                  f"unmapped rather than risking a wrong-instrument order.", file=sys.stderr)
            continue
        cache[sym] = {"epic": chosen.get("epic"), "instrumentName": chosen.get("instrumentName")}
        changed = True
        print(f"[epic-map] {sym} ({name}) -> epic {chosen.get('epic')} ({chosen.get('instrumentName')})")

    if changed:
        EPIC_MAP_FILE.write_text(json.dumps(cache, indent=2))
    return cache


def get_equity(sess):
    accounts = sess.accounts()
    if not accounts:
        raise RuntimeError("Capital.com returned no accounts")
    acct = next((a for a in accounts if a.get("preferred")), accounts[0])
    balance = acct.get("balance", {})
    equity = balance.get("balance") if isinstance(balance, dict) else None
    if equity is None:
        raise RuntimeError(f"Could not find a balance value in the accounts response — "
                            f"field names may differ from what this script expects. Raw: {acct}")
    return float(equity), acct.get("accountId")


def get_available_margin(sess):
    """Free margin (not full equity) — what's actually left to open a NEW
    position with. Confirmed necessary in practice: a real BTC-USD order
    (2026-08-25) sized purely off the 0.5%-of-equity risk amount was
    rejected by Capital.com with rejectReason RISK_CHECK — its notional
    value, at BTCUSD's 50% marginFactor, needed ~$1,968 of margin against
    only ~$604 available. Risk-based sizing alone doesn't know about
    per-instrument leverage; size_position() below caps against this."""
    accounts = sess.accounts()
    if not accounts:
        raise RuntimeError("Capital.com returned no accounts")
    acct = next((a for a in accounts if a.get("preferred")), accounts[0])
    balance = acct.get("balance", {})
    available = balance.get("available") if isinstance(balance, dict) else None
    return float(available) if available is not None else None


def get_account_currency(sess):
    accounts = sess.accounts()
    if not accounts:
        raise RuntimeError("Capital.com returned no accounts")
    acct = next((a for a in accounts if a.get("preferred")), accounts[0])
    return acct.get("currency")


def fx_rate(sess, from_ccy, to_ccy, rate_cache):
    """1 unit of from_ccy -> this many units of to_ccy. Cached per (from,to)
    pair for the caller's cycle — a handful of currencies at most, no
    reason to hit the API more than once per pair per cycle.

    Found necessary 2026-08-25: this account is AUD-denominated but almost
    every traded instrument (Gold, BTC, US shares, most indices) is
    USD-denominated, and size_position() was comparing an AUD equity/margin
    figure directly against USD prices as if they were the same currency —
    at AUDUSD ~0.715, that overstated real buying power by ~28%, which is
    why EVERY live Capital.com order today was rejected with RISK_CHECK
    regardless of instrument or stop distance: the size computed here
    always came out too large once Capital.com's own risk engine converted
    back to the account's real AUD terms."""
    if from_ccy == to_ccy:
        return 1.0
    if (from_ccy, to_ccy) in rate_cache:
        return rate_cache[(from_ccy, to_ccy)]
    for epic, invert in ((f"{from_ccy}{to_ccy}", False), (f"{to_ccy}{from_ccy}", True)):
        try:
            snap = sess.market_details(epic).get("snapshot", {})
            bid, offer = snap.get("bid"), snap.get("offer")
            if bid is None or offer is None:
                continue
            mid = (float(bid) + float(offer)) / 2
            rate = (1 / mid) if invert else mid
            rate_cache[(from_ccy, to_ccy)] = rate
            return rate
        except Exception:
            continue
    raise RuntimeError(f"could not resolve an FX rate from {from_ccy} to {to_ccy}")


def load_daily_state(tz, env):
    """Keyed by (date, env) — NOT just date. A demo-account baseline is
    meaningless compared against live equity and vice versa; without the
    env key, switching --env mid-day silently reuses the wrong baseline
    and the circuit breaker's math becomes garbage (confirmed happened:
    a live loop inherited a demo day_start_equity of 1000 from earlier
    testing)."""
    today = datetime.now(tz).strftime("%Y-%m-%d")
    if DAILY_STATE_FILE.exists():
        state = json.loads(DAILY_STATE_FILE.read_text())
        if state.get("date") == today and state.get("env") == env:
            return state
    return {"date": today, "env": env, "day_start_equity": None}


def save_daily_state(state):
    DAILY_STATE_FILE.write_text(json.dumps(state, indent=2))


def check_circuit_breaker(sess, tz, env):
    """Returns (halted: bool, reason: str|None, current_equity: float|None)."""
    if HALT_FILE.exists():
        return True, f"manual halt file present ({HALT_FILE})", None

    equity, _ = get_equity(sess)
    state = load_daily_state(tz, env)
    if state["day_start_equity"] is None:
        state["day_start_equity"] = equity
        save_daily_state(state)

    day_start = state["day_start_equity"]
    pnl_pct = (equity - day_start) / day_start if day_start else 0.0
    daily_loss_cap_pct = ts.get("daily_loss_cap_pct")
    if pnl_pct <= -daily_loss_cap_pct:
        halt(f"daily P&L {pnl_pct:.2%} breached -{daily_loss_cap_pct:.0%} cap "
             f"(day-start equity {day_start:.2f}, now {equity:.2f})", tz)
        return True, f"daily loss cap breached ({pnl_pct:.2%} <= -{daily_loss_cap_pct:.0%})", equity
    return False, None, equity


def halt(reason, tz):
    HALT_FILE.write_text(
        f"Auto-halted {datetime.now(tz).isoformat()}: {reason}\n"
        f"Delete this file to resume trading (both Zerodha and Capital.com) once reviewed.\n"
    )


def size_position(sess, epic, entry, stop, equity, strategy, available_margin=None,
                   account_currency=None, rate_cache=None):
    """Position sized so a stop-loss hit costs exactly PER_TRADE_RISK_PCT of
    the relevant capital POOL (never more) — AND, separately, so the position's
    required margin (notional value * the instrument's own marginFactor%) never
    exceeds available_margin. These are two independent caps: the first is
    our own risk discipline, the second is what the broker will actually
    let us open regardless of how tight our stop is. Rounded DOWN to the
    instrument's minimum deal-size step either way. Returns (None, reason)
    if either cap rounds the size below the broker's minimum — the trade
    must be skipped, never oversized to compensate.

    equity/available_margin arrive in the ACCOUNT's currency; entry/stop
    are in the INSTRUMENT's currency (e.g. this account is AUD, most
    instruments here are USD) — both get converted into the instrument's
    currency below before any risk/margin math touches them. Skipping this
    was the actual cause of every live Capital.com order being rejected
    with RISK_CHECK on 2026-08-25: an AUD figure was used as if it were
    USD, overstating real buying power by whatever AUDUSD happened to be.

    `strategy` picks which capital pool this trade risks against
    (INTRADAY_CAPITAL_PCT for BREAKOUT_INTRADAY, the rest for everything
    else) — added 2026-09-02. This only affects NEW entries; positions
    already open under the old whole-equity model are untouched."""
    try:
        details = sess.market_details(epic)
        rules = details.get("dealingRules", {})
        min_size = float((rules.get("minDealSize") or {}).get("value") or 0)
        step = float((rules.get("minStepDistance") or {}).get("value") or 0) or min_size or 0.01
        margin_factor_pct = float((details.get("instrument") or {}).get("marginFactor") or 100)
        instrument_currency = (details.get("instrument") or {}).get("currency")
    except Exception as e:
        return None, f"could not fetch market details for sizing: {e}"

    if account_currency and instrument_currency and account_currency != instrument_currency:
        try:
            rate = fx_rate(sess, account_currency, instrument_currency, rate_cache if rate_cache is not None else {})
        except Exception as e:
            return None, f"could not convert {account_currency}->{instrument_currency} for sizing: {e}"
        equity = equity * rate
        if available_margin is not None:
            available_margin = available_margin * rate

    intraday_capital_pct = ts.get("intraday_capital_pct")
    pool_equity = equity * intraday_capital_pct if strategy == "BREAKOUT_INTRADAY" else equity * (1 - intraday_capital_pct)
    risk_amount = max(pool_equity * ts.get("per_trade_risk_pct"), MIN_RISK_PER_TRADE_USD)
    stop_distance = abs(entry - stop)
    if stop_distance <= 0:
        return None, "invalid stop distance"

    raw_size = risk_amount / stop_distance

    if step <= 0:
        step = 0.01

    margin_capped = False
    if entry > 0 and margin_factor_pct > 0 and available_margin is not None:
        margin_capped_size = available_margin / (entry * margin_factor_pct / 100)
        if margin_capped_size < raw_size:
            raw_size = margin_capped_size
            margin_capped = True

    size = round((raw_size // step) * step, 8)
    if size < min_size or size <= 0:
        cap_note = " (margin-capped — this instrument's leverage limits what's affordable, not the risk math)" if margin_capped else ""
        return None, (f"risk-capped size {raw_size:.6f} rounds to {size}, below the broker minimum "
                       f"{min_size}{cap_note} — skipped rather than oversized")
    return size, None


def _capital_bars_to_df(prices_response):
    """Converts Capital.com's /api/v1/prices response into the same
    Close/High/Low/Volume DataFrame shape trade_monitor.evaluate_symbol()
    expects from yfinance — using the bid/ask MIDPOINT for each OHLC field
    (a neutral reference price, not biased toward either trade direction)."""
    rows = prices_response.get("prices") or []
    if not rows:
        return None

    def mid(bar, field):
        v = bar.get(field) or {}
        bid, ask = v.get("bid"), v.get("ask")
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2

    records = []
    for p in rows:
        c, h, l = mid(p, "closePrice"), mid(p, "highPrice"), mid(p, "lowPrice")
        if c is None or h is None or l is None:
            continue
        records.append({
            "time": p.get("snapshotTimeUTC"),
            "Close": c, "High": h, "Low": l,
            "Volume": p.get("lastTradedVolume") or 0,
        })
    if not records:
        return None
    df = pd.DataFrame(records)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.set_index("time").sort_index()


def fetch_capital_bars(sess, epic, resolution="MINUTE_5", max_points=1000):
    """Live 5-minute bars straight from Capital.com for one epic — the exact
    tradeable instrument and price, not a separately-guessed Yahoo Finance
    ticker. 1000 bars (the API's per-request cap) comfortably covers the
    20-bar lookback + 14-period RSI/ATR this strategy needs; used only for
    the live signal check, not the once-daily historical backtest gate
    (that stays on yfinance — Capital.com's 1000-bar cap would mean
    thousands of paginated requests to rebuild a 60-day/5m history across
    the whole universe, slower and more rate-limit risk for no change in
    which symbols get traded)."""
    try:
        resp = sess.prices(epic, resolution=resolution, max_points=max_points)
    except Exception as e:
        print(f"[capital-data] prices fetch failed for {epic}: {e}", file=sys.stderr)
        return None
    return _capital_bars_to_df(resp)


def evaluate_capitalcom_signals(sess, session, all_passing, epic_map, gate_results=None):
    """BREAKOUT_INTRADAY. Reuses trade_monitor's own evaluate_symbol()
    directly, so 'what counts as a signal' is byte-for-byte identical to
    what the plain monitor prints — only the price data source differs
    (Capital.com's own feed, keyed by the already-resolved, broker-confirmed
    epic, instead of Yahoo Finance). A symbol with no trusted epic mapping
    is skipped here rather than traded off an unverifiable instrument.
    Tags each result with its strategy name and that symbol's backtested
    edge so pick_best_per_symbol() can compare it against any swing/inbound
    candidate on the same symbol."""
    args = argparse.Namespace(**tm.STRATEGY_DEFAULTS, interval="5m", period="5d")
    gate_results = gate_results or {}
    results = []
    for row in all_passing:
        sym = row["symbol"]
        mapping = epic_map.get(sym)
        if not mapping:
            continue
        df = fetch_capital_bars(sess, mapping["epic"])
        r = tm.evaluate_symbol(sym, df, args)
        if r.get("verdict") in ("LONG", "SHORT"):
            gate = gate_results.get(sym, {})
            r["strategy"] = "BREAKOUT_INTRADAY"
            r["edge_expectancy_r"] = gate.get("bt_expectancy_r")
            r["edge_profit_factor"] = gate.get("bt_profit_factor")
            results.append(r)
    return results


def evaluate_capitalcom_swing_signals(sess, epic_map, watchlist):
    """CNC-equivalent candidates across every ENABLED multi-day strategy on
    Capital.com (BREAKOUT_SWING, INBOUNDTRADEALGO) — a single combined list
    so pick_best_per_symbol() can compare across strategies, not just
    within one. Unlike Zerodha's CNC path, Capital.com CFDs can short
    freely, so both LONG and SHORT are allowed here."""
    candidates = []  # (symbol, strategy)
    if strategy_config.is_enabled("BREAKOUT_SWING"):
        syms = [row["symbol"] for row in watchlist.get("swing", {}).get("capitalcom", {}).get("all_passing", [])]
        candidates += [(s, "BREAKOUT_SWING") for s in syms]
    if strategy_config.is_enabled("INBOUNDTRADEALGO"):
        syms = [row["symbol"] for row in watchlist.get("inbound", {}).get("capitalcom", {}).get("all_passing", [])]
        candidates += [(s, "INBOUNDTRADEALGO") for s in syms]
    if not candidates:
        return []

    gate_lookup = {"BREAKOUT_SWING": watchlist.get("swing_gate_results", {}),
                   "INBOUNDTRADEALGO": watchlist.get("inbound_gate_results", {})}
    results = []
    for sym, strat in candidates:
        mapping = epic_map.get(sym)
        if not mapping:
            continue
        df = fetch_capital_bars(sess, mapping["epic"])
        try:
            if strat == "BREAKOUT_SWING":
                r = tm.evaluate_symbol(sym, df, tm._SWING_ARGS)
            else:
                r = tm.evaluate_symbol_inbound(sym, df)
        except Exception as e:
            print(f"  SWING evaluate crashed for {sym} [{strat}]: {e}", file=sys.stderr)
            continue
        if r.get("verdict") not in ("LONG", "SHORT"):
            continue
        gate = gate_lookup[strat].get(sym, {})
        r["strategy"] = strat
        r["edge_expectancy_r"] = gate.get("bt_expectancy_r")
        r["edge_profit_factor"] = gate.get("bt_profit_factor")
        results.append(r)
    return results


def strategy_breakeven_params(strategy):
    params = tm.inbound_trade_algo.DEFAULTS if strategy == "INBOUNDTRADEALGO" else tm.SWING_STRATEGY_DEFAULTS
    return params.get("breakeven_trigger_r", 0.0), params.get("breakeven_lock_r", 0.0)


def run_capitalcom_cnc_exit_checks(args, tz, sess):
    """Two jobs for every tracked swing/inbound position:

    1. Max-hold-days forced exit — Capital.com's own stop/profit levels
       handle price-based exits automatically (bundled into
       create_position), but nothing on the broker side enforces a
       strategy's max_hold_bars time limit, so this does.
    2. Breakeven stop move (added 2026-08-29, user request): once a position
       is up breakeven_trigger_r, amend its stopLevel to lock in
       breakeven_lock_r of that gain via update_position — unlike Zerodha's
       GTT, Capital.com lets the stop on an open position be amended
       in-place, no delete+replace needed."""
    checked_at = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z")
    today = datetime.now(tz).date()
    swing_state = load_swing_positions()
    for epic, pos in list(swing_state.items()):
        opened = datetime.strptime(pos["opened_at"], "%Y-%m-%d").date()
        held = trading_days_between(opened, today)
        strat = pos.get("strategy", "BREAKOUT_SWING")
        max_hold = strategy_max_hold_bars(strat)

        trigger_r, lock_r = strategy_breakeven_params(strat)
        if trigger_r > 0 and not pos.get("breakeven_moved") and pos.get("entry") is not None:
            try:
                snap = sess.market_details(epic).get("snapshot", {})
                direction = pos.get("direction", "BUY")
                last_price = snap.get("bid") if direction == "BUY" else snap.get("offer")
            except Exception:
                last_price = None
            if last_price is not None:
                entry, stop = pos["entry"], pos["stop"]
                risk = abs(entry - stop)
                trigger_price = entry + trigger_r * risk if direction == "BUY" else entry - trigger_r * risk
                reached = last_price >= trigger_price if direction == "BUY" else last_price <= trigger_price
                if risk > 0 and reached:
                    new_stop = entry + lock_r * risk if direction == "BUY" else entry - lock_r * risk
                    if not args.live:
                        print(f"  SWING DRY-RUN BREAKEVEN {pos.get('symbol', epic)} [{strat}]: last={last_price} "
                              f"past trigger {trigger_price:.4f} — would move stop {stop} -> {new_stop:.4f}")
                        pos["breakeven_moved"] = True
                        swing_state[epic] = pos
                        save_swing_positions(swing_state)
                    else:
                        try:
                            sess.update_position(pos["deal_id"], stop_level=new_stop)
                            pos["stop"], pos["breakeven_moved"] = new_stop, True
                            swing_state[epic] = pos
                            save_swing_positions(swing_state)
                            print(f"  SWING BREAKEVEN {pos.get('symbol', epic)} [{strat}]: stop moved "
                                  f"{stop} -> {new_stop:.4f} (last={last_price})")
                        except Exception as e:
                            print(f"  WARNING: breakeven stop move failed for {pos.get('symbol', epic)} "
                                  f"[{strat}]: {e} — original stop left as-is, will retry next cycle",
                                  file=sys.stderr)

        if held < max_hold:
            continue
        row = {"checked_at": checked_at, "env": args.env, "symbol": pos.get("symbol", epic), "name": pos.get("symbol", epic),
               "verdict": "EXIT", "entry": None, "stop": pos.get("stop"), "target": pos.get("target"),
               "epic": epic, "size": pos.get("size"), "mode": "live" if args.live else "dry-run",
               "opened_at": pos["opened_at"], "held_trading_days": held, "strategy": strat, "deal_id": pos.get("deal_id")}
        if not args.live:
            row["outcome"], row["reason"] = "dry-run", f"would force-exit (held {held} trading days, max {max_hold})"
            print(f"  SWING DRY-RUN EXIT {pos.get('symbol', epic)} [{strat}]: held {held} trading days, past max hold")
            log_swing_decision(row)
            continue
        try:
            sess.close_position(pos["deal_id"])
            row["outcome"], row["reason"] = "exited", f"max-hold exit after {held} trading days"
            print(f"  SWING EXIT {pos.get('symbol', epic)} [{strat}] (held {held} trading days, past max hold)")
            del swing_state[epic]
            save_swing_positions(swing_state)
        except Exception as e:
            # A 404 here means the position doesn't exist anymore — it was
            # ALREADY closed on the broker's side (stop/target hit, expiry,
            # a manual close) before this max-hold check ran. That's the
            # goal already achieved, not a failure: treat it as a normal
            # exit, clean up local state, and move on. Found 2026-09-03
            # after KC=F halted the account 13+ times over several hours —
            # this code previously treated EVERY close_position error alike
            # (including 404) as CRITICAL_EXIT_FAILED and halted trading
            # every single cycle, forever, since the stale entry was never
            # removed from swing_state to stop the retry. Any OTHER error
            # (auth, network, a genuine API failure) still halts exactly as
            # before — only "the position is already gone" is now handled.
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 404:
                row["outcome"], row["reason"] = "already_closed", (
                    f"max-hold check found this position already closed on the broker's side "
                    f"(404 on close attempt after {held} trading days) — removing from local tracking")
                print(f"  SWING ALREADY CLOSED {pos.get('symbol', epic)} [{strat}]: {row['reason']}")
                del swing_state[epic]
                save_swing_positions(swing_state)
            else:
                row["outcome"], row["reason"] = "CRITICAL_EXIT_FAILED", f"max-hold exit failed: {e}"[:300]
                print(f"  *** CRITICAL: SWING exit failed for {pos.get('symbol', epic)}: {e} — position likely still open, VERIFY ***", file=sys.stderr)
                halt(row["reason"], tz)
        log_swing_decision(row)


def get_open_position_epics(sess):
    """Epics we already hold a position in — checked before placing any
    new order so a signal that stays active across consecutive cycles
    can't pyramid into repeated entries on the same instrument."""
    try:
        positions = sess.positions()
    except Exception as e:
        print(f"  WARNING: could not fetch open positions ({e}) — proceeding without duplicate-entry protection this cycle", file=sys.stderr)
        return set()
    epics = set()
    for p in positions:
        epic = (p.get("market") or {}).get("epic") or p.get("epic")
        if epic:
            epics.add(epic)
    return epics


US_STOCK_MARKET_OPEN_UTC = (13, 30)   # NYSE/NASDAQ real cash-market hours — narrower
US_STOCK_MARKET_CLOSE_UTC = (20, 0)   # than our broader "us" session bucket (13:00-21:00 UTC),
                                       # which exists for futures/commodities/crypto that trade
                                       # nearly 24h. Individual stock CFDs only fill inside this.


def us_stock_market_open(now_utc):
    if now_utc.weekday() >= 5:
        return False
    t = now_utc.hour * 60 + now_utc.minute
    return (US_STOCK_MARKET_OPEN_UTC[0] * 60 + US_STOCK_MARKET_OPEN_UTC[1]) <= t < \
           (US_STOCK_MARKET_CLOSE_UTC[0] * 60 + US_STOCK_MARKET_CLOSE_UTC[1])


def revalidate_against_live_price(sess, epic, direction, stop, target):
    """
    Signals are computed from a batch data fetch, then epic resolution and
    position sizing each make their own network calls — by the time we're
    about to submit, real seconds (sometimes more, for a fast mover) have
    passed and the live price can have moved past our stop or target
    already. Confirmed in practice: a stock that moved +1.5% in that gap
    made our take-profit invalid (already below the live price) and
    Capital.com correctly rejected it. Re-check against a fresh quote
    immediately before submitting rather than trusting the earlier calc.
    """
    try:
        details = sess.market_details(epic)
        snap = details.get("snapshot", {})
        live_price = snap.get("offer") if direction == "BUY" else snap.get("bid")
    except Exception as e:
        return False, f"could not fetch live price to re-validate: {e}", None
    if live_price is None:
        return False, "no live price available to re-validate against", None

    if direction == "BUY":
        if live_price >= target:
            return False, f"live price {live_price} already at/past target {target} — setup no longer valid", live_price
        if live_price <= stop:
            return False, f"live price {live_price} already at/past stop {stop} — setup no longer valid", live_price
    else:
        if live_price <= target:
            return False, f"live price {live_price} already at/past target {target} — setup no longer valid", live_price
        if live_price >= stop:
            return False, f"live price {live_price} already at/past stop {stop} — setup no longer valid", live_price
    return True, None, live_price


def place_capital_position(sess, epic, direction, size, stop, target):
    """Shared entry mechanics for EVERY strategy — Capital.com's POST
    /positions only acknowledges the request (returns a dealReference);
    this confirms it to get the real fill status and the dealId a later
    max-hold exit will need to close it. Returns a dict: outcome
    ('placed'/'rejected'/'error'/'CRITICAL_UNKNOWN'), reason, deal_id."""
    try:
        result = sess.create_position(epic, direction, size, stop_level=stop, profit_level=target)
        deal_ref = result.get("dealReference") if isinstance(result, dict) else None
    except Exception as e:
        return {"outcome": "error", "reason": f"entry order failed: {e}"[:300], "deal_id": None}
    if not deal_ref:
        return {"outcome": "error", "reason": f"no dealReference in response: {result}"[:300], "deal_id": None}

    try:
        confirm = sess.confirm_deal(deal_ref)
    except Exception as e:
        reason = (f"could not confirm dealReference {deal_ref}: {e} — a position MAY be open, "
                  f"check Capital.com directly, now.")
        print(f"  *** CRITICAL: {reason} ***", file=sys.stderr)
        return {"outcome": "CRITICAL_UNKNOWN", "reason": reason, "deal_id": None}

    status = (confirm or {}).get("dealStatus") or (confirm or {}).get("status")
    deal_id = (confirm or {}).get("dealId")
    if status != "ACCEPTED":
        return {"outcome": "rejected", "reason": f"deal {deal_ref} status={status}: {json.dumps(confirm)[:200]}",
                "deal_id": deal_id}
    return {"outcome": "placed", "reason": f"dealReference={deal_ref}, dealId={deal_id}", "deal_id": deal_id}


def run_trading_once(args, tz, watchlist):
    """Unified per-cycle trading pass across EVERY enabled strategy on
    Capital.com: gathers BREAKOUT_INTRADAY candidates for the currently
    active session plus BREAKOUT_SWING/INBOUNDTRADEALGO candidates
    (broker-wide, not session-restricted, same as trade_monitor's own swing
    check), resolves any symbol with more than one simultaneous signal to
    whichever strategy has the stronger backtested edge on that instrument
    (tm.pick_best_per_symbol — "execute based on best strategy"), then
    dispatches each winner. Capital.com has no MIS/CNC product-type split
    the way Zerodha does — a position is a position — so the SAME open-
    positions check (open_epics) governs every strategy here: one position
    per epic, ever, regardless of which strategy signaled it."""
    checked_at = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z")
    sess = CapitalSession()
    try:
        sess.login()
    except Exception as e:
        print(f"[{checked_at}] Capital.com login failed: {e}", file=sys.stderr)
        return

    halted, reason, equity = check_circuit_breaker(sess, tz, args.env)
    if halted:
        print(f"[{checked_at}] TRADING HALTED — {reason}")
        return
    print(f"[{checked_at}] equity={equity:.2f}  circuit breaker OK")

    run_capitalcom_cnc_exit_checks(args, tz, sess)

    now_utc = datetime.now(timezone.utc)
    session = tm.current_capitalcom_session(now_utc.hour)
    intraday_all_passing = (watchlist["buckets"].get(session, {}).get("all_passing", [])
                             if strategy_config.is_enabled("BREAKOUT_INTRADAY") else [])
    swing_all_passing = (watchlist.get("swing", {}).get("capitalcom", {}).get("all_passing", [])
                         if strategy_config.is_enabled("BREAKOUT_SWING") else [])
    inbound_all_passing = (watchlist.get("inbound", {}).get("capitalcom", {}).get("all_passing", [])
                           if strategy_config.is_enabled("INBOUNDTRADEALGO") else [])

    # Epics must be resolved BEFORE evaluating signals (evaluate_*_signals
    # fetch each candidate's price history straight from Capital.com, which
    # requires already knowing its epic) — one combined resolve for every
    # symbol any of the three strategies might need this cycle.
    all_syms_names = {row["symbol"]: row["name"] for row in intraday_all_passing + swing_all_passing + inbound_all_passing}
    if not all_syms_names:
        print(f"[{checked_at}] {session.upper()}: no gate-passing candidates this session.")
        return
    epic_map = resolve_epic_map(sess, list(all_syms_names.items()), refresh=args.refresh_epics)

    mis_candidates = (evaluate_capitalcom_signals(sess, session, intraday_all_passing, epic_map, watchlist.get("gate_results", {}))
                       if intraday_all_passing else [])
    swing_candidates = evaluate_capitalcom_swing_signals(sess, epic_map, watchlist)
    all_candidates = mis_candidates + swing_candidates
    if not all_candidates:
        print(f"[{checked_at}] {session.upper()}: no actionable signals this check.")
        return

    winners, losers = tm.pick_best_per_symbol(all_candidates)
    for c in losers:
        print(f"  SKIP {c['symbol']} [{c['strategy']}]: lower-ranked than another strategy's signal on the same "
              f"symbol this cycle (this strategy's edge here: exp={c.get('edge_expectancy_r')}R)")

    open_epics = get_open_position_epics(sess) if args.live else set()
    swing_state = load_swing_positions()
    try:
        available_margin = get_available_margin(sess)
    except Exception as e:
        print(f"  WARNING: could not read available margin this cycle ({e}) — sizing on risk alone, "
              f"without the leverage cap", file=sys.stderr)
        available_margin = None
    try:
        account_currency = get_account_currency(sess)
    except Exception as e:
        print(f"  WARNING: could not read account currency this cycle ({e}) — sizing without "
              f"currency conversion, which is wrong whenever it differs from an instrument's own "
              f"currency", file=sys.stderr)
        account_currency = None
    fx_cache = {}

    for r in winners:
        strat = r["strategy"]
        is_cnc = strat in ("BREAKOUT_SWING", "INBOUNDTRADEALGO")
        sym = r["symbol"]
        mapping = epic_map.get(sym)
        log_fn = log_swing_decision if is_cnc else log_decision
        row = {"checked_at": checked_at, "env": args.env, "symbol": sym, "name": r["name"], "verdict": r["verdict"],
               "entry": r["entry"], "stop": r["stop"], "target": r["target"],
               "epic": mapping.get("epic") if mapping else None,
               "size": None, "mode": "live" if args.live else "dry-run", "outcome": None, "reason": None,
               "strategy": strat, "opened_at": None, "held_trading_days": None, "deal_id": None}

        if not mapping:
            row["outcome"], row["reason"] = "skipped", "no Capital.com epic resolved for this symbol"
            print(f"  SKIP {sym} [{strat}]: {row['reason']}")
            log_fn(row)
            continue
        epic = mapping["epic"]

        if epic in open_epics:
            row["outcome"], row["reason"] = "skipped", "already have an open position in this instrument — not pyramiding"
            print(f"  SKIP {sym} [{strat}]: {row['reason']}")
            log_fn(row)
            continue

        if tm.INSTRUMENTS.get(sym, {}).get("class") == "stock" and not us_stock_market_open(datetime.now(timezone.utc)):
            row["outcome"], row["reason"] = "skipped", (
                f"US stock market not actually open yet (real hours {US_STOCK_MARKET_OPEN_UTC[0]:02d}:"
                f"{US_STOCK_MARKET_OPEN_UTC[1]:02d}-{US_STOCK_MARKET_CLOSE_UTC[0]:02d}:{US_STOCK_MARKET_CLOSE_UTC[1]:02d} UTC, "
                f"narrower than our broader US session window)")
            print(f"  SKIP {sym} [{strat}]: {row['reason']}")
            log_fn(row)
            continue

        size, size_reason = size_position(sess, epic, r["entry"], r["stop"], equity, strat, available_margin,
                                           account_currency, fx_cache)
        if size is None:
            row["outcome"], row["reason"] = "skipped", size_reason
            print(f"  SKIP {sym} [{strat}]: {size_reason}")
            log_fn(row)
            continue
        row["size"] = size

        direction = "BUY" if r["verdict"] == "LONG" else "SELL"
        if not args.live:
            row["outcome"], row["reason"] = "dry-run", "would place this order — use --live --confirm to actually trade"
            print(f"  DRY-RUN {direction} {sym} [{strat}] size={size} entry~{r['entry']} stop={r['stop']} target={r['target']}")
            log_fn(row)
            continue

        still_valid, revalidate_reason, live_price = revalidate_against_live_price(sess, epic, direction, r["stop"], r["target"])
        if not still_valid:
            row["outcome"], row["reason"] = "skipped", f"stale by execution time: {revalidate_reason}"
            print(f"  SKIP {sym} [{strat}]: {row['reason']}")
            log_fn(row)
            continue

        placed = place_capital_position(sess, epic, direction, size, r["stop"], r["target"])
        row["outcome"], row["reason"], row["deal_id"] = placed["outcome"], placed["reason"], placed["deal_id"]
        if placed["outcome"] == "placed":
            print(f"  PLACED {direction} {sym} [{strat}] size={size} stop={r['stop']} target={r['target']} dealId={placed['deal_id']}")
            open_epics.add(epic)  # so another winner this same cycle can't also target this epic
            if is_cnc:
                opened_at = datetime.now(tz).date().strftime("%Y-%m-%d")
                swing_state[epic] = {"symbol": sym, "strategy": strat, "opened_at": opened_at, "size": size,
                                      "entry": r["entry"], "direction": direction,
                                      "stop": r["stop"], "target": r["target"], "deal_id": placed["deal_id"],
                                      "breakeven_moved": False}
                save_swing_positions(swing_state)
                row["opened_at"], row["held_trading_days"] = opened_at, 0
        else:
            stream = sys.stderr if "CRITICAL" in placed["outcome"] else sys.stdout
            print(f"  {sym} [{strat}]: {placed['outcome']} — {placed['reason']}", file=stream)
        log_fn(row)


def build_tm_args():
    """Args for trade_monitor's build_watchlist/refresh_ranking/run_once —
    this makes auto_trader.py fully self-sufficient (it keeps the watchlist
    fresh itself, on the same daily-gate + hourly-rerank schedule
    trade_monitor.py uses) rather than depending on that script also being
    run separately.

    Gate thresholds (min_trades/min_profit_factor/min_expectancy/min_win_rate)
    read from trading_settings.py (2026-09-03, user request) — dashboard-
    editable. Reload semantics: this Namespace is built ONCE at process
    start and reused for the life of the loop, so a settings change only
    takes effect on the next process restart (or immediately for a
    standalone `trade_monitor.py --rebuild-watchlist` run triggered via the
    Control Panel, since that's a fresh process) — see trading_settings.py's
    module docstring for the full reload-semantics table."""
    return argparse.Namespace(
        **tm.STRATEGY_DEFAULTS,
        top=5, w_vol=0.40, w_momentum=0.35, w_liquidity=0.25,
        backtest_period="60d", backtest_interval="5m",
        min_trades=ts.get("intraday_min_trades"),
        min_profit_factor=ts.get("intraday_min_profit_factor"),
        min_expectancy=ts.get("intraday_min_expectancy"),
        min_win_rate=ts.get("intraday_min_win_rate"),
        refresh_hours=1.0,
        interval="5m", period="5d", quiet=True, log=True,
    )


def main():
    p = argparse.ArgumentParser(description="Capital.com auto-trader with hard risk caps — see module docstring before use")
    p.add_argument("--env", choices=["demo", "live"], default=None,
                    help="Which Capital.com account to trade: 'demo' (fake money) or 'live' (real money). "
                         "Overrides CAPITAL_ENV in automation/.env for this run. Defaults to whatever .env says "
                         "(itself defaulting to demo) if not passed.")
    p.add_argument("--live", action="store_true", help="Actually place orders (on whichever --env account). Requires --confirm too.")
    p.add_argument("--confirm", default="", help='Must exactly equal "I UNDERSTAND" to allow --live')
    p.add_argument("--once", action="store_true")
    p.add_argument("--loop-seconds", type=int, default=300)
    p.add_argument("--tz", default=tm.DEFAULT_TZ_NAME)
    p.add_argument("--refresh-epics", action="store_true", help="Re-resolve the symbol->epic map instead of using the cache")
    args = p.parse_args()

    if args.env:
        os.environ["CAPITAL_ENV"] = args.env  # overrides automation/.env for this process only
    args.env = os.environ.get("CAPITAL_ENV", "demo").lower()

    if args.live and args.confirm != "I UNDERSTAND":
        print('Refusing to run live: pass --confirm "I UNDERSTAND" exactly, alongside --live.', file=sys.stderr)
        sys.exit(1)
    if args.once:
        args.loop_seconds = 0

    tz = ZoneInfo(args.tz)
    env_label = ("DEMO — fake money, safe to test" if args.env == "demo"
                 else "LIVE — REAL MONEY ACCOUNT")
    order_label = "ORDERS WILL BE PLACED" if args.live else "DRY RUN — no orders will be placed"
    print("=" * 72)
    print(f"  Capital.com account:  {env_label}")
    print(f"  Order mode:           {order_label}")
    print("=" * 72)
    if args.env == "live":
        print("  *** REAL MONEY IS AT RISK ON THIS RUN ***")
    print(f"Per-trade risk cap: {ts.get('per_trade_risk_pct'):.1%} of equity | Daily loss circuit breaker: {ts.get('daily_loss_cap_pct'):.0%}")
    if HALT_FILE.exists():
        print(f"NOTE: {HALT_FILE} currently exists — trading is halted until it's removed.")

    # Self-sufficient: builds/refreshes the watchlist itself (same daily
    # gate + hourly re-rank schedule as trade_monitor.py) so this script
    # alone is enough to run — no separate trade_monitor.py process needed.
    tm_args = build_tm_args()
    watchlist = tm.build_watchlist(tm_args, tz, force=False)
    tm.print_startup_banner(tz)

    # Check credentials ONCE — if missing, keep running for visibility (the
    # watchlist/signal monitoring below needs no Capital.com credentials at
    # all) but skip the trading pass every cycle instead of repeating the
    # same failure every 5 minutes.
    trading_enabled = True
    try:
        capital_credentials()
    except Exception as e:
        trading_enabled = False
        print(f"NOTE: {e}\nCapital.com trading is DISABLED for this run — showing signals only. "
              f"Fill in automation/.env and restart to enable trading.\n")

    while True:
        try:
            if watchlist.get("as_of_date") != datetime.now(tz).strftime("%Y-%m-%d"):
                watchlist = tm.build_watchlist(tm_args, tz, force=True)
            elif tm.ranking_stale(watchlist, tm_args.refresh_hours):
                watchlist = tm.refresh_ranking(watchlist, tm_args, tz)

            # Capital.com-only visibility — Zerodha has its own process
            # (zerodha_trader.py) showing its own visibility now, so each
            # process is self-sufficient and neither depends on the other
            # actually being up/interleaving its output correctly.
            now_utc = datetime.now(timezone.utc)
            capitalcom_bucket = tm.current_capitalcom_session(now_utc.hour)
            tm.run_once(watchlist, tm_args, tz, buckets_override=[capitalcom_bucket], swing_brokers=("capitalcom",))
            if trading_enabled:
                run_trading_once(args, tz, watchlist)  # Capital.com signals only: sized, risk-capped, placed/dry-run
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
        if args.loop_seconds <= 0:
            break
        time.sleep(args.loop_seconds)


if __name__ == "__main__":
    main()
