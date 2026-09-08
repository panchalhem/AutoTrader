#!/usr/bin/env python3
"""
trade_monitor.py — single self-contained intraday trade monitor for both
brokers: Zerodha (NSE cash equities + Nifty/Bank Nifty index) and
Capital.com (global indices, commodities, and crypto CFDs).

No other file is required — indicator math, the instrument universe, the
backtest gate, the ranking, and the live signal loop are all in this one
script (INBOUNDTRADEALGO's rule lives in its own inbound_trade_algo.py —
see STRATEGIES below — but everything else, including running its
backtest and live checks, happens here). It manages its own research cycle
internally at two speeds — an expensive once-a-day gate and a cheap hourly
re-rank — so you just run this file and read the log.

--------------------------------------------------------------------------
STRATEGIES — three, each independently gated, each with its own name
--------------------------------------------------------------------------
  BREAKOUT_INTRADAY  price crosses OUTSIDE its rolling range on volume,
                     5-minute bars, 60-day backtest window. Gate:
                     gate_results in the watchlist JSON.
  BREAKOUT_SWING     the same breakout rule, daily bars, 2-year backtest
                     window, longer hold. Gate: swing_gate_results.
  INBOUNDTRADEALGO   the opposite thesis — mean-reversion INSIDE the same
                     kind of range, buying near its bottom / shorting near
                     its top for a small reversion move, daily bars only
                     (see inbound_trade_algo.py for why intraday was tested
                     and rejected). Gate: inbound_gate_results. Toggled on/
                     off via inbound_trade_algo.is_enabled() — independent
                     of the other two, no code change needed to disable it.
Each strategy is backtested and gated entirely separately — clearing one
says nothing about another; a symbol can pass none, one, two, or all three.

--------------------------------------------------------------------------
WHAT IT DOES, IN ORDER
--------------------------------------------------------------------------
1. BACKTEST GATE (expensive; cached per local calendar day in
   output/watchlist_latest.json under "gate_results"): for every instrument
   in the universe below, backtest the live breakout/breakdown strategy
   over its last 60 days of 5-minute bars. An instrument is DROPPED from
   consideration entirely unless it cleared:
       trade_count  >= --min-trades        (default 8)
       expectancy   >= --min-expectancy    (default 0.0R — must not have
                                             lost money on average)
       profit_factor>= --min-profit-factor (default 1.1)
   Every simulated trade is also charged a round-trip cost_bps (slippage +
   commission estimate) against entry price before these stats are computed —
   see STRATEGY_DEFAULTS — so a thin, fee-blind edge no longer clears. That
   cost model does most of the real filtering here: on 2026-08-24 it alone
   cut a 152-instrument universe's intraday passes from 29 (no cost priced
   in) to 3 — raising min-profit-factor/min-expectancy further on top of
   that returned zero, consistently, so those two stay at the original bar.
   The swing gate (SWING_MIN_PROFIT_FACTOR/SWING_MIN_EXPECTANCY, same file)
   is intentionally held to a stricter 1.3/0.05R — it still clears plenty of
   names there, so there's no data-driven reason to loosen it too.
   Only the first run each local day (or --rebuild-watchlist) redoes this;
   every run after that on the same day reuses the cached gate instantly.

2. HOURLY RANKING REFRESH (cheap; --refresh-hours, default 1.0): every
   instrument that passed the gate gets re-scored using freshly fetched
   daily bars — a weighted percentile rank of volatility (ATR%), momentum
   (|RSI-50|), and liquidity (20d avg volume). This does NOT re-run the
   60-day backtest (that edge doesn't meaningfully change hour to hour) —
   only which gate-passing instrument is most "in play" right now does.
   Top --top per bucket is flagged as today's headline picks, but —
   important — EVERY gate-passing instrument stays on the live-check list
   regardless of rank, specifically so a real signal on a lower-ranked
   instrument is never silently skipped just because it wasn't in the
   morning's top N.

3. LIVE 5-MINUTE LOOP: for whichever bucket(s) are actually open right now
   (NSE cash hours for Zerodha; Asia/Europe/US session windows for
   Capital.com — commodities & crypto are candidates in all three), pull
   fresh 5-minute bars for every gate-passing symbol in that bucket (not
   just the top-N picks) and evaluate the same breakout rule live. Prints
   WAIT / LONG / SHORT with entry, stop, and target for each check
   (picks are marked with `*`); logs every check to CSV if --log.

--------------------------------------------------------------------------
INSTRUMENT UNIVERSE — read this before trusting the picks
--------------------------------------------------------------------------
Zerodha bucket: the Nifty 50 constituents, fetched LIVE every day from
NSE's own public archive (ind_nifty50list.csv — official symbol + company
name, no auth needed, cached ~7 days) — not a hardcoded list, so index
reconstitutions (a stock added/removed from Nifty 50) are picked up
automatically. Each symbol is then cross-checked against Zerodha's own
public instrument master (api.kite.trade/instruments — also no auth
needed) to confirm it's currently tradeable on Zerodha and to catch
ticker/corporate-action mismatches (e.g. Yahoo Finance still using an old
symbol after a demerger); mismatches print a warning rather than failing
silently. Zerodha technically supports the entire NSE/BSE universe
(thousands of symbols), but backtesting thousands of tickers daily is
neither fast nor meaningful — Nifty 50 is the honest "most liquid, most
realistically intraday-tradable" subset. If both live fetches fail (no
network), a small hardcoded fallback list is used so the script still runs.
Zerodha's Kite Connect trading/order API itself needs a paid subscription
and OAuth login and is NOT used here — only its free, public instrument
list.

Capital.com bucket: major global indices (Nikkei, Hang Seng, ASX, KOSPI,
Nifty, DAX, FTSE, CAC, Euro Stoxx, NAS100, SPX500, US30, Russell 2000),
commodities (Gold, Silver, Crude, Copper, Nat Gas), 8 US mega-cap stocks,
and 2 crypto pairs (BTC, ETH) — all tradable as CFDs on Capital.com.
Futures/index tickers (Yahoo Finance) are used as data PROXIES for the CFD
price, not a live feed of Capital.com's own quotes. Capital.com does have a
REST API (open-api.capital.com) that can list its actual tradeable
"epics" — but every market-data endpoint requires an authenticated session
(API key + a password set at key-generation + 2FA on your account; see
Settings > API integrations in the Capital.com app). This script does not
call that API because it would require your personal credentials; ask to
wire it in if you want to generate a (demo-account recommended) API key
and provide it via environment variables — never paste a key in chat.

--------------------------------------------------------------------------
USAGE
--------------------------------------------------------------------------
    # first run of the day: builds the watchlist, then loops every 5 min
    ./.venv/bin/python automation/trade_monitor.py --loop-seconds 300 --log

    # single check, whatever is open right now, then exit
    ./.venv/bin/python automation/trade_monitor.py --once

    # force a fresh backtest+ranking pass instead of using today's cache
    ./.venv/bin/python automation/trade_monitor.py --rebuild-watchlist --once

    # cron this every 5 minutes instead of using --loop-seconds:
    */5 * * * * cd /mnt/g/adhoc/stocktrade && ./.venv/bin/python automation/trade_monitor.py --once --quiet --log

Not investment advice. A backtest pass means the mechanical rule set had a
positive historical edge on that instrument recently — not a guarantee for
today. Re-run the daily research regularly (it auto-refreshes once every
calendar day) and treat every printed signal as a technical read to verify
against your broker's live price and any relevant news, not an instruction.
"""
import argparse
import csv
import io
import json
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inbound_trade_algo
import strategy_config
import trading_settings as ts

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
OUT_DIR = ROOT / "output"
DATA_DIR.mkdir(exist_ok=True)
OUT_DIR.mkdir(exist_ok=True)

WATCHLIST_LATEST = OUT_DIR / "watchlist_latest.json"
SIGNAL_LOG = OUT_DIR / "signal_log.csv"

# All session-window math below stays in UTC (exchanges don't move with your
# DST), but everything printed/logged is converted to this local timezone so
# "what to trade now" reads naturally. Override with --tz if you're not in
# Australia/Sydney; zoneinfo handles the AEST/AEDT switch automatically.
DEFAULT_TZ_NAME = "Australia/Sydney"

DAILY_PERIOD, DAILY_INTERVAL = "3mo", "1d"
BATCH_SIZE = 20

# ---------------------------------------------------------------------------
# Strategy parameters — single source of truth used by BOTH the backtest
# and the live signal evaluation, so they can never drift apart.
# ---------------------------------------------------------------------------
STRATEGY_DEFAULTS = {
    "lookback_bars": 20,
    "rsi_period": 14,
    "atr_period": 14,
    "rsi_long_min": 50.0,
    "rsi_long_max": 75.0,
    "rsi_short_min": 25.0,
    "rsi_short_max": 50.0,
    "stop_atr_mult": 1.5,
    "target_atr_mult": 2.5,
    "max_hold_bars": 48,  # 48 * 5m = 4 hours
    "cost_bps": 5.0,  # round-trip slippage + commission, as bps of entry price
    "breakeven_trigger_r": 1.0,  # once unrealized gain reaches 1R, move the stop
    "breakeven_lock_r": 0.0,     # ...to lock this many R of profit (0.0 = plain breakeven)
}

# ---------------------------------------------------------------------------
# Swing strategy: same breakout/RSI/ATR mechanics as STRATEGY_DEFAULTS, but
# run on DAILY bars instead of 5-minute ones — for a stock showing several
# days of genuine follow-through, worth holding rather than exiting same-day.
# lookback_bars=10 -> breakout vs the prior 10 trading days' range (~2 weeks).
# max_hold_bars=7 -> exits (at target/stop/time) within ~7 trading days if
# neither hits first — "retained for a week", not held indefinitely.
# target_atr_mult is wider than the intraday default (3.5x vs 2.5x) since a
# multi-day hold should expect more room to run before calling it profit.
# ---------------------------------------------------------------------------
SWING_STRATEGY_DEFAULTS = {
    "lookback_bars": 10,
    "rsi_period": 14,
    "atr_period": 14,
    "rsi_long_min": 50.0,
    "rsi_long_max": 75.0,
    "rsi_short_min": 25.0,
    "rsi_short_max": 50.0,
    "stop_atr_mult": 1.5,
    "target_atr_mult": 3.5,
    "max_hold_bars": 7,  # 7 trading days ~= 1 calendar week
    "cost_bps": 5.0,  # round-trip slippage + commission, as bps of entry price
    # Added 2026-08-29: user reported live positions repeatedly showing an
    # unrealized gain then rounding trip into a loss by the time stop/target/
    # time-exit fires. Once a trade is up breakeven_trigger_r (in R, i.e.
    # multiples of its own initial risk), move the stop to lock in at least
    # breakeven_lock_r of that gain — so a winner's worst case becomes
    # "no loss", not "wait it out and hope". See _simulate_from_signals.
    "breakeven_trigger_r": 1.0,
    "breakeven_lock_r": 0.0,
}
SWING_BACKTEST_PERIOD = "2y"   # daily bars have no 60-day cap like intraday does — use real history
SWING_BACKTEST_INTERVAL = "1d"
# SWING_MIN_TRADES/PROFIT_FACTOR/EXPECTANCY/WIN_RATE all moved into
# trading_settings.py (2026-09-03, user request) — dashboard-editable.
# History: PF/expectancy raised from 1.1/0.0R (barely cleared a frictionless
# backtest) to 1.3/0.05R (a margin over cost) on 2026-08-29, the same day
# win-rate was added at 0.80 — which then produced zero gate-passing
# instruments for days, including several (Kajaria Ceramics 65%, Asian
# Paints 59%, GMM Pfaudler 60%, PF 2.2-2.9x, expectancy +0.4R+) that had a
# clearly real edge and were excluded on win rate alone — expected for a
# trend/breakout system, not a flaw in those instruments. Lowered to 0.65 on
# 2026-09-03 for exactly that reason. These are read fresh (module-level,
# at import time) from trading_settings.get(...) below — a change takes
# effect on the next process start; see that module's docstring for the
# full reload-semantics table.
SWING_MIN_TRADES = ts.get("swing_min_trades")
SWING_MIN_PROFIT_FACTOR = ts.get("swing_min_profit_factor")
SWING_MIN_EXPECTANCY = ts.get("swing_min_expectancy")
SWING_MIN_WIN_RATE = ts.get("swing_min_win_rate")

# ---------------------------------------------------------------------------
# Trading-session windows, UTC. NSE (Zerodha) is a hard weekday+clock-time
# gate since it's a single physical exchange; Capital.com's three regional
# windows overlap at the edges by design (e.g. Europe/US 13:00-16:00 UTC) —
# whichever is checked first in CAPITALCOM_PRIORITY wins the overlap.
# ---------------------------------------------------------------------------
NSE_OPEN_UTC = (3, 45)   # 09:15 IST
NSE_CLOSE_UTC = (10, 0)  # 15:30 IST

CAPITALCOM_SESSION_HOURS_UTC = {
    "asia": (23, 8),
    "europe": (7, 16),
    "us": (13, 21),
}
CAPITALCOM_PRIORITY = ["us", "europe", "asia"]


def is_nse_open(now_utc):
    if now_utc.weekday() >= 5:
        return False
    t = now_utc.hour * 60 + now_utc.minute
    return (NSE_OPEN_UTC[0] * 60 + NSE_OPEN_UTC[1]) <= t < (NSE_CLOSE_UTC[0] * 60 + NSE_CLOSE_UTC[1])


def current_capitalcom_session(now_utc_hour):
    for name in CAPITALCOM_PRIORITY:
        start, end = CAPITALCOM_SESSION_HOURS_UTC[name]
        in_window = (start <= now_utc_hour < end) if start < end else (now_utc_hour >= start or now_utc_hour < end)
        if in_window:
            return name
    return "asia"


def _local_clock(now_utc, hour, minute, tz):
    """UTC hour/minute -> 'HH:MM' in tz, anchored to today's UTC date (session
    windows are UTC clock-times, so this is just a display conversion)."""
    dt = now_utc.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return dt.astimezone(tz).strftime("%H:%M %Z")


def print_session_schedule(now_utc, tz):
    local_now = now_utc.astimezone(tz)
    print(f"\nLocal time now: {local_now.strftime('%Y-%m-%d %H:%M:%S %Z')}  "
          f"({'weekday' if now_utc.weekday() < 5 else 'weekend'})")
    print("Today's session windows, converted to local time:")
    nse_days = "Mon-Fri" if now_utc.weekday() < 5 else "next weekday"
    print(f"  Zerodha (NSE)      {_local_clock(now_utc, *NSE_OPEN_UTC, tz)} - "
          f"{_local_clock(now_utc, *NSE_CLOSE_UTC, tz)}  ({nse_days})")
    for name in ["asia", "europe", "us"]:
        start, end = CAPITALCOM_SESSION_HOURS_UTC[name]
        wraps = " (wraps past midnight UTC)" if start > end else ""
        print(f"  Capital.com {name:8s} {_local_clock(now_utc, start, 0, tz)} - "
              f"{_local_clock(now_utc, end, 0, tz)}{wraps}")


# ---------------------------------------------------------------------------
# Instrument universe
# ---------------------------------------------------------------------------
NSE_NIFTY50_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv"
NSE_NIFTY50_CACHE = DATA_DIR / "nifty50_list.csv"
NSE_LIST_MAX_AGE_DAYS = 7  # index reconstitution happens quarterly, not daily
NSE_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept": "text/csv"}

KITE_INSTRUMENTS_URL = "https://api.kite.trade/instruments"
KITE_INSTRUMENTS_CACHE = DATA_DIR / "kite_instruments.csv"
KITE_CACHE_MAX_AGE_HOURS = 24

# Overrides for cases where even the live NSE symbol doesn't match what
# Yahoo Finance serves data under (data-provider-specific quirks only —
# NSE's own symbol is otherwise used as-is). Empty by default now that the
# Nifty 50 list is fetched live rather than hand-maintained.
TICKER_OVERRIDES = {}

# Emergency fallback only — used if BOTH the NSE list and any local cache
# of it are unavailable (e.g. first run with no network). Will drift from
# the real index over time; live fetch is what's actually used normally.
NIFTY50_FALLBACK = {
    "RELIANCE": "Reliance Industries", "TCS": "Tata Consultancy Services",
    "HDFCBANK": "HDFC Bank", "ICICIBANK": "ICICI Bank", "INFY": "Infosys",
    "HINDUNILVR": "Hindustan Unilever", "ITC": "ITC", "SBIN": "State Bank of India",
    "BHARTIARTL": "Bharti Airtel", "KOTAKBANK": "Kotak Mahindra Bank",
    "LT": "Larsen & Toubro", "AXISBANK": "Axis Bank", "BAJFINANCE": "Bajaj Finance",
    "MARUTI": "Maruti Suzuki", "ASIANPAINT": "Asian Paints", "HCLTECH": "HCL Technologies",
    "SUNPHARMA": "Sun Pharmaceutical", "TITAN": "Titan Company",
    "ULTRACEMCO": "UltraTech Cement", "NESTLEIND": "Nestle India", "WIPRO": "Wipro",
    "ONGC": "Oil & Natural Gas Corp", "NTPC": "NTPC", "POWERGRID": "Power Grid Corp",
    "M&M": "Mahindra & Mahindra", "TATASTEEL": "Tata Steel",
    "JSWSTEEL": "JSW Steel", "ADANIENT": "Adani Enterprises", "ADANIPORTS": "Adani Ports & SEZ",
    "COALINDIA": "Coal India", "BAJAJFINSV": "Bajaj Finserv", "HDFCLIFE": "HDFC Life Insurance",
    "SBILIFE": "SBI Life Insurance", "DRREDDY": "Dr. Reddy's Laboratories",
    "GRASIM": "Grasim Industries", "EICHERMOT": "Eicher Motors",
    "BAJAJ-AUTO": "Bajaj Auto", "CIPLA": "Cipla",
    "APOLLOHOSP": "Apollo Hospitals", "TECHM": "Tech Mahindra",
    "HINDALCO": "Hindalco Industries",
    "SHRIRAMFIN": "Shriram Finance", "TATACONSUM": "Tata Consumer Products",
}


def _clean_company_name(name):
    name = str(name).strip()
    for suffix in (" Ltd.", " Ltd", " Limited"):
        if name.endswith(suffix):
            return name[: -len(suffix)].strip()
    return name


def fetch_nifty50_list(refresh=False):
    """Live Nifty 50 constituents from NSE's own public archive — official
    Symbol + Company Name, no auth needed. Returns {symbol: company_name}.
    Falls back to a cached copy, then to NIFTY50_FALLBACK, if the fetch fails."""
    if NSE_NIFTY50_CACHE.exists() and not refresh:
        age_days = (time.time() - NSE_NIFTY50_CACHE.stat().st_mtime) / 86400
        if age_days < NSE_LIST_MAX_AGE_DAYS:
            df = pd.read_csv(NSE_NIFTY50_CACHE)
            return dict(zip(df["Symbol"].str.strip(), df["Company Name"].map(_clean_company_name)))

    try:
        r = requests.get(NSE_NIFTY50_URL, headers=NSE_HEADERS, timeout=15)
        r.raise_for_status()
        NSE_NIFTY50_CACHE.write_bytes(r.content)
        df = pd.read_csv(NSE_NIFTY50_CACHE)
        print(f"[zerodha] fetched live Nifty 50 list ({len(df)} constituents) from NSE")
        return dict(zip(df["Symbol"].str.strip(), df["Company Name"].map(_clean_company_name)))
    except Exception as e:
        if NSE_NIFTY50_CACHE.exists():
            print(f"[zerodha] NSE fetch failed ({e}), using stale cache", file=sys.stderr)
            df = pd.read_csv(NSE_NIFTY50_CACHE)
            return dict(zip(df["Symbol"].str.strip(), df["Company Name"].map(_clean_company_name)))
        print(f"[zerodha] NSE fetch failed ({e}), no cache available — using hardcoded fallback list", file=sys.stderr)
        return dict(NIFTY50_FALLBACK)


def fetch_zerodha_tradable_symbols(refresh=False):
    """Zerodha's own public instrument master (api.kite.trade/instruments,
    no API key needed) — used only to confirm a symbol is actually
    tradeable on Zerodha right now. Returns a set of NSE-EQ tradingsymbols,
    or None if unavailable (validation is then skipped, not treated as failure)."""
    try:
        if KITE_INSTRUMENTS_CACHE.exists() and not refresh:
            age_hours = (time.time() - KITE_INSTRUMENTS_CACHE.stat().st_mtime) / 3600
            if age_hours >= KITE_CACHE_MAX_AGE_HOURS:
                r = requests.get(KITE_INSTRUMENTS_URL, timeout=30)
                r.raise_for_status()
                KITE_INSTRUMENTS_CACHE.write_bytes(r.content)
        else:
            r = requests.get(KITE_INSTRUMENTS_URL, timeout=30)
            r.raise_for_status()
            KITE_INSTRUMENTS_CACHE.write_bytes(r.content)
        df = pd.read_csv(KITE_INSTRUMENTS_CACHE)
        nse_eq = df[(df["segment"] == "NSE") & (df["instrument_type"] == "EQ")]
        return set(nse_eq["tradingsymbol"])
    except Exception as e:
        print(f"[zerodha] Kite instrument list unavailable ({e}) — skipping tradability validation", file=sys.stderr)
        return None


def build_universe():
    u = {}
    nifty50 = fetch_nifty50_list()
    zerodha_tradable = fetch_zerodha_tradable_symbols()
    for sym, company_name in nifty50.items():
        ysym = TICKER_OVERRIDES.get(sym, f"{sym}.NS")
        bare = ysym[:-3] if ysym.endswith(".NS") else ysym
        if zerodha_tradable is not None and bare not in zerodha_tradable:
            print(f"[zerodha] WARNING: {sym} ({ysym}) not found in Zerodha's live NSE-EQ instrument list "
                  f"— possible ticker/corporate-action mismatch, double-check before trading it", file=sys.stderr)
        u[ysym] = {"name": company_name, "class": "stock", "broker": "zerodha", "buckets": ["zerodha"]}
    u["^NSEI"] = {"name": "Nifty 50 Index", "class": "index", "broker": "zerodha", "buckets": ["zerodha"]}
    u["^NSEBANK"] = {"name": "Bank Nifty Index", "class": "index", "broker": "zerodha", "buckets": ["zerodha"]}

    # Added 2026-08-24 from a one-off full-NSE-universe scan (2,320 Zerodha-
    # tradable symbols, large+mid+small+micro cap, screened for a live
    # breakout/breakdown signal then backtested on the same 2y-daily swing
    # gate as everything else here) — these 8 passed with a positive
    # historical edge and are liquid enough (Large/Mid/liquid-Small cap) to
    # trust, unlike the dozen sub-Rs50 micro-caps the scan also surfaced
    # that were dropped as likely data/slippage artifacts. Not a permanent
    # index membership — a manual, dated addition; revisit periodically.
    extra_symbols = {
        "DIXON": "Dixon Technologies (India)", "AUBANK": "AU Small Finance Bank",
        "THOMASCOOK": "Thomas Cook (India)", "BSE": "BSE Ltd",
        "CUMMINSIND": "Cummins India", "KEC": "KEC International",
        "GMDCLTD": "Gujarat Mineral Development Corporation",
        "CROMPTON": "Crompton Greaves Consumer Electricals",
        # Added 2026-09-03 (user request, wider analysis scope) — well-known
        # liquid large/mid-cap NSE names beyond Nifty50, not from a fresh scan
        # this time (weekend_discovery.py's own 80% swing-gate re-scan just
        # found 0/800 candidates worth adding) — hand-picked for breadth only,
        # still subject to the exact same backtest gate as everything else.
        "ETERNAL": "Eternal (formerly Zomato)", "PAYTM": "One97 Communications (Paytm)",
        "NYKAA": "FSN E-Commerce Ventures (Nykaa)", "IRCTC": "Indian Railway Catering & Tourism",
        "PERSISTENT": "Persistent Systems", "LTIM": "LTIMindtree", "MPHASIS": "Mphasis",
        "COFORGE": "Coforge", "POLYCAB": "Polycab India", "SUPREMEIND": "Supreme Industries",
        "PIIND": "PI Industries", "DEEPAKNTR": "Deepak Nitrite", "ASTRAL": "Astral Ltd",
        "TATAELXSI": "Tata Elxsi", "IEX": "Indian Energy Exchange", "CDSL": "Central Depository Services",
        "ANGELONE": "Angel One", "MCX": "Multi Commodity Exchange of India",
        "IRFC": "Indian Railway Finance Corporation", "RVNL": "Rail Vikas Nigam",
    }
    for sym, company_name in extra_symbols.items():
        ysym = TICKER_OVERRIDES.get(sym, f"{sym}.NS")
        bare = ysym[:-3] if ysym.endswith(".NS") else ysym
        if zerodha_tradable is not None and bare not in zerodha_tradable:
            print(f"[zerodha] WARNING: {sym} ({ysym}) not found in Zerodha's live NSE-EQ instrument list "
                  f"— possible ticker/corporate-action mismatch, double-check before trading it", file=sys.stderr)
        u[ysym] = {"name": company_name, "class": "stock", "broker": "zerodha", "buckets": ["zerodha"]}

    asia_idx = {"^N225": "Nikkei 225", "^HSI": "Hang Seng", "^AXJO": "ASX 200", "^KS11": "KOSPI"}
    europe_idx = {"^GDAXI": "DAX 40", "^FTSE": "FTSE 100", "^FCHI": "CAC 40", "^STOXX50E": "Euro Stoxx 50"}
    us_idx = {"NQ=F": "NAS100", "ES=F": "SPX500", "YM=F": "US30", "RTY=F": "Russell 2000"}
    # Capital.com's own "most traded" US shares list (pulled live via their
    # API on 2026-08-21), not a hand-picked guess — this is what Capital.com
    # itself surfaces as the most liquid US equity CFDs.
    us_stocks = {
        "MU": "Micron Technology", "TSLA": "Tesla", "MRNA": "Moderna", "NVDA": "Nvidia",
        "SOXL": "Direxion Daily Semiconductor Bull 3X", "NBIS": "Nebius Group",
        "MRVL": "Marvell Technology", "AMD": "AMD", "MSTR": "Strategy", "META": "Meta",
        "AAPL": "Apple", "INTC": "Intel", "BYND": "Beyond Meat", "AMZN": "Amazon",
        "PLTR": "Palantir Technologies", "COIN": "Coinbase Global", "AVGO": "Broadcom",
        "SMCI": "Super Micro Computer", "MSFT": "Microsoft", "ORCL": "Oracle",
        "QQQ": "Invesco QQQ Trust", "NFLX": "Netflix", "GOOGL": "Alphabet (A)", "CSCO": "Cisco",
        "NKE": "Nike", "NOW": "ServiceNow", "IREN": "IREN", "RKLB": "Rocket Lab USA",
        "DELL": "Dell Technologies", "GME": "GameStop", "RDDT": "Reddit", "IONQ": "IonQ",
        "ASML": "ASML Holding", "MARA": "Marathon Digital Holdings", "HOOD": "Robinhood Markets",
        "SPY": "SPDR S&P 500 ETF", "WDC": "Western Digital", "AMAT": "Applied Materials",
        "GOOG": "Alphabet (C)", "BABA": "Alibaba Group", "VOO": "Vanguard S&P 500 ETF",
        "AMC": "AMC Entertainment", "ADBE": "Adobe Systems", "COHR": "Coherent Corp",
        "IBM": "IBM", "MELI": "MercadoLibre", "TSM": "Taiwan Semiconductor Manufacturing",
        "MP": "MP Materials", "WMT": "Walmart", "APP": "Applovin",
        # Added 2026-09-03 (user request, alongside the loosened 65% intraday
        # gate) — broader liquid US large-cap/ETF coverage beyond Capital.com's
        # original "most traded" snapshot, for wider analysis scope.
        "JPM": "JPMorgan Chase", "V": "Visa", "MA": "Mastercard", "UNH": "UnitedHealth Group",
        "XOM": "Exxon Mobil", "CVX": "Chevron", "PG": "Procter & Gamble", "KO": "Coca-Cola",
        "PEP": "PepsiCo", "COST": "Costco", "HD": "Home Depot", "LLY": "Eli Lilly",
        "ABBV": "AbbVie", "MCD": "McDonald's", "CRM": "Salesforce", "ACN": "Accenture",
        "LIN": "Linde", "DIS": "Walt Disney", "PYPL": "PayPal", "UBER": "Uber Technologies",
        "SHOP": "Shopify", "SQ": "Block", "SNOW": "Snowflake", "PANW": "Palo Alto Networks",
        "CRWD": "CrowdStrike", "ARM": "Arm Holdings", "XLF": "Financial Select Sector SPDR",
        "XLE": "Energy Select Sector SPDR", "IWM": "iShares Russell 2000 ETF",
        "DIA": "SPDR Dow Jones Industrial Average ETF", "GLD": "SPDR Gold Shares",
        "SLV": "iShares Silver Trust", "TLT": "iShares 20+ Year Treasury Bond ETF",
        "UVXY": "ProShares Ultra VIX Short-Term Futures", "TQQQ": "ProShares UltraPro QQQ",
        "SQQQ": "ProShares UltraPro Short QQQ",
    }
    # Major forex pairs, per Capital.com's own "most traded" ranking — trade
    # ~24h on weekdays, so (like commodities/crypto) a candidate in every session.
    # Disabled below (2026-08-24 audit): 0 of 15 cleared either backtest gate
    # on every run observed — pure daily fetch+backtest cost for zero output.
    # Kept here, not deleted, so it's a one-line re-enable if that changes;
    # revisit as part of the weekly universe-curation pass (weekly_research.py).
    forex = {
        "EURUSD=X": "EUR/USD", "USDJPY=X": "USD/JPY", "GBPUSD=X": "GBP/USD", "USDCHF=X": "USD/CHF",
        "AUDUSD=X": "AUD/USD", "NZDUSD=X": "NZD/USD", "EURJPY=X": "EUR/JPY", "USDMXN=X": "USD/MXN",
        "GBPJPY=X": "GBP/JPY", "USDCAD=X": "USD/CAD", "AUDJPY=X": "AUD/JPY", "EURCHF=X": "EUR/CHF",
        "EURGBP=X": "EUR/GBP", "EURAUD=X": "EUR/AUD", "AUDNZD=X": "AUD/NZD",
    }
    commodities = {
        "GC=F": "Gold", "SI=F": "Silver", "CL=F": "Crude Oil WTI", "HG=F": "Copper", "NG=F": "Natural Gas",
        "ZN=F": "Zinc", "PL=F": "Platinum", "PA=F": "Palladium", "BZ=F": "Brent Crude",
        "HO=F": "Heating Oil", "RB=F": "RBOB Gasoline", "ZW=F": "Wheat", "ZC=F": "Corn",
        "ZS=F": "Soybeans", "KC=F": "Coffee", "SB=F": "Sugar", "CC=F": "Cocoa", "ALI=F": "Aluminum",
        # CT=F (Cotton) deliberately excluded — confirmed too thin on Yahoo (~196 intraday
        # bars vs thousands for the others), would just fail the backtest gate on data volume.
    }
    # Major crypto pairs (Capital.com offers ~250; this is a liquid-majors
    # subset with confirmed Yahoo Finance data, not an attempt at all of them).
    crypto = {
        "BTC-USD": "Bitcoin", "ETH-USD": "Ethereum", "XRP-USD": "Ripple", "SOL-USD": "Solana",
        "DOGE-USD": "Dogecoin", "ADA-USD": "Cardano", "LINK-USD": "Chainlink", "DOT-USD": "Polkadot",
        "AVAX-USD": "Avalanche", "LTC-USD": "Litecoin", "BCH-USD": "Bitcoin Cash", "TRX-USD": "Tron",
    }

    for sym, name in asia_idx.items():
        u[sym] = {"name": name, "class": "index", "broker": "capitalcom", "buckets": ["asia"]}
    for sym, name in europe_idx.items():
        u[sym] = {"name": name, "class": "index", "broker": "capitalcom", "buckets": ["europe"]}
    for sym, name in us_idx.items():
        u[sym] = {"name": name, "class": "index", "broker": "capitalcom", "buckets": ["us"]}
    for sym, name in us_stocks.items():
        u[sym] = {"name": name, "class": "stock", "broker": "capitalcom", "buckets": ["us"]}
    # Each category below is gated on a trading_settings.py toggle
    # (2026-09-03, user request) — dashboard-editable, but only takes effect
    # for the long-running traders on their next RESTART, since INSTRUMENTS
    # (below) is built once at module import, not re-read every cycle. A
    # standalone rebuild (the Control Panel's "Rebuild watchlist now"
    # button) is a fresh process, so it reflects a toggle change immediately.
    if ts.get("universe_commodities_enabled"):
        for sym, name in commodities.items():
            u[sym] = {"name": name, "class": "commodity", "broker": "capitalcom", "buckets": ["asia", "europe", "us"]}
    # Forex re-enabled 2026-09-03 (user request, alongside a loosened 65%
    # intraday win-rate gate) — previously excluded because 0/15 cleared the
    # 80% gate; worth re-testing now that the bar is lower.
    if ts.get("universe_forex_enabled"):
        for sym, name in forex.items():
            u[sym] = {"name": name, "class": "forex", "broker": "capitalcom", "buckets": ["asia", "europe", "us"]}
    if ts.get("universe_crypto_enabled"):
        for sym, name in crypto.items():
            u[sym] = {"name": name, "class": "crypto", "broker": "capitalcom", "buckets": ["asia", "europe", "us"]}

    # Discovered universe (weekend_discovery.py) — symbols outside the hand-
    # curated lists above that a weekend scan found clearing the swing gate.
    # setdefault, not assignment: a hand-curated entry above always wins if a
    # symbol is somehow in both. This is what makes discovery additive to the
    # daily gate automatically — no manual edit needed here after a weekend run.
    for sym, meta in load_discovered_universe().items():
        u.setdefault(sym, meta)

    # Wide US universe (S&P 400+500+600, see load_wide_us_universe) — added
    # 2026-09-03, user request for wider analysis scope. setdefault: a
    # curated/discovered entry above always wins over this bulk addition.
    if ts.get("universe_wide_us_enabled"):
        for sym, meta in load_wide_us_universe().items():
            u.setdefault(sym, meta)
    return u


# Symbols weekend_discovery.py has found clearing the swing gate outside the
# hand-curated lists above. Kept in its own small file (not one more entry in
# the hardcoded dicts above) because it's written by a script, not a person —
# see weekend_discovery.py's module docstring for how it's populated.
DISCOVERED_UNIVERSE_FILE = DATA_DIR / "discovered_universe.json"


US_MARKET_DATA = ROOT.parent / "us_market" / "data"


def load_wide_us_universe():
    """S&P 400 (mid) + 500 + 600 (small) constituents — added 2026-09-03 (user
    request for wider analysis scope, "at least 1000 instruments"). Same
    static CSVs weekend_discovery.py's us_candidate_pool() reads, but merged
    straight into the live universe here rather than requiring a discovery
    scan to pre-clear the swing gate first — these still go through the
    exact same backtest gate as everything else once merged, this just adds
    them as candidates instead of excluding an entire index's worth of
    stocks from ever being tested. NSE was deliberately NOT expanded the
    same way (Zerodha's full tradable list runs into the thousands, mixing
    in many thin/illiquid names — weekend_discovery.py's own docstring
    already flags the Yahoo/NSE rate-limit risk of a full sweep); US S&P
    index membership is a much safer bulk source since it's pre-filtered to
    real, liquid, exchange-listed companies."""
    out = {}
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
                out.setdefault(sym, {"name": sym, "class": "stock", "broker": "capitalcom", "buckets": ["us"]})
    return out


def load_discovered_universe():
    if not DISCOVERED_UNIVERSE_FILE.exists():
        return {}
    try:
        raw = json.loads(DISCOVERED_UNIVERSE_FILE.read_text())
    except Exception:
        return {}
    out = {}
    for sym, v in raw.items():
        try:
            out[sym] = {"name": v["name"], "class": v["class"], "broker": v["broker"], "buckets": v["buckets"]}
        except (KeyError, TypeError):
            continue  # malformed entry — skip rather than crash universe construction
    return out


INSTRUMENTS = build_universe()
ALL_BUCKETS = ["zerodha", "asia", "europe", "us"]


def candidates_for_bucket(bucket):
    return [s for s, m in INSTRUMENTS.items() if bucket in m["buckets"]]


# ---------------------------------------------------------------------------
# Indicator math
# ---------------------------------------------------------------------------
def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def atr(df, period=14):
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period).mean()


def fetch_history(symbol, interval, period):
    df = yf.download(symbol, period=period, interval=interval, auto_adjust=True, progress=False)
    if df.empty:
        return None
    df.columns = df.columns.get_level_values(0)
    return df.dropna()


def fetch_batch(symbols, interval, period, batch_size=BATCH_SIZE):
    out = {}
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i : i + batch_size]
        try:
            data = yf.download(
                batch, period=period, interval=interval, group_by="ticker",
                threads=True, progress=False, auto_adjust=True,
            )
        except Exception as e:
            print(f"  batch fetch failed ({interval}/{period}) {batch}: {e}", file=sys.stderr)
            continue
        for sym in batch:
            try:
                # data[sym] correctly selects that ticker's Field-named
                # columns (Open/High/Low/Close/Volume) regardless of batch
                # size — a former len(batch)==1 special case skipped this
                # selection and left columns as (Ticker, Ticker, ...), which
                # silently broke every single-instrument batch (see git log).
                df = data[sym]
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                df = df.dropna()
                if not df.empty:
                    out[sym] = df
            except Exception:
                continue
    return out


def _simulate_from_signals(df, params, long_sig, short_sig):
    """Shared trade-lifecycle simulator: given precomputed entry signals
    (the only thing that differs between strategies — see simulate_trades
    for BREAKOUT_INTRADAY/BREAKOUT_SWING's entry rule and
    inbound_trade_algo.signals() for INBOUNDTRADEALGO's), walks bar-by-bar
    applying the SAME stop/target/max-hold/cost-model exit mechanics every
    strategy in this file uses. Two strategies with two different entry
    rules and one execution engine, not two copies of the execution engine."""
    p = params
    close, high, low = df["Close"], df["High"], df["Low"]
    atr_series = atr(df, p["atr_period"])
    c, h, l, a = close.values, high.values, low.values, atr_series.values
    long_arr, short_arr = long_sig.fillna(False).values, short_sig.fillna(False).values
    times = df.index
    n = len(df)

    trades = []
    i = p["lookback_bars"]
    while i < n:
        direction = "LONG" if long_arr[i] else ("SHORT" if short_arr[i] else None)
        if direction is None or np.isnan(a[i]):
            i += 1
            continue
        entry, atr_val = float(c[i]), float(a[i])
        if direction == "LONG":
            stop, target = entry - p["stop_atr_mult"] * atr_val, entry + p["target_atr_mult"] * atr_val
        else:
            stop, target = entry + p["stop_atr_mult"] * atr_val, entry - p["target_atr_mult"] * atr_val
        risk = abs(entry - stop)
        trigger_r = p.get("breakeven_trigger_r", 0.0)
        lock_r = p.get("breakeven_lock_r", 0.0)
        breakeven_price = (entry + lock_r * risk) if direction == "LONG" else (entry - lock_r * risk)
        trigger_price = (entry + trigger_r * risk) if direction == "LONG" else (entry - trigger_r * risk)
        breakeven_armed = trigger_r > 0 and risk > 0
        breakeven_moved = False

        j_end = min(i + p["max_hold_bars"], n - 1)
        exit_price, outcome, exit_i = None, None, None
        for j in range(i + 1, j_end + 1):
            if direction == "LONG":
                hit_stop, hit_target = l[j] <= stop, h[j] >= target
            else:
                hit_stop, hit_target = h[j] >= stop, l[j] <= target
            if hit_stop:
                exit_price, outcome, exit_i = stop, ("breakeven_stop" if breakeven_moved else "stop"), j
                break
            if hit_target:
                exit_price, outcome, exit_i = target, "target", j
                break
            # Stop/target for THIS bar are already resolved above (using the
            # stop level as it stood at the start of the bar) before we look
            # at whether this bar's own move newly qualifies for a breakeven
            # lock — avoids same-bar look-ahead (can't lock in and get stopped
            # at the new level within the same bar it was triggered).
            if breakeven_armed and not breakeven_moved:
                reached_trigger = h[j] >= trigger_price if direction == "LONG" else l[j] <= trigger_price
                if reached_trigger:
                    stop, breakeven_moved = breakeven_price, True
        if exit_price is None:
            exit_i, exit_price, outcome = j_end, float(c[j_end]), "time"

        # risk was fixed above at the ORIGINAL (pre-breakeven-move) stop
        # distance — r_multiple must stay denominated in the risk actually
        # taken at entry, not shrink just because the stop was later moved
        # to lock in profit.
        raw_pl = (exit_price - entry) if direction == "LONG" else (entry - exit_price)
        # Round-trip cost (slippage + commission), modeled as a flat bps hit
        # on entry price — the backtest was previously frictionless, which
        # let purely-fee-thin edges (PF just over 1.0) clear the gate. Real
        # fills always cost something; charge every simulated trade for it.
        raw_pl -= entry * (p.get("cost_bps", 0.0) / 10000.0)
        trades.append({
            "direction": direction, "entry_time": str(times[i]), "exit_time": str(times[exit_i]),
            "entry": round(entry, 4), "stop": round(stop, 4), "target": round(target, 4),
            "exit_price": round(exit_price, 4), "outcome": outcome,
            "r_multiple": round(raw_pl / risk, 3) if risk > 0 else 0.0,
        })
        i = exit_i + 1
    return trades


def simulate_trades(df, params):
    """BREAKOUT_INTRADAY / BREAKOUT_SWING entry rule: price crosses OUTSIDE
    its own rolling range on above-average volume, RSI confirming momentum
    (not exhaustion) in the breakout's direction."""
    p = params
    lb = p["lookback_bars"]
    close, high, low, vol = df["Close"], df["High"], df["Low"], df["Volume"]
    rsi_series = rsi(close, p["rsi_period"])
    prior_high = high.rolling(lb).max().shift(1)
    prior_low = low.rolling(lb).min().shift(1)
    vol_avg = vol.rolling(lb).mean().shift(1)
    long_sig = (close > prior_high) & (vol > vol_avg) & rsi_series.between(p["rsi_long_min"], p["rsi_long_max"])
    short_sig = (close < prior_low) & (vol > vol_avg) & rsi_series.between(p["rsi_short_min"], p["rsi_short_max"])
    return _simulate_from_signals(df, params, long_sig, short_sig)


def simulate_inbound_trades(df, params):
    """INBOUNDTRADEALGO entry rule: the mirror image of simulate_trades —
    price pulls back to near the INSIDE edge of its own rolling range
    (still inside it, not a breakout) with RSI in a moderate, non-extreme
    band. See inbound_trade_algo.py for the feasibility check behind this
    rule and why it's swing-only (daily bars), never intraday."""
    close, high, low = df["Close"], df["High"], df["Low"]
    rsi_series = rsi(close, params["rsi_period"])
    long_sig, short_sig, _, _ = inbound_trade_algo.signals(close, high, low, rsi_series, params)
    return _simulate_from_signals(df, params, long_sig, short_sig)


def summarize_trades(trades):
    n = len(trades)
    if n == 0:
        return {"trade_count": 0, "win_rate": None, "profit_factor": None, "expectancy_r": None}
    rs = [t["r_multiple"] for t in trades]
    wins, losses = [r for r in rs if r > 0], [r for r in rs if r <= 0]
    gross_win, gross_loss = sum(wins), abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (999.99 if gross_win > 0 else None)
    return {
        "trade_count": n, "win_rate": round(len(wins) / n, 3),
        "profit_factor": round(pf, 3) if pf is not None else None,
        "expectancy_r": round(sum(rs) / n, 3),
    }


# ---------------------------------------------------------------------------
# Daily research: backtest gate + ranking -> watchlist
# ---------------------------------------------------------------------------
def compute_daily_metrics(symbol, df, rsi_period, atr_period):
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
        "symbol": symbol, "last_close": round(last_close, 4), "atr_pct": round(atr_pct, 3),
        "rsi14": round(last_rsi, 1), "vol_avg20": round(vol_avg20, 0),
        "ret_5d_pct": round(ret_5d, 2) if ret_5d is not None and pd.notna(ret_5d) else None,
        "trend_up": bool(last_close > sma20), "last_date": str(df.index[-1].date()),
    }


def run_backtest(symbol, df, strategy_params, min_trades, min_pf, min_exp, simulate_fn=simulate_trades,
                  min_win_rate=0.0):
    min_bars = strategy_params["lookback_bars"] + strategy_params["max_hold_bars"] + max(
        strategy_params["rsi_period"], strategy_params["atr_period"]) + 5
    if df is None or len(df) < min_bars:
        return {"bt_trade_count": 0, "bt_win_rate": None, "bt_profit_factor": None,
                "bt_expectancy_r": None, "bt_pass": False, "bt_reason": "insufficient intraday history"}
    stats = summarize_trades(simulate_fn(df, strategy_params))
    passed, reason = True, None
    if stats["trade_count"] < min_trades:
        passed, reason = False, f"only {stats['trade_count']} historical trades (<{min_trades} required)"
    elif stats["expectancy_r"] is None or stats["expectancy_r"] < min_exp:
        passed, reason = False, f"expectancy {stats['expectancy_r']}R below required {min_exp}R"
    elif stats["profit_factor"] is None or stats["profit_factor"] < min_pf:
        passed, reason = False, f"profit factor {stats['profit_factor']} below required {min_pf}"
    elif stats["win_rate"] is None or stats["win_rate"] < min_win_rate:
        passed, reason = False, f"win rate {stats['win_rate']} below required {min_win_rate}"
    return {"bt_trade_count": stats["trade_count"], "bt_win_rate": stats["win_rate"],
            "bt_profit_factor": stats["profit_factor"], "bt_expectancy_r": stats["expectancy_r"],
            "bt_pass": passed, "bt_reason": reason}


def score_and_rank_bucket(bucket, gate_results, daily_metrics, top_n, weights):
    """Thin wrapper: ranks a session bucket's candidates. See score_and_rank_symbols."""
    return score_and_rank_symbols(candidates_for_bucket(bucket), gate_results, daily_metrics, top_n, weights)


def score_and_rank_symbols(syms, gate_results, daily_metrics, top_n, weights):
    """
    Ranks only the symbols (from the given list) that already cleared the
    backtest gate. Returns (top_n_picks, all_passing_ranked) — callers that
    only want the headline picks use the first; callers that must not skip
    a live signal use the second, since a gate-passing instrument outside
    the top N can still fire a real LONG/SHORT.
    """
    rows = []
    for s in syms:
        g = gate_results.get(s)
        m = daily_metrics.get(s)
        if g and g.get("bt_pass") and m:
            rows.append({**m, **g})
    if not rows:
        return [], []

    df = pd.DataFrame(rows)
    df["momentum_signal"] = (df["rsi14"] - 50).abs()
    df["vol_rank"] = df["atr_pct"].rank(pct=True)
    df["momentum_rank"] = df["momentum_signal"].rank(pct=True)
    df["liquidity_rank"] = df["vol_avg20"].rank(pct=True)
    df["score"] = weights["vol"] * df["vol_rank"] + weights["momentum"] * df["momentum_rank"] + weights["liquidity"] * df["liquidity_rank"]
    df["bias"] = df["rsi14"].apply(lambda r: "bullish" if r >= 55 else ("bearish" if r <= 45 else "neutral"))
    df = df.sort_values("score", ascending=False)

    all_ranked = []
    for r in df.itertuples():
        meta = INSTRUMENTS[r.symbol]
        all_ranked.append({
            "symbol": r.symbol, "name": meta["name"], "class": meta["class"], "broker": meta["broker"],
            "score": round(r.score, 4), "bias": r.bias, "last_close": r.last_close, "atr_pct": r.atr_pct,
            "rsi14": r.rsi14, "ret_5d_pct": r.ret_5d_pct, "vol_avg20": r.vol_avg20, "trend_up": r.trend_up,
            "backtest": {"trade_count": r.bt_trade_count, "win_rate": r.bt_win_rate,
                         "profit_factor": r.bt_profit_factor, "expectancy_r": r.bt_expectancy_r},
        })
    return all_ranked[:top_n], all_ranked


def strategy_params_from_args(args):
    return {**STRATEGY_DEFAULTS, "rsi_period": args.rsi_period, "atr_period": args.atr_period,
            "lookback_bars": args.lookback_bars, "stop_atr_mult": args.stop_atr_mult,
            "target_atr_mult": args.target_atr_mult, "max_hold_bars": args.max_hold_bars,
            "rsi_long_min": args.rsi_long_min, "rsi_long_max": args.rsi_long_max,
            "rsi_short_min": args.rsi_short_min, "rsi_short_max": args.rsi_short_max}


def build_watchlist(args, tz, force=False):
    """
    STAGE 1 (expensive, once per local calendar day): backtests every
    instrument and records pass/fail in gate_results. Cached — a same-day
    re-run skips straight to refresh_ranking() instead of redoing this.
    """
    today = datetime.now(tz).strftime("%Y-%m-%d")  # local calendar day, not UTC
    if WATCHLIST_LATEST.exists() and not force:
        cached = json.loads(WATCHLIST_LATEST.read_text())
        if cached.get("as_of_date") == today:
            print(f"Using cached backtest gate from {cached.get('generated_at_local', cached['generated_at_utc'])} "
                  f"(same local day, not rebuilding).")
            return cached

    strategy_params = strategy_params_from_args(args)
    all_symbols = sorted(INSTRUMENTS.keys())
    weights = {"vol": args.w_vol, "momentum": args.w_momentum, "liquidity": args.w_liquidity}

    print(f"[research] fetching daily bars for {len(all_symbols)} instruments...")
    daily_data = fetch_batch(all_symbols, DAILY_INTERVAL, DAILY_PERIOD)
    daily_metrics = {s: compute_daily_metrics(s, daily_data.get(s), args.rsi_period, args.atr_period) for s in all_symbols}

    breakout_intraday_enabled = strategy_config.is_enabled("BREAKOUT_INTRADAY")
    gate_results = {}
    if breakout_intraday_enabled:
        print(f"[research] backtesting {len(all_symbols)} instruments on {args.backtest_interval} bars "
              f"over {args.backtest_period} (min {args.min_trades} trades, PF>={args.min_profit_factor}, "
              f"expectancy>={args.min_expectancy}R, win_rate>={args.min_win_rate})...")
        intraday_data = fetch_batch(all_symbols, args.backtest_interval, args.backtest_period)
        for sym in all_symbols:
            if daily_metrics.get(sym) is None:
                gate_results[sym] = {"bt_trade_count": 0, "bt_win_rate": None, "bt_profit_factor": None,
                                      "bt_expectancy_r": None, "bt_pass": False, "bt_reason": "no daily data"}
                continue
            try:
                bt = run_backtest(sym, intraday_data.get(sym), strategy_params, args.min_trades,
                                   args.min_profit_factor, args.min_expectancy, min_win_rate=args.min_win_rate)
            except Exception as e:
                bt = {"bt_trade_count": 0, "bt_win_rate": None, "bt_profit_factor": None,
                      "bt_expectancy_r": None, "bt_pass": False, "bt_reason": f"error: {e}"}
            gate_results[sym] = bt
            status = "PASS" if bt["bt_pass"] else f"FAIL ({bt['bt_reason']})"
            print(f"  {sym:12s} {INSTRUMENTS[sym]['broker']:10s} trades={bt['bt_trade_count']:<4} "
                  f"win%={bt['bt_win_rate']} PF={bt['bt_profit_factor']} exp={bt['bt_expectancy_r']}R -> {status}")
    else:
        print(f"\n[research] BREAKOUT_INTRADAY is disabled (automation/data/strategy_config.json) — skipping its gate entirely.")
        gate_results = {sym: {"bt_trade_count": 0, "bt_win_rate": None, "bt_profit_factor": None,
                               "bt_expectancy_r": None, "bt_pass": False, "bt_reason": "BREAKOUT_INTRADAY strategy disabled"}
                         for sym in all_symbols}

    # swing_data is fetched unconditionally — INBOUNDTRADEALGO's gate below
    # reuses it even if BREAKOUT_SWING itself is disabled, so this fetch
    # can't be skipped just because that one strategy is off.
    breakout_swing_enabled = strategy_config.is_enabled("BREAKOUT_SWING")
    print(f"\n[research] fetching {len(all_symbols)} instruments' {SWING_BACKTEST_INTERVAL}/{SWING_BACKTEST_PERIOD} "
          f"bars (used by BREAKOUT_SWING and/or {inbound_trade_algo.STRATEGY_NAME})...")
    swing_data = fetch_batch(all_symbols, SWING_BACKTEST_INTERVAL, SWING_BACKTEST_PERIOD)
    swing_gate_results = {}
    if breakout_swing_enabled:
        print(f"[research] BREAKOUT_SWING gate: backtesting {len(all_symbols)} instruments "
              f"(hold up to {SWING_STRATEGY_DEFAULTS['max_hold_bars']} trading days)...")
        for sym in all_symbols:
            try:
                bt = run_backtest(sym, swing_data.get(sym), SWING_STRATEGY_DEFAULTS,
                                   SWING_MIN_TRADES, SWING_MIN_PROFIT_FACTOR, SWING_MIN_EXPECTANCY,
                                   min_win_rate=SWING_MIN_WIN_RATE)
            except Exception as e:
                bt = {"bt_trade_count": 0, "bt_win_rate": None, "bt_profit_factor": None,
                      "bt_expectancy_r": None, "bt_pass": False, "bt_reason": f"error: {e}"}
            swing_gate_results[sym] = bt
            if bt["bt_pass"]:
                print(f"  {sym:12s} {INSTRUMENTS[sym]['broker']:10s} trades={bt['bt_trade_count']:<4} "
                      f"win%={bt['bt_win_rate']} PF={bt['bt_profit_factor']} exp={bt['bt_expectancy_r']}R -> PASS")
    else:
        print(f"[research] BREAKOUT_SWING is disabled (automation/data/strategy_config.json) — skipping its gate entirely.")
        swing_gate_results = {sym: {"bt_trade_count": 0, "bt_win_rate": None, "bt_profit_factor": None,
                                     "bt_expectancy_r": None, "bt_pass": False, "bt_reason": "BREAKOUT_SWING strategy disabled"}
                               for sym in all_symbols}

    inbound_gate_results = {}
    inbound_enabled = inbound_trade_algo.is_enabled()
    if inbound_enabled:
        print(f"\n[research] {inbound_trade_algo.STRATEGY_NAME} gate: backtesting {len(all_symbols)} instruments "
              f"on the same {SWING_BACKTEST_INTERVAL}/{SWING_BACKTEST_PERIOD} data as the swing gate above "
              f"(mean-reversion into the range, not breakout)...")
        for sym in all_symbols:
            try:
                bt = run_backtest(sym, swing_data.get(sym), inbound_trade_algo.DEFAULTS,
                                   inbound_trade_algo.MIN_TRADES, inbound_trade_algo.MIN_PROFIT_FACTOR,
                                   inbound_trade_algo.MIN_EXPECTANCY, simulate_fn=simulate_inbound_trades,
                                   min_win_rate=inbound_trade_algo.MIN_WIN_RATE)
            except Exception as e:
                bt = {"bt_trade_count": 0, "bt_win_rate": None, "bt_profit_factor": None,
                      "bt_expectancy_r": None, "bt_pass": False, "bt_reason": f"error: {e}"}
            inbound_gate_results[sym] = bt
            if bt["bt_pass"]:
                print(f"  {sym:12s} {INSTRUMENTS[sym]['broker']:10s} trades={bt['bt_trade_count']:<4} "
                      f"win%={bt['bt_win_rate']} PF={bt['bt_profit_factor']} exp={bt['bt_expectancy_r']}R -> PASS")
    else:
        print(f"\n[research] {inbound_trade_algo.STRATEGY_NAME} is disabled (automation/data/strategy_config.json) "
              f"— skipping its gate entirely.")

    generated_at = datetime.now(timezone.utc)
    watchlist = {
        "generated_at_utc": generated_at.isoformat(timespec="seconds"),
        "generated_at_local": generated_at.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "as_of_date": today,
        "params": {"top": args.top, "weights": weights, "strategy": strategy_params,
                    "backtest_gate": {"period": args.backtest_period, "interval": args.backtest_interval,
                                       "min_trades": args.min_trades, "min_profit_factor": args.min_profit_factor,
                                       "min_expectancy": args.min_expectancy, "min_win_rate": args.min_win_rate},
                    "swing_backtest_gate": {"period": SWING_BACKTEST_PERIOD, "interval": SWING_BACKTEST_INTERVAL,
                                             "min_trades": SWING_MIN_TRADES, "min_profit_factor": SWING_MIN_PROFIT_FACTOR,
                                             "min_expectancy": SWING_MIN_EXPECTANCY, "min_win_rate": SWING_MIN_WIN_RATE},
                    "inbound_backtest_gate": {"enabled": inbound_enabled, "period": inbound_trade_algo.BACKTEST_PERIOD,
                                               "interval": inbound_trade_algo.BACKTEST_INTERVAL,
                                               "min_trades": inbound_trade_algo.MIN_TRADES,
                                               "min_profit_factor": inbound_trade_algo.MIN_PROFIT_FACTOR,
                                               "min_expectancy": inbound_trade_algo.MIN_EXPECTANCY,
                                               "min_win_rate": inbound_trade_algo.MIN_WIN_RATE}},
        "gate_results": gate_results,
        "swing_gate_results": swing_gate_results,
        "inbound_gate_results": inbound_gate_results,
        "buckets": {},
        "swing": {},
        "inbound": {},
    }
    for bucket in ALL_BUCKETS:
        rejected = [{"symbol": s, "name": INSTRUMENTS[s]["name"], "reason": gate_results[s]["bt_reason"]}
                    for s in candidates_for_bucket(bucket) if not gate_results.get(s, {}).get("bt_pass")]
        watchlist["buckets"][bucket] = {"picks": [], "all_passing": [], "rejected_by_backtest": rejected}
    for broker in ("zerodha", "capitalcom"):
        syms = [s for s, meta in INSTRUMENTS.items() if meta["broker"] == broker]
        rejected = [{"symbol": s, "name": INSTRUMENTS[s]["name"], "reason": swing_gate_results[s]["bt_reason"]}
                    for s in syms if not swing_gate_results.get(s, {}).get("bt_pass")]
        watchlist["swing"][broker] = {"picks": [], "all_passing": [], "rejected_by_backtest": rejected}
        if inbound_enabled:
            rejected = [{"symbol": s, "name": INSTRUMENTS[s]["name"], "reason": inbound_gate_results[s]["bt_reason"]}
                        for s in syms if not inbound_gate_results.get(s, {}).get("bt_pass")]
            watchlist["inbound"][broker] = {"picks": [], "all_passing": [], "rejected_by_backtest": rejected}
        else:
            watchlist["inbound"][broker] = {"picks": [], "all_passing": [], "rejected_by_backtest": []}

    print()
    watchlist = refresh_ranking(watchlist, args, tz, daily_metrics=daily_metrics, save=True)
    print(f"[research] wrote {WATCHLIST_LATEST}")
    return watchlist


def refresh_ranking(watchlist, args, tz, daily_metrics=None, save=True):
    """
    STAGE 2 (cheap, refreshed every --refresh-hours): re-ranks whichever
    instruments already passed the backtest gate, using freshly fetched
    daily bars. Does NOT re-run the 60-day backtest — that edge doesn't
    meaningfully change hour to hour, only which gate-passing instrument is
    most "in play" right now does. This is what keeps today's top-N picks
    current through the day instead of frozen at whatever was true at the
    first run.
    """
    gate_results = watchlist["gate_results"]
    swing_gate_results = watchlist.get("swing_gate_results", {})
    inbound_gate_results = watchlist.get("inbound_gate_results", {})
    passing_symbols = sorted(s for s, g in gate_results.items() if g.get("bt_pass"))
    swing_passing_symbols = sorted(s for s, g in swing_gate_results.items() if g.get("bt_pass"))
    inbound_passing_symbols = sorted(s for s, g in inbound_gate_results.items() if g.get("bt_pass"))
    weights = {"vol": args.w_vol, "momentum": args.w_momentum, "liquidity": args.w_liquidity}

    if daily_metrics is None:
        need = sorted(set(passing_symbols) | set(swing_passing_symbols) | set(inbound_passing_symbols))
        if need:
            print(f"[refresh] re-ranking {len(need)} gate-passing instruments (intraday + swing + inbound) with fresh daily bars...")
            daily_data = fetch_batch(need, DAILY_INTERVAL, DAILY_PERIOD)
            daily_metrics = {s: compute_daily_metrics(s, daily_data.get(s), args.rsi_period, args.atr_period)
                              for s in need}
        else:
            daily_metrics = {}

    for bucket in ALL_BUCKETS:
        top, all_passing = score_and_rank_bucket(bucket, gate_results, daily_metrics, args.top, weights)
        watchlist["buckets"][bucket]["picks"] = top
        watchlist["buckets"][bucket]["all_passing"] = all_passing

    for broker in ("zerodha", "capitalcom"):
        syms = [s for s, meta in INSTRUMENTS.items() if meta["broker"] == broker]
        top, all_passing = score_and_rank_symbols(syms, swing_gate_results, daily_metrics, args.top, weights)
        watchlist["swing"][broker]["picks"] = top
        watchlist["swing"][broker]["all_passing"] = all_passing
        if watchlist["inbound"].get(broker) is not None:
            top, all_passing = score_and_rank_symbols(syms, inbound_gate_results, daily_metrics, args.top, weights)
            watchlist["inbound"][broker]["picks"] = top
            watchlist["inbound"][broker]["all_passing"] = all_passing

    now = datetime.now(timezone.utc)
    watchlist["ranking_refreshed_at_utc"] = now.isoformat(timespec="seconds")
    watchlist["ranking_refreshed_at_local"] = now.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z")

    if save:
        WATCHLIST_LATEST.write_text(json.dumps(watchlist, indent=2))
        (OUT_DIR / f"watchlist_{watchlist['as_of_date']}.json").write_text(json.dumps(watchlist, indent=2))
        for bucket in ALL_BUCKETS:
            b = watchlist["buckets"][bucket]
            print(f"  {bucket.upper():9s} top {len(b['picks'])} of {len(b['all_passing'])} gate-passing "
                  f"({len(b['rejected_by_backtest'])} rejected by gate): "
                  + ", ".join(r["symbol"] for r in b["all_passing"]))
        for broker in ("zerodha", "capitalcom"):
            s = watchlist["swing"][broker]
            print(f"  SWING/{broker.upper():10s} top {len(s['picks'])} of {len(s['all_passing'])} gate-passing "
                  f"({len(s['rejected_by_backtest'])} rejected by gate): "
                  + ", ".join(r["symbol"] for r in s["all_passing"]))
            ib = watchlist["inbound"].get(broker)
            if ib and ib.get("all_passing"):
                print(f"  {inbound_trade_algo.STRATEGY_NAME}/{broker.upper():10s} top {len(ib['picks'])} of "
                      f"{len(ib['all_passing'])} gate-passing ({len(ib['rejected_by_backtest'])} rejected): "
                      + ", ".join(r["symbol"] for r in ib["all_passing"]))
    return watchlist


def ranking_stale(watchlist, refresh_hours):
    ts = watchlist.get("ranking_refreshed_at_utc") or watchlist.get("generated_at_utc")
    if not ts:
        return True
    age_hours = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds() / 3600
    return age_hours >= refresh_hours


# ---------------------------------------------------------------------------
# Live 5-minute signal evaluation
# ---------------------------------------------------------------------------
def evaluate_symbol(symbol, df, args):
    name = INSTRUMENTS.get(symbol, {}).get("name", "")
    if df is None or len(df) < max(args.lookback_bars, args.rsi_period, args.atr_period) + 2:
        return {"symbol": symbol, "name": name, "error": "not enough intraday data"}
    missing_cols = [c for c in ("Close", "High", "Low", "Volume") if c not in df.columns]
    if missing_cols:
        return {"symbol": symbol, "name": name, "error": f"malformed data, missing columns {missing_cols}"}
    close, vol = df["Close"], df["Volume"]
    last_close = float(close.iloc[-1])
    last_rsi = float(rsi(close, args.rsi_period).iloc[-1])
    last_atr = float(atr(df, args.atr_period).iloc[-1])
    prior_high = float(df["High"].iloc[-(args.lookback_bars + 1):-1].max())
    prior_low = float(df["Low"].iloc[-(args.lookback_bars + 1):-1].min())
    vol_avg = float(vol.iloc[-(args.lookback_bars + 1):-1].mean())
    last_vol = float(vol.iloc[-1])
    vol_confirmed = last_vol > vol_avg if vol_avg > 0 else False

    long_trigger = last_close > prior_high and vol_confirmed and args.rsi_long_min <= last_rsi <= args.rsi_long_max
    short_trigger = last_close < prior_low and vol_confirmed and args.rsi_short_min <= last_rsi <= args.rsi_short_max

    verdict, entry, stop, target, reason = "WAIT", None, None, None, ""
    if long_trigger:
        verdict, entry = "LONG", last_close
        stop, target = round(entry - args.stop_atr_mult * last_atr, 4), round(entry + args.target_atr_mult * last_atr, 4)
        reason = f"broke prior {args.lookback_bars}-bar high {prior_high:.2f} w/ volume"
    elif short_trigger:
        verdict, entry = "SHORT", last_close
        stop, target = round(entry + args.stop_atr_mult * last_atr, 4), round(entry - args.target_atr_mult * last_atr, 4)
        reason = f"broke prior {args.lookback_bars}-bar low {prior_low:.2f} w/ volume"
    elif last_close > prior_high:
        reason = "broke high, no volume/RSI confirmation"
    elif last_close < prior_low:
        reason = "broke low, no volume/RSI confirmation"
    else:
        reason = f"inside range [{prior_low:.2f}, {prior_high:.2f}]"

    return {"symbol": symbol, "name": name, "last_close": round(last_close, 4), "rsi": round(last_rsi, 1),
            "verdict": verdict, "entry": entry, "stop": stop, "target": target, "reason": reason,
            "strategy": getattr(args, "strategy", "BREAKOUT_INTRADAY")}


def evaluate_symbol_inbound(symbol, df, params=None):
    """INBOUNDTRADEALGO's live check — see inbound_trade_algo.py for the
    rule and the feasibility check behind it. Deliberately its own function,
    not a variant of evaluate_symbol(): the entry condition (near a range
    edge, still inside it) is the mirror image of a breakout, not a
    parameterization of one."""
    p = params or inbound_trade_algo.DEFAULTS
    name = INSTRUMENTS.get(symbol, {}).get("name", "")
    lb = p["lookback_bars"]
    if df is None or len(df) < max(lb, p["rsi_period"], p["atr_period"]) + 2:
        return {"symbol": symbol, "name": name, "error": "not enough data"}
    missing_cols = [c for c in ("Close", "High", "Low") if c not in df.columns]
    if missing_cols:
        return {"symbol": symbol, "name": name, "error": f"malformed data, missing columns {missing_cols}"}
    close, high, low = df["Close"], df["High"], df["Low"]
    last_close = float(close.iloc[-1])
    last_rsi = float(rsi(close, p["rsi_period"]).iloc[-1])
    last_atr = float(atr(df, p["atr_period"]).iloc[-1])
    prior_high = float(high.iloc[-(lb + 1):-1].max())
    prior_low = float(low.iloc[-(lb + 1):-1].min())
    band = p["band_pct"] * (prior_high - prior_low)
    still_inside = prior_low < last_close < prior_high

    long_trigger = still_inside and last_close <= prior_low + band and p["rsi_long_min"] <= last_rsi <= p["rsi_long_max"]
    short_trigger = still_inside and last_close >= prior_high - band and p["rsi_short_min"] <= last_rsi <= p["rsi_short_max"]

    verdict, entry, stop, target, reason = "WAIT", None, None, None, ""
    if long_trigger:
        verdict, entry = "LONG", last_close
        stop, target = round(entry - p["stop_atr_mult"] * last_atr, 4), round(entry + p["target_atr_mult"] * last_atr, 4)
        reason = f"near range bottom {prior_low:.2f} of [{prior_low:.2f}, {prior_high:.2f}], RSI {last_rsi:.1f} in reversion zone"
    elif short_trigger:
        verdict, entry = "SHORT", last_close
        stop, target = round(entry + p["stop_atr_mult"] * last_atr, 4), round(entry - p["target_atr_mult"] * last_atr, 4)
        reason = f"near range top {prior_high:.2f} of [{prior_low:.2f}, {prior_high:.2f}], RSI {last_rsi:.1f} in reversion zone"
    elif not still_inside:
        reason = "outside its own range — breakout territory, not an inbound setup"
    else:
        reason = f"inside range [{prior_low:.2f}, {prior_high:.2f}] but not near an edge yet"

    return {"symbol": symbol, "name": name, "last_close": round(last_close, 4), "rsi": round(last_rsi, 1),
            "verdict": verdict, "entry": entry, "stop": stop, "target": target, "reason": reason,
            "strategy": inbound_trade_algo.STRATEGY_NAME}


def pick_best_per_symbol(candidates):
    """"Based on best strategy" execution: when more than one strategy
    signals the SAME symbol in the SAME cycle (e.g. BREAKOUT_INTRADAY and
    INBOUNDTRADEALGO both fire on the same stock at once), only one order
    should ever go out for it — never two conflicting or duplicate
    positions on one instrument. Keep whichever candidate has the higher
    backtested expectancy_r for that exact symbol+strategy (tie-broken by
    profit_factor) — the strategy with the more PROVEN edge on that specific
    instrument, not just whichever happened to fire first or was checked
    first in code.

    candidates: list of dicts, each with at least 'symbol', 'strategy',
    'edge_expectancy_r' (float or None), 'edge_profit_factor' (float or
    None) — callers attach these from the relevant gate_results/
    swing_gate_results/inbound_gate_results entry before calling this.

    Returns (winners, losers) — losers are returned, not discarded, so
    callers can log exactly why a candidate didn't execute this cycle
    rather than have it silently vanish.
    """
    def edge_key(c):
        exp = c.get("edge_expectancy_r")
        pf = c.get("edge_profit_factor")
        return (exp if exp is not None else float("-inf"), pf if pf is not None else float("-inf"))

    best = {}
    for c in candidates:
        sym = c["symbol"]
        if sym not in best or edge_key(c) > edge_key(best[sym]):
            best[sym] = c
    winners = list(best.values())
    winner_ids = {(c["symbol"], c["strategy"]) for c in winners}
    losers = [c for c in candidates if (c["symbol"], c["strategy"]) not in winner_ids]
    return winners, losers


def print_bucket_report(bucket, results, quiet, checked_at):
    actionable = [r for r in results if r.get("verdict") in ("LONG", "SHORT")]
    if quiet and not actionable:
        line = " | ".join(
            f"{'*' if r.get('is_pick') else ' '}{r['symbol']} ({r.get('name','')}):"
            f"{r.get('last_close','-')}(rsi{r.get('rsi','-')})" for r in results
        )
        print(f"[{checked_at}] {bucket.upper():9s} WAIT-ALL  {line}")
        return
    print(f"\n=== {checked_at} | {bucket.upper()} ===  (* = today's top-N pick; unmarked = other "
          f"gate-passing candidate, still checked so no signal is skipped)")
    for r in results:
        if "error" in r:
            print(f"  {r['symbol']:12s} ERROR: {r['error']}")
            continue
        star = "*" if r.get("is_pick") else " "
        name = r.get("name") or INSTRUMENTS.get(r["symbol"], {}).get("name", "")
        print(f"{star} {r['symbol']:12s} {name:18s} {r['verdict']:5s} px={r['last_close']} rsi={r['rsi']}  ({r['reason']})")
        if r["verdict"] in ("LONG", "SHORT"):
            rr = abs(r["target"] - r["entry"]) / abs(r["entry"] - r["stop"])
            print(f"      entry={r['entry']}  stop={r['stop']}  target={r['target']}  R:R~{rr:.2f}:1")


# Fixed column set for signal_log.csv — every row is written with exactly
# these columns (missing keys -> blank), regardless of which fields happen
# to be present on a given result (e.g. an error row has fewer keys than a
# normal one). Append-only CSVs write their header once, ever; if the set
# of keys drifts between calls the file silently becomes unparseable. Add
# new fields here, not just to the result dict, if you extend this later.
SIGNAL_LOG_COLUMNS = ["checked_at_local", "checked_at_utc", "bucket", "symbol", "name",
                      "last_close", "rsi", "verdict", "entry", "stop", "target", "reason",
                      "is_pick", "error", "strategy"]


def _ensure_signal_log_schema():
    """SIGNAL_LOG_COLUMNS has grown once before (an "error" column was added
    without touching the file already on disk) and the CSV header — written
    once, ever, on first append — silently fell out of sync with every row
    written after that: readers matching rows against that stale header
    (e.g. pandas' on_bad_lines="skip") drop exactly the newer, correct rows
    and keep only the old ones. Self-heal instead of relying on nobody ever
    extending the schema again: if the on-disk header doesn't match
    SIGNAL_LOG_COLUMNS, back the file up and rewrite every row against the
    current schema (short old rows padded with "", long/reordered rows
    matched positionally) before the next append.
    """
    if not SIGNAL_LOG.exists():
        return
    with open(SIGNAL_LOG, newline="") as f:
        reader = csv.reader(f)
        try:
            on_disk_header = next(reader)
        except StopIteration:
            return
        if on_disk_header == SIGNAL_LOG_COLUMNS:
            return
        rows = list(reader)

    backup = OUT_DIR / f"signal_log_pre_migration_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.csv.bak"
    SIGNAL_LOG.rename(backup)
    n = len(SIGNAL_LOG_COLUMNS)
    with open(SIGNAL_LOG, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(SIGNAL_LOG_COLUMNS)
        for row in rows:
            row = (row + [""] * n)[:n] if len(row) < n else row[:n]
            writer.writerow(row)
    print(f"[signal_log] schema drift detected (on-disk header had {len(on_disk_header)} cols, "
          f"code expects {n}) — migrated {len(rows)} rows, backed up original to {backup}",
          file=sys.stderr)


def log_results(bucket, checked_at_utc, checked_at_local, results):
    _ensure_signal_log_schema()
    rows = [{"checked_at_local": checked_at_local, "checked_at_utc": checked_at_utc, "bucket": bucket, **r}
            for r in results]
    df = pd.DataFrame(rows).reindex(columns=SIGNAL_LOG_COLUMNS)
    header = not SIGNAL_LOG.exists()
    df.to_csv(SIGNAL_LOG, mode="a", header=header, index=False)


def active_buckets_now(now_utc, tz, verbose=False):
    buckets = []
    if is_nse_open(now_utc):
        buckets.append("zerodha")
    elif verbose:
        print(f"  Zerodha/NSE closed now — opens {_local_clock(now_utc, *NSE_OPEN_UTC, tz)} weekdays")
    buckets.append(current_capitalcom_session(now_utc.hour))
    return buckets


def print_startup_banner(tz):
    """Printed once when the script is launched — the schedule/disclaimer
    don't repeat on every subsequent tick, only the live positions do."""
    now_utc = datetime.now(timezone.utc)
    print_session_schedule(now_utc, tz)
    active_buckets_now(now_utc, tz, verbose=True)
    print("Not investment advice — check your broker's live price before acting.\n")


def run_once(watchlist, args, tz, buckets_override=None, swing_brokers=("zerodha", "capitalcom")):
    """
    buckets_override: restrict which session buckets get shown (default:
    whatever active_buckets_now() says is currently open) — used by
    zerodha_trader.py to print ONLY the zerodha bucket, so it's
    self-sufficient for Zerodha visibility instead of relying on
    auto_trader.py's process happening to also be running and interleaving
    its output correctly.
    swing_brokers: same idea, restricts which broker(s)' swing section prints.
    """
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)
    checked_at_utc = now_utc.isoformat(timespec="seconds")
    checked_at_local = now_local.strftime("%Y-%m-%d %H:%M:%S %Z")

    buckets = buckets_override if buckets_override is not None else active_buckets_now(now_utc, tz)
    for bucket in buckets:
        bucket_data = watchlist["buckets"].get(bucket, {})
        all_passing = bucket_data.get("all_passing", bucket_data.get("picks", []))  # fallback for old cache shape
        pick_symbols = {row["symbol"] for row in bucket_data.get("picks", [])}
        symbols = [row["symbol"] for row in all_passing]
        if not symbols:
            print(f"[{checked_at_local}] {bucket.upper()}: no watchlist entries passed the backtest gate today.")
            continue
        # Every gate-passing candidate is checked live, not just the top-N
        # picks — a lower-ranked instrument can still fire a real signal
        # and shouldn't be skipped just because it wasn't today's #1-#5.
        try:
            intraday = fetch_batch(symbols, args.interval, args.period)
        except Exception as e:
            print(f"[{checked_at_local}] {bucket.upper()}: batch data fetch failed ({e}) — skipping this cycle", file=sys.stderr)
            continue
        results = []
        for sym in symbols:
            # One bad symbol (malformed data, unexpected exception) must
            # never take down the whole cycle — that would silently skip
            # every other symbol AND the trading pass that follows this call.
            try:
                r = evaluate_symbol(sym, intraday.get(sym), args)
            except Exception as e:
                r = {"symbol": sym, "name": INSTRUMENTS.get(sym, {}).get("name", ""), "error": f"evaluate_symbol crashed: {e}"}
            r["is_pick"] = sym in pick_symbols
            results.append(r)
        print_bucket_report(bucket, results, args.quiet, checked_at_local)
        if args.log:
            log_results(bucket, checked_at_utc, checked_at_local, results)

    run_swing_check(watchlist, args, tz, checked_at_utc, checked_at_local, brokers=swing_brokers)
    run_inbound_check(watchlist, args, tz, checked_at_utc, checked_at_local, brokers=swing_brokers)


_SWING_ARGS = argparse.Namespace(**SWING_STRATEGY_DEFAULTS, interval=SWING_BACKTEST_INTERVAL, period="6mo",
                                  strategy="BREAKOUT_SWING")


def run_swing_check(watchlist, args, tz, checked_at_utc, checked_at_local, brokers=("zerodha", "capitalcom")):
    """
    Multi-day companion to the 5-minute intraday check above: same
    breakout/RSI/ATR mechanics, but on DAILY bars with up to a
    SWING_STRATEGY_DEFAULTS['max_hold_bars']-day hold — for a stock showing
    a genuine multi-day run worth riding for the week rather than exiting
    same-day. Only evaluates instruments that already passed the SEPARATE
    2-year daily backtest gate (swing_gate_results) — a different,
    independently-validated edge from the intraday one, not the same
    signal on a slower clock.
    """
    for broker in brokers:
        swing_data = watchlist.get("swing", {}).get(broker, {})
        all_passing = swing_data.get("all_passing", [])
        pick_symbols = {row["symbol"] for row in swing_data.get("picks", [])}
        symbols = [row["symbol"] for row in all_passing]
        if not symbols:
            continue
        try:
            daily = fetch_batch(symbols, _SWING_ARGS.interval, _SWING_ARGS.period)
        except Exception as e:
            print(f"[{checked_at_local}] SWING/{broker.upper()}: batch data fetch failed ({e}) — skipping this cycle", file=sys.stderr)
            continue
        results = []
        for sym in symbols:
            try:
                r = evaluate_symbol(sym, daily.get(sym), _SWING_ARGS)
            except Exception as e:
                r = {"symbol": sym, "name": INSTRUMENTS.get(sym, {}).get("name", ""), "error": f"evaluate_symbol crashed: {e}"}
            r["is_pick"] = sym in pick_symbols
            results.append(r)
        print_bucket_report(f"swing/{broker}", results, args.quiet, checked_at_local)
        if args.log:
            log_results(f"swing/{broker}", checked_at_utc, checked_at_local, results)


def run_inbound_check(watchlist, args, tz, checked_at_utc, checked_at_local, brokers=("zerodha", "capitalcom")):
    """INBOUNDTRADEALGO's live loop — structurally identical to
    run_swing_check() above (same daily-bar cadence, same "every gate-passer
    gets checked, not just today's top-N" principle), just calling
    evaluate_symbol_inbound() against inbound_gate_results instead of
    evaluate_symbol() against swing_gate_results. No-ops entirely if the
    strategy is disabled — see inbound_trade_algo.is_enabled()."""
    if not inbound_trade_algo.is_enabled():
        return
    for broker in brokers:
        inbound_data = watchlist.get("inbound", {}).get(broker, {})
        all_passing = inbound_data.get("all_passing", [])
        pick_symbols = {row["symbol"] for row in inbound_data.get("picks", [])}
        symbols = [row["symbol"] for row in all_passing]
        if not symbols:
            continue
        try:
            daily = fetch_batch(symbols, inbound_trade_algo.BACKTEST_INTERVAL, "6mo")
        except Exception as e:
            print(f"[{checked_at_local}] {inbound_trade_algo.STRATEGY_NAME}/{broker.upper()}: batch data fetch "
                  f"failed ({e}) — skipping this cycle", file=sys.stderr)
            continue
        results = []
        for sym in symbols:
            try:
                r = evaluate_symbol_inbound(sym, daily.get(sym))
            except Exception as e:
                r = {"symbol": sym, "name": INSTRUMENTS.get(sym, {}).get("name", ""),
                     "error": f"evaluate_symbol_inbound crashed: {e}"}
            r["is_pick"] = sym in pick_symbols
            results.append(r)
        bucket_label = f"{inbound_trade_algo.STRATEGY_NAME.lower()}/{broker}"
        print_bucket_report(bucket_label, results, args.quiet, checked_at_local)
        if args.log:
            log_results(bucket_label, checked_at_utc, checked_at_local, results)


def main():
    p = argparse.ArgumentParser(description="Unified Zerodha + Capital.com intraday trade monitor")
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--interval", default="5m", help="Live-check bar interval")
    p.add_argument("--period", default="5d", help="Live-check history window")
    p.add_argument("--loop-seconds", type=int, default=0, help="If >0, re-check every N seconds until Ctrl+C")
    p.add_argument("--once", action="store_true", help="Run a single check and exit (same as --loop-seconds 0)")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--log", action="store_true")
    p.add_argument("--rebuild-watchlist", action="store_true", help="Force a fresh daily research pass even if today's cache exists")
    p.add_argument("--refresh-hours", type=float, default=1.0,
                    help="Re-rank gate-passing instruments this often using fresh daily bars (default 1.0h). "
                         "Does not re-run the 60-day backtest — only the daily gate rebuild (new local day) does that.")

    p.add_argument("--rsi-period", type=int, default=STRATEGY_DEFAULTS["rsi_period"])
    p.add_argument("--atr-period", type=int, default=STRATEGY_DEFAULTS["atr_period"])
    p.add_argument("--lookback-bars", type=int, default=STRATEGY_DEFAULTS["lookback_bars"])
    p.add_argument("--rsi-long-min", type=float, default=STRATEGY_DEFAULTS["rsi_long_min"])
    p.add_argument("--rsi-long-max", type=float, default=STRATEGY_DEFAULTS["rsi_long_max"])
    p.add_argument("--rsi-short-min", type=float, default=STRATEGY_DEFAULTS["rsi_short_min"])
    p.add_argument("--rsi-short-max", type=float, default=STRATEGY_DEFAULTS["rsi_short_max"])
    p.add_argument("--stop-atr-mult", type=float, default=STRATEGY_DEFAULTS["stop_atr_mult"])
    p.add_argument("--target-atr-mult", type=float, default=STRATEGY_DEFAULTS["target_atr_mult"])
    p.add_argument("--max-hold-bars", type=int, default=STRATEGY_DEFAULTS["max_hold_bars"])

    p.add_argument("--w-vol", type=float, default=0.40)
    p.add_argument("--w-momentum", type=float, default=0.35)
    p.add_argument("--w-liquidity", type=float, default=0.25)

    p.add_argument("--backtest-period", default="60d")
    p.add_argument("--backtest-interval", default="5m")
    # Defaults for all four below now come from trading_settings.py
    # (2026-09-03, user request) — dashboard-editable, single source of
    # truth shared with auto_trader.py/zerodha_trader.py's build_tm_args().
    # Passing the CLI flag explicitly (e.g. a one-off `--min-win-rate 0.5`
    # test run) still overrides this default for that invocation only,
    # exactly like before — nothing about that behavior changed.
    #
    # History: profit-factor/expectancy settled at 1.1/0.0R on 2026-08-24 —
    # the 5bps cost_bps model above already did the real work (cut intraday
    # passes from 29 to 3 on this universe), and requiring swing's stricter
    # 1.3 PF/0.05R on top of that returned zero, every time. Win rate was
    # added at 0.80 on 2026-08-29 (expected to fail the whole universe),
    # then lowered to 0.65 on 2026-09-03 for the same reason as
    # SWING_MIN_WIN_RATE above.
    p.add_argument("--min-trades", type=int, default=ts.get("intraday_min_trades"))
    p.add_argument("--min-profit-factor", type=float, default=ts.get("intraday_min_profit_factor"))
    p.add_argument("--min-expectancy", type=float, default=ts.get("intraday_min_expectancy"))
    p.add_argument("--min-win-rate", type=float, default=ts.get("intraday_min_win_rate"))

    p.add_argument("--tz", default=DEFAULT_TZ_NAME,
                    help=f"IANA timezone for all displayed/logged times and the daily-refresh boundary "
                         f"(default {DEFAULT_TZ_NAME})")

    args = p.parse_args()
    args.strategy = "BREAKOUT_INTRADAY"
    if args.once:
        args.loop_seconds = 0
    tz = ZoneInfo(args.tz)

    watchlist = build_watchlist(args, tz, force=args.rebuild_watchlist)
    print_startup_banner(tz)

    while True:
        try:
            if watchlist.get("as_of_date") != datetime.now(tz).strftime("%Y-%m-%d"):
                watchlist = build_watchlist(args, tz, force=True)  # new day: full gate rebuild (includes a ranking refresh)
            elif ranking_stale(watchlist, args.refresh_hours):
                watchlist = refresh_ranking(watchlist, args, tz)  # same day: cheap re-rank only
            run_once(watchlist, args, tz)
        except Exception as e:
            print(f"[{datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S %Z')}] ERROR: {e}", file=sys.stderr)

        if args.loop_seconds <= 0:
            break
        try:
            time.sleep(args.loop_seconds)
        except KeyboardInterrupt:
            print("\nStopped.")
            break


if __name__ == "__main__":
    main()
