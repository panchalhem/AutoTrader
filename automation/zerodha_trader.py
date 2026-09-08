#!/usr/bin/env python3
"""
zerodha_trader.py — places REAL orders on Zerodha (NSE cash equities, MIS
intraday) from trade_monitor.py's live signals, with hard risk caps
enforced in code. Mirrors automation/auto_trader.py's design (same risk
caps, same safety philosophy) but the actual order mechanics differ
significantly from Capital.com, see below.

################################################################################
# THIS SCRIPT CAN PLACE REAL ORDERS WITH REAL MONEY.
# It defaults to dry-run. Live order placement requires BOTH --live AND
# --confirm "I UNDERSTAND" (exact string) at the same time.
#
# Zerodha has NO sandbox/paper-trading account — unlike Capital.com,
# --env demo is SIMULATED here: it behaves exactly like dry-run (nothing
# is ever sent to the real broker). Only --env live places real orders.
################################################################################

Hard risk caps — same numbers as auto_trader.py, not CLI flags on purpose:
    PER_TRADE_RISK_PCT = 0.005   0.5% of account equity (Zerodha margins)
    DAILY_LOSS_CAP_PCT  = 0.04   4% of account equity, halts new orders

WHY ZERODHA'S ORDER FLOW IS RISKIER THAN CAPITAL.COM'S — READ THIS:
Capital.com attaches a stop-loss and take-profit in the SAME API call that
opens a position — the position is never unprotected, even for a moment.
Zerodha has no equivalent single call: entry is one order, protection
(stop + target) is a SEPARATE two-leg GTT order placed afterward. That
means there's a real failure mode: the entry fills, but the GTT call then
fails (network blip, rate limit, bad params) — leaving a REAL, OPEN,
UNPROTECTED position. This script handles that explicitly:
    1. Place entry (MARKET, product=MIS).
    2. Poll order history until it's COMPLETE (or REJECTED/CANCELLED, in
       which case nothing is open and we stop here) — up to ~10 seconds.
    3. If still not resolved after that: treat as UNKNOWN STATE. Halt all
       further trading (output/TRADING_HALTED) and print/log a CRITICAL
       message telling you to check Zerodha directly. Do NOT guess.
    4. If entry is COMPLETE: place the two-leg GTT (stop + target).
    5. If the GTT call fails: the position IS open and IS unprotected.
       This script immediately attempts to flatten it with an opposite
       MARKET order (safety net), halts all trading, and logs a CRITICAL
       line either way (whether the flatten succeeded or not) — always
       verify manually against Zerodha after seeing this.

Other safety mechanisms (same as auto_trader.py):
    - Daily circuit breaker on account equity (margins.equity.net),
      snapshotted at the start of each local trading day.
    - Manual kill switch: output/TRADING_HALTED (shared with auto_trader.py
      — either script tripping it halts both, since it's one account risk
      picture even though the brokers are separate).
    - Position size that would round to 0 whole shares is skipped, never
      bumped up to 1 share past the risk cap.
    - Every decision logged to output/zerodha_trader_log.csv.

Usage — ALWAYS dry-run first, repeatedly:
    ./.venv/bin/python automation/zerodha_trader.py --once
    ./.venv/bin/python automation/zerodha_trader.py --once --debug-login   # if login automation needs debugging

    # only once you trust it:
    ./.venv/bin/python automation/zerodha_trader.py --live --confirm "I UNDERSTAND" --loop-seconds 300

UNTESTED against the real Kite Connect API and the real Zerodha login page
— no credentials were available in the environment that wrote this. Expect
to need to debug the login selectors (see zerodha_api.py) and possibly
adjust field names once run against your real account.
"""
import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from zerodha_api import KiteSession, credentials as zerodha_credentials
import trade_monitor as tm
import strategy_config
import trading_settings as ts

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "output"
OUT_DIR.mkdir(exist_ok=True)

HALT_FILE = OUT_DIR / "TRADING_HALTED"  # shared with auto_trader.py, deliberately
DAILY_STATE_FILE = OUT_DIR / "zerodha_daily_risk_state.json"
TRADE_LOG = OUT_DIR / "zerodha_trader_log.csv"
# Re-added 2026-08-29 (reverses the 2026-08-26 MIS-for-everything decision,
# see the module comment above evaluate_zerodha_cnc_signals — this file
# path/behavior is the same as before that change): BREAKOUT_SWING and
# INBOUNDTRADEALGO now execute as CNC again (multi-day cash/delivery hold,
# no same-day square-off), tracked here since Zerodha's own position list
# carries no entry price, strategy, or open-date. Mirrors
# auto_trader.py's SWING_POSITIONS_FILE/load_swing_positions pattern.
SWING_POSITIONS_FILE = OUT_DIR / "zerodha_swing_positions.json"
SWING_TRADE_LOG = OUT_DIR / "zerodha_swing_trader_log.csv"

# PER_TRADE_RISK_PCT, DAILY_LOSS_CAP_PCT, and INTRADAY_CAPITAL_PCT moved into
# trading_settings.py (2026-09-03, user request) — see auto_trader.py's
# matching comment for full rationale. Read live via ts.get(...) at point of
# use, not cached at import.
MIN_STOP_DISTANCE_RS = 10.0  # Zerodha-only floor on entry-to-stop distance (user-requested
                             # safety net, 2026-08-25) — ATR-based stops are normally well
                             # above this, but this guards against a thin-ATR quiet stock
                             # ever producing an unreasonably tight stop. Widens the stop
                             # only (never the target), applied before position sizing so
                             # qty correctly shrinks for the wider risk. Capital.com is
                             # untouched — its instruments (indices, forex, crypto, US
                             # equities) don't share NSE's price scale, so a flat rupee
                             # floor wouldn't translate the same way there.

FILL_POLL_ATTEMPTS = 10
FILL_POLL_DELAY_SECONDS = 1


# Fixed column set — see trade_monitor.SIGNAL_LOG_COLUMNS for why this
# can't just be list(row.keys()).
TRADE_LOG_COLUMNS = ["checked_at", "env", "symbol", "name", "verdict", "entry", "stop", "target",
                     "qty", "mode", "outcome", "reason", "strategy"]


def log_decision(row):
    header = not TRADE_LOG.exists()
    with open(TRADE_LOG, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_LOG_COLUMNS, extrasaction="ignore")
        if header:
            w.writeheader()
        w.writerow({k: row.get(k) for k in TRADE_LOG_COLUMNS})


SWING_TRADE_LOG_COLUMNS = ["checked_at", "env", "symbol", "name", "verdict", "entry", "stop", "target",
                           "qty", "mode", "outcome", "reason", "strategy", "opened_at", "held_trading_days"]


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
    """Weekday count between two date objects — see auto_trader.py's
    identical helper; kept duplicated rather than shared since the two
    trader scripts are otherwise independent (no shared import between
    them beyond trade_monitor/strategy_config)."""
    days, d = 0, start_date
    while d < end_date:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


def strategy_swing_params(strategy):
    """(max_hold_bars, breakeven_trigger_r, breakeven_lock_r) for a swing
    strategy — pulled from the same params dict the backtest gate uses, so
    live behavior can't silently drift from what was actually backtested."""
    params = tm.inbound_trade_algo.DEFAULTS if strategy == "INBOUNDTRADEALGO" else tm.SWING_STRATEGY_DEFAULTS
    return (params["max_hold_bars"], params.get("breakeven_trigger_r", 0.0), params.get("breakeven_lock_r", 0.0))


def get_equity(sess):
    m = sess.margins()
    net = (m or {}).get("equity", {}).get("net")
    if net is None:
        raise RuntimeError(f"Could not find equity net margin in response — field names may differ. Raw: {m}")
    net = float(net)
    if net <= 0:
        # A real account reading zero (or negative) equity is far more
        # likely a flaky/malformed API response (seen in practice: Zerodha's
        # margins endpoint intermittently 500s or returns a degenerate
        # payload) than an actual wipeout — trust neither for circuit-breaker
        # math. Treat as a failed read, not a legitimate data point, so a
        # transient glitch can't trip a false "-100% loss" halt.
        raise RuntimeError(f"Implausible equity reading ({net}) — treating as a failed/unreliable API "
                            f"response rather than a real balance. Raw: {m}")
    return net


def load_daily_state(tz):
    today = datetime.now(tz).strftime("%Y-%m-%d")
    if DAILY_STATE_FILE.exists():
        state = json.loads(DAILY_STATE_FILE.read_text())
        if state.get("date") == today:
            return state
    return {"date": today, "day_start_equity": None}


def save_daily_state(state):
    DAILY_STATE_FILE.write_text(json.dumps(state, indent=2))


def halt(reason, tz):
    HALT_FILE.write_text(
        f"Auto-halted {datetime.now(tz).isoformat()} by zerodha_trader.py: {reason}\n"
        f"Delete this file to resume trading (both Zerodha and Capital.com) once reviewed.\n"
    )


def check_circuit_breaker(sess, tz):
    if HALT_FILE.exists():
        return True, f"manual/auto halt file present ({HALT_FILE})", None
    equity = get_equity(sess)
    state = load_daily_state(tz)
    if state["day_start_equity"] is None:
        state["day_start_equity"] = equity
        save_daily_state(state)
    day_start = state["day_start_equity"]
    pnl_pct = (equity - day_start) / day_start if day_start else 0.0
    daily_loss_cap_pct = ts.get("daily_loss_cap_pct")
    if pnl_pct <= -daily_loss_cap_pct:
        halt(f"daily P&L {pnl_pct:.2%} breached -{daily_loss_cap_pct:.0%} cap "
             f"(day-start equity {day_start:.2f}, now {equity:.2f})", tz)
        return True, f"daily loss cap breached ({pnl_pct:.2%})", equity
    return False, None, equity


def get_available_cash(sess):
    """CNC (cash/delivery) has no intraday leverage — the full purchase cost
    must be covered by actual free cash, unlike MIS where the risk-based
    size alone was sufficient (margin funded the rest). Falls back to None
    (no cap applied) if the response shape doesn't have this field, rather
    than guessing or blocking every CNC trade on a parsing miss."""
    try:
        m = sess.margins()
        avail = (m or {}).get("equity", {}).get("available", {})
        cash = avail.get("live_balance", avail.get("cash"))
        return float(cash) if cash is not None else None
    except Exception:
        return None


def size_position(entry, stop, equity, strategy, available_cash=None):
    """NSE cash equity — whole shares only, no fractional/lot sizing.
    `available_cash`, when given (CNC only — see get_available_cash), caps
    qty so the purchase actually fits in free cash, not just the risk-based
    figure that assumed leveraged (MIS) buying power.

    `strategy` picks which capital pool this trade risks against
    (INTRADAY_CAPITAL_PCT for BREAKOUT_INTRADAY, the rest for everything
    else) — added 2026-09-02. Only affects NEW entries; already-open
    positions sized under the old whole-equity model are untouched."""
    intraday_capital_pct = ts.get("intraday_capital_pct")
    pool_equity = equity * intraday_capital_pct if strategy == "BREAKOUT_INTRADAY" else equity * (1 - intraday_capital_pct)
    risk_amount = pool_equity * ts.get("per_trade_risk_pct")
    stop_distance = abs(entry - stop)
    if stop_distance <= 0:
        return None, "invalid stop distance"
    qty = int(risk_amount // stop_distance)
    if available_cash is not None and entry > 0:
        cash_qty = int(available_cash // entry)
        if cash_qty < qty:
            qty = cash_qty
    if qty < 1:
        return None, (f"risk-capped size {risk_amount / stop_distance:.3f} shares rounds to 0 "
                       f"(or exceeds available cash) — skipped rather than buying past the cap")
    return qty, None


def wait_for_fill(sess, order_id):
    """Polls order history until COMPLETE/REJECTED/CANCELLED or the poll
    budget runs out. Returns (status, history)."""
    status = None
    history = []
    for _ in range(FILL_POLL_ATTEMPTS):
        try:
            history = sess.order_history(order_id) or []
        except Exception:
            history = []
        if history:
            status = history[-1].get("status")
            if status in ("COMPLETE", "REJECTED", "CANCELLED"):
                return status, history
        time.sleep(FILL_POLL_DELAY_SECONDS)
    return status, history  # None or a non-terminal status: unknown/timed out


def evaluate_zerodha_mis_signals():
    """BREAKOUT_INTRADAY (MIS) — reuses trade_monitor's own evaluate_symbol()
    so this never drifts from what the monitor displays. Only meaningful
    when NSE is open. Tags each result with its strategy name and that
    symbol's backtested edge (from gate_results) so pick_best_per_symbol()
    can compare it against any CNC candidate on the same symbol."""
    now_utc = datetime.now(timezone.utc)
    if not tm.is_nse_open(now_utc):
        return []
    if not tm.WATCHLIST_LATEST.exists():
        return []
    watchlist = json.loads(tm.WATCHLIST_LATEST.read_text())
    bucket_data = watchlist["buckets"].get("zerodha", {})
    all_passing = bucket_data.get("all_passing", [])
    symbols = [row["symbol"] for row in all_passing]
    if not symbols:
        return []
    args = argparse.Namespace(**tm.STRATEGY_DEFAULTS, interval="5m", period="5d")
    intraday = tm.fetch_batch(symbols, args.interval, args.period)
    gate_results = watchlist.get("gate_results", {})
    results = []
    for sym in symbols:
        r = tm.evaluate_symbol(sym, intraday.get(sym), args)
        if r.get("verdict") in ("LONG", "SHORT"):
            gate = gate_results.get(sym, {})
            r["strategy"] = "BREAKOUT_INTRADAY"
            r["edge_expectancy_r"] = gate.get("bt_expectancy_r")
            r["edge_profit_factor"] = gate.get("bt_profit_factor")
            results.append(r)
    return results


################################################################################
# DAILY-BAR SIGNALS (BREAKOUT_SWING, INBOUNDTRADEALGO) — product=CNC (cash/
# delivery), reverted 2026-08-29 back to how this originally worked: on
# 2026-08-26 these were switched to product=MIS (forcing Zerodha's own
# same-day square-off), which meant neither strategy actually traded under
# the multi-day-hold conditions its backtest was validated against —
# BREAKOUT_SWING's edge depends on running for up to max_hold_bars days, and
# INBOUNDTRADEALGO's own module docstring already found its true intraday
# variant loses money (expectancy -0.24R to -0.31R). User explicitly asked
# (2026-08-29) for week-long swing holds instead of intraday, which is
# exactly what this reverts back to.
#
# CNC needs three things MIS didn't: (1) sizing capped by actual available
# cash, not just risk-based qty (no intraday leverage) — get_available_cash/
# size_position's available_cash arg; (2) a local position-tracking file
# (SWING_POSITIONS_FILE) since Zerodha's own position list carries no entry
# price/strategy/open-date; (3) a periodic force-exit past max_hold_bars
# trading days, since nothing on the broker side enforces that — see
# run_zerodha_cnc_exit_checks, which also moves the GTT stop to breakeven
# once a position is far enough in profit (breakeven_trigger_r/lock_r in
# SWING_STRATEGY_DEFAULTS / inbound_trade_algo.DEFAULTS).
#
# ONE POSITION PER SYMBOL, REGARDLESS OF STRATEGY: if a symbol has a signal
# from more than one enabled strategy in the same cycle, only the one with
# the strongest backtested edge on that specific instrument executes — see
# trade_monitor.pick_best_per_symbol(). The others are logged as skipped,
# not silently dropped. Still LONG-only for now (a policy choice, not a
# technical limit) — ask if you want SHORT signals from these two enabled
# as well.
################################################################################

def evaluate_zerodha_cnc_signals():
    """Daily-bar candidates across every ENABLED multi-day-signal strategy on
    Zerodha (BREAKOUT_SWING, INBOUNDTRADEALGO) — a single combined list so
    pick_best_per_symbol() can compare across strategies, not just within
    one. LONG only, see the module comment above. (Function name kept from
    when this path was CNC-only — it now feeds MIS execution too.)"""
    now_utc = datetime.now(timezone.utc)
    if not tm.is_nse_open(now_utc):
        return []
    if not tm.WATCHLIST_LATEST.exists():
        return []
    watchlist = json.loads(tm.WATCHLIST_LATEST.read_text())

    candidates = []  # (symbol, strategy)
    if strategy_config.is_enabled("BREAKOUT_SWING"):
        syms = [row["symbol"] for row in watchlist.get("swing", {}).get("zerodha", {}).get("all_passing", [])]
        candidates += [(s, "BREAKOUT_SWING") for s in syms if not s.startswith("^")]  # indices: no CNC delivery
    if strategy_config.is_enabled("INBOUNDTRADEALGO"):
        syms = [row["symbol"] for row in watchlist.get("inbound", {}).get("zerodha", {}).get("all_passing", [])]
        candidates += [(s, "INBOUNDTRADEALGO") for s in syms if not s.startswith("^")]
    if not candidates:
        return []

    symbols = sorted({sym for sym, _ in candidates})
    daily = tm.fetch_batch(symbols, tm.SWING_BACKTEST_INTERVAL, "6mo")
    gate_lookup = {"BREAKOUT_SWING": watchlist.get("swing_gate_results", {}),
                   "INBOUNDTRADEALGO": watchlist.get("inbound_gate_results", {})}

    results = []
    for sym, strat in candidates:
        try:
            if strat == "BREAKOUT_SWING":
                r = tm.evaluate_symbol(sym, daily.get(sym), tm._SWING_ARGS)
            else:
                r = tm.evaluate_symbol_inbound(sym, daily.get(sym))
        except Exception as e:
            print(f"  daily-bar evaluate crashed for {sym} [{strat}]: {e}", file=sys.stderr)
            continue
        verdict = r.get("verdict")
        if verdict == "SHORT":
            print(f"  SKIP {sym} [{strat}]: SHORT signal — this path is LONG-only for now "
                  f"(policy choice, not a technical limit; ask if you want SHORT enabled)")
            continue
        if verdict != "LONG":
            continue
        gate = gate_lookup[strat].get(sym, {})
        r["strategy"] = strat
        r["edge_expectancy_r"] = gate.get("bt_expectancy_r")
        r["edge_profit_factor"] = gate.get("bt_profit_factor")
        results.append(r)
    return results


def place_zerodha_entry(sess, tz, sym_bare, direction, qty, entry, stop, target, product):
    """Shared entry+protection mechanics for EVERY strategy and EVERY
    product type (MIS or CNC) — this exact fill-wait / two-leg-GTT /
    critical-halt-on-failure safety sequence must never diverge between
    them, so it lives in exactly one place. Returns a dict: outcome,
    reason, order_id, gtt_id (gtt_id is None on failure)."""
    try:
        entry_result = sess.place_order("NSE", sym_bare, direction, qty, order_type="MARKET", product=product)
        order_id = entry_result.get("order_id") if isinstance(entry_result, dict) else None
    except Exception as e:
        return {"outcome": "error", "reason": f"entry order failed: {e}"[:300], "order_id": None, "gtt_id": None}

    if not order_id:
        return {"outcome": "error", "reason": f"no order_id in response: {entry_result}", "order_id": None, "gtt_id": None}

    status, history = wait_for_fill(sess, order_id)
    if status in ("REJECTED", "CANCELLED"):
        return {"outcome": "rejected", "reason": f"entry order {status}, no position opened",
                "order_id": order_id, "gtt_id": None}
    if status != "COMPLETE":
        # UNKNOWN STATE: do not guess whether a position exists.
        reason = (f"entry order_id={order_id} status unresolved after {FILL_POLL_ATTEMPTS}s poll "
                  f"— a position MAY be open and UNPROTECTED. Check Zerodha directly, now.")
        print(f"  *** CRITICAL: {reason} ***", file=sys.stderr)
        halt(reason, tz)
        return {"outcome": "CRITICAL_UNKNOWN", "reason": reason, "order_id": order_id, "gtt_id": None}

    # GTT's `last_price` must reflect the market price AT THE MOMENT OF
    # SUBMISSION — Zerodha validates it against the live LTP server-side and
    # rejects the whole two-leg trigger ("Invalid trigger data", HTTP 400)
    # if it's stale. `entry` is the pre-trade SIGNAL price computed from the
    # scan a cycle earlier — by the time the market order has actually
    # filled, the real price has moved on, so using `entry` here caused
    # exactly that rejection (observed on INDIGO, 2026-08-25), which in turn
    # triggered the emergency-flatten path below: a CNC position bought and
    # immediately sold back out, i.e. what should have been a multi-day
    # swing hold looked like an intraday round-trip. Use the entry order's
    # own average_price instead — falls back to `entry` only if Zerodha's
    # order history doesn't carry it, which should not happen for a
    # COMPLETE order.
    fill_price = None
    if history:
        try:
            fill_price = float(history[-1].get("average_price") or 0) or None
        except (TypeError, ValueError):
            fill_price = None
    last_price = fill_price if fill_price is not None else entry

    exit_txn = "SELL" if direction == "BUY" else "BUY"
    try:
        gtt_result = sess.place_gtt_oco("NSE", sym_bare, last_price, qty, exit_txn, stop, target, product=product)
        gtt_id = (gtt_result or {}).get("trigger_id")
        return {"outcome": "placed", "reason": f"entry order_id={order_id}, GTT protection placed",
                "order_id": order_id, "gtt_id": gtt_id}
    except Exception as e:
        # Position IS open and IS unprotected. Try to flatten immediately.
        flatten_note = ""
        try:
            sess.place_order("NSE", sym_bare, exit_txn, qty, order_type="MARKET", product=product)
            flatten_note = "attempted emergency flatten via opposite MARKET order — VERIFY on Zerodha"
        except Exception as e2:
            flatten_note = f"emergency flatten ALSO FAILED ({e2}) — POSITION IS LIKELY STILL OPEN, ACT NOW"
        reason = f"GTT placement failed ({e}); {flatten_note}"
        print(f"  *** CRITICAL: {sym_bare} position open, protection failed: {e}. {flatten_note} ***", file=sys.stderr)
        halt(reason, tz)
        return {"outcome": "CRITICAL_UNPROTECTED", "reason": reason, "order_id": order_id, "gtt_id": None}


def get_open_zerodha_symbols(sess):
    """Bare tradingsymbols with a nonzero NET position right now, regardless
    of product — used to stop a later cycle from opening a second position
    on a symbol that's already live (replaces the old CNC-only, file-based
    "already have an open position" check, which never both wrote AND read
    consistently for MIS and had no way to notice a position closed outside
    this script, e.g. a manual trade or GTT firing on the exchange)."""
    try:
        net = sess.positions().get("net", [])
    except Exception as e:
        print(f"  WARNING: could not read open positions this cycle ({e}) — pyramiding check "
              f"skipped for this cycle", file=sys.stderr)
        return set()
    return {p["tradingsymbol"] for p in net if p.get("quantity")}


def run_zerodha_cnc_exit_checks(args, tz, sess):
    """Two jobs for every tracked CNC swing position, mirrors
    auto_trader.py's run_capitalcom_cnc_exit_checks with one addition
    (breakeven stop move) Capital.com doesn't need in the same place since
    its stop is on the broker position itself, not a separate GTT:

    1. Force-exit anything held past its strategy's max_hold_bars trading
       days — nothing on Zerodha's side enforces this for CNC.
    2. Once a position is up breakeven_trigger_r (from SWING_STRATEGY_DEFAULTS
       / inbound_trade_algo.DEFAULTS), replace its GTT (Kite has no in-place
       GTT modify — delete + re-place) with the stop moved to lock in
       breakeven_lock_r of that gain, so a winner's worst case stops being
       "wait it out and hope", per the 2026-08-29 request."""
    checked_at = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z")
    today = datetime.now(tz).date()
    swing_state = load_swing_positions()
    for sym_bare, pos in list(swing_state.items()):
        opened = datetime.strptime(pos["opened_at"], "%Y-%m-%d").date()
        held = trading_days_between(opened, today)
        strat = pos.get("strategy", "BREAKOUT_SWING")
        max_hold, trigger_r, lock_r = strategy_swing_params(strat)

        # --- breakeven stop move (checked every cycle, not just at max-hold) ---
        if trigger_r > 0 and not pos.get("breakeven_moved"):
            try:
                quote_net = sess.positions().get("net", [])
                live = next((p for p in quote_net if p.get("tradingsymbol") == sym_bare), None)
                last_price = float(live["last_price"]) if live and live.get("last_price") else None
            except Exception:
                last_price = None
            if last_price is not None:
                entry, stop = pos["entry"], pos["stop"]
                risk = abs(entry - stop)
                trigger_price = entry + trigger_r * risk  # LONG-only, see module comment
                if risk > 0 and last_price >= trigger_price:
                    new_stop = entry + lock_r * risk
                    if not args.live:
                        print(f"  SWING DRY-RUN BREAKEVEN {sym_bare} [{strat}]: last={last_price} past trigger "
                              f"{trigger_price:.2f} — would move stop {stop:.2f} -> {new_stop:.2f}")
                        pos["breakeven_moved"] = True  # don't re-log every cycle in dry-run either
                        swing_state[sym_bare] = pos
                        save_swing_positions(swing_state)
                    else:
                        try:
                            if pos.get("gtt_id"):
                                sess.delete_gtt(pos["gtt_id"])
                            gtt_result = sess.place_gtt_oco("NSE", sym_bare, last_price, pos["qty"], "SELL",
                                                             new_stop, pos["target"], product="CNC")
                            pos["stop"], pos["gtt_id"], pos["breakeven_moved"] = new_stop, (gtt_result or {}).get("trigger_id"), True
                            swing_state[sym_bare] = pos
                            save_swing_positions(swing_state)
                            print(f"  SWING BREAKEVEN {sym_bare} [{strat}]: stop moved {stop:.2f} -> {new_stop:.2f} "
                                  f"(last={last_price})")
                        except Exception as e:
                            print(f"  WARNING: breakeven stop move failed for {sym_bare} [{strat}]: {e} "
                                  f"— original stop/target GTT (if any) left as-is, will retry next cycle",
                                  file=sys.stderr)

        # --- max-hold force exit ---
        if held < max_hold:
            continue
        row = {"checked_at": checked_at, "env": args.env, "symbol": sym_bare, "name": sym_bare,
               "verdict": "EXIT", "entry": pos.get("entry"), "stop": pos.get("stop"), "target": pos.get("target"),
               "qty": pos.get("qty"), "mode": "live" if args.live else "dry-run",
               "opened_at": pos["opened_at"], "held_trading_days": held, "strategy": strat}
        if not args.live:
            row["outcome"], row["reason"] = "dry-run", f"would force-exit (held {held} trading days, max {max_hold})"
            print(f"  SWING DRY-RUN EXIT {sym_bare} [{strat}]: held {held} trading days, past max hold")
            log_swing_decision(row)
            continue
        try:
            if pos.get("gtt_id"):
                try:
                    sess.delete_gtt(pos["gtt_id"])
                except Exception:
                    pass  # GTT may have already fired/expired on its own — proceed to flatten regardless
            sess.place_order("NSE", sym_bare, "SELL", pos["qty"], order_type="MARKET", product="CNC")
            row["outcome"], row["reason"] = "exited", f"max-hold exit after {held} trading days"
            print(f"  SWING EXIT {sym_bare} [{strat}] (held {held} trading days, past max hold)")
            del swing_state[sym_bare]
            save_swing_positions(swing_state)
        except Exception as e:
            row["outcome"], row["reason"] = "CRITICAL_EXIT_FAILED", f"max-hold exit failed: {e}"[:300]
            print(f"  *** CRITICAL: SWING exit failed for {sym_bare}: {e} — position likely still open, VERIFY ***",
                  file=sys.stderr)
            halt(row["reason"], tz)
        log_swing_decision(row)


def run_trading_once(args, tz, sess):
    """Unified per-cycle trading pass across EVERY enabled strategy: gathers
    BREAKOUT_INTRADAY, BREAKOUT_SWING, and INBOUNDTRADEALGO candidates
    together, resolves any symbol with more than one simultaneous signal to
    whichever strategy has the stronger backtested edge on that instrument
    (tm.pick_best_per_symbol — "execute based on best strategy"), then
    places each winner. BREAKOUT_INTRADAY executes as MIS; BREAKOUT_SWING
    and INBOUNDTRADEALGO execute as CNC, multi-day (see the module comment
    above evaluate_zerodha_cnc_signals) — also runs run_zerodha_cnc_exit_checks
    first, so max-hold force-exits and breakeven stop moves happen before any
    new entries are considered this cycle. One position per symbol, ever,
    regardless of which strategy signaled it — enforced against Zerodha's
    own live position list (get_open_zerodha_symbols), not a local file.

    Gated on NSE being open BEFORE any Zerodha network call, including
    login: outside trading hours there is nothing this function could do
    anyway (no new MIS/CNC signal is possible, evaluate_zerodha_*_signals()
    already return [] when NSE is closed) — the only thing an ungated call
    would accomplish is an unnecessary login attempt every 5 minutes, 24/7,
    including through Zerodha's own maintenance window (observed in
    practice: login failures with a bare "404 page not found" outside
    trading hours — see output/zerodha_login_failure_*.png timestamps,
    which cluster in the Indian pre-dawn/weekend hours). Skipping the whole
    pass when NSE is closed avoids hammering a host that may legitimately
    be down for maintenance at that hour, not just the wasted API calls."""
    checked_at = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z")
    now_utc = datetime.now(timezone.utc)
    nse_open = tm.is_nse_open(now_utc)
    if not nse_open:
        print(f"[{checked_at}] NSE closed — skipping Zerodha login/trading pass entirely "
              f"(also avoids hitting Zerodha outside trading hours, including its maintenance window).")
        return

    try:
        sess.ensure_login()  # reuses a cached/in-memory token — only drives the browser if actually needed
    except Exception as e:
        print(f"[{checked_at}] Zerodha login failed: {e}", file=sys.stderr)
        return

    halted, reason, equity = check_circuit_breaker(sess, tz)
    if halted:
        print(f"[{checked_at}] TRADING HALTED — {reason}")
        return
    print(f"[{checked_at}] equity={equity:.2f}  circuit breaker OK")

    run_zerodha_cnc_exit_checks(args, tz, sess)

    mis_candidates = evaluate_zerodha_mis_signals() if strategy_config.is_enabled("BREAKOUT_INTRADAY") else []
    daily_candidates = evaluate_zerodha_cnc_signals()
    all_candidates = mis_candidates + daily_candidates
    if not all_candidates:
        print(f"[{checked_at}] Zerodha: no actionable signals this check (no triggers).")
        return

    winners, losers = tm.pick_best_per_symbol(all_candidates)
    for c in losers:
        sym_bare = c["symbol"][:-3] if c["symbol"].endswith(".NS") else c["symbol"]
        print(f"  SKIP {sym_bare} [{c['strategy']}]: lower-ranked than another strategy's signal on the same "
              f"symbol this cycle (this strategy's edge here: exp={c.get('edge_expectancy_r')}R)")

    open_symbols = get_open_zerodha_symbols(sess) if args.live else set()
    swing_state = load_swing_positions()
    available_cash = None
    if any(r["strategy"] != "BREAKOUT_INTRADAY" for r in winners):
        available_cash = get_available_cash(sess)
        if available_cash is None:
            print("  WARNING: could not read available cash this cycle — CNC sizing falling back to "
                  "risk-based only (uncapped by free cash)", file=sys.stderr)

    for r in winners:
        strat = r["strategy"]
        sym_bare = r["symbol"][:-3] if r["symbol"].endswith(".NS") else r["symbol"]
        is_cnc = strat in ("BREAKOUT_SWING", "INBOUNDTRADEALGO")
        product = "CNC" if is_cnc else "MIS"
        log_fn = log_swing_decision if is_cnc else log_decision

        stop_distance = abs(r["entry"] - r["stop"])
        if stop_distance < MIN_STOP_DISTANCE_RS:
            widened = (r["entry"] - MIN_STOP_DISTANCE_RS if r["verdict"] == "LONG"
                       else r["entry"] + MIN_STOP_DISTANCE_RS)
            print(f"  {sym_bare} [{strat}]: ATR stop only ₹{stop_distance:.2f} from entry — "
                  f"widening to the ₹{MIN_STOP_DISTANCE_RS:.0f} floor (₹{r['stop']:.2f} -> ₹{widened:.2f})")
            r["stop"] = widened

        row = {"checked_at": checked_at, "env": args.env, "symbol": sym_bare, "name": r["name"],
               "verdict": r["verdict"], "entry": r["entry"], "stop": r["stop"], "target": r["target"],
               "qty": None, "mode": "live" if args.live else "dry-run", "outcome": None, "reason": None,
               "strategy": strat, "opened_at": None, "held_trading_days": None}

        if sym_bare in open_symbols or sym_bare in swing_state:
            row["outcome"], row["reason"] = "skipped", "already have an open position in this symbol — not pyramiding"
            print(f"  SKIP {sym_bare} [{strat}]: {row['reason']}")
            log_fn(row)
            continue

        qty, size_reason = size_position(r["entry"], r["stop"], equity, strat, available_cash if is_cnc else None)

        if qty is None:
            row["outcome"], row["reason"] = "skipped", size_reason
            print(f"  SKIP {sym_bare} [{strat}]: {size_reason}")
            log_fn(row)
            continue
        row["qty"] = qty

        direction = "BUY" if r["verdict"] == "LONG" else "SELL"
        if is_cnc and direction != "BUY":
            row["outcome"], row["reason"] = "skipped", "CNC path is LONG-only for now — see module comment"
            print(f"  SKIP {sym_bare} [{strat}]: {row['reason']}")
            log_fn(row)
            continue

        if not args.live:
            row["outcome"], row["reason"] = "dry-run", f"would place this {product} order — use --live --confirm to actually trade"
            print(f"  DRY-RUN {direction} {sym_bare} [{strat}] qty={qty} entry~{r['entry']} "
                  f"stop={r['stop']} target={r['target']} ({product})")
            log_fn(row)
            continue

        placed = place_zerodha_entry(sess, tz, sym_bare, direction, qty, r["entry"], r["stop"], r["target"], product)
        row["outcome"], row["reason"] = placed["outcome"], placed["reason"]
        if placed["outcome"] == "placed":
            print(f"  PLACED {direction} {sym_bare} [{strat}] qty={qty} order_id={placed['order_id']} "
                  f"stop={r['stop']} target={r['target']} ({product}, GTT OK)")
            open_symbols.add(sym_bare)  # so another winner this same cycle can't also target this symbol
            if is_cnc:
                opened_at = datetime.now(tz).date().strftime("%Y-%m-%d")
                swing_state[sym_bare] = {"strategy": strat, "opened_at": opened_at, "qty": qty,
                                          "entry": r["entry"], "stop": r["stop"], "target": r["target"],
                                          "gtt_id": placed.get("gtt_id"), "breakeven_moved": False}
                save_swing_positions(swing_state)
                row["opened_at"], row["held_trading_days"] = opened_at, 0
        else:
            stream = sys.stderr if "CRITICAL" in placed["outcome"] else sys.stdout
            print(f"  {sym_bare} [{strat}]: {placed['outcome']} — {placed['reason']}", file=stream)
        log_fn(row)


def build_tm_args():
    # Gate thresholds read from trading_settings.py (2026-09-03) — see
    # auto_trader.py's build_tm_args() for full rationale/reload semantics.
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
    p = argparse.ArgumentParser(description="Zerodha auto-trader with hard risk caps — see module docstring before use")
    p.add_argument("--env", choices=["demo", "live"], default=None,
                    help="'demo' behaves as dry-run (Zerodha has no real paper account); only 'live' places real orders.")
    p.add_argument("--live", action="store_true", help="Actually place orders. Requires --confirm too.")
    p.add_argument("--confirm", default="", help='Must exactly equal "I UNDERSTAND" to allow --live')
    p.add_argument("--once", action="store_true")
    p.add_argument("--loop-seconds", type=int, default=300)
    p.add_argument("--tz", default=tm.DEFAULT_TZ_NAME)
    p.add_argument("--debug-login", action="store_true", help="Non-headless login browser + screenshot on failure")
    args = p.parse_args()

    args.env = (args.env or os.environ.get("ZERODHA_ENV", "demo")).lower()

    if args.live and args.confirm != "I UNDERSTAND":
        print('Refusing to run live: pass --confirm "I UNDERSTAND" exactly, alongside --live.', file=sys.stderr)
        sys.exit(1)
    if args.env == "demo" and args.live:
        print("NOTE: --env demo forces dry-run — Zerodha has no fake-money account to actually trade against.")
        args.live = False  # Zerodha has no paper account — demo can never place real orders
    if args.once:
        args.loop_seconds = 0

    tz = ZoneInfo(args.tz)
    env_label = "DEMO (simulated as dry-run — Zerodha has no paper account)" if args.env == "demo" else "LIVE — REAL MONEY ACCOUNT"
    order_label = "ORDERS WILL BE PLACED" if args.live else "DRY RUN — no orders will be placed"
    print("=" * 72)
    print(f"  Zerodha account:  {env_label}")
    print(f"  Order mode:       {order_label}")
    print("=" * 72)
    if args.env == "live":
        print("  *** REAL MONEY IS AT RISK ON THIS RUN ***")
    print(f"Per-trade risk cap: {ts.get('per_trade_risk_pct'):.1%} of equity | Daily loss circuit breaker: {ts.get('daily_loss_cap_pct'):.0%}")
    if HALT_FILE.exists():
        print(f"NOTE: {HALT_FILE} currently exists — trading is halted until it's removed.")

    tm_args = build_tm_args()
    watchlist = tm.build_watchlist(tm_args, tz, force=False)

    # Check credentials ONCE — see auto_trader.py for the same pattern.
    # Also create the KiteSession ONCE here, reused across every cycle —
    # this is what actually prevents a fresh browser-automation login every
    # 5 minutes; ensure_login()/the token cache in zerodha_api.py mean the
    # expensive login only happens when there's genuinely no valid token.
    trading_enabled = True
    sess = None
    try:
        zerodha_credentials()
        sess = KiteSession(debug_login=args.debug_login)
    except Exception as e:
        trading_enabled = False
        print(f"NOTE: {e}\nZerodha trading is DISABLED for this run. "
              f"Fill in automation/.env and restart to enable trading.\n")

    while True:
        try:
            if watchlist.get("as_of_date") != datetime.now(tz).strftime("%Y-%m-%d"):
                watchlist = tm.build_watchlist(tm_args, tz, force=True)
            elif tm.ranking_stale(watchlist, tm_args.refresh_hours):
                watchlist = tm.refresh_ranking(watchlist, tm_args, tz)
            # Zerodha-only visibility — self-sufficient, doesn't depend on
            # auto_trader.py's process also being up. Only shows the zerodha
            # bucket (empty list, i.e. nothing printed, if NSE is closed).
            now_utc = datetime.now(timezone.utc)
            zerodha_buckets = ["zerodha"] if tm.is_nse_open(now_utc) else []
            tm.run_once(watchlist, tm_args, tz, buckets_override=zerodha_buckets, swing_brokers=("zerodha",))

            if trading_enabled:
                run_trading_once(args, tz, sess)  # handles MIS (BREAKOUT_INTRADAY) + CNC (BREAKOUT_SWING, INBOUNDTRADEALGO) together
            else:
                print(f"[{datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S %Z')}] Zerodha trading disabled "
                      f"(no credentials) — visibility only.")
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
        if args.loop_seconds <= 0:
            break
        time.sleep(args.loop_seconds)


if __name__ == "__main__":
    main()
