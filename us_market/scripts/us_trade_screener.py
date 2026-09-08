#!/usr/bin/env python3
"""
On-demand US-market intraday/swing trade screener.

Pulls the current S&P 500 (Large), S&P 400 (Mid), and S&P 600 (Small)
constituent lists from Wikipedia, downloads daily OHLCV for all of them via
yfinance, computes volatility/volume/breakout signals, and classifies each
stock into a trade bucket with a stop-loss and target.

Usage:
    ./.venv/bin/python us_market/scripts/us_trade_screener.py
    ./.venv/bin/python us_market/scripts/us_trade_screener.py --refresh-lists
    ./.venv/bin/python us_market/scripts/us_trade_screener.py --segments large,mid
    ./.venv/bin/python us_market/scripts/us_trade_screener.py --top 15 --no-html

Outputs (in us_market/output/):
    master_full_screen.csv       every stock, every computed metric
    master_buy_core.csv          breakout confirmed, RSI < overbought, liquid
    master_buy_caution.csv       breakout confirmed, RSI >= overbought or thin
    master_sell_core.csv         fresh breakdown, not yet deeply oversold
    master_sell_extended.csv     breakdown but already crashed (bounce risk)
    master_watch.csv             high volume surge, no confirmed direction
    trade_setup.html             self-contained report, open in any browser
"""
import argparse
import io
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
OUT_DIR = ROOT / "output"
DATA_DIR.mkdir(exist_ok=True)
OUT_DIR.mkdir(exist_ok=True)

LOOKBACK = "9mo"
BATCH_SIZE = 25
LIST_MAX_AGE_DAYS = 7

LIQUIDITY_FLOOR = 200_000
RSI_OVERBOUGHT = 75.0
RSI_OVERSOLD = 30.0
STOP_ATR_MULT = 1.5
TARGET_ATR_MULT = 2.5
EXTENDED_RET5D_PCT = -8.0
WATCH_VOL_SURGE = 3.0

SEGMENTS = {
    "large": ("Large", "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "sp500_list.csv"),
    "mid": ("Mid", "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies", "sp400_list.csv"),
    "small": ("Small", "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies", "sp600_list.csv"),
}

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def fetch_list(label, url, filename, refresh=False):
    path = DATA_DIR / filename
    if path.exists() and not refresh:
        age_days = (time.time() - path.stat().st_mtime) / 86400
        if age_days < LIST_MAX_AGE_DAYS:
            try:
                df = pd.read_csv(path)
                return [s.strip().replace(".", "-") for s in df["Symbol"].tolist()]
            except Exception:
                pass

    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    table = pd.read_html(io.StringIO(resp.text))[0]
    table[["Symbol"]].to_csv(path, index=False)
    print(f"[{label}] fetched {len(table)} constituents -> {path.name}")
    return [s.strip().replace(".", "-") for s in table["Symbol"].tolist()]


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


def analyze_one(symbol, segment, df):
    if df is None or len(df) < 60:
        return None
    df = df.dropna().copy()
    if len(df) < 60:
        return None
    close = df["Close"]
    vol = df["Volume"]
    last_close = close.iloc[-1]
    last_date = df.index[-1]

    atr14 = atr(df, 14)
    atr_pct = (atr14.iloc[-1] / last_close) * 100

    vol_avg20 = vol.rolling(20).mean().iloc[-1]
    vol_today = vol.iloc[-1]
    vol_surge = vol_today / vol_avg20 if vol_avg20 and vol_avg20 > 0 else np.nan

    sma20 = close.rolling(20).mean().iloc[-1]
    sma50 = close.rolling(50).mean().iloc[-1]
    rsi14 = rsi(close, 14).iloc[-1]

    high20 = df["High"].rolling(20).max()
    low20 = df["Low"].rolling(20).min()
    prior_high20 = high20.iloc[-2]
    prior_low20 = low20.iloc[-2]

    dist_to_high_pct = (prior_high20 - last_close) / last_close * 100
    breakout_up = last_close > prior_high20
    breakout_down = last_close < prior_low20
    near_breakout = (0 <= dist_to_high_pct <= 2) or breakout_up

    ret_5d = (close.iloc[-1] / close.iloc[-6] - 1) * 100 if len(close) > 6 else np.nan
    ret_20d = (close.iloc[-1] / close.iloc[-21] - 1) * 100 if len(close) > 21 else np.nan

    trend_up = last_close > sma20 > sma50 if pd.notna(sma20) and pd.notna(sma50) else False
    trend_down = last_close < sma20 < sma50 if pd.notna(sma20) and pd.notna(sma50) else False

    return {
        "symbol": symbol,
        "cap_segment": segment,
        "last_date": last_date.strftime("%Y-%m-%d"),
        "last_close": round(float(last_close), 2),
        "atr_pct": round(float(atr_pct), 2) if pd.notna(atr_pct) else None,
        "vol_surge_x": round(float(vol_surge), 2) if pd.notna(vol_surge) else None,
        "vol_today": int(vol_today),
        "vol_avg20": int(vol_avg20) if pd.notna(vol_avg20) else None,
        "rsi14": round(float(rsi14), 1) if pd.notna(rsi14) else None,
        "trend_up": bool(trend_up),
        "trend_down": bool(trend_down),
        "ret_5d_pct": round(float(ret_5d), 2) if pd.notna(ret_5d) else None,
        "ret_20d_pct": round(float(ret_20d), 2) if pd.notna(ret_20d) else None,
        "dist_to_20d_high_pct": round(float(dist_to_high_pct), 2),
        "breakout_up_today": bool(breakout_up),
        "breakout_down_today": bool(breakout_down),
        "near_breakout": bool(near_breakout),
    }


def download_segment(symbols, segment_label):
    results = []
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i : i + BATCH_SIZE]
        try:
            data = yf.download(
                batch, period=LOOKBACK, interval="1d", group_by="ticker",
                threads=True, progress=False, auto_adjust=True,
            )
        except Exception as e:
            print(f"  batch {i} failed: {e}")
            continue
        for sym in batch:
            try:
                df = data if len(batch) == 1 else data[sym]
                res = analyze_one(sym, segment_label, df)
                if res:
                    results.append(res)
            except Exception:
                continue
        print(f"  [{segment_label}] {min(i + BATCH_SIZE, len(symbols))}/{len(symbols)}")
        time.sleep(0.4)
    return results


def stop_target(close, atr_pct, direction):
    if direction == "buy":
        stop = round(close * (1 - STOP_ATR_MULT * atr_pct / 100), 2)
        target = round(close * (1 + TARGET_ATR_MULT * atr_pct / 100), 2)
    else:
        stop = round(close * (1 + STOP_ATR_MULT * atr_pct / 100), 2)
        target = round(close * (1 - TARGET_ATR_MULT * atr_pct / 100), 2)
    return stop, target


def classify(master):
    liquid = master[master["vol_avg20"].fillna(0) > LIQUIDITY_FLOOR].copy()

    buy = liquid[liquid["breakout_up_today"] == True].copy()
    if not buy.empty:
        buy["stop_loss"], buy["target"] = zip(*buy.apply(
            lambda r: stop_target(r["last_close"], r["atr_pct"] or 0, "buy"), axis=1))
    else:
        buy["stop_loss"] = pd.Series(dtype=float)
        buy["target"] = pd.Series(dtype=float)

    buy_core = buy[buy["rsi14"].fillna(100) < RSI_OVERBOUGHT].sort_values("vol_surge_x", ascending=False)
    buy_caution = buy[buy["rsi14"].fillna(100) >= RSI_OVERBOUGHT].sort_values("vol_surge_x", ascending=False)

    sell = liquid[
        (liquid["breakout_down_today"] == True)
        | ((liquid["trend_down"] == True) & (liquid["vol_surge_x"].fillna(0) > 1.2))
    ].copy()
    if not sell.empty:
        sell["stop_loss"], sell["target"] = zip(*sell.apply(
            lambda r: stop_target(r["last_close"], r["atr_pct"] or 0, "sell"), axis=1))
    else:
        sell["stop_loss"] = pd.Series(dtype=float)
        sell["target"] = pd.Series(dtype=float)

    is_extended = (sell["ret_5d_pct"].fillna(0) < EXTENDED_RET5D_PCT) | (sell["rsi14"].fillna(50) < RSI_OVERSOLD)
    sell_core = sell[~is_extended].sort_values("ret_5d_pct")
    sell_extended = sell[is_extended].sort_values("ret_5d_pct")

    directional_syms = set(buy["symbol"]) | set(sell["symbol"])
    watch = liquid[
        (~liquid["symbol"].isin(directional_syms)) & (liquid["vol_surge_x"].fillna(0) > WATCH_VOL_SURGE)
    ].sort_values("vol_surge_x", ascending=False)

    return buy_core, buy_caution, sell_core, sell_extended, watch


DISPLAY_COLS_BUY = ["symbol", "cap_segment", "last_close", "atr_pct", "vol_surge_x", "rsi14", "stop_loss", "target"]
DISPLAY_COLS_SELL = ["symbol", "cap_segment", "last_close", "atr_pct", "rsi14", "ret_5d_pct", "stop_loss", "target"]
DISPLAY_COLS_WATCH = ["symbol", "cap_segment", "last_close", "vol_surge_x", "rsi14", "breakout_up_today", "breakout_down_today"]


def print_section(title, df, cols):
    print(f"\n=== {title} ({len(df)}) ===")
    if df.empty:
        print("  (none)")
    else:
        print(df[cols].to_string(index=False))


def render_html(buy_core, buy_caution, sell_core, sell_extended, watch, as_of_date, top_n):
    def rows_buy(df):
        if df.empty:
            return "<tr><td colspan='8' style='text-align:center; color:var(--muted); font-style:italic;'>No candidates identified in this category</td></tr>"
        return "".join(
            f"<tr><td><b>{r.symbol}</b></td><td><span class='seg seg-{r.cap_segment}'>{r.cap_segment}</span></td>"
            f"<td>{r.last_close:,.2f}</td><td>{r.atr_pct}%</td><td>{r.vol_surge_x}x</td><td>{r.rsi14}</td>"
            f"<td class='buy'>{r.stop_loss:,.2f}</td><td class='buy'>{r.target:,.2f}</td></tr>"
            for r in df.head(top_n).itertuples()
        )

    def rows_sell(df):
        if df.empty:
            return "<tr><td colspan='8' style='text-align:center; color:var(--muted); font-style:italic;'>No candidates identified in this category</td></tr>"
        return "".join(
            f"<tr><td><b>{r.symbol}</b></td><td><span class='seg seg-{r.cap_segment}'>{r.cap_segment}</span></td>"
            f"<td>{r.last_close:,.2f}</td><td>{r.atr_pct}%</td><td>{r.rsi14}</td><td>{r.ret_5d_pct}%</td>"
            f"<td class='sell'>{r.stop_loss:,.2f}</td><td class='sell'>{r.target:,.2f}</td></tr>"
            for r in df.head(top_n).itertuples()
        )

    def rows_watch(df):
        if df.empty:
            return "<tr><td colspan='5' style='text-align:center; color:var(--muted); font-style:italic;'>No candidates identified in this category</td></tr>"
        return "".join(
            f"<tr><td><b>{r.symbol}</b></td><td><span class='seg seg-{r.cap_segment}'>{r.cap_segment}</span></td>"
            f"<td>{r.last_close:,.2f}</td><td>{r.vol_surge_x}x</td><td>{r.rsi14}</td></tr>"
            for r in df.head(top_n).itertuples()
        )

    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>US Trade Setup - {as_of_date}</title>
<style>
:root {{ --surface:#fcfcfb; --plane:#f9f9f7; --text:#0b0b0b; --sub:#52514e; --muted:#898781;
  --grid:#e1e0d9; --border:rgba(11,11,11,0.10); --blue:#2a78d6; --good:#0ca30c; --crit:#d03b3b; }}
@media (prefers-color-scheme: dark) {{ :root {{ --surface:#1a1a19; --plane:#0d0d0d; --text:#fff; --sub:#c3c2b7;
  --muted:#898781; --grid:#2c2c2a; --border:rgba(255,255,255,0.10); --blue:#3987e5; --good:#0ca30c; --crit:#e66767; }} }}
body {{ background:var(--plane); color:var(--text); font-family:system-ui,-apple-system,"Segoe UI",sans-serif;
  max-width:1100px; margin:0 auto; padding:32px 20px 64px; }}
h1 {{ font-size:1.6rem; margin:0 0 4px; }}
.sub {{ color:var(--sub); font-size:0.92rem; margin:0 0 20px; }}
.disclaimer {{ background:var(--surface); border:1px solid var(--border); border-left:3px solid var(--crit);
  border-radius:8px; padding:12px 16px; font-size:0.85rem; color:var(--sub); margin-bottom:26px; }}
h2 {{ font-size:1.1rem; margin:28px 0 4px; }}
.card {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:16px 18px; overflow-x:auto; }}
table {{ width:100%; border-collapse:collapse; font-size:0.83rem; font-variant-numeric:tabular-nums; min-width:600px; }}
th,td {{ text-align:right; padding:6px 10px; border-bottom:1px solid var(--grid); white-space:nowrap; }}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2) {{ text-align:left; }}
th {{ color:var(--muted); font-size:0.7rem; text-transform:uppercase; }}
.seg {{ font-size:0.66rem; font-weight:700; padding:2px 6px; border-radius:5px; }}
.seg-Large {{ background:rgba(42,120,214,0.15); color:var(--blue); }}
.seg-Mid {{ background:rgba(250,178,25,0.20); color:#a86a00; }}
.seg-Small {{ background:rgba(12,163,12,0.15); color:var(--good); }}
.buy {{ color:var(--good); font-weight:700; }}
.sell {{ color:var(--crit); font-weight:700; }}
footer {{ color:var(--muted); font-size:0.78rem; border-top:1px solid var(--grid); padding-top:14px; margin-top:24px; }}
</style></head><body>
<h1>US Trade Setup — {as_of_date}</h1>
<p class="sub">Large (S&amp;P 500) + Mid (S&amp;P 400) + Small (S&amp;P 600), regenerated on demand.</p>
<div class="disclaimer"><b>Not investment advice.</b> Mechanical technical screen only — verify news/live quotes before trading.</div>

<h2>Buy — core</h2>
<div class="card"><table><thead><tr><th>Symbol</th><th>Seg</th><th>Close</th><th>ATR%</th><th>Vol surge</th><th>RSI</th><th>Stop</th><th>Target</th></tr></thead>
<tbody>{rows_buy(buy_core)}</tbody></table></div>

<h2>Buy — aggressive (RSI ≥ {RSI_OVERBOUGHT:.0f})</h2>
<div class="card"><table><thead><tr><th>Symbol</th><th>Seg</th><th>Close</th><th>ATR%</th><th>Vol surge</th><th>RSI</th><th>Stop</th><th>Target</th></tr></thead>
<tbody>{rows_buy(buy_caution)}</tbody></table></div>

<h2>Sell / short-bias — fresh breakdowns</h2>
<div class="card"><table><thead><tr><th>Symbol</th><th>Seg</th><th>Close</th><th>ATR%</th><th>RSI</th><th>5D ret</th><th>Stop</th><th>Target</th></tr></thead>
<tbody>{rows_sell(sell_core)}</tbody></table></div>

<h2>Already crashed — avoid chasing the short</h2>
<div class="card"><table><thead><tr><th>Symbol</th><th>Seg</th><th>Close</th><th>ATR%</th><th>RSI</th><th>5D ret</th><th>Stop</th><th>Target</th></tr></thead>
<tbody>{rows_sell(sell_extended)}</tbody></table></div>

<h2>Needs a news check — high volume, no confirmed direction</h2>
<div class="card"><table><thead><tr><th>Symbol</th><th>Seg</th><th>Close</th><th>Vol surge</th><th>RSI</th></tr></thead>
<tbody>{rows_watch(watch)}</tbody></table></div>

<footer>Liquidity floor: 20-day avg volume &gt; {LIQUIDITY_FLOOR:,}. Stops/targets use {STOP_ATR_MULT}x / {TARGET_ATR_MULT}x ATR(14).
Generated by us_market/scripts/us_trade_screener.py</footer>
</body></html>"""
    out_path = OUT_DIR / "trade_setup.html"
    out_path.write_text(html)
    return out_path


def main():
    parser = argparse.ArgumentParser(description="On-demand US-market trade screener")
    parser.add_argument("--refresh-lists", action="store_true", help="Force re-download of index constituent lists")
    parser.add_argument("--segments", default="large,mid,small", help="Comma-separated: large,mid,small")
    parser.add_argument("--top", type=int, default=15, help="Rows to show per category")
    parser.add_argument("--no-html", action="store_true", help="Skip HTML report generation")
    args = parser.parse_args()

    wanted = [s.strip().lower() for s in args.segments.split(",")]
    all_results = []
    for key in wanted:
        if key not in SEGMENTS:
            print(f"Unknown segment '{key}', skipping"); continue
        label, url, filename = SEGMENTS[key]
        symbols = fetch_list(label, url, filename, refresh=args.refresh_lists)
        print(f"\nDownloading {len(symbols)} {label} symbols...")
        all_results.extend(download_segment(symbols, label))

    master = pd.DataFrame(all_results).drop_duplicates(subset="symbol", keep="first")
    master.to_csv(OUT_DIR / "master_full_screen.csv", index=False)
    as_of_date = master["last_date"].max() if not master.empty else datetime.now().strftime("%Y-%m-%d")
    print(f"\nAnalyzed {len(master)} unique stocks. Data as of {as_of_date}.")

    buy_core, buy_caution, sell_core, sell_extended, watch = classify(master)
    buy_core.to_csv(OUT_DIR / "master_buy_core.csv", index=False)
    buy_caution.to_csv(OUT_DIR / "master_buy_caution.csv", index=False)
    sell_core.to_csv(OUT_DIR / "master_sell_core.csv", index=False)
    sell_extended.to_csv(OUT_DIR / "master_sell_extended.csv", index=False)
    watch.to_csv(OUT_DIR / "master_watch.csv", index=False)

    print_section("BUY - CORE", buy_core.head(args.top), DISPLAY_COLS_BUY)
    print_section("BUY - AGGRESSIVE (overbought)", buy_caution.head(args.top), DISPLAY_COLS_BUY)
    print_section("SELL - CORE (fresh breakdown)", sell_core.head(args.top), DISPLAY_COLS_SELL)
    print_section("SELL - EXTENDED (avoid chasing)", sell_extended.head(args.top), DISPLAY_COLS_SELL)
    print_section("WATCH (needs news check)", watch.head(args.top), DISPLAY_COLS_WATCH)

    if not args.no_html:
        path = render_html(buy_core, buy_caution, sell_core, sell_extended, watch, as_of_date, args.top)
        print(f"\nHTML report: {path}")


if __name__ == "__main__":
    main()
