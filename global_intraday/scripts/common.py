"""
Shared indicator math and instrument universe for the global_intraday scripts.

build_watchlist.py (daily research) and session_signal.py (5-min polling)
both import from here so the RSI/ATR math and the tradable-instrument list
stay in exactly one place.
"""
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
OUT_DIR = ROOT / "output"
DATA_DIR.mkdir(exist_ok=True)
OUT_DIR.mkdir(exist_ok=True)

WATCHLIST_LATEST = OUT_DIR / "watchlist_latest.json"

# ---------------------------------------------------------------------------
# Trading-session windows, in UTC hours. Deliberately overlapping at the
# edges (e.g. Europe/US overlap 13:00-15:00 UTC) reflects reality; a session
# lookup picks whichever window contains "now", preferring the more specific
# one if two match (US wins over Europe in the overlap since that's when US
# equity-index liquidity takes over). Tune with --session-hours if your
# broker's actual liquid hours differ.
# ---------------------------------------------------------------------------
SESSION_HOURS_UTC = {
    "asia": (23, 8),     # Sydney/Tokyo/HK/Singapore: 23:00 -> 08:00 UTC (wraps midnight)
    "europe": (7, 16),   # Frankfurt/London: 07:00 -> 16:00 UTC
    "us": (13, 21),      # NYSE/Nasdaq: 13:00 -> 21:00 UTC
}
SESSION_PRIORITY = ["us", "europe", "asia"]  # tie-break order when windows overlap


def current_session(now_utc_hour, session_hours=None):
    session_hours = session_hours or SESSION_HOURS_UTC
    matches = []
    for name in SESSION_PRIORITY:
        if name not in session_hours:
            continue
        start, end = session_hours[name]
        in_window = (start <= now_utc_hour < end) if start < end else (now_utc_hour >= start or now_utc_hour < end)
        if in_window:
            matches.append(name)
    return matches[0] if matches else "asia"  # off-hours gap defaults to the next-up session


# ---------------------------------------------------------------------------
# Instrument universe. Each entry: display name, asset class, and which
# session(s) it's a genuine candidate for (i.e. when its home market is open
# or it's a globally liquid 24h commodity). Futures tickers are used over
# cash-index tickers for the US majors because they carry real volume for
# the liquidity score around the clock.
# ---------------------------------------------------------------------------
INSTRUMENTS = {
    # Asia
    "^N225":     {"name": "Nikkei 225",    "class": "index",     "sessions": ["asia"]},
    "^HSI":      {"name": "Hang Seng",     "class": "index",     "sessions": ["asia"]},
    "^AXJO":     {"name": "ASX 200",       "class": "index",     "sessions": ["asia"]},
    "^KS11":     {"name": "KOSPI",         "class": "index",     "sessions": ["asia"]},
    "^NSEI":     {"name": "Nifty 50",      "class": "index",     "sessions": ["asia"]},
    # Europe
    "^GDAXI":    {"name": "DAX 40",        "class": "index",     "sessions": ["europe"]},
    "^FTSE":     {"name": "FTSE 100",      "class": "index",     "sessions": ["europe"]},
    "^FCHI":     {"name": "CAC 40",        "class": "index",     "sessions": ["europe"]},
    "^STOXX50E": {"name": "Euro Stoxx 50", "class": "index",     "sessions": ["europe"]},
    # US
    "NQ=F":      {"name": "NAS100",        "class": "index",     "sessions": ["us"]},
    "ES=F":      {"name": "SPX500",        "class": "index",     "sessions": ["us"]},
    "YM=F":      {"name": "US30",          "class": "index",     "sessions": ["us"]},
    "RTY=F":     {"name": "Russell 2000",  "class": "index",     "sessions": ["us"]},
    "AAPL":      {"name": "Apple",         "class": "stock",     "sessions": ["us"]},
    "MSFT":      {"name": "Microsoft",     "class": "stock",     "sessions": ["us"]},
    "NVDA":      {"name": "Nvidia",        "class": "stock",     "sessions": ["us"]},
    "TSLA":      {"name": "Tesla",         "class": "stock",     "sessions": ["us"]},
    "AMZN":      {"name": "Amazon",        "class": "stock",     "sessions": ["us"]},
    "META":      {"name": "Meta",          "class": "stock",     "sessions": ["us"]},
    "GOOGL":     {"name": "Alphabet",      "class": "stock",     "sessions": ["us"]},
    "AMD":       {"name": "AMD",           "class": "stock",     "sessions": ["us"]},
    # Commodities: 24h-traded, candidate in every session
    "GC=F":      {"name": "Gold",          "class": "commodity", "sessions": ["asia", "europe", "us"]},
    "SI=F":      {"name": "Silver",        "class": "commodity", "sessions": ["asia", "europe", "us"]},
    "CL=F":      {"name": "Crude Oil WTI", "class": "commodity", "sessions": ["asia", "europe", "us"]},
    "HG=F":      {"name": "Copper",        "class": "commodity", "sessions": ["asia", "europe", "us"]},
    "NG=F":      {"name": "Natural Gas",   "class": "commodity", "sessions": ["asia", "europe", "us"]},
}


def candidates_for_session(session):
    return [sym for sym, meta in INSTRUMENTS.items() if session in meta["sessions"]]


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


# ---------------------------------------------------------------------------
# The single source of truth for the breakout/breakdown strategy's
# parameters. session_signal.py's CLI defaults and backtest.py's simulation
# both read from this dict, so a backtest can never silently drift from what
# the live poller actually trades.
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
    "max_hold_bars": 48,  # 48 * 5m = 4 hours; a trade still open after this is closed at market
}


def simulate_trades(df, params=None):
    """
    Replays the exact breakout/breakdown + volume + RSI rule session_signal.py
    trades live, bar by bar over historical data, and returns the list of
    trades it would have taken. Same-bar stop+target hits are resolved
    conservatively (stop wins) since intrabar order is unknown from OHLC bars.
    """
    p = {**STRATEGY_DEFAULTS, **(params or {})}
    lb = p["lookback_bars"]

    close, high, low, vol = df["Close"], df["High"], df["Low"], df["Volume"]
    rsi_series = rsi(close, p["rsi_period"])
    atr_series = atr(df, p["atr_period"])
    prior_high = high.rolling(lb).max().shift(1)
    prior_low = low.rolling(lb).min().shift(1)
    vol_avg = vol.rolling(lb).mean().shift(1)

    long_sig = (close > prior_high) & (vol > vol_avg) & rsi_series.between(p["rsi_long_min"], p["rsi_long_max"])
    short_sig = (close < prior_low) & (vol > vol_avg) & rsi_series.between(p["rsi_short_min"], p["rsi_short_max"])

    c, h, l, a = close.values, high.values, low.values, atr_series.values
    long_arr, short_arr = long_sig.fillna(False).values, short_sig.fillna(False).values
    times = df.index
    n = len(df)

    trades = []
    i = lb
    while i < n:
        direction = "LONG" if long_arr[i] else ("SHORT" if short_arr[i] else None)
        if direction is None or np.isnan(a[i]):
            i += 1
            continue

        entry = float(c[i])
        atr_val = float(a[i])
        if direction == "LONG":
            stop = entry - p["stop_atr_mult"] * atr_val
            target = entry + p["target_atr_mult"] * atr_val
        else:
            stop = entry + p["stop_atr_mult"] * atr_val
            target = entry - p["target_atr_mult"] * atr_val

        j_end = min(i + p["max_hold_bars"], n - 1)
        exit_price, outcome, exit_i = None, None, None
        for j in range(i + 1, j_end + 1):
            if direction == "LONG":
                hit_stop, hit_target = l[j] <= stop, h[j] >= target
            else:
                hit_stop, hit_target = h[j] >= stop, l[j] <= target
            if hit_stop:
                exit_price, outcome, exit_i = stop, "stop", j
                break
            if hit_target:
                exit_price, outcome, exit_i = target, "target", j
                break
        if exit_price is None:
            exit_i = j_end
            exit_price = float(c[exit_i])
            outcome = "time"

        risk = abs(entry - stop)
        raw_pl = (exit_price - entry) if direction == "LONG" else (entry - exit_price)
        r_multiple = raw_pl / risk if risk > 0 else 0.0

        trades.append({
            "direction": direction,
            "entry_time": str(times[i]),
            "exit_time": str(times[exit_i]),
            "entry": round(entry, 4),
            "stop": round(stop, 4),
            "target": round(target, 4),
            "exit_price": round(exit_price, 4),
            "outcome": outcome,
            "r_multiple": round(r_multiple, 3),
        })
        i = exit_i + 1

    return trades


def summarize_trades(trades):
    n = len(trades)
    if n == 0:
        return {"trade_count": 0, "win_rate": None, "avg_r": None, "profit_factor": None, "expectancy_r": None}
    rs = [t["r_multiple"] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    win_rate = len(wins) / n
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else None)
    expectancy_r = sum(rs) / n
    return {
        "trade_count": n,
        "win_rate": round(win_rate, 3),
        "avg_r": round(expectancy_r, 3),
        "profit_factor": round(profit_factor, 3) if profit_factor not in (None, float("inf")) else profit_factor,
        "expectancy_r": round(expectancy_r, 3),
    }
