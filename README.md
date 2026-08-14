# NSE Trade Screener

On-demand technical screener for NSE Large (Nifty 100), Mid (Nifty Midcap 150),
and Small (Nifty Smallcap 250) cap stocks. Pulls fresh OHLCV via yfinance,
computes ATR/RSI/volume-surge/breakout signals, and classifies every liquid
stock into a trade bucket with a stop-loss and target.

## Setup (one-time)

```bash
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install yfinance pandas numpy requests
```

## Run

```bash
./.venv/bin/python scripts/trade_screener.py
```

Options:

```
--refresh-lists   force re-download of index constituent lists (cached 7 days otherwise)
--segments        comma-separated subset: large,mid,small (default: all three)
--top N           rows to show per category (default 15)
--no-html         skip generating output/trade_setup.html
```

## Output (in `output/`, regenerated each run)

- `master_full_screen.csv` — every stock, every computed metric
- `master_buy_core.csv` — breakout confirmed, RSI < 75, liquid
- `master_buy_caution.csv` — breakout confirmed but overbought (RSI >= 75)
- `master_sell_core.csv` — fresh breakdown, not yet deeply oversold
- `master_sell_extended.csv` — breakdown but already crashed (bounce risk)
- `master_watch.csv` — high volume surge, no confirmed direction — check news
- `trade_setup.html` — self-contained report, open directly in a browser

## Notes

- Not investment advice — a mechanical technical screen only. Verify news and
  live quotes before trading; the script's data reflects the last close it
  could download, not necessarily the current live price.
- Liquidity floor: 20-day avg volume > 200,000 shares.
- Stops/targets: 1.5x / 2.5x ATR(14) from the last close.
