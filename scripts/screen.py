"""
Nifty 200 intraday/swing screener.

Downloads ~6 months of daily OHLCV for each Nifty 200 constituent via yfinance,
computes volatility, volume-surge, and breakout signals, and ranks stocks for:
  - intraday candidates (tomorrow)   -> high ATR%, volume surge, near a breakout level
  - swing candidates (week-ish hold) -> trend/momentum + recent breakout confirmation

Output: CSV + JSON summaries in output/
"""
import json
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
OUT_DIR = ROOT / "output"
OUT_DIR.mkdir(exist_ok=True)

LIST_CSV = Path(sys.argv[1]) if len(sys.argv) > 1 else DATA_DIR / "nifty200_list.csv"
OUT_PREFIX = sys.argv[2] if len(sys.argv) > 2 else ""
LOOKBACK = "9mo"
INTERVAL = "1d"


def load_symbols():
    df = pd.read_csv(LIST_CSV)
    symbols = [f"{s.strip()}.NS" for s in df["Symbol"].tolist()]
    return symbols


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
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period).mean()


def analyze_one(symbol, df):
    if df is None or len(df) < 60:
        return None
    df = df.dropna().copy()
    close = df["Close"]
    vol = df["Volume"]

    last = df.iloc[-1]
    last_close = close.iloc[-1]

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
    dist_to_low_pct = (last_close - prior_low20) / last_close * 100

    breakout_up = last_close > prior_high20
    breakout_down = last_close < prior_low20
    near_breakout = (0 <= dist_to_high_pct <= 2) or breakout_up

    ret_5d = (close.iloc[-1] / close.iloc[-6] - 1) * 100 if len(close) > 6 else np.nan
    ret_20d = (close.iloc[-1] / close.iloc[-21] - 1) * 100 if len(close) > 21 else np.nan

    day_range_pct = (last["High"] - last["Low"]) / last_close * 100

    trend_up = last_close > sma20 > sma50 if pd.notna(sma20) and pd.notna(sma50) else False
    trend_down = last_close < sma20 < sma50 if pd.notna(sma20) and pd.notna(sma50) else False

    return {
        "symbol": symbol.replace(".NS", ""),
        "last_close": round(float(last_close), 2),
        "atr_pct": round(float(atr_pct), 2) if pd.notna(atr_pct) else None,
        "day_range_pct": round(float(day_range_pct), 2),
        "vol_surge_x": round(float(vol_surge), 2) if pd.notna(vol_surge) else None,
        "vol_today": int(vol_today),
        "vol_avg20": int(vol_avg20) if pd.notna(vol_avg20) else None,
        "rsi14": round(float(rsi14), 1) if pd.notna(rsi14) else None,
        "sma20": round(float(sma20), 2) if pd.notna(sma20) else None,
        "sma50": round(float(sma50), 2) if pd.notna(sma50) else None,
        "trend_up": bool(trend_up),
        "trend_down": bool(trend_down),
        "ret_5d_pct": round(float(ret_5d), 2) if pd.notna(ret_5d) else None,
        "ret_20d_pct": round(float(ret_20d), 2) if pd.notna(ret_20d) else None,
        "dist_to_20d_high_pct": round(float(dist_to_high_pct), 2),
        "dist_above_20d_low_pct": round(float(dist_to_low_pct), 2),
        "breakout_up_today": bool(breakout_up),
        "breakout_down_today": bool(breakout_down),
        "near_breakout": bool(near_breakout),
    }


def main():
    symbols = load_symbols()
    print(f"Downloading {len(symbols)} symbols...")

    results = []
    batch_size = 25
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i : i + batch_size]
        try:
            data = yf.download(
                batch,
                period=LOOKBACK,
                interval=INTERVAL,
                group_by="ticker",
                threads=True,
                progress=False,
                auto_adjust=True,
            )
        except Exception as e:
            print(f"batch {i} failed: {e}")
            continue

        for sym in batch:
            try:
                if len(batch) == 1:
                    df = data
                else:
                    df = data[sym]
                res = analyze_one(sym, df)
                if res:
                    results.append(res)
            except Exception:
                continue
        print(f"  done {min(i+batch_size, len(symbols))}/{len(symbols)}")
        time.sleep(0.5)

    full = pd.DataFrame(results)
    full.to_csv(OUT_DIR / f"{OUT_PREFIX}full_screen.csv", index=False)
    print(f"Analyzed {len(full)} stocks. Saved output/{OUT_PREFIX}full_screen.csv")

    # Liquidity floor: avoid illiquid names for intraday
    liquid = full[(full["vol_avg20"].fillna(0) > 200000)].copy()

    # --- Intraday candidates (tomorrow) ---
    # High volatility + volume surge + near/at a breakout level = likely to move with volume
    intraday = liquid.copy()
    intraday["score"] = (
        intraday["atr_pct"].fillna(0) * 1.0
        + intraday["vol_surge_x"].fillna(1) * 8.0
        + intraday["near_breakout"].astype(int) * 10
        + intraday["breakout_up_today"].astype(int) * 8
    )
    intraday = intraday.sort_values("score", ascending=False).head(25)
    intraday.to_csv(OUT_DIR / f"{OUT_PREFIX}intraday_candidates.csv", index=False)

    # --- Swing candidates (week-ish hold) ---
    # Confirmed uptrend + recent breakout + positive momentum, not overbought (RSI < 75)
    swing = liquid.copy()
    swing = swing[(swing["trend_up"] == True) & (swing["rsi14"] < 75)]
    swing["score"] = (
        swing["ret_20d_pct"].fillna(0) * 1.0
        + swing["ret_5d_pct"].fillna(0) * 1.5
        + swing["breakout_up_today"].astype(int) * 15
        + swing["near_breakout"].astype(int) * 8
        + swing["vol_surge_x"].fillna(1) * 3.0
    )
    swing = swing.sort_values("score", ascending=False).head(25)
    swing.to_csv(OUT_DIR / f"{OUT_PREFIX}swing_candidates.csv", index=False)

    # --- Bearish / sell-avoid candidates ---
    # Breakdown below 20d low today, or confirmed downtrend with volume backing
    bearish = liquid.copy()
    bearish = bearish[
        (bearish["breakout_down_today"] == True)
        | ((bearish["trend_down"] == True) & (bearish["vol_surge_x"].fillna(0) > 1.2))
    ]
    bearish["score"] = (
        bearish["atr_pct"].fillna(0) * 1.0
        + bearish["vol_surge_x"].fillna(1) * 8.0
        + bearish["breakout_down_today"].astype(int) * 10
    )
    bearish = bearish.sort_values("score", ascending=False).head(25)
    bearish.to_csv(OUT_DIR / f"{OUT_PREFIX}bearish_candidates.csv", index=False)

    print("\n=== TOP INTRADAY CANDIDATES ===")
    print(intraday[["symbol", "last_close", "atr_pct", "vol_surge_x", "near_breakout", "breakout_up_today"]].to_string(index=False))

    print("\n=== TOP SWING (WEEK) CANDIDATES ===")
    print(swing[["symbol", "last_close", "ret_5d_pct", "ret_20d_pct", "rsi14", "breakout_up_today"]].to_string(index=False))

    print("\n=== TOP BEARISH / SELL-AVOID CANDIDATES ===")
    print(bearish[["symbol", "last_close", "atr_pct", "vol_surge_x", "rsi14", "ret_5d_pct", "breakout_down_today"]].to_string(index=False))


if __name__ == "__main__":
    main()
