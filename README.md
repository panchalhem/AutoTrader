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

## Licensed desktop platform (automation/, license_server/, desktop/)

The live trading automation (`automation/`) can also run as a license-gated
desktop app you distribute to clients, with a central admin panel you use to
issue/revoke licenses. Three pieces:

- **`automation/`** — the trading engine (unchanged behavior), plus a FastAPI
  service (`automation/api/`) and a `BrokerAdapter` interface
  (`automation/broker_adapter.py`, implemented by
  `automation/adapters/zerodha_adapter.py` and `capital_adapter.py`) so new
  brokers can be added without touching strategy code.
- **`license_server/`** — you run this centrally. Issues RS256-signed JWT
  licenses, tracks clients/revocation in SQLite, and serves an admin panel.
- **`desktop/`** — an Electron shell each client installs. Locks behind a
  license key (verified offline against the bundled public key) before
  spawning the local Python dashboard and displaying it.

### Run the license server (you, centrally)

```bash
.venv/bin/uvicorn license_server.server:app --host 0.0.0.0 --port 8790
```

First run prints/caches an admin password at `license_server/data/.admin_password`
(or set `LICENSE_ADMIN_PASSWORD`). Open `http://<host>:8790/admin/panel`
(HTTP Basic Auth, user `admin`) to create clients and issue license tokens.
`license_server/keys/license_public_key.pem` is generated on first use —
ship that file inside every desktop build (it's what lets the app verify
licenses offline); the paired private key never leaves this server.

### Run the desktop app (each client)

```bash
cd desktop && npm install
npm start
```

First launch shows an activation screen — paste the license token you issued
from the admin panel. Once activated it's cached in the OS user-data
directory and skips the prompt on future launches. The app then spawns
`automation/dashboard.py` itself and displays it, auto-authenticating with
the same Basic Auth the dashboard already uses.

### Building a double-click installer (.exe / .dmg)

The desktop app now packages into a real installer with an embedded Python
backend — the end user does **not** need Python, Node, or this repo. The
backend (`automation/dashboard.py` + its dependencies) is frozen with
PyInstaller into a self-contained executable, then `electron-builder` wraps
it into an NSIS installer (Windows) or a `.dmg` (Mac). The Windows installer
prompts for an install location (not one-click, not admin-elevated — a
per-user install into a folder you choose).

**Easiest path — let GitHub build both automatically:** push a version tag
and `.github/workflows/build-desktop.yml` builds the Windows `.exe` and Mac
`.dmg` on GitHub's own Windows/Mac runners and attaches them to a GitHub
Release:

```bash
git tag v1.0.0
git push origin v1.0.0
```

Then download the installer straight from the repo's Releases page — no
local Windows or Mac machine needed. You can also trigger a test build
without tagging from the Actions tab ("Build desktop installers" →
"Run workflow").

**Building locally instead**, e.g. on your own Windows laptop:

```powershell
cd desktop
powershell -ExecutionPolicy Bypass -File build-backend.ps1   # freezes dashboard.py -> backend/win/
npm install
npm run dist:win                                              # -> desktop/dist/*.exe
```

And on a Mac:

```bash
cd desktop
./build-backend.sh          # freezes dashboard.py -> backend/mac/
npm install
npm run dist:mac             # -> desktop/dist/*.dmg
```

Both require Python 3.11+ on the *build* machine only (used to create a
throwaway venv and run PyInstaller); the resulting installer needs nothing
on the end user's machine beyond the OS itself.

### Notes / what's not built yet

- **Control Panel (start/stop/restart/discover) doesn't work from the
  packaged installer.** Those buttons wrap `automation/stock.sh`, a bash
  script — it isn't bundled (a shell script can't be frozen into the
  Windows/Mac backend exe the same way Python code can) and doesn't run
  natively on Windows. Clicking them in a packaged build now returns a clear
  error explaining this, instead of failing silently. The live
  positions/P&L monitoring view (the main reason to use the desktop app)
  works fully in the packaged build — only the Control Panel's
  start/stop/restart/discover actions are source-checkout-only, run those
  from a terminal (Linux/Mac, or WSL on Windows) instead.
- License enforcement currently lives in the desktop shell only (blocks
  reaching the dashboard); the API/dashboard themselves don't independently
  check license status yet — a real deployment should add that as
  defense-in-depth before relying on it commercially.
- Whether offering this as a paid auto-trading product requires
  algo-trading/advisory registration in your target jurisdictions is a legal
  question, not a code one — get that reviewed before selling live-trading
  access to real clients.
# AutoTrader
