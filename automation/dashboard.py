#!/usr/bin/env python3
"""
dashboard.py — local web dashboard for the trading automation.

Two kinds of data now:
  1. READ-ONLY signal/log data (as before) — from the output files stock.sh's
     two processes already write. No broker calls needed for this part.
  2. LIVE broker data — real open positions (with live P&L), real account
     equity/margin, and a Close button that places a REAL close order. This
     part DOES call the broker APIs, using the same encrypted credentials as
     everything else, so this must be launched through automation/with_env.sh
     (or it'll fail with a clear "still encrypted" error, same as the other
     scripts).

################################################################################
# THE CLOSE BUTTON PLACES A REAL ORDER (once you're trading live, not demo).
# It closes exactly the position you click on: Capital.com via its native
# close-position call; Zerodha by cancelling the associated GTT stop/target
# first, then placing an opposite MARKET order for the same quantity to
# flatten it (Kite Connect has no single "close position" call). Both
# require a client-side confirm() before the request is even sent.
################################################################################

Broker sessions are created once and reused for the life of this process
(same pattern as auto_trader.py/zerodha_trader.py) — polling positions every
few seconds does NOT re-run Zerodha's browser login each time; it reuses
the cached token exactly like the traders do.

Trade decisions and order placement are made entirely by auto_trader.py and
zerodha_trader.py (run separately via stock.sh) — this dashboard never
decides or places entry trades itself. It reads their live state (which
symbols currently qualify, what they've done, real account/position data)
and surfaces it for monitoring, plus lets you close a position early.

Usage:
    automation/with_env.sh ./.venv/bin/python automation/dashboard.py
    automation/with_env.sh ./.venv/bin/python automation/dashboard.py --port 8899

Open http://127.0.0.1:8787 . Binds to 127.0.0.1 only, deliberately.
"""
import argparse
import base64
import csv
import hmac
import json
import os
import secrets
import subprocess
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import strategy_config  # noqa: E402 — after sys.path insert above
import trading_settings  # noqa: E402

STOCK_SH = ROOT / "stock.sh"
OUT_DIR = ROOT / "output"

WATCHLIST_LATEST = OUT_DIR / "watchlist_latest.json"
SIGNAL_LOG = OUT_DIR / "signal_log.csv"
CAPITAL_TRADE_LOG = OUT_DIR / "auto_trader_log.csv"
ZERODHA_TRADE_LOG = OUT_DIR / "zerodha_trader_log.csv"
# BREAKOUT_SWING/INBOUNDTRADEALGO decisions log here, separately from the two
# above — added 2026-09-02 after "Recent trade decisions" was found showing
# stale data: it only ever read the two intraday logs above, so once
# BREAKOUT_INTRADAY stopped trading the table looked frozen even though the
# swing/inbound strategies were actively logging decisions the whole time.
CAPITAL_SWING_TRADE_LOG = OUT_DIR / "capital_swing_trader_log.csv"
ZERODHA_SWING_TRADE_LOG = OUT_DIR / "zerodha_swing_trader_log.csv"
HALT_FILE = OUT_DIR / "TRADING_HALTED"
CAPITAL_RISK_STATE = OUT_DIR / "daily_risk_state.json"
ZERODHA_RISK_STATE = OUT_DIR / "zerodha_daily_risk_state.json"
CAPITAL_PID_FILE = OUT_DIR / "capital_run.pid"
ZERODHA_PID_FILE = OUT_DIR / "zerodha_run.pid"

# Mirrors the hard-coded circuit breaker in auto_trader.py / zerodha_trader.py —
# duplicated here (not imported) so this read-only dashboard never pulls in
# those scripts' heavier top-level imports just to read one constant.
DAILY_LOSS_CAP_PCT = 4.0


# ---------------------------------------------------------------------------
# Read-only file-based data (unchanged approach from before)
# ---------------------------------------------------------------------------
def _read_json(path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _latest_signal_rows():
    if not SIGNAL_LOG.exists():
        return []
    try:
        # on_bad_lines='skip': a handful of legacy rows predate the fixed
        # SIGNAL_LOG_COLUMNS schema in trade_monitor.py and have a ragged
        # field count — skip just those rather than losing the whole file.
        df = pd.read_csv(SIGNAL_LOG, on_bad_lines="skip", engine="c")
    except Exception:
        return []
    if df.empty or "bucket" not in df.columns:
        return []
    df = df.sort_values("checked_at_local")
    latest = df.groupby(["bucket", "symbol"], as_index=False).tail(1)
    latest = latest.sort_values(["bucket", "symbol"])
    return json.loads(latest.to_json(orient="records"))


def _recent_trade_rows(path, broker, limit=30):
    if not path.exists():
        return []
    try:
        df = pd.read_csv(path)
    except Exception:
        return []
    if df.empty:
        return []
    df = df.tail(limit).iloc[::-1]
    df.insert(0, "broker", broker)
    return json.loads(df.to_json(orient="records"))


def _backtest_lookup(watchlist):
    """symbol -> {trade_count, win_rate, profit_factor, expectancy_r}, per bucket,
    so the signals table can show each idea's actual historical edge, not just
    today's technical trigger."""
    lookup = {}
    for bucket, bv in (watchlist.get("buckets") or {}).items():
        for item in (bv or {}).get("all_passing") or []:
            lookup[(bucket, item.get("symbol"))] = item.get("backtest")
    for broker, sv in (watchlist.get("swing") or {}).items():
        bucket = f"swing/{broker}"
        for item in (sv or {}).get("all_passing") or []:
            lookup[(bucket, item.get("symbol"))] = item.get("backtest")
    return lookup


def _gate_counts(gate_results, gate_params=None):
    if not gate_results:
        return None
    total = len(gate_results)
    passing = sum(1 for v in gate_results.values() if v.get("bt_pass"))
    out = {"total": total, "passing": passing}
    if gate_params:
        out["min_trades"] = gate_params.get("min_trades")
        out["min_profit_factor"] = gate_params.get("min_profit_factor")
        out["min_expectancy"] = gate_params.get("min_expectancy")
    return out


def _passing_symbols(watchlist, kind, signal_lookup=None):
    """List of {symbol, name, strategies, bias, live_verdict} for every
    instrument that's currently cleared the backtest gate for the given
    pool — added 2026-09-02, extended 2026-09-03 with direction: `bias`
    (bullish/bearish/neutral, the same RSI-based lean score_and_rank_symbols
    already computes per symbol) tells you which side the backtested edge
    leans toward; `live_verdict` (LONG/SHORT/WAIT), pulled from the same
    live signal rows the "Tradeable now" bucket tabs use, tells you whether
    an actual breakout trigger is firing RIGHT NOW — a symbol can have a
    genuine backtested edge and a clear bias and still be WAIT most of the
    time, since the trigger itself (price breaking its recent range on
    volume) is a comparatively rare event. Both fields recompute from
    scratch on every dashboard snapshot, so they're never more stale than
    the underlying watchlist_latest.json / signal_log.csv files themselves —
    no separate refresh step needed."""
    signal_lookup = signal_lookup or {}
    found = {}
    if kind == "intraday":
        for bv in (watchlist.get("buckets") or {}).values():
            for item in (bv or {}).get("all_passing") or []:
                sym = item.get("symbol")
                if sym:
                    found.setdefault(sym, {"symbol": sym, "name": item.get("name"),
                                            "strategies": set(), "bias": item.get("bias")})
                    found[sym]["strategies"].add("BREAKOUT_INTRADAY")
    elif kind == "investment":
        for broker_key, strategy in (("swing", "BREAKOUT_SWING"), ("inbound", "INBOUNDTRADEALGO")):
            for sv in (watchlist.get(broker_key) or {}).values():
                for item in (sv or {}).get("all_passing") or []:
                    sym = item.get("symbol")
                    if sym:
                        found.setdefault(sym, {"symbol": sym, "name": item.get("name"),
                                                "strategies": set(), "bias": item.get("bias")})
                        found[sym]["strategies"].add(strategy)
    out = []
    for row in found.values():
        row = dict(row)
        row["strategies"] = sorted(row["strategies"])
        row["live_verdict"] = signal_lookup.get(row["symbol"], "WAIT")
        out.append(row)
    return sorted(out, key=lambda r: r["symbol"])


def _process_info(pid_file, name_pattern):
    """Ground-truth engine status read straight from the OS, the same way you'd
    check with `ps aux` — not trusted metadata, an actual live process check."""
    if not pid_file.exists():
        return {"running": False, "env": None, "live": False}
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
    except Exception:
        return {"running": False, "env": None, "live": False}
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\x00")
        cmdline = [c.decode(errors="ignore") for c in raw if c]
    except Exception:
        return {"running": True, "env": None, "live": False}
    if name_pattern not in " ".join(cmdline):
        return {"running": False, "env": None, "live": False}
    env_val = None
    for i, tok in enumerate(cmdline):
        if tok == "--env" and i + 1 < len(cmdline):
            env_val = cmdline[i + 1]
    return {"running": True, "env": env_val, "live": "--live" in cmdline}


def engine_status():
    cap = _process_info(CAPITAL_PID_FILE, "auto_trader.py")
    zer = _process_info(ZERODHA_PID_FILE, "zerodha_trader.py")
    return {
        "capital_running": cap["running"], "capital_env": cap["env"], "capital_live": cap["live"],
        "zerodha_running": zer["running"], "zerodha_env": zer["env"], "zerodha_live": zer["live"],
    }


def build_snapshot():
    watchlist = _read_json(WATCHLIST_LATEST) or {}
    capital_risk = _read_json(CAPITAL_RISK_STATE) or {}
    zerodha_risk = _read_json(ZERODHA_RISK_STATE) or {}

    halted = HALT_FILE.exists()
    halt_text = HALT_FILE.read_text() if halted else None

    bt_lookup = _backtest_lookup(watchlist)
    signal_rows = _latest_signal_rows()
    buckets = {}
    # symbol -> most recent live verdict (LONG/SHORT/WAIT), across whichever
    # bucket last checked it — used by _passing_symbols() below so the
    # intraday/investment tables show whether a breakout trigger is actually
    # firing right now, not just that the instrument has a backtested edge.
    signal_lookup = {}
    for row in signal_rows:
        row["backtest"] = bt_lookup.get((row.get("bucket"), row.get("symbol")))
        buckets.setdefault(row["bucket"], []).append(row)
        if row.get("symbol"):
            signal_lookup[row["symbol"]] = row.get("verdict")

    trades = (_recent_trade_rows(CAPITAL_TRADE_LOG, "capitalcom")
              + _recent_trade_rows(ZERODHA_TRADE_LOG, "zerodha")
              + _recent_trade_rows(CAPITAL_SWING_TRADE_LOG, "capitalcom")
              + _recent_trade_rows(ZERODHA_SWING_TRADE_LOG, "zerodha"))
    trades.sort(key=lambda r: str(r.get("checked_at", "")), reverse=True)

    return {
        "server_time_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "watchlist_generated_at_local": watchlist.get("generated_at_local"),
        "ranking_refreshed_at_local": watchlist.get("ranking_refreshed_at_local"),
        "as_of_date": watchlist.get("as_of_date"),
        "halted": halted,
        "halt_text": halt_text,
        "capital_day_start_equity": capital_risk.get("day_start_equity"),
        "zerodha_day_start_equity": zerodha_risk.get("day_start_equity"),
        "buckets": buckets,
        "trades": trades[:10],
        "intraday_gate": _gate_counts(watchlist.get("gate_results"), (watchlist.get("params") or {}).get("backtest_gate")),
        "swing_gate": _gate_counts(watchlist.get("swing_gate_results"), (watchlist.get("params") or {}).get("swing_backtest_gate")),
        "inbound_gate": _gate_counts(watchlist.get("inbound_gate_results"), (watchlist.get("params") or {}).get("inbound_backtest_gate")),
        "intraday_passing_symbols": _passing_symbols(watchlist, "intraday", signal_lookup),
        "investment_passing_symbols": _passing_symbols(watchlist, "investment", signal_lookup),
        "strategy_status": strategy_config.status(),
        "trading_settings": trading_settings.status(),
        "engine": engine_status(),
        "daily_loss_cap_pct": DAILY_LOSS_CAP_PCT,
    }


# ---------------------------------------------------------------------------
# Live broker sessions — created once, reused for the life of this process.
# Zerodha's login is real browser automation; never call .login() directly
# on every poll — ensure_login() only does that if there's truly no valid
# (possibly disk-cached) token, same discipline as the trader scripts.
# ---------------------------------------------------------------------------
_capital_session = None
_zerodha_session = None


def get_capital_session():
    global _capital_session
    if _capital_session is None:
        from capital_api import CapitalSession
        _capital_session = CapitalSession()
    return _capital_session


def get_zerodha_session():
    global _zerodha_session
    if _zerodha_session is None:
        from zerodha_api import KiteSession
        _zerodha_session = KiteSession()
    return _zerodha_session


def fetch_capital_positions():
    try:
        sess = get_capital_session()
        sess.ensure_login()
        raw = sess.positions()
    except Exception as e:
        return str(e), []
    out = []
    for p in raw or []:
        pos, market = p.get("position", {}), p.get("market", {})
        direction = pos.get("direction")
        current = market.get("bid") if direction == "SELL" else market.get("offer")
        out.append({
            "broker": "capitalcom",
            "close_ref": {"dealId": pos.get("dealId")},
            "symbol": market.get("epic"), "name": market.get("instrumentName"),
            "direction": direction, "size": pos.get("size"), "entry": pos.get("level"),
            "current_price": current, "stop": pos.get("stopLevel"), "target": pos.get("profitLevel"),
            "pnl": pos.get("upl"),
        })
    return None, out


def fetch_zerodha_positions():
    try:
        sess = get_zerodha_session()
        sess.ensure_login()
        raw = sess.positions()
        gtts = sess.gtt_triggers()
    except Exception as e:
        return str(e), []
    net = (raw or {}).get("net", [])
    gtt_by_symbol = {}
    for g in gtts or []:
        cond = g.get("condition", {})
        if g.get("status") == "active" and cond.get("tradingsymbol"):
            gtt_by_symbol[cond["tradingsymbol"]] = g

    out = []
    for p in net:
        qty = p.get("quantity", 0)
        if not qty:
            continue
        sym = p.get("tradingsymbol")
        gtt = gtt_by_symbol.get(sym)
        trig = gtt.get("condition", {}).get("trigger_values", []) if gtt else []
        is_long = qty > 0
        stop = (min(trig) if is_long else max(trig)) if trig else None
        target = (max(trig) if is_long else min(trig)) if trig else None
        out.append({
            "broker": "zerodha",
            "close_ref": {"tradingsymbol": sym, "exchange": p.get("exchange"),
                          "quantity": qty, "gtt_id": gtt.get("id") if gtt else None},
            "symbol": sym, "name": sym,
            "direction": "BUY" if is_long else "SELL", "size": abs(qty),
            "entry": p.get("average_price"), "current_price": p.get("last_price"),
            "stop": stop, "target": target, "pnl": p.get("pnl"),
        })
    return None, out


def _pct(delta, base):
    return (delta / base * 100.0) if (delta is not None and base) else None


def fetch_capital_account_metrics():
    capital_risk = _read_json(CAPITAL_RISK_STATE) or {}
    try:
        sess = get_capital_session()
        sess.ensure_login()
        acct = sess.accounts()[0]
        bal = acct.get("balance", {})
        equity = bal.get("balance")
        day_start = capital_risk.get("day_start_equity")
        daily_pnl = (equity - day_start) if (equity is not None and day_start is not None) else None
        daily_pnl_pct = _pct(daily_pnl, day_start)
        return None, {
            "account_name": acct.get("accountName"),
            "currency": acct.get("currency"), "currency_symbol": acct.get("symbol", ""),
            "equity": equity, "available": bal.get("available"), "deposit": bal.get("deposit"),
            "unrealized_pnl": bal.get("profitLoss"),
            "day_start_equity": day_start, "daily_pnl": daily_pnl, "daily_pnl_pct": daily_pnl_pct,
        }
    except Exception as e:
        return str(e), None


def fetch_zerodha_account_metrics():
    zerodha_risk = _read_json(ZERODHA_RISK_STATE) or {}
    try:
        sess = get_zerodha_session()
        sess.ensure_login()
        m = sess.margins()
        net = (m or {}).get("equity", {}).get("net")
        equity = float(net) if net not in (None, "") else None
        if equity is not None and equity <= 0:
            raise RuntimeError(f"Implausible equity reading ({equity}) — treating as a failed API response.")
        day_start = zerodha_risk.get("day_start_equity")
        daily_pnl = (equity - day_start) if (equity is not None and day_start is not None) else None
        daily_pnl_pct = _pct(daily_pnl, day_start)
        return None, {
            "currency": "INR", "currency_symbol": "₹",
            "equity": equity,
            "day_start_equity": day_start, "daily_pnl": daily_pnl, "daily_pnl_pct": daily_pnl_pct,
        }
    except Exception as e:
        return str(e), None


def fetch_zerodha_trade_attribution():
    """Splits TODAY's Zerodha activity into automated (placed by
    zerodha_trader.py, tagged "autotrader" — see place_order()) vs manual
    (everything else — placed by hand in Kite's own app/site). Realized P&L
    per symbol comes straight from Kite's own "day" positions bucket (it
    already does the buy/sell matching), so this is real broker-computed
    P&L, not a re-derivation. Capital.com has no equivalent order-tag/source
    field in its API, so this attribution is Zerodha-only — the dashboard
    is explicit about that rather than guessing for Capital.com.
    Returns None on any failure (session/API not available) rather than a
    half-populated result."""
    try:
        sess = get_zerodha_session()
        sess.ensure_login()
        orders = sess.orders() or []
        day_positions = (sess.positions() or {}).get("day", []) or []
    except Exception as e:
        return {"error": str(e)}

    automated_symbols = set()
    manual_symbols = set()
    for o in orders:
        if o.get("status") != "COMPLETE":
            continue
        sym = o.get("tradingsymbol")
        if not sym:
            continue
        if o.get("tag") == "autotrader":
            automated_symbols.add(sym)
        else:
            manual_symbols.add(sym)
    # A symbol touched by both an automated and a manual order today can't be
    # cleanly split (Kite's day P&L is per-symbol, not per-order) — count it
    # as "mixed" rather than silently attributing its whole P&L to one side.
    mixed_symbols = automated_symbols & manual_symbols
    automated_symbols -= mixed_symbols
    manual_symbols -= mixed_symbols

    def _sum_pnl(symbols):
        return sum(p.get("pnl") or 0 for p in day_positions if p.get("tradingsymbol") in symbols)

    return {
        "error": None,
        "automated_trades": len(automated_symbols), "automated_pnl": _sum_pnl(automated_symbols),
        "manual_trades": len(manual_symbols), "manual_pnl": _sum_pnl(manual_symbols),
        "mixed_trades": len(mixed_symbols), "mixed_pnl": _sum_pnl(mixed_symbols),
    }


INTRADAY_PNL_LOG = OUT_DIR / "intraday_pnl.csv"
INTRADAY_PNL_COLUMNS = ["timestamp_local", "date", "capital_daily_pnl", "zerodha_daily_pnl"]
INTRADAY_PNL_MIN_GAP_SECONDS = 60


def _append_intraday_pnl(capital_account, zerodha_account):
    """Samples today's running daily P&L (equity - day_start_equity, the same
    definition the risk bar already uses — so deposits/withdrawals aren't the
    point here, profit is) at most once a minute, so the resulting chart is
    "profit made today" over time rather than an equity-in/equity-out ledger.
    Rows from a prior calendar day are dropped on every write, so the file
    never needs a separate cleanup job and always represents just today."""
    today = datetime.now().strftime("%Y-%m-%d")
    rows = []
    if INTRADAY_PNL_LOG.exists():
        try:
            with open(INTRADAY_PNL_LOG, newline="") as f:
                rows = [r for r in csv.DictReader(f) if r.get("date") == today]
        except Exception:
            rows = []
    if rows:
        last_ts = rows[-1].get("timestamp_local")
        try:
            elapsed = (datetime.now() - datetime.strptime(last_ts, "%Y-%m-%d %H:%M:%S")).total_seconds()
            if elapsed < INTRADAY_PNL_MIN_GAP_SECONDS:
                return rows
        except Exception:
            pass
    cap_pnl = (capital_account or {}).get("daily_pnl")
    zer_pnl = (zerodha_account or {}).get("daily_pnl")
    if cap_pnl is None and zer_pnl is None:
        return rows
    rows.append({
        "timestamp_local": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "date": today,
        "capital_daily_pnl": cap_pnl, "zerodha_daily_pnl": zer_pnl,
    })
    with open(INTRADAY_PNL_LOG, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=INTRADAY_PNL_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return rows


def _capital_bundle():
    """Positions + account metrics for Capital.com, sequential (same session,
    not thread-safe across concurrent calls) — but run as one unit in
    parallel with the Zerodha bundle below."""
    pos_err, positions = fetch_capital_positions()
    acct_err, account = fetch_capital_account_metrics()
    return pos_err or acct_err, positions, account, None


def _zerodha_bundle():
    pos_err, positions = fetch_zerodha_positions()
    acct_err, account = fetch_zerodha_account_metrics()
    attribution = fetch_zerodha_trade_attribution()
    return pos_err or acct_err, positions, account, attribution


def build_positions_snapshot():
    """Capital.com and Zerodha each require 2+ sequential live broker API
    round-trips (positions, gtt/account) — independent of each other, so run
    the two broker bundles concurrently rather than one after another. Cuts
    per-poll latency roughly to whichever broker is slower, not the sum."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        capital_future = pool.submit(_capital_bundle)
        zerodha_future = pool.submit(_zerodha_bundle)
        capital_err, capital_positions, capital_account, _ = capital_future.result()
        zerodha_err, zerodha_positions, zerodha_account, zerodha_attribution = zerodha_future.result()

    history = _append_equity_history(capital_account, zerodha_account)
    intraday_pnl = _append_intraday_pnl(capital_account, zerodha_account)

    return {
        "server_time_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "capital_error": capital_err, "zerodha_error": zerodha_err,
        "positions": capital_positions + zerodha_positions,
        "account": {
            "capital": capital_account, "capital_error": capital_err,
            "zerodha": zerodha_account, "zerodha_error": zerodha_err,
        },
        "portfolio_history": history,
        "recommendation": _portfolio_recommendation(history),
        "intraday_pnl": intraday_pnl,
        "zerodha_trade_attribution": zerodha_attribution,
    }


def close_position(payload):
    broker = payload.get("broker")
    ref = payload.get("close_ref", {})
    if broker == "capitalcom":
        sess = get_capital_session()
        sess.ensure_login()
        result = sess.close_position(ref["dealId"])
        return {"ok": True, "result": result}
    elif broker == "zerodha":
        sess = get_zerodha_session()
        sess.ensure_login()
        if ref.get("gtt_id"):
            try:
                sess.cancel_gtt(ref["gtt_id"])
            except Exception as e:
                print(f"WARNING: could not cancel GTT {ref['gtt_id']} before closing: {e}", file=sys.stderr)
        qty = ref["quantity"]
        exit_txn = "SELL" if qty > 0 else "BUY"
        result = sess.place_order(ref["exchange"], ref["tradingsymbol"], exit_txn, abs(qty),
                                   order_type="MARKET", product="MIS")
        return {"ok": True, "result": result}
    else:
        return {"ok": False, "error": f"unknown broker '{broker}'"}


EQUITY_HISTORY = OUT_DIR / "equity_history.csv"
EQUITY_HISTORY_COLUMNS = ["date", "capital_equity", "zerodha_equity", "combined_equity"]
EQUITY_HISTORY_DAYS = 10


def _load_equity_history():
    if not EQUITY_HISTORY.exists():
        return []
    try:
        with open(EQUITY_HISTORY, newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _append_equity_history(capital_account, zerodha_account):
    """Upserts today's row (by local calendar date) so repeated polling doesn't
    pile up duplicate rows, then trims to the last EQUITY_HISTORY_DAYS distinct
    dates. This is the only place portfolio trend data is captured — there is
    no other running record of past equity, so this file IS the trend."""
    cap_eq = (capital_account or {}).get("equity")
    zer_eq = (zerodha_account or {}).get("equity")
    if cap_eq is None and zer_eq is None:
        return _load_equity_history()[-EQUITY_HISTORY_DAYS:]
    today = datetime.now().strftime("%Y-%m-%d")
    rows = [r for r in _load_equity_history() if r.get("date") != today]
    combined = (cap_eq or 0) + (zer_eq or 0)
    rows.append({"date": today, "capital_equity": cap_eq, "zerodha_equity": zer_eq, "combined_equity": combined})
    rows = rows[-EQUITY_HISTORY_DAYS:]
    with open(EQUITY_HISTORY, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=EQUITY_HISTORY_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return rows


def _portfolio_recommendation(history):
    """Simple rule-based read of the combined-equity trend — no ML, just the
    same kind of headroom/streak math the risk-alert insights already use, so
    the dashboard can surface one plain-English recommendation line instead of
    making the user read the chart themselves."""
    valid = [r for r in history if r.get("combined_equity") not in (None, "")]
    if len(valid) < 2:
        return {"text": "Not enough portfolio history yet — check back after a few more trading days.",
                "trend": "flat", "pct_change": None, "streak_days": 0}
    first, last = float(valid[0]["combined_equity"]), float(valid[-1]["combined_equity"])
    pct = ((last - first) / first * 100) if first else None
    streak, direction = 0, None
    for i in range(len(valid) - 1, 0, -1):
        d = float(valid[i]["combined_equity"]) - float(valid[i - 1]["combined_equity"])
        this_dir = "up" if d > 0 else ("down" if d < 0 else "flat")
        if direction is None:
            direction = this_dir
        if this_dir != direction:
            break
        streak += 1
    span = f"{len(valid)} trading day{'s' if len(valid) != 1 else ''}"
    streak_clause = f", {streak} in a row {direction}" if streak >= 2 else ""
    if pct is None:
        return {"text": "Not enough data to compute a trend yet.", "trend": "flat", "pct_change": None, "streak_days": streak}
    if pct > 1:
        trend, text = "up", f"Portfolio is up {pct:.1f}% over the last {span}{streak_clause} — current settings look healthy, no changes recommended."
    elif pct < -1:
        trend, text = "down", f"Portfolio is down {abs(pct):.1f}% over the last {span}{streak_clause} — review the win-rate gate and recent trade reasons before increasing size."
    else:
        trend, text = "flat", f"Portfolio is roughly flat ({pct:+.1f}%) over the last {span} — steady state, keep monitoring."
    return {"text": text, "trend": trend, "pct_change": pct, "streak_days": streak, "streak_direction": direction}


HALT_OVERRIDE_LOG = OUT_DIR / "halt_overrides.csv"
HALT_OVERRIDE_LOG_COLUMNS = ["timestamp_utc", "broker", "reason", "previous_day_start_equity",
                            "new_day_start_equity", "halt_text"]


def _log_halt_override(row):
    header = not HALT_OVERRIDE_LOG.exists()
    with open(HALT_OVERRIDE_LOG, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HALT_OVERRIDE_LOG_COLUMNS, extrasaction="ignore")
        if header:
            w.writeheader()
        w.writerow({k: row.get(k) for k in HALT_OVERRIDE_LOG_COLUMNS})


def acknowledge_halt(payload):
    """Manual override for a halt caused by something OUTSIDE the
    automation — a manual trade, most often — not a bug in the strategy or
    the risk math. Resets the named broker's day-start-equity baseline to
    its CURRENT live equity (so today's -4% circuit breaker measures
    forward from this acknowledgment, not from a loss the algo never made)
    and clears the shared TRADING_HALTED file. Does NOT touch the other
    broker's baseline — if it's also over its own cap, it re-halts itself
    (independently, correctly) on its next check.

    This is a real risk-control override, not a routine click — every use
    is appended to halt_overrides.csv (previous/new baseline, reason,
    the halt text it overrode) so there's a permanent, reviewable trail of
    exactly when and why the automated cap was set aside."""
    broker = payload.get("broker")
    reason = (payload.get("reason") or "manual trade").strip()[:300]
    if broker not in ("capitalcom", "zerodha"):
        return {"ok": False, "error": f"unknown or missing broker '{broker}'"}

    halt_text = HALT_FILE.read_text() if HALT_FILE.exists() else None
    if broker == "capitalcom":
        state_file = CAPITAL_RISK_STATE
        err, metrics = fetch_capital_account_metrics()
    else:
        state_file = ZERODHA_RISK_STATE
        err, metrics = fetch_zerodha_account_metrics()

    if err or not metrics or metrics.get("equity") is None:
        return {"ok": False, "error": f"could not read live {broker} equity to reset the baseline: {err}"}

    state = _read_json(state_file) or {}
    previous = state.get("day_start_equity")
    new_equity = metrics["equity"]
    state["day_start_equity"] = new_equity
    state_file.write_text(json.dumps(state, indent=2))

    _log_halt_override({
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "broker": broker, "reason": reason,
        "previous_day_start_equity": previous, "new_day_start_equity": new_equity,
        "halt_text": halt_text,
    })

    if HALT_FILE.exists():
        HALT_FILE.unlink()

    return {"ok": True, "broker": broker, "previous_day_start_equity": previous,
            "new_day_start_equity": new_equity, "halt_cleared": True}


# ---------------------------------------------------------------------------
# Control Panel actions (added 2026-09-03) — every one of these wraps
# stock.sh, never re-implements its logic, so there is exactly one source of
# truth for "how do I restart/rebuild/toggle/discover" whether it's typed at
# a terminal, cron, or clicked here. Never build a shell string from request
# input: every subprocess call below passes a fixed argv list, with any
# client-supplied value (mode, strategy name, source) validated against a
# fixed whitelist BEFORE it's placed in that list — payload content is
# untrusted, this being a zero-auth localhost server (see module docstring).
# ---------------------------------------------------------------------------
CONTROL_LOG = OUT_DIR / "control_panel.log"
_VALID_MODES = ("demo", "live", "dry-run")
_VALID_SOURCES = ("nse", "us", "both")


def _stock_sh_unavailable():
    """stock.sh is a bash script — not present (or not runnable) in the
    packaged Windows/Mac desktop build, which bundles only the frozen
    dashboard.py backend. Control Panel start/stop/restart/discover need the
    full automation/ source tree with a shell to run stock.sh in; give a
    clear error here instead of a raw FileNotFoundError from Popen."""
    if not STOCK_SH.exists():
        return "Control Panel actions need automation/stock.sh, which isn't bundled in this packaged app. Run from the full source checkout (Linux/Mac, or WSL on Windows) instead."
    if sys.platform == "win32" and not os.environ.get("WSL_DISTRO_NAME"):
        return "stock.sh is a bash script and can't run directly on Windows. Use WSL, or run the automation from Linux/Mac."
    return None


def _run_detached(args):
    """For actions that take minutes (restart, rebuild, discover) — launch
    and return immediately rather than blocking the HTTP response on a
    multi-minute subprocess. start_new_session=True mirrors stock.sh's own
    setsid+disown pattern: the child must outlive this request-handling
    thread. Output goes to a single shared control_panel.log (distinct from
    each action's own log file, which stock.sh already writes to) so the
    dashboard has one place to show "what did the last click actually do."
    """
    unavailable = _stock_sh_unavailable()
    if unavailable:
        return {"ok": False, "error": unavailable}
    with open(CONTROL_LOG, "a") as f:
        f.write(f"\n[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] $ {' '.join(args)}\n")
        f.flush()
        subprocess.Popen(args, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                          start_new_session=True, cwd=str(ROOT.parent))
    return {"ok": True, "started": True, "command": " ".join(args)}


def _run_sync(args, timeout=15):
    unavailable = _stock_sh_unavailable()
    if unavailable:
        return {"ok": False, "error": unavailable}
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=str(ROOT.parent))
        ok = proc.returncode == 0
        return {"ok": ok, "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timed out after {timeout}s"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def control_traders(payload):
    action = payload.get("action")
    mode = payload.get("mode")
    if action not in ("start", "stop", "restart"):
        return {"ok": False, "error": f"unknown action {action!r} — must be start/stop/restart"}
    if action == "stop":
        return _run_detached([str(STOCK_SH), "stop"])
    if mode not in _VALID_MODES:
        return {"ok": False, "error": f"unknown mode {mode!r} — must be one of {_VALID_MODES}"}
    args = [str(STOCK_SH), action]
    if mode in ("demo", "live"):
        args.append(f"--{mode}")
    if mode == "live":
        # Server-side check, deliberately not trusting that the client only
        # ever sends this because its own UI gated it — a client-side-only
        # check is not a check.
        if payload.get("confirm") != "I UNDERSTAND":
            return {"ok": False, "error": 'live mode requires "confirm": "I UNDERSTAND" in the request body'}
        args += ["--confirm", "I UNDERSTAND"]
    return _run_detached(args)


def control_rebuild_watchlist(payload):
    args = [str(STOCK_SH), "rebuild-watchlist"]
    min_win_rate = payload.get("min_win_rate")
    if min_win_rate is not None:
        try:
            args += ["--min-win-rate", str(float(min_win_rate))]
        except (TypeError, ValueError):
            return {"ok": False, "error": f"invalid min_win_rate {min_win_rate!r}"}
    return _run_detached(args)


def control_strategy(payload):
    name = payload.get("name")
    enabled = payload.get("enabled")
    if name not in strategy_config.STRATEGY_NAMES:
        return {"ok": False, "error": f"unknown strategy {name!r} — must be one of {strategy_config.STRATEGY_NAMES}"}
    if not isinstance(enabled, bool):
        return {"ok": False, "error": "enabled must be a boolean"}
    return _run_sync([str(STOCK_SH), "enable" if enabled else "disable", name])


def control_discover(payload):
    source = payload.get("source", "both")
    if source not in _VALID_SOURCES:
        return {"ok": False, "error": f"unknown source {source!r} — must be one of {_VALID_SOURCES}"}
    args = [str(STOCK_SH), "discover", "--source", source]
    batch_size = payload.get("batch_size")
    if batch_size is not None:
        try:
            args += ["--batch-size", str(int(batch_size))]
        except (TypeError, ValueError):
            return {"ok": False, "error": f"invalid batch_size {batch_size!r}"}
    return _run_detached(args)


def control_status():
    return _run_sync([str(STOCK_SH), "status"], timeout=15)


def control_settings(payload):
    """Validates against trading_settings' own declared name list and
    range/type (server-side — never trust the client sent a value its own
    UI already range-checked) before writing. Returns the coerced value and
    its reload-semantics tag so the UI can show "saved — takes effect on
    next rebuild" inline without hardcoding that mapping a second time."""
    name = payload.get("name")
    value = payload.get("value")
    if name not in trading_settings.SETTING_NAMES:
        return {"ok": False, "error": f"unknown setting {name!r} — must be one of {trading_settings.SETTING_NAMES}"}
    try:
        new_value = trading_settings.set_value(name, value)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "name": name, "value": new_value, "reload": trading_settings.RELOAD_SEMANTICS[name]}


HTML_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Trading Cockpit</title>
<style>
  :root { --bg:#0a0c10; --panel:#12151b; --panel2:#171b23; --border:#232830; --text:#e6e9ee; --muted:#8b93a1;
          --long:#22c55e; --short:#ef4444; --wait:#5c6472; --pick:#f5b32c; --accent:#3b82f6; --warn:#f59e0b; }
  * { box-sizing: border-box; }
  body { background:var(--bg); color:var(--text); font-family:-apple-system,Segoe UI,Roboto,sans-serif;
         margin:0; padding:20px 24px 40px; font-feature-settings:"tnum"; }
  .topbar { display:flex; align-items:center; gap:14px; flex-wrap:wrap; margin-bottom:2px; }
  .brand { font-size:1.25rem; font-weight:700; letter-spacing:-0.01em; }
  .pills { display:flex; gap:8px; flex-wrap:wrap; }
  .pill { font-size:0.72rem; font-weight:600; padding:3px 10px; border-radius:20px; border:1px solid var(--border); }
  .pill-live { background:rgba(34,197,94,0.15); color:var(--long); border-color:rgba(34,197,94,0.4); }
  .pill-demo { background:rgba(245,179,44,0.15); color:var(--pick); border-color:rgba(245,179,44,0.4); }
  .pill-off { background:rgba(239,68,68,0.12); color:var(--short); border-color:rgba(239,68,68,0.35); }
  .nav { margin-left:auto; }
  .nav a { color:var(--muted); text-decoration:none; margin-left:10px; font-size:0.82rem; border:1px solid var(--border);
           padding:6px 12px; border-radius:6px; }
  .nav a.active { color:var(--text); border-color:#3a4150; background:var(--panel); }
  .filterbar { display:flex; align-items:center; gap:8px; margin:10px 0 14px; }
  .filterlabel { color:var(--muted); font-size:0.78rem; margin-right:2px; }
  .filterbtn { background:var(--panel); border:1px solid var(--border); color:var(--muted); font-size:0.78rem;
               font-weight:600; padding:5px 14px; border-radius:20px; cursor:pointer; }
  .filterbtn:hover { border-color:#3a4150; color:var(--text); }
  .filterbtn.active { background:rgba(59,130,246,0.15); border-color:rgba(59,130,246,0.5); color:#bcd4fb; }
  .sub { color:var(--muted); font-size:0.8rem; margin:6px 0 16px; }
  .topinfo { display:grid; grid-template-columns:1fr; gap:10px; margin-bottom:16px; }
  .topinfo.halted { grid-template-columns:1fr 1fr; }
  @media (max-width:760px) { .topinfo.halted { grid-template-columns:1fr; } }
  .autobanner { background:rgba(59,130,246,0.08); border:1px solid rgba(59,130,246,0.35); color:#bcd4fb;
                padding:10px 14px; border-radius:10px; font-size:0.8rem; line-height:1.4; }
  .halt-banner { background:#3a1216; border:1px solid var(--short); color:#ffb3b5; padding:10px 14px;
                 border-radius:10px; white-space:pre-wrap; font-size:0.8rem; line-height:1.4; }
  .halt-actions { display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin-top:8px; white-space:normal; }
  .ackbtn { background:rgba(245,179,44,0.15); border:1px solid rgba(245,179,44,0.5); color:var(--pick);
            font-size:0.76rem; font-weight:600; padding:6px 12px; border-radius:8px; cursor:pointer; flex-shrink:0; }
  .ackbtn:hover { background:rgba(245,179,44,0.25); }
  .ackbtn:disabled { opacity:0.6; cursor:default; }
  .halt-hint { color:#c99; font-size:0.72rem; line-height:1.4; }
  .herogrid { display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:14px; margin-bottom:18px; }
  .herocard { background:var(--panel); border:1px solid var(--border); border-radius:12px; padding:14px 16px; }
  .herocard .label { color:var(--muted); font-size:0.72rem; text-transform:uppercase; letter-spacing:0.05em; margin-bottom:6px; }
  .herocard .value { font-size:1.5rem; font-weight:700; }
  .herocard .sub2 { color:var(--muted); font-size:0.76rem; margin-top:4px; }
  .up { color:var(--long); } .down { color:var(--short); }
  .riskbar { height:6px; border-radius:4px; background:#232830; margin-top:10px; overflow:hidden; }
  .riskbar-fill { height:100%; background:var(--long); border-radius:4px; }
  .riskbar-fill.mid { background:var(--warn); }
  .riskbar-fill.danger { background:var(--short); }
  .riskbar-label { color:var(--muted); font-size:0.7rem; margin-top:4px; }
  .insights-2col { display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:20px; align-items:start; }
  @media (max-width:760px) { .insights-2col { grid-template-columns:1fr; } }
  .insights { display:flex; flex-direction:column; gap:6px; }
  .insight { padding:8px 12px; border-radius:8px; font-size:0.82rem; border:1px solid var(--border); background:var(--panel2); }
  .insight-info { border-left:3px solid var(--accent); }
  .insight-warn { border-left:3px solid var(--warn); color:#ffd9a3; }
  .insight-danger { border-left:3px solid var(--short); color:#ffb3b5; }
  .card { background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:12px 16px; overflow-x:auto; }
  .grid { display:flex; flex-direction:column; gap:16px; margin-bottom:20px; }
  .section-title { font-size:0.95rem; margin:0 0 8px; color:var(--muted); text-transform:uppercase; letter-spacing:0.04em; }
  table { width:100%; border-collapse:collapse; font-size:0.82rem; }
  th,td { text-align:left; padding:5px 8px; border-bottom:1px solid var(--border); white-space:nowrap; }
  th { color:var(--muted); font-weight:600; font-size:0.7rem; text-transform:uppercase; }
  .verdict { font-weight:700; padding:2px 8px; border-radius:5px; font-size:0.75rem; }
  .LONG, .BUY { background:rgba(34,197,94,0.18); color:var(--long); }
  .SHORT, .SELL { background:rgba(239,68,68,0.18); color:var(--short); }
  .WAIT { background:rgba(92,100,114,0.18); color:var(--muted); }
  .pick { color:var(--pick); }
  .pnl-pos { color:var(--long); font-weight:700; }
  .pnl-neg { color:var(--short); font-weight:700; }
  .nostop { color:var(--short); font-weight:700; }
  .outcome-placed { color:var(--long); }
  .outcome-skipped, .outcome-dry-run { color:var(--muted); }
  .outcome-error, .outcome-CRITICAL_UNKNOWN, .outcome-CRITICAL_UNPROTECTED { color:var(--short); font-weight:700; }
  .closebtn { background:#3a1216; color:#ffb3b5; border:1px solid var(--short); border-radius:5px;
              padding:3px 10px; font-size:0.75rem; cursor:pointer; }
  .closebtn:hover { background:var(--short); color:#fff; }
  .closebtn:disabled { opacity:0.5; cursor:default; }
  .trades-wrap { margin-top:20px; }
  .empty { color:var(--muted); font-style:italic; padding:8px; }
  .err { color:var(--short); font-size:0.8rem; }

  .trendrow { display:grid; grid-template-columns:2fr 1fr; gap:16px; margin-bottom:20px; align-items:stretch; }
  @media (max-width:900px) { .trendrow { grid-template-columns:1fr; } }
  .trend-legend { display:flex; gap:14px; align-items:center; margin-bottom:6px; }
  .legend-item { display:flex; align-items:center; gap:6px; font-size:0.76rem; color:var(--muted); }
  .legend-swatch { width:10px; height:10px; border-radius:2px; display:inline-block; }
  .chart-wrap { position:relative; }
  .chart-tooltip { position:absolute; pointer-events:none; background:var(--panel2); border:1px solid var(--border);
                    border-radius:6px; padding:6px 10px; font-size:0.74rem; line-height:1.4; color:var(--text);
                    transform:translate(-50%,-115%); white-space:nowrap; opacity:0; transition:opacity 0.1s; z-index:5; }
  .chart-tooltip.show { opacity:1; }
  .reco-card { background:var(--panel); border:1px solid var(--border); border-radius:12px; padding:16px;
               display:flex; flex-direction:column; gap:10px; }
  .reco-badge { align-self:flex-start; font-size:0.68rem; font-weight:700; text-transform:uppercase; letter-spacing:0.05em;
                padding:3px 9px; border-radius:20px; }
  .reco-up { background:rgba(34,197,94,0.15); color:var(--long); }
  .reco-down { background:rgba(239,68,68,0.15); color:var(--short); }
  .reco-flat { background:rgba(139,147,161,0.15); color:var(--muted); }
  .reco-text { font-size:0.86rem; line-height:1.5; }
  .tabbar { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:10px; }
  .tabbtn { background:var(--panel2); border:1px solid var(--border); color:var(--muted); font-size:0.76rem;
            font-weight:600; padding:6px 12px; border-radius:8px; cursor:pointer; }
  .tabbtn:hover { color:var(--text); border-color:#3a4150; }
  .tabbtn.active { background:rgba(59,130,246,0.15); border-color:rgba(59,130,246,0.5); color:#bcd4fb; }
  .tabbtn .count { color:inherit; opacity:0.7; margin-left:4px; }

  .attrib-row { display:flex; gap:18px; flex-wrap:wrap; margin-bottom:10px; }
  .attrib-stat { flex:1; min-width:130px; }
  .attrib-stat .n { font-size:1.3rem; font-weight:700; }
  .attrib-stat .l { color:var(--muted); font-size:0.72rem; text-transform:uppercase; letter-spacing:0.04em; }
  .attrib-bar { height:10px; border-radius:5px; overflow:hidden; display:flex; background:#232830; margin-top:2px; }
  .attrib-bar .seg-auto { background:var(--accent); }
  .attrib-bar .seg-manual { background:var(--pick); }
  .attrib-bar .seg-mixed { background:var(--muted); }
  .profitgrid { display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:20px; }
  @media (max-width:760px) { .profitgrid { grid-template-columns:1fr; } }
  .profit-card .value { font-size:1.15rem; font-weight:700; }
</style></head>
<body>
  <div class="topbar">
    <div class="brand">📈 Trading Cockpit</div>
    <div class="pills" id="enginePills"></div>
    <div class="nav">
      <a href="/" id="navHome">Home</a>
      <a href="/?view=all" id="navAll">All Signals</a>
      <a href="/?view=control" id="navControl">Control Panel</a>
    </div>
  </div>
  <div id="mainView">
  <div class="sub" id="metaline">loading…</div>
  <div class="filterbar" id="filterbar">
    <span class="filterlabel">Show:</span>
    <button class="filterbtn active" data-broker="all">All</button>
    <button class="filterbtn" data-broker="capitalcom">Capital.com</button>
    <button class="filterbtn" data-broker="zerodha">Zerodha</button>
  </div>
  <div class="topinfo" id="topinfo">
    <div id="haltbanner"></div>
    <div class="autobanner">🤖 Trades automatically every 5 min within fixed risk caps (0.5%/trade, 4%/day). This
      page is monitor + emergency Close only.</div>
  </div>

  <div class="herogrid" id="heroGrid"></div>

  <div class="trendrow">
    <div class="card">
      <div class="section-title" style="margin-bottom:2px;">Portfolio trend — last 10 trading days</div>
      <div class="trend-legend" id="trendLegend"></div>
      <div class="chart-wrap" id="trendChartWrap"></div>
    </div>
    <div class="reco-card" id="recoCard">
      <div class="section-title" style="margin:0;">Recommendation</div>
      <div class="empty">loading…</div>
    </div>
  </div>

  <div class="card" id="attribCard" style="margin-bottom:20px;">
    <div class="section-title" style="margin-bottom:10px;">Today's trades — automated vs manual (Zerodha)</div>
    <div id="attribBody"></div>
  </div>

  <div class="section-title">Daily profit today (from day-start baseline, not deposits/withdrawals)</div>
  <div class="profitgrid" id="profitGrid"></div>

  <div class="insights-2col">
    <div>
      <div class="section-title">General guidance</div>
      <div class="insights" id="insightsGeneral"></div>
    </div>
    <div>
      <div class="section-title">Risk alerts</div>
      <div class="insights" id="insightsRisk"></div>
    </div>
  </div>

  <div class="section-title">Open positions (live from broker)</div>
  <table id="positionsTable"><thead><tr>
    <th>Broker</th><th>Symbol</th><th>Dir</th><th>Size</th><th>Entry</th><th>Current</th>
    <th>Stop</th><th>Target</th><th>Est. P&amp;L</th><th></th>
  </tr></thead><tbody></tbody></table>
  <div id="positionsErr" class="err"></div>

  <div style="margin-top:22px;" id="signalsHeading" class="section-title">Tradeable now (LONG / SHORT only)</div>
  <div class="tabbar" id="bucketTabs"></div>
  <div class="grid" id="buckets"></div>

  <div style="margin-top:22px;">
    <div class="section-title" style="margin-bottom:2px;">Intraday backtest-passing instruments (BREAKOUT_INTRADAY)</div>
    <div class="muted" style="font-size:0.85rem; margin-bottom:8px;">5-min bars, 60-day backtest, 80% win-rate gate — instruments currently cleared to trade with the intraday capital pool.</div>
    <table id="intradayPassingTable"><thead><tr><th>Symbol</th><th>Name</th><th>Bias</th><th>Live verdict</th></tr></thead><tbody></tbody></table>
  </div>

  <div style="margin-top:22px;">
    <div class="section-title" style="margin-bottom:2px;">Investment backtest-passing instruments (BREAKOUT_SWING + INBOUNDTRADEALGO)</div>
    <div class="muted" style="font-size:0.85rem; margin-bottom:8px;">Daily bars, 2-year backtest, 80% win-rate gate — multi-day instruments currently cleared to trade with the investment capital pool.</div>
    <table id="investmentPassingTable"><thead><tr><th>Symbol</th><th>Name</th><th>Strategy</th><th>Bias</th><th>Live verdict</th></tr></thead><tbody></tbody></table>
  </div>

  <div class="trades-wrap">
    <div class="section-title">Recent trade decisions (latest 10)</div>
    <table id="tradesTable"><thead><tr>
      <th>Time</th><th>Broker</th><th>Symbol</th><th>Verdict</th><th>Size</th><th>Mode</th><th>Outcome</th><th>Reason</th>
    </tr></thead><tbody></tbody></table>
  </div>
  </div>

  <div id="controlView" hidden>
    <div class="sub">Control Panel — every button here just runs an automation/stock.sh subcommand. No AI needed for any of these day to day.</div>

    <div style="margin-top:18px;">
      <div class="section-title">Engine status</div>
      <table><tbody>
        <tr><td style="width:140px;">Capital.com</td><td id="ctlCapitalStatus">loading…</td></tr>
        <tr><td>Zerodha</td><td id="ctlZerodhaStatus">loading…</td></tr>
        <tr><td>Dashboard</td><td id="ctlDashboardStatus">loading…</td></tr>
        <tr><td>Halt</td><td id="ctlHaltStatus">loading…</td></tr>
        <tr><td>Watchlist</td><td id="ctlWatchlistStatus">loading…</td></tr>
      </tbody></table>
    </div>

    <div style="margin-top:22px;">
      <div class="section-title">Start / stop / restart traders</div>
      <div class="muted" style="font-size:0.85rem; margin-bottom:8px;">Wraps stock.sh start/stop/restart — same PID-file logic as launching it from a terminal.</div>
      <select id="ctlMode">
        <option value="dry-run">Dry-run (no real orders)</option>
        <option value="demo">Demo (Capital.com fake money)</option>
        <option value="live">Live (REAL MONEY on both brokers)</option>
      </select>
      <input id="ctlLiveConfirm" type="text" placeholder='Type I UNDERSTAND to enable Live' style="width:260px;" oninput="onCtlLiveConfirmInput()">
      <button onclick="controlTraders('start')">Start</button>
      <button onclick="controlTraders('stop')">Stop</button>
      <button onclick="controlTraders('restart')">Restart</button>
      <div id="ctlTradersMsg" class="muted" style="font-size:0.8rem; margin-top:6px;"></div>
    </div>

    <div style="margin-top:22px;">
      <div class="section-title">Rebuild watchlist now</div>
      <div class="muted" style="font-size:0.85rem; margin-bottom:8px;">Forces a fresh backtest-gate rebuild (trade_monitor.py --rebuild-watchlist). Takes several minutes for the full universe — runs in the background, check back on this page.</div>
      <input id="ctlMinWinRate" type="number" step="0.01" min="0" max="1" placeholder="min win rate (blank = current default)" style="width:260px;">
      <button onclick="controlRebuildWatchlist()">Rebuild now</button>
      <div id="ctlRebuildMsg" class="muted" style="font-size:0.8rem; margin-top:6px;"></div>
    </div>

    <div style="margin-top:22px;">
      <div class="section-title">Strategies</div>
      <table><tbody id="ctlStrategyTable"></tbody></table>
    </div>

    <div style="margin-top:22px;">
      <div class="section-title">Run discovery scan</div>
      <div class="muted" style="font-size:0.85rem; margin-bottom:8px;">Widens the live instrument universe (weekend_discovery.py). Runs in the background.</div>
      <select id="ctlDiscoverSource">
        <option value="both">Both (NSE + US)</option>
        <option value="nse">NSE only</option>
        <option value="us">US only</option>
      </select>
      <input id="ctlDiscoverBatch" type="number" min="1" placeholder="batch size (blank = default)" style="width:220px;">
      <button onclick="controlDiscover()">Run scan</button>
      <div id="ctlDiscoverMsg" class="muted" style="font-size:0.8rem; margin-top:6px;"></div>
    </div>

    <div style="margin-top:22px;">
      <div class="section-title">Risk &amp; sizing</div>
      <div class="muted" style="font-size:0.85rem; margin-bottom:8px;">Applies immediately — no rebuild or restart needed, picked up on the next 5-minute loop tick.</div>
      <table><tbody id="ctlSettingsRisk"></tbody></table>
    </div>

    <div style="margin-top:22px;">
      <div class="section-title">Backtest gate thresholds</div>
      <div class="muted" style="font-size:0.85rem; margin-bottom:8px;">Applies on the next watchlist rebuild — click "Rebuild watchlist now" above after saving, or wait for the 7AM IST cron.</div>
      <table><tbody id="ctlSettingsGate"></tbody></table>
    </div>

    <div style="margin-top:22px;">
      <div class="section-title">Universe categories</div>
      <div class="muted" style="font-size:0.85rem; margin-bottom:8px;">Requires a rebuild AND a trader restart to fully take effect — the running traders hold the instrument list in memory from when they started.</div>
      <table><tbody id="ctlSettingsUniverse"></tbody></table>
    </div>

    <div class="muted" style="font-size:0.85rem; margin-top:22px;">
      Need to clear a trading halt? Use the "Acknowledge Halt" button on the Home page — it resets the risk baseline correctly, which a blunt file-delete would not.
    </div>
  </div>

<script>
const params = new URLSearchParams(window.location.search);
const VIEW_ALL = params.get('view') === 'all';
const VIEW_CONTROL = params.get('view') === 'control';
if (VIEW_CONTROL) {
  document.getElementById('navControl').classList.add('active');
  document.getElementById('mainView').hidden = true;
  document.getElementById('controlView').hidden = false;
} else {
  document.getElementById(VIEW_ALL ? 'navAll' : 'navHome').classList.add('active');
  document.getElementById('signalsHeading').textContent = VIEW_ALL
    ? 'All signals (including WAIT)' : 'Tradeable now (LONG / SHORT only)';
}

const BUCKET_ORDER = ["zerodha","asia","europe","us","swing/zerodha","swing/capitalcom"];
function esc(s) { return (s === null || s === undefined) ? "" : String(s); }
function renderVerdictCell(v) { return `<span class="verdict ${esc(v)}">${esc(v)}</span>`; }
function fmt(n, d) { return (n === null || n === undefined || Number.isNaN(n)) ? '—' : Number(n).toFixed(d ?? 2); }
function fmtMoney(n, sym) { return (n === null || n === undefined) ? '—' : `${sym || ''}${Number(n).toLocaleString(undefined,{maximumFractionDigits:2})}`; }

function compactHaltText(raw) {
  // Halt file's raw text is written for someone reading the file directly
  // (full ISO timestamp, an instructional second line) — condense that down
  // to one line for the dashboard's tight 2-column banner.
  if (!raw) return '';
  const firstLine = String(raw).split('\\n')[0] || String(raw);
  const m = firstLine.match(/^Auto-halted (\\S+) by ([^:]+): (.+)$/);
  if (!m) return firstLine;
  const [, iso, by, reason] = m;
  const d = new Date(iso);
  const when = isNaN(d) ? iso : d.toLocaleString(undefined, {hour:'2-digit', minute:'2-digit', month:'short', day:'numeric'});
  return `${reason} — ${by}, ${when}`;
}

function detectHaltBroker(raw) {
  // zerodha_trader.py's halt() writes "...by zerodha_trader.py: ..."; Capital.com's
  // auto_trader.py halt() has no "by X" segment at all — that absence IS the signal.
  if (!raw) return null;
  return /\\bby zerodha_trader\\.py\\b/.test(raw) ? 'zerodha' : 'capitalcom';
}

async function acknowledgeHalt(broker) {
  const label = broker === 'zerodha' ? 'Zerodha' : 'Capital.com';
  const reason = prompt(
    `Acknowledge this halt as caused by something outside the automation (e.g. a manual trade) and resume ${label} trading?\\n\\n` +
    `This resets ${label}'s daily-loss-cap baseline to its CURRENT equity — today's -4% cap will measure forward from right now, not from this loss — and clears the halt for BOTH brokers (it's a shared switch). Logged to halt_overrides.csv.\\n\\n` +
    `Reason (optional):`, 'manual trade');
  if (reason === null) return;  // cancelled
  const btn = document.getElementById('ackHaltBtn');
  if (btn) { btn.disabled = true; btn.textContent = 'Acknowledging…'; }
  try {
    const res = await fetch('/api/acknowledge-halt', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({broker, reason: reason || 'manual trade'})
    });
    const result = await res.json();
    if (result.ok) {
      alert(`${label} acknowledged — trading resumed. Baseline reset ${fmtMoney(result.previous_day_start_equity)} -> ${fmtMoney(result.new_day_start_equity)}.`);
      refreshSignals();
    } else {
      alert('Acknowledge failed: ' + (result.error || 'unknown error'));
      if (btn) { btn.disabled = false; btn.textContent = 'Acknowledge (manual trade) & Resume'; }
    }
  } catch (e) {
    alert('Acknowledge request failed: ' + e);
    if (btn) { btn.disabled = false; btn.textContent = 'Acknowledge (manual trade) & Resume'; }
  }
}

let lastData = null, lastPositions = null;
let brokerFilter = 'all';  // 'all' | 'capitalcom' | 'zerodha'

function bucketBroker(bucket) { return bucket.includes('zerodha') ? 'zerodha' : 'capitalcom'; }
function matchesFilter(broker) { return brokerFilter === 'all' || broker === brokerFilter; }

function setBrokerFilter(b) {
  brokerFilter = b;
  document.querySelectorAll('#filterbar .filterbtn').forEach(btn => btn.classList.toggle('active', btn.dataset.broker === b));
  renderHero();
  renderInsights();
  renderBuckets();
  renderTrades();
  renderPassingSymbols();
  renderPositionsTable();
  renderTrend();
  renderAttribution();
  renderProfitToday();
}
document.querySelectorAll('#filterbar .filterbtn').forEach(btn => btn.addEventListener('click', () => setBrokerFilter(btn.dataset.broker)));

function pill(label, running, live) {
  if (!running) return `<span class="pill pill-off">${label}: stopped</span>`;
  return live ? `<span class="pill pill-live">${label}: LIVE</span>` : `<span class="pill pill-demo">${label}: demo</span>`;
}

function riskBarHtml(dailyPnlPct, capPct) {
  if (dailyPnlPct === null || dailyPnlPct === undefined) {
    return `<div class="riskbar"><div class="riskbar-fill" style="width:0%"></div></div><div class="riskbar-label">no baseline yet today</div>`;
  }
  const used = dailyPnlPct < 0 ? Math.min(100, (-dailyPnlPct / capPct) * 100) : 0;
  const cls = used > 75 ? 'danger' : (used > 40 ? 'mid' : '');
  const label = dailyPnlPct >= 0
    ? `+${fmt(dailyPnlPct)}% today — no loss-budget used`
    : `${used.toFixed(0)}% of the -${capPct}% daily loss cap used`;
  return `<div class="riskbar"><div class="riskbar-fill ${cls}" style="width:${used.toFixed(0)}%"></div></div><div class="riskbar-label">${label}</div>`;
}

function accountCard(brokerLabel, a, err, capPct) {
  if (err) return `<div class="herocard"><div class="label">${brokerLabel}</div><div class="err">${esc(err)}</div></div>`;
  if (!a) return `<div class="herocard"><div class="label">${brokerLabel}</div><div class="value">—</div></div>`;
  const sym = a.currency_symbol || '';
  const pnlCls = (a.daily_pnl ?? 0) >= 0 ? 'up' : 'down';
  const sign = (a.daily_pnl ?? 0) >= 0 ? '+' : '';
  return `<div class="herocard">
    <div class="label">${brokerLabel}${a.account_name ? ' · ' + esc(a.account_name) : ''}</div>
    <div class="value">${fmtMoney(a.equity, sym)}</div>
    <div class="sub2 ${pnlCls}">${sign}${fmtMoney(a.daily_pnl, sym)} (${sign}${fmt(a.daily_pnl_pct)}%) today</div>
    ${a.available !== undefined ? `<div class="sub2">Available: ${fmtMoney(a.available, sym)}</div>` : ''}
    ${riskBarHtml(a.daily_pnl_pct, capPct)}
  </div>`;
}

function renderHero() {
  if (!lastData) return;
  const capPct = lastData.daily_loss_cap_pct ?? 4.0;
  const acct = (lastPositions && lastPositions.account) || {};
  const allPositions = (lastPositions && lastPositions.positions) || [];
  const positions = allPositions.filter(p => matchesFilter(p.broker));
  const openCount = positions.length;
  const byBroker = {};
  positions.forEach(p => { (byBroker[p.broker] = byBroker[p.broker] || []).push(p); });

  let html = '';
  if (matchesFilter('capitalcom')) html += accountCard('Capital.com', acct.capital, acct.capital_error, capPct);
  if (matchesFilter('zerodha')) html += accountCard('Zerodha', acct.zerodha, acct.zerodha_error, capPct);
  html += `<div class="herocard"><div class="label">Open positions</div><div class="value">${openCount}</div>
    <div class="sub2">${Object.entries(byBroker).map(([b,ps]) => `${esc(b)}: ${ps.length}`).join(' · ') || 'none open'}</div></div>`;
  if (brokerFilter === 'all') {
    const ig = lastData.intraday_gate, sg = lastData.swing_gate;
    html += `<div class="herocard"><div class="label">Backtest gate today</div>
      <div class="value">${ig ? ig.passing : '—'}${ig ? '/' + ig.total : ''}</div>
      <div class="sub2">intraday instruments cleared${sg ? ` · swing: ${sg.passing}/${sg.total}` : ''} (both brokers combined)</div></div>`;
  }
  document.getElementById('heroGrid').innerHTML = html;
}

function renderInsights() {
  if (!lastData) return;
  const items = [];  // each item optionally carries `broker` ('capitalcom'/'zerodha'); omitted = always relevant
  const ig = lastData.intraday_gate, sg = lastData.swing_gate, cg = lastData.inbound_gate;
  if (brokerFilter === 'all') {
    if (ig) items.push({level:'info', text:`${ig.passing} of ${ig.total} instruments pass today's intraday backtest gate${ig.min_trades != null ? ` (min ${ig.min_trades} trades, profit factor ≥${ig.min_profit_factor}, expectancy ≥${ig.min_expectancy}R)` : ''} — everything else is filtered out before it can even generate a signal.`});
    if (sg) items.push({level:'info', text:`${sg.passing} of ${sg.total} instruments pass today's swing (multi-day hold) backtest gate${sg.min_trades != null ? ` (min ${sg.min_trades} trades, profit factor ≥${sg.min_profit_factor}, expectancy ≥${sg.min_expectancy}R)` : ''}.`});
    if (cg) items.push({level:'info', text:`${cg.passing} of ${cg.total} instruments pass today's INBOUNDTRADEALGO gate${cg.min_trades != null ? ` (min ${cg.min_trades} trades, profit factor ≥${cg.min_profit_factor}, expectancy ≥${cg.min_expectancy}R)` : ''} — range mean-reversion, disable via automation/data/strategy_config.json.`});
  }

  const acct = (lastPositions && lastPositions.account) || {};
  const capPct = lastData.daily_loss_cap_pct ?? 4.0;
  for (const [label, key, broker] of [['Capital.com','capital','capitalcom'], ['Zerodha','zerodha','zerodha']]) {
    const a = acct[key];
    if (a && a.daily_pnl_pct !== null && a.daily_pnl_pct !== undefined) {
      const headroom = capPct + a.daily_pnl_pct;
      const level = headroom < 1 ? 'danger' : (headroom < 2 ? 'warn' : 'info');
      items.push({level, broker, text: `${label} daily P&L ${a.daily_pnl_pct >= 0 ? '+' : ''}${fmt(a.daily_pnl_pct)}% — ${fmt(headroom,1)} percentage points of headroom left before the -${capPct}% circuit breaker halts trading.`});
    }
  }

  (( lastPositions && lastPositions.positions) || []).forEach(p => {
    if (p.stop === null || p.stop === undefined) {
      items.push({level:'danger', broker: p.broker, text:`${esc(p.broker)} ${esc(p.symbol)} has NO stop-loss attached — fully exposed to further downside until you add one or close it.`});
    }
  });

  if (lastData.halted) {
    items.push({level:'danger', text:'Trading is currently HALTED — the automated engine will not place any new trades until the halt is cleared.'});
  }
  if (lastData.engine && !lastData.engine.capital_running) items.push({level:'warn', broker:'capitalcom', text:'Capital.com trading loop is not running — no signals are being acted on for that broker.'});
  if (lastData.engine && !lastData.engine.zerodha_running) items.push({level:'warn', broker:'zerodha', text:'Zerodha trading loop is not running — no signals are being acted on for that broker.'});

  const visible = items.filter(i => !i.broker || matchesFilter(i.broker));

  // info = general guidance (context, not something needing action);
  // warn/danger = risk alerts, danger (red) surfaced above warn (orange).
  const general = visible.filter(i => i.level === 'info');
  const severityOrder = {danger: 0, warn: 1};
  const risk = visible.filter(i => i.level !== 'info').sort((a, b) => severityOrder[a.level] - severityOrder[b.level]);

  const render = (list, emptyText) => list.length
    ? list.map(i => `<div class="insight insight-${i.level}">${esc(i.text)}</div>`).join('')
    : `<div class="empty">${emptyText}</div>`;
  document.getElementById('insightsGeneral').innerHTML = render(general, 'No general notes right now.');
  document.getElementById('insightsRisk').innerHTML = render(risk, 'No risk items right now.');
}

async function refreshSignals() {
  let data;
  try {
    const res = await fetch('/api/data');
    data = await res.json();
  } catch (e) {
    document.getElementById('metaline').textContent = 'Could not reach dashboard server — is it still running?';
    return;
  }
  lastData = data;

  document.getElementById('metaline').textContent =
    `As of ${data.as_of_date || '?'} — gate built ${data.watchlist_generated_at_local || '?'} — ` +
    `ranking refreshed ${data.ranking_refreshed_at_local || '?'} — page updated ${new Date().toLocaleTimeString()}`;

  document.getElementById('enginePills').innerHTML =
    pill('Capital.com', data.engine && data.engine.capital_running, data.engine && data.engine.capital_live) +
    pill('Zerodha', data.engine && data.engine.zerodha_running, data.engine && data.engine.zerodha_live);

  document.getElementById('topinfo').classList.toggle('halted', !!data.halted);
  const haltDiv = document.getElementById('haltbanner');
  if (data.halted) {
    const detected = detectHaltBroker(data.halt_text);
    haltDiv.innerHTML = `<div class="halt-banner">⛔ TRADING HALTED — ${esc(compactHaltText(data.halt_text))}` +
      `<div class="halt-actions"><button id="ackHaltBtn" class="ackbtn" onclick="acknowledgeHalt('${detected}')">` +
      `Acknowledge (manual trade) & Resume</button>` +
      `<span class="halt-hint">Only use this if YOU know the loss came from something outside the automation (e.g. a manual trade) — resets ${esc(detected === 'zerodha' ? 'Zerodha' : 'Capital.com')}'s baseline to current equity and clears the halt.</span></div></div>`;
  } else {
    haltDiv.innerHTML = '';
  }

  renderHero();
  renderInsights();
  renderBuckets();
  renderTrades();
  renderPassingSymbols();
}

let activeBucketTab = null;

function bucketRows(data, bucket) {
  let rows = data.buckets[bucket] || [];
  if (!VIEW_ALL) rows = rows.filter(r => r.verdict === 'LONG' || r.verdict === 'SHORT');
  return rows;
}

function renderBuckets() {
  if (!lastData) return;
  const data = lastData;
  const tabsDiv = document.getElementById('bucketTabs');
  const bucketsDiv = document.getElementById('buckets');
  const seen = new Set();
  const orderedKeys = [...BUCKET_ORDER.filter(k => data.buckets[k]), ...Object.keys(data.buckets).filter(k => !BUCKET_ORDER.includes(k))]
    .filter(k => !seen.has(k) && seen.add(k) && matchesFilter(bucketBroker(k)))
    .filter(k => VIEW_ALL || bucketRows(data, k).length > 0);  // hide empty tabs entirely on the home view

  if (orderedKeys.length === 0) {
    tabsDiv.innerHTML = '';
    bucketsDiv.innerHTML = '<div class="empty">No LONG/SHORT signals right now — the gate + strategy haven\\'t found a qualifying setup this cycle. See "All Signals" for the full WAIT list.</div>';
    return;
  }
  if (!activeBucketTab || !orderedKeys.includes(activeBucketTab)) activeBucketTab = orderedKeys[0];

  tabsDiv.innerHTML = orderedKeys.map(bucket => {
    const n = bucketRows(data, bucket).length;
    return `<button class="tabbtn ${bucket === activeBucketTab ? 'active' : ''}" data-bucket="${esc(bucket)}">${esc(bucket)}<span class="count">${n}</span></button>`;
  }).join('');
  tabsDiv.querySelectorAll('.tabbtn').forEach(btn => btn.addEventListener('click', () => {
    activeBucketTab = btn.dataset.bucket;
    renderBuckets();
  }));

  const rows = bucketRows(data, activeBucketTab);
  let rowsHtml = rows.map(r => {
    const bt = r.backtest || {};
    return `
    <tr>
      <td class="${r.is_pick ? 'pick' : ''}">${r.is_pick ? '★ ' : ''}${esc(r.symbol)}</td>
      <td>${esc(r.name)}</td>
      <td>${renderVerdictCell(r.verdict)}</td>
      <td>${esc(r.last_close)}</td>
      <td>${esc(r.rsi)}</td>
      <td>${esc(r.entry)}</td>
      <td>${esc(r.stop)}</td>
      <td>${esc(r.target)}</td>
      <td>${bt.win_rate != null ? (bt.win_rate*100).toFixed(0)+'%' : '—'}</td>
      <td>${bt.profit_factor != null ? bt.profit_factor.toFixed(2) : '—'}</td>
      <td>${bt.expectancy_r != null ? bt.expectancy_r.toFixed(2)+'R' : '—'}</td>
      <td style="white-space:normal;color:#8b93a1;font-size:0.78rem;">${esc(r.reason || r.error || '')}</td>
    </tr>`;
  }).join('') || `<tr><td colspan="12" class="empty">no data yet</td></tr>`;
  bucketsDiv.innerHTML = `<div class="card">
    <table><thead><tr>
      <th>Symbol</th><th>Name</th><th>Verdict</th><th>Px</th><th>RSI</th><th>Entry</th><th>Stop</th><th>Target</th>
      <th>Win%</th><th>PF</th><th>Exp</th><th>Reason</th>
    </tr></thead><tbody>${rowsHtml}</tbody></table></div>`;
}

const TREND_SERIES = [
  {key: 'capital_equity', broker: 'capitalcom', label: 'Capital.com', color: 'var(--accent)'},
  {key: 'zerodha_equity', broker: 'zerodha', label: 'Zerodha', color: 'var(--pick)'},
];

function renderTrend() {
  const wrap = document.getElementById('trendChartWrap');
  const legend = document.getElementById('trendLegend');
  const recoCard = document.getElementById('recoCard');
  if (!lastPositions) return;
  const history = lastPositions.portfolio_history || [];
  const reco = lastPositions.recommendation;

  const series = TREND_SERIES.filter(s => matchesFilter(s.broker));
  legend.innerHTML = series.map(s => `<span class="legend-item"><span class="legend-swatch" style="background:${s.color}"></span>${s.label}</span>`).join('')
    + `<span class="legend-item" style="margin-left:auto;">indexed to first day = 100</span>`;

  const W = 560, H = 150, padL = 34, padR = 16, padT = 14, padB = 22;
  const plotW = W - padL - padR, plotH = H - padT - padB;

  // Build indexed series (first valid point = 100) so two different-currency
  // accounts can share one axis without a dual-scale chart.
  const indexed = series.map(s => {
    const pts = history.map((r, i) => ({i, raw: r[s.key] !== '' && r[s.key] != null ? Number(r[s.key]) : null}));
    const base = pts.find(p => p.raw !== null);
    const vals = pts.map(p => (p.raw !== null && base) ? (p.raw / base.raw * 100) : null);
    return {...s, vals};
  }).filter(s => s.vals.some(v => v !== null));

  if (history.length < 2 || indexed.length === 0) {
    wrap.innerHTML = '<div class="empty">Trend builds up as the dashboard polls live equity — check back after a couple of trading days.</div>';
  } else {
    const allVals = indexed.flatMap(s => s.vals.filter(v => v !== null));
    const min = Math.min(...allVals, 100), max = Math.max(...allVals, 100);
    const span = (max - min) || 1;
    const x = i => padL + (history.length === 1 ? 0 : i / (history.length - 1) * plotW);
    const y = v => padT + plotH - ((v - min) / span) * plotH;

    let svg = `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" style="overflow:visible;">`;
    // recessive gridlines
    [0, 0.5, 1].forEach(f => {
      const gy = padT + f * plotH;
      svg += `<line x1="${padL}" y1="${gy}" x2="${W-padR}" y2="${gy}" stroke="var(--border)" stroke-width="1"/>`;
    });
    svg += `<text x="4" y="${y(100)+3}" font-size="9" fill="var(--muted)">100</text>`;
    history.forEach((r, i) => {
      if (i === 0 || i === history.length - 1 || history.length <= 6) {
        svg += `<text x="${x(i)}" y="${H-4}" font-size="8" fill="var(--muted)" text-anchor="middle">${esc((r.date||'').slice(5))}</text>`;
      }
    });
    indexed.forEach(s => {
      const pts = s.vals.map((v, i) => v === null ? null : [x(i), y(v)]).filter(Boolean);
      if (pts.length === 0) return;
      const d = pts.map((p, i) => `${i===0?'M':'L'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
      svg += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`;
      pts.forEach((p, i) => {
        svg += `<circle class="trendpt" data-broker="${s.broker}" data-i="${history.findIndex((r,ri)=>x(ri)===p[0])}" cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="3.5" fill="var(--panel)" stroke="${s.color}" stroke-width="2"/>`;
      });
    });
    svg += `</svg><div class="chart-tooltip" id="trendTooltip"></div>`;
    wrap.innerHTML = svg;

    const tip = document.getElementById('trendTooltip');
    wrap.querySelectorAll('.trendpt').forEach(pt => {
      pt.addEventListener('mouseenter', () => {
        const i = parseInt(pt.dataset.i, 10);
        const row = history[i];
        const s = TREND_SERIES.find(t => t.broker === pt.dataset.broker);
        const raw = row[s.key];
        tip.innerHTML = `<b>${esc(s.label)}</b> · ${esc(row.date)}<br>${fmtMoney(raw)}`;
        const rect = pt.getBoundingClientRect(), wrapRect = wrap.getBoundingClientRect();
        tip.style.left = (rect.left - wrapRect.left + rect.width/2) + 'px';
        tip.style.top = (rect.top - wrapRect.top) + 'px';
        tip.classList.add('show');
      });
      pt.addEventListener('mouseleave', () => tip.classList.remove('show'));
    });
  }

  if (reco) {
    recoCard.innerHTML = `<div class="section-title" style="margin:0;">Recommendation</div>
      <span class="reco-badge reco-${esc(reco.trend)}">${reco.trend === 'up' ? '▲ Trending up' : reco.trend === 'down' ? '▼ Trending down' : '● Flat'}</span>
      <div class="reco-text">${esc(reco.text)}</div>`;
  }
}

function renderAttribution() {
  const card = document.getElementById('attribCard');
  const body = document.getElementById('attribBody');
  if (!matchesFilter('zerodha')) { card.style.display = 'none'; return; }
  card.style.display = '';
  const a = lastPositions && lastPositions.zerodha_trade_attribution;
  if (!a) { body.innerHTML = '<div class="empty">loading…</div>'; return; }
  if (a.error) { body.innerHTML = `<div class="err">Could not read Zerodha's order book: ${esc(a.error)}</div>`; return; }
  const total = a.automated_trades + a.manual_trades + a.mixed_trades;
  const pnlCls = v => (v ?? 0) >= 0 ? 'up' : 'down';
  const sign = v => (v ?? 0) >= 0 ? '+' : '';
  const pct = n => total ? (n / total * 100) : 0;
  body.innerHTML = `
    <div class="attrib-row">
      <div class="attrib-stat"><div class="n">${a.automated_trades}</div><div class="l">Automated symbols</div>
        <div class="sub2 ${pnlCls(a.automated_pnl)}">${sign(a.automated_pnl)}${fmtMoney(a.automated_pnl,'₹')} realized</div></div>
      <div class="attrib-stat"><div class="n">${a.manual_trades}</div><div class="l">Manual symbols</div>
        <div class="sub2 ${pnlCls(a.manual_pnl)}">${sign(a.manual_pnl)}${fmtMoney(a.manual_pnl,'₹')} realized</div></div>
      ${a.mixed_trades ? `<div class="attrib-stat"><div class="n">${a.mixed_trades}</div><div class="l">Mixed (both today)</div>
        <div class="sub2 ${pnlCls(a.mixed_pnl)}">${sign(a.mixed_pnl)}${fmtMoney(a.mixed_pnl,'₹')} realized</div></div>` : ''}
    </div>
    <div class="attrib-bar">${total ? `
      <div class="seg-auto" style="width:${pct(a.automated_trades)}%" title="Automated"></div>
      <div class="seg-manual" style="width:${pct(a.manual_trades)}%" title="Manual"></div>
      ${a.mixed_trades ? `<div class="seg-mixed" style="width:${pct(a.mixed_trades)}%" title="Mixed"></div>` : ''}
    ` : ''}</div>
    <div class="sub2" style="margin-top:6px;">${total ? '' : 'No completed Zerodha orders yet today.'}
      Based on today's order tags — orders zerodha_trader.py places are tagged "autotrader"; anything else was placed by hand in Kite. Capital.com's API has no equivalent tag, so this split covers Zerodha only.</div>`;
}

function miniProfitChart(label, currencySym, rows, key) {
  const W = 280, H = 100, padL = 4, padR = 4, padT = 10, padB = 16;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const vals = rows.map(r => r[key] !== '' && r[key] != null ? Number(r[key]) : null);
  const present = vals.map((v,i) => ({v,i})).filter(p => p.v !== null);
  if (present.length < 2) {
    return `<div class="card profit-card"><div class="label" style="color:var(--muted);font-size:0.76rem;">${esc(label)}</div>
      <div class="empty">Not enough samples yet today — check back in a few minutes.</div></div>`;
  }
  const allV = present.map(p => p.v);
  const min = Math.min(...allV, 0), max = Math.max(...allV, 0);
  const span = (max - min) || 1;
  const x = i => padL + (present.length === 1 ? 0 : (present.findIndex(p=>p.i===i)) / (present.length - 1) * plotW);
  const y = v => padT + plotH - ((v - min) / span) * plotH;
  const last = present[present.length-1].v;
  const color = last >= 0 ? 'var(--long)' : 'var(--short)';
  const zeroY = y(0);
  const pts = present.map(p => [x(p.i), y(p.v)]);
  const line = pts.map((p,i) => `${i===0?'M':'L'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
  const area = `${line} L${pts[pts.length-1][0].toFixed(1)},${zeroY.toFixed(1)} L${pts[0][0].toFixed(1)},${zeroY.toFixed(1)} Z`;
  const first = rows[present[0].i], lastRow = rows[present[present.length-1].i];
  return `<div class="card profit-card">
    <div style="display:flex;justify-content:space-between;align-items:baseline;">
      <div class="label" style="color:var(--muted);font-size:0.76rem;">${esc(label)}</div>
      <div class="value ${last>=0?'up':'down'}">${last>=0?'+':''}${fmtMoney(last, currencySym)}</div>
    </div>
    <svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}">
      <line x1="${padL}" y1="${zeroY.toFixed(1)}" x2="${W-padR}" y2="${zeroY.toFixed(1)}" stroke="var(--border)" stroke-width="1"/>
      <path d="${area}" fill="${color}" fill-opacity="0.12" stroke="none"/>
      <path d="${line}" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>
    <div class="sub2" style="display:flex;justify-content:space-between;">
      <span>${esc((first.timestamp_local||'').slice(11,16))}</span><span>${esc((lastRow.timestamp_local||'').slice(11,16))}</span>
    </div>
  </div>`;
}

function renderProfitToday() {
  const grid = document.getElementById('profitGrid');
  if (!lastPositions) return;
  const rows = lastPositions.intraday_pnl || [];
  let html = '';
  if (matchesFilter('capitalcom')) html += miniProfitChart('Capital.com', '', rows, 'capital_daily_pnl');
  if (matchesFilter('zerodha')) html += miniProfitChart('Zerodha', '₹', rows, 'zerodha_daily_pnl');
  grid.innerHTML = html;
}

function renderTrades() {
  if (!lastData) return;
  const trades = (lastData.trades || []).filter(t => matchesFilter(t.broker));
  const tbody = document.querySelector('#tradesTable tbody');
  tbody.innerHTML = trades.map(t => `
    <tr>
      <td>${esc(t.checked_at)}</td>
      <td>${esc(t.broker)}</td>
      <td>${esc(t.symbol)}</td>
      <td>${renderVerdictCell(t.verdict)}</td>
      <td>${esc(t.size ?? t.qty)}</td>
      <td>${esc(t.mode)}</td>
      <td class="outcome-${esc(t.outcome)}">${esc(t.outcome)}</td>
      <td style="white-space:normal;color:#8b93a1;font-size:0.78rem;">${esc(t.reason)}</td>
    </tr>`).join('') || `<tr><td colspan="8" class="empty">no trade decisions logged yet</td></tr>`;
}

function renderBiasCell(bias) {
  const b = esc(bias || 'neutral');
  const cls = b === 'bullish' ? 'LONG' : (b === 'bearish' ? 'SHORT' : '');
  return cls ? `<span class="verdict ${cls}">${b}</span>` : b;
}

function renderPassingSymbols() {
  if (!lastData) return;
  const intraday = lastData.intraday_passing_symbols || [];
  const investment = lastData.investment_passing_symbols || [];
  const iBody = document.querySelector('#intradayPassingTable tbody');
  iBody.innerHTML = intraday.map(r => `
    <tr><td>${esc(r.symbol)}</td><td>${esc(r.name)}</td><td>${renderBiasCell(r.bias)}</td><td>${renderVerdictCell(r.live_verdict)}</td></tr>`).join('')
    || `<tr><td colspan="4" class="empty">0 symbols currently pass the intraday backtest gate</td></tr>`;
  const vBody = document.querySelector('#investmentPassingTable tbody');
  vBody.innerHTML = investment.map(r => `
    <tr><td>${esc(r.symbol)}</td><td>${esc(r.name)}</td><td>${esc((r.strategies || []).join(', '))}</td><td>${renderBiasCell(r.bias)}</td><td>${renderVerdictCell(r.live_verdict)}</td></tr>`).join('')
    || `<tr><td colspan="5" class="empty">0 symbols currently pass the investment backtest gate</td></tr>`;
}

async function closePosition(payload, btn) {
  const label = `${payload.broker} ${payload.symbol}`;
  if (!confirm(`Close this position now?\\n\\n${label}\\n\\nThis places a REAL order if you're in live mode. This cannot be undone from here.`)) return;
  btn.disabled = true;
  btn.textContent = 'Closing…';
  try {
    const res = await fetch('/api/close', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
    });
    const result = await res.json();
    if (result.ok) {
      btn.textContent = 'Closed';
      setTimeout(refreshPositions, 1500);
    } else {
      alert('Close failed: ' + (result.error || 'unknown error'));
      btn.disabled = false; btn.textContent = 'Close';
    }
  } catch (e) {
    alert('Close request failed: ' + e);
    btn.disabled = false; btn.textContent = 'Close';
  }
}

async function refreshPositions() {
  let data;
  try {
    const res = await fetch('/api/positions');
    data = await res.json();
  } catch (e) {
    document.getElementById('positionsErr').textContent = 'Could not reach positions endpoint.';
    return;
  }
  lastPositions = data;
  const errs = [];
  if (data.capital_error) errs.push('Capital.com: ' + data.capital_error);
  if (data.zerodha_error) errs.push('Zerodha: ' + data.zerodha_error);
  document.getElementById('positionsErr').textContent = errs.join(' | ');

  renderHero();
  renderInsights();
  renderPositionsTable();
  renderTrend();
  renderAttribution();
  renderProfitToday();
}

function renderPositionsTable() {
  if (!lastPositions) return;
  const tbody = document.querySelector('#positionsTable tbody');
  const positions = (lastPositions.positions || []).filter(p => matchesFilter(p.broker));
  if (positions.length === 0) {
    tbody.innerHTML = `<tr><td colspan="10" class="empty">no open positions</td></tr>`;
    return;
  }
  tbody.innerHTML = positions.map((p, i) => {
    const pnlClass = (p.pnl ?? 0) >= 0 ? 'pnl-pos' : 'pnl-neg';
    const stopCell = (p.stop === null || p.stop === undefined) ? '<span class="nostop">none ⚠</span>' : esc(p.stop);
    return `<tr>
      <td>${esc(p.broker)}</td>
      <td>${esc(p.symbol)} <span style="color:#8b93a1;font-size:0.78rem;">${esc(p.name)}</span></td>
      <td>${renderVerdictCell(p.direction)}</td>
      <td>${esc(p.size)}</td>
      <td>${esc(p.entry)}</td>
      <td>${esc(p.current_price)}</td>
      <td>${stopCell}</td>
      <td>${esc(p.target)}</td>
      <td class="${pnlClass}">${esc(p.pnl)}</td>
      <td><button class="closebtn" data-idx="${i}">Close</button></td>
    </tr>`;
  }).join('');
  tbody.querySelectorAll('.closebtn').forEach(btn => {
    btn.addEventListener('click', () => {
      const p = positions[parseInt(btn.dataset.idx, 10)];
      closePosition({broker: p.broker, symbol: p.symbol, close_ref: p.close_ref}, btn);
    });
  });
}

// --- Control Panel -----------------------------------------------------
function onCtlLiveConfirmInput() {
  document.getElementById('ctlMode').dataset.liveArmed =
    document.getElementById('ctlLiveConfirm').value === 'I UNDERSTAND' ? '1' : '';
}

async function postControl(path, body, msgEl) {
  msgEl.textContent = 'working…';
  try {
    const res = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const result = await res.json();
    if (result.ok) {
      msgEl.textContent = result.started ? 'started — check back in a few minutes.' : 'done.';
      refreshControlStatus();
    } else {
      msgEl.textContent = 'failed: ' + (result.error || result.stderr || 'unknown error');
    }
    return result;
  } catch (e) {
    msgEl.textContent = 'request failed: ' + e;
  }
}

function controlTraders(action) {
  const mode = document.getElementById('ctlMode').value;
  const msgEl = document.getElementById('ctlTradersMsg');
  const body = {action, mode};
  if (mode === 'live') {
    if (document.getElementById('ctlMode').dataset.liveArmed !== '1') {
      msgEl.textContent = 'Type "I UNDERSTAND" in the box first to arm Live mode.';
      return;
    }
    if (!confirm('This places REAL orders with REAL money on both brokers. Continue?')) return;
    body.confirm = 'I UNDERSTAND';
  } else if (action !== 'stop' && !confirm(`${action} traders in ${mode} mode?`)) {
    return;
  }
  postControl('/api/control/traders', body, msgEl);
}

function controlRebuildWatchlist() {
  const raw = document.getElementById('ctlMinWinRate').value;
  const body = {};
  if (raw !== '') body.min_win_rate = Number(raw);
  postControl('/api/control/rebuild-watchlist', body, document.getElementById('ctlRebuildMsg'));
}

function controlDiscover() {
  const source = document.getElementById('ctlDiscoverSource').value;
  const raw = document.getElementById('ctlDiscoverBatch').value;
  const body = {source};
  if (raw !== '') body.batch_size = Number(raw);
  postControl('/api/control/discover', body, document.getElementById('ctlDiscoverMsg'));
}

function controlStrategyToggle(name, enabled, msgEl) {
  postControl('/api/control/strategy', {name, enabled}, msgEl);
}

function renderStrategyTable(strategyStatus) {
  const tbody = document.getElementById('ctlStrategyTable');
  if (!tbody) return;
  const entries = Object.entries(strategyStatus || {});
  tbody.innerHTML = entries.map(([name, enabled]) => `
    <tr>
      <td style="width:220px;">${esc(name)}</td>
      <td><span class="verdict ${enabled ? 'LONG' : ''}">${enabled ? 'enabled' : 'disabled'}</span></td>
      <td><button data-name="${esc(name)}" data-enabled="${enabled ? '0' : '1'}">${enabled ? 'Disable' : 'Enable'}</button></td>
      <td class="muted" style="font-size:0.78rem;" id="ctlStrategyMsg_${esc(name)}"></td>
    </tr>`).join('') || `<tr><td class="empty">no strategies found</td></tr>`;
  tbody.querySelectorAll('button[data-name]').forEach(btn => {
    btn.addEventListener('click', () => {
      const name = btn.dataset.name, enabled = btn.dataset.enabled === '1';
      controlStrategyToggle(name, enabled, document.getElementById(`ctlStrategyMsg_${name}`));
    });
  });
}

// Grouping + confirm-on-save is UI policy only — the server independently
// validates every value regardless of which group it's shown in.
const SETTINGS_GROUPS = {
  ctlSettingsRisk: {names: ['intraday_capital_pct', 'per_trade_risk_pct', 'daily_loss_cap_pct'], confirm: true},
  ctlSettingsGate: {names: ['intraday_min_win_rate', 'swing_min_win_rate', 'intraday_min_trades',
                             'intraday_min_profit_factor', 'intraday_min_expectancy',
                             'swing_min_trades', 'swing_min_profit_factor', 'swing_min_expectancy'], confirm: false},
  ctlSettingsUniverse: {names: ['universe_forex_enabled', 'universe_commodities_enabled',
                                 'universe_crypto_enabled', 'universe_wide_us_enabled'], confirm: false},
};
const RELOAD_LABEL = {immediate: 'applies immediately', next_rebuild: 'next rebuild',
                       next_rebuild_and_restart: 'next rebuild + restart'};

function saveSetting(name, rawValue, isBool, msgEl) {
  const value = isBool ? rawValue : Number(rawValue);
  if (!isBool && Number.isNaN(value)) { msgEl.textContent = 'not a number'; return; }
  if (SETTINGS_GROUPS.ctlSettingsRisk.names.includes(name)
      && !confirm(`Change ${name} to ${value}? This affects real trade sizing/risk.`)) return;
  postControl('/api/control/settings', {name, value}, msgEl).then(result => {
    if (result && result.ok) {
      msgEl.textContent = `saved (${RELOAD_LABEL[result.reload] || result.reload})`;
      if (result.reload !== 'immediate') {
        msgEl.innerHTML += ` — <a href="#" onclick="controlRebuildWatchlist(); return false;">rebuild now</a>`;
      }
      if (result.reload === 'next_rebuild_and_restart') {
        msgEl.innerHTML += ` <a href="#" onclick="controlTraders('restart'); return false;">/ restart traders</a>`;
      }
    }
  });
}

function renderSettingsTable(tbodyId, settings) {
  const tbody = document.getElementById(tbodyId);
  if (!tbody) return;
  const group = SETTINGS_GROUPS[tbodyId];
  tbody.innerHTML = group.names.map(name => {
    const info = (settings || {})[name] || {value: '', reload: '?'};
    const isBool = typeof info.value === 'boolean';
    const inputHtml = isBool
      ? `<input type="checkbox" id="ctlSetting_${name}" ${info.value ? 'checked' : ''}>`
      : `<input type="number" step="any" id="ctlSetting_${name}" value="${esc(info.value)}" style="width:110px;">`;
    return `
      <tr>
        <td style="width:220px;">${esc(name)}</td>
        <td>${inputHtml}</td>
        <td class="muted" style="font-size:0.75rem;">${RELOAD_LABEL[info.reload] || esc(info.reload)}</td>
        <td><button data-name="${esc(name)}" data-bool="${isBool ? '1' : '0'}">Save</button></td>
        <td class="muted" style="font-size:0.78rem;" id="ctlSettingMsg_${esc(name)}"></td>
      </tr>`;
  }).join('');
  tbody.querySelectorAll('button[data-name]').forEach(btn => {
    btn.addEventListener('click', () => {
      const name = btn.dataset.name, isBool = btn.dataset.bool === '1';
      const input = document.getElementById(`ctlSetting_${name}`);
      const rawValue = isBool ? input.checked : input.value;
      saveSetting(name, rawValue, isBool, document.getElementById(`ctlSettingMsg_${name}`));
    });
  });
}

function renderAllSettingsTables(tradingSettings) {
  Object.keys(SETTINGS_GROUPS).forEach(tbodyId => renderSettingsTable(tbodyId, tradingSettings));
}

async function refreshControlStatus() {
  if (!VIEW_CONTROL) return;
  try {
    const res = await fetch('/api/control/status');
    const result = await res.json();
    const lines = {};
    (result.stdout || '').split('\\n').forEach(line => {
      const i = line.indexOf(': ');
      if (i > 0) lines[line.slice(0, i)] = line.slice(i + 2);
    });
    document.getElementById('ctlCapitalStatus').textContent =
      lines.capital_running === 'true' ? `running (pid ${lines.capital_pid}, ${lines.capital_mode})` : 'stopped';
    document.getElementById('ctlZerodhaStatus').textContent =
      lines.zerodha_running === 'true' ? `running (pid ${lines.zerodha_pid}, ${lines.zerodha_mode})` : 'stopped';
    document.getElementById('ctlDashboardStatus').textContent =
      lines.dashboard_running === 'true' ? 'running' : 'stopped';
    document.getElementById('ctlHaltStatus').textContent =
      lines.halted === 'true' ? `HALTED — ${lines.halt_text || ''}` : 'not halted';
    document.getElementById('ctlWatchlistStatus').textContent =
      `generated ${lines.watchlist_generated_at_local || 'never'} (as_of ${lines.watchlist_as_of_date || '—'})`;
  } catch (e) {
    document.getElementById('ctlCapitalStatus').textContent = 'status check failed: ' + e;
  }
  if (lastData && lastData.strategy_status) renderStrategyTable(lastData.strategy_status);
  // Settings tables render ONCE, not on every 5s poll — unlike the strategy
  // toggle buttons above, these hold free-text/number inputs a user may be
  // mid-edit on; re-rendering on a timer would silently wipe unsaved typing.
  if (!settingsRendered && lastData && lastData.trading_settings) {
    renderAllSettingsTables(lastData.trading_settings);
    settingsRendered = true;
  }
}

let settingsRendered = false;
if (VIEW_CONTROL) {
  refreshControlStatus();
  setInterval(refreshControlStatus, 5000);
}

refreshSignals();
refreshPositions();
setInterval(refreshSignals, 5000);
setInterval(refreshPositions, 2000);
</script>
</body></html>
"""


# DASHBOARD_DATA_DIR overrides where the password file lives — the packaged
# desktop app (frozen via PyInstaller) sets this to a guaranteed-writable
# per-user directory, since __file__/ROOT resolution inside a frozen exe
# isn't a real, predictable filesystem path to write into.
DASHBOARD_PASSWORD_FILE = Path(os.environ.get("DASHBOARD_DATA_DIR", str(ROOT / "data"))) / ".dashboard_password"


def _load_or_create_password():
    """HTTP Basic Auth password. DASHBOARD_PASSWORD env var wins; otherwise
    a random one is generated once and cached on disk so it survives restarts."""
    env_pw = os.environ.get("DASHBOARD_PASSWORD")
    if env_pw:
        return env_pw
    if DASHBOARD_PASSWORD_FILE.exists():
        return DASHBOARD_PASSWORD_FILE.read_text().strip()
    pw = secrets.token_urlsafe(18)
    DASHBOARD_PASSWORD_FILE.parent.mkdir(parents=True, exist_ok=True)
    DASHBOARD_PASSWORD_FILE.write_text(pw)
    return pw


DASHBOARD_PASSWORD = _load_or_create_password()
DASHBOARD_USER = "admin"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _authorized(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            user, _, pw = decoded.partition(":")
        except Exception:
            return False
        return hmac.compare_digest(user, DASHBOARD_USER) and hmac.compare_digest(pw, DASHBOARD_PASSWORD)

    def _require_auth(self):
        if self._authorized():
            return True
        body = b"Authentication required."
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Trading Dashboard"')
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def _send_json(self, obj, status=200):
        # A closed/navigated-away browser tab racing a slow broker poll is
        # normal traffic, not a server fault — swallow the resulting
        # BrokenPipeError/ConnectionResetError instead of dumping a
        # traceback for every dropped connection.
        try:
            body = json.dumps(obj, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if not self._require_auth():
            return
        if self.path == "/" or self.path.startswith("/?"):
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/data"):
            try:
                self._send_json(build_snapshot())
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif self.path.startswith("/api/positions"):
            try:
                self._send_json(build_positions_snapshot())
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif self.path.startswith("/api/control/status"):
            try:
                self._send_json(control_status())
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
        else:
            self.send_response(404)
            self.end_headers()

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_POST(self):
        if not self._require_auth():
            return
        if self.path.startswith("/api/control/traders"):
            try:
                result = control_traders(self._read_json_body())
                self._send_json(result, 200 if result.get("ok") else 400)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
        elif self.path.startswith("/api/control/rebuild-watchlist"):
            try:
                result = control_rebuild_watchlist(self._read_json_body())
                self._send_json(result, 200 if result.get("ok") else 400)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
        elif self.path.startswith("/api/control/strategy"):
            try:
                result = control_strategy(self._read_json_body())
                self._send_json(result, 200 if result.get("ok") else 400)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
        elif self.path.startswith("/api/control/discover"):
            try:
                result = control_discover(self._read_json_body())
                self._send_json(result, 200 if result.get("ok") else 400)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
        elif self.path.startswith("/api/control/settings"):
            try:
                result = control_settings(self._read_json_body())
                self._send_json(result, 200 if result.get("ok") else 400)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
        elif self.path.startswith("/api/close"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
                result = close_position(payload)
                self._send_json(result, 200 if result.get("ok") else 400)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
        elif self.path.startswith("/api/acknowledge-halt"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
                result = acknowledge_halt(payload)
                self._send_json(result, 200 if result.get("ok") else 400)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
        else:
            self.send_response(404)
            self.end_headers()


def main():
    p = argparse.ArgumentParser(description="Local dashboard for the trading automation — reads signals, shows live positions/account state, can close positions")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--host", default="127.0.0.1", help="Bind address — leave as 127.0.0.1 unless you specifically want network exposure")
    p.add_argument("--no-open", action="store_true", help="Don't auto-open a browser tab on startup")
    args = p.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"Dashboard running at {url}")
    print(f"Login — user: {DASHBOARD_USER}  password: {DASHBOARD_PASSWORD}")
    print(f"(password cached at {DASHBOARD_PASSWORD_FILE}; set DASHBOARD_PASSWORD env var to override)")
    print("Signal/log data is read-only. Position/account data is LIVE from the broker APIs, and Close buttons place")
    print("real orders once you're in live mode. Leave stock.sh running separately — it's what actually makes")
    print("trade decisions and places orders; this page only monitors it and lets you close positions early.")
    if not args.no_open:
        # webbrowser spawns the OS opener (e.g. WSL's "gio") as a subprocess
        # that writes straight to this process's stderr fd — a Python
        # try/except around webbrowser.open() doesn't catch that, since it's
        # not a Python exception. Redirect fd 2 at the OS level for the
        # duration of the call so a launcher failure (WSL with no browser
        # wired up, most commonly) doesn't dump noise into the server log;
        # it's not fatal either way — the URL is printed above regardless.
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        saved_stderr_fd = os.dup(2)
        try:
            os.dup2(devnull_fd, 2)
            webbrowser.open(url)
        except Exception:
            pass
        finally:
            os.dup2(saved_stderr_fd, 2)
            os.close(devnull_fd)
            os.close(saved_stderr_fd)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
