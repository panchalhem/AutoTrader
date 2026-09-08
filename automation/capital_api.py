#!/usr/bin/env python3
"""
Capital.com REST API client — authentication + market lookup/prices.

SECURITY NOTE: credentials are never hardcoded, never passed as CLI args
(args are visible in `ps aux` and shell history), and this script never
prints them. They're read from environment variables, optionally loaded
from automation/.env (gitignored — see automation/.env.example for the
template). Fill in .env yourself; there's no need to show Claude the
values — only the connectivity test's pass/fail output matters.

Capital.com does NOT authenticate with your regular account login password.
You must first generate a dedicated API key:
    Capital.com app -> Settings -> API integrations -> Generate new key
    (requires 2FA on your account)
    You set a custom password for that key at creation time — THAT
    password (not your account password) is what CAPITAL_API_PASSWORD is.
    The key itself is only ever shown once, at creation — copy it then.

Required env vars (put these in automation/.env):
    CAPITAL_API_KEY        the generated API key
    CAPITAL_API_PASSWORD   the custom password you set for that key
    CAPITAL_IDENTIFIER     your Capital.com account login (usually email)
Optional:
    CAPITAL_ENV             "demo" (default, recommended while testing) or "live"

Usage:
    ./.venv/bin/python automation/capital_api.py --test
    ./.venv/bin/python automation/capital_api.py --search "gold"
    ./.venv/bin/python automation/capital_api.py --search "nikkei"

Rate limits (per Capital.com's docs): 1 login/sec, 10 general requests/sec
per user, session tokens expire after 10 minutes of inactivity — this
client re-logs-in automatically on a 401.
"""
import argparse
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"

DEMO_BASE = "https://demo-api-capital.backend-capital.com"
LIVE_BASE = "https://api-capital.backend-capital.com"


def _load_env_file():
    """Minimal KEY=VALUE .env loader (no external dependency). An explicit
    `export` in your shell always wins over the file."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def credentials():
    _load_env_file()
    api_key = os.environ.get("CAPITAL_API_KEY")
    password = os.environ.get("CAPITAL_API_PASSWORD")
    identifier = os.environ.get("CAPITAL_IDENTIFIER")
    env = os.environ.get("CAPITAL_ENV", "demo").lower()
    missing = [n for n, v in [("CAPITAL_API_KEY", api_key), ("CAPITAL_API_PASSWORD", password),
                               ("CAPITAL_IDENTIFIER", identifier)] if not v]
    if missing:
        raise RuntimeError(
            f"Missing Capital.com credentials: {', '.join(missing)}. "
            f"Copy automation/.env.example to automation/.env and fill them in."
        )
    still_encrypted = [n for n, v in [("CAPITAL_API_KEY", api_key), ("CAPITAL_API_PASSWORD", password),
                                       ("CAPITAL_IDENTIFIER", identifier)] if v.startswith("encrypted:")]
    if still_encrypted:
        raise RuntimeError(
            f"{', '.join(still_encrypted)} still encrypted (automation/.env is encrypted at rest) — "
            f"launch through automation/with_env.sh (or stock.sh, which already does this) instead of "
            f"running python directly, so dotenvx can decrypt it first."
        )
    base = LIVE_BASE if env == "live" else DEMO_BASE
    return api_key, identifier, password, base


class CapitalSession:
    """An authenticated Capital.com session. Tokens expire after 10 minutes
    of inactivity, so callers should go through ensure_login()/the request
    methods here (which auto-retry once on a 401) rather than logging in
    once and holding the tokens indefinitely."""

    def __init__(self):
        self.api_key, self.identifier, self.password, self.base = credentials()
        self.cst = None
        self.security_token = None

    def login(self):
        r = requests.post(
            f"{self.base}/api/v1/session",
            headers={"X-CAP-API-KEY": self.api_key, "Content-Type": "application/json"},
            json={"identifier": self.identifier, "password": self.password},
            timeout=15,
        )
        if r.status_code != 200:
            try:
                detail = r.json().get("errorCode", r.text)
            except Exception:
                detail = r.text
            raise RuntimeError(f"Capital.com login failed: HTTP {r.status_code} — {str(detail)[:200]}")
        self.cst = r.headers["CST"]
        self.security_token = r.headers["X-SECURITY-TOKEN"]

    def ensure_login(self):
        if not self.cst or not self.security_token:
            self.login()

    def _headers(self):
        return {"X-CAP-API-KEY": self.api_key, "CST": self.cst, "X-SECURITY-TOKEN": self.security_token}

    def _get(self, path, params=None):
        self.ensure_login()
        r = requests.get(f"{self.base}{path}", headers=self._headers(), params=params, timeout=15)
        if r.status_code == 401:
            self.login()
            r = requests.get(f"{self.base}{path}", headers=self._headers(), params=params, timeout=15)
        r.raise_for_status()
        return r.json()

    def _post(self, path, body):
        self.ensure_login()
        r = requests.post(f"{self.base}{path}", headers=self._headers(), json=body, timeout=15)
        if r.status_code == 401:
            self.login()
            r = requests.post(f"{self.base}{path}", headers=self._headers(), json=body, timeout=15)
        if r.status_code not in (200, 201):
            try:
                detail = r.json()
            except Exception:
                detail = r.text
            raise RuntimeError(f"Capital.com POST {path} failed: HTTP {r.status_code} — {str(detail)[:300]}")
        return r.json()

    def search_markets(self, search_term):
        return self._get("/api/v1/markets", params={"searchTerm": search_term}).get("markets", [])

    def market_details(self, epic):
        return self._get(f"/api/v1/markets/{epic}")

    def prices(self, epic, resolution="MINUTE_5", max_points=200):
        return self._get(f"/api/v1/prices/{epic}", params={"resolution": resolution, "max": max_points})

    def accounts(self):
        return self._get("/api/v1/accounts").get("accounts", [])

    def positions(self):
        return self._get("/api/v1/positions").get("positions", [])

    def create_position(self, epic, direction, size, stop_level=None, profit_level=None):
        """direction: 'BUY' or 'SELL'. stop_level/profit_level are absolute
        prices (Capital.com also supports distance-based stops, not used
        here — we always compute an absolute level from our own ATR math
        so the risk math stays in one place, not split across two systems).
        Returns {"dealReference": ...} — position creation is async on
        Capital.com's side; call confirm_deal() with that reference to get
        the final status and the dealId needed to close it later."""
        body = {"epic": epic, "direction": direction, "size": size}
        if stop_level is not None:
            body["stopLevel"] = stop_level
        if profit_level is not None:
            body["profitLevel"] = profit_level
        return self._post("/api/v1/positions", body)

    def confirm_deal(self, deal_reference):
        """Resolves a dealReference (from create_position) to its final
        status and dealId. Capital.com's position-open call is
        fire-and-confirm, not fire-and-forget: the initial POST only
        acknowledges the request, this is what tells you whether it
        actually filled and what to call close_position() with later."""
        return self._get(f"/api/v1/confirms/{deal_reference}")

    def _put(self, path, body):
        self.ensure_login()
        r = requests.put(f"{self.base}{path}", headers=self._headers(), json=body, timeout=15)
        if r.status_code == 401:
            self.login()
            r = requests.put(f"{self.base}{path}", headers=self._headers(), json=body, timeout=15)
        if r.status_code not in (200, 201):
            try:
                detail = r.json()
            except Exception:
                detail = r.text
            raise RuntimeError(f"Capital.com PUT {path} failed: HTTP {r.status_code} — {str(detail)[:300]}")
        return r.json()

    def update_position(self, deal_id, stop_level=None, profit_level=None):
        """Amend stopLevel/profitLevel on an already-open position (absolute
        price levels, same convention as create_position)."""
        body = {}
        if stop_level is not None:
            body["stopLevel"] = stop_level
        if profit_level is not None:
            body["profitLevel"] = profit_level
        return self._put(f"/api/v1/positions/{deal_id}", body)

    def close_position(self, deal_id):
        self.ensure_login()
        r = requests.delete(f"{self.base}/api/v1/positions/{deal_id}", headers=self._headers(), timeout=15)
        if r.status_code == 401:
            self.login()
            r = requests.delete(f"{self.base}/api/v1/positions/{deal_id}", headers=self._headers(), timeout=15)
        r.raise_for_status()
        return r.json()


def main():
    p = argparse.ArgumentParser(description="Capital.com API connectivity test / market search")
    p.add_argument("--test", action="store_true", help="Just verify login works")
    p.add_argument("--search", help="Search markets by name (e.g. 'gold', 'nikkei', 'apple')")
    p.add_argument("--env", choices=["demo", "live"], default=None,
                    help="Override CAPITAL_ENV from automation/.env for this run")
    args = p.parse_args()
    if args.env:
        os.environ["CAPITAL_ENV"] = args.env

    try:
        sess = CapitalSession()
        sess.login()
    except Exception as e:
        print(f"FAILED: {e}", file=sys.stderr)
        sys.exit(1)

    masked = sess.identifier[:3] + "***" if sess.identifier else "***"
    print(f"Login OK — authenticated against {sess.base} as {masked} ({os.environ.get('CAPITAL_ENV', 'demo')} env)")

    if args.search:
        markets = sess.search_markets(args.search)
        print(f"\n{len(markets)} result(s) for '{args.search}':")
        for m in markets[:20]:
            print(f"  epic={m.get('epic'):16s} name={m.get('instrumentName')}  type={m.get('instrumentType')}  "
                  f"bid={m.get('bid')} offer={m.get('offer')}")


if __name__ == "__main__":
    main()
