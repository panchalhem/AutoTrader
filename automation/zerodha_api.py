#!/usr/bin/env python3
"""
Zerodha Kite Connect API client — automated daily login + order/GTT
placement. No official kiteconnect SDK dependency; talks to the REST API
directly with `requests`, and drives the official web login flow with a
headless browser (Playwright) since Kite Connect access tokens expire
every day at 6am (regulatory requirement) and there's no other supported
way to obtain a fresh one without a human clicking through a login page.

SECURITY NOTE: this needs your actual Zerodha login password and TOTP
secret (not just a revocable API key) to automate the daily login — that's
inherently more sensitive than the Capital.com credentials. Read
automation/.env.example before filling in automation/.env. Never pasted
in chat, never logged, never printed.

Auth flow (per Kite Connect docs):
    1. Open https://kite.zerodha.com/connect/login?v=3&api_key=...
    2. Fill user_id + password, submit.
    3. Fill TOTP (generated locally from ZERODHA_TOTP_SECRET via pyotp —
       no code ever leaves this machine except the 6-digit value into the
       Zerodha login form itself), submit.
    4. Zerodha redirects to the app's registered redirect URL with a
       one-time request_token in the query string.
    5. Exchange request_token for access_token: POST /session/token with
       checksum = sha256(api_key + request_token + api_secret).
    6. access_token is valid until ~6am the next day; this whole flow
       re-runs automatically whenever a call gets a 403/session-expired.

THE LOGIN-FORM SELECTORS BELOW ARE UNVERIFIED against a live account (no
credentials were available in the environment that wrote this) — they
follow Zerodha's Kite login page as documented/observed across public
community Kite Connect automation scripts, but Zerodha can change their
page at any time. If automated_login() fails, run with --debug-login to
see it (screenshot + non-headless) and fix the selectors.

Usage:
    ./.venv/bin/python automation/zerodha_api.py --test
    ./.venv/bin/python automation/zerodha_api.py --test --debug-login
"""
import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pyotp
import requests

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
OUT_DIR = ROOT / "output"
OUT_DIR.mkdir(exist_ok=True)
TOKEN_CACHE_FILE = OUT_DIR / "zerodha_session.json"
IST = ZoneInfo("Asia/Kolkata")

BASE = "https://api.kite.trade"
LOGIN_URL = "https://kite.zerodha.com/connect/login"

NSE_TICK_SIZE = 0.05  # fallback only — see _tick_size_for() below, tick size is NOT uniform across NSE
DATA_DIR = ROOT / "data"
KITE_INSTRUMENTS_CACHE = DATA_DIR / "kite_instruments.csv"  # same file trade_monitor.py maintains

_tick_size_cache = None  # lazy-loaded {tradingsymbol: tick_size}, built once per process


def _load_tick_sizes():
    global _tick_size_cache
    if _tick_size_cache is not None:
        return _tick_size_cache
    _tick_size_cache = {}
    if KITE_INSTRUMENTS_CACHE.exists():
        try:
            import csv as _csv
            with open(KITE_INSTRUMENTS_CACHE, newline="") as f:
                for row in _csv.DictReader(f):
                    if row.get("exchange") == "NSE" and row.get("instrument_type") == "EQ":
                        try:
                            _tick_size_cache[row["tradingsymbol"]] = float(row["tick_size"])
                        except (KeyError, ValueError):
                            pass
        except Exception:
            _tick_size_cache = {}
    return _tick_size_cache


def _tick_size_for(tradingsymbol):
    """NSE tick size is NOT a uniform ₹0.05 — confirmed 2026-08-26 when CIPLA
    rejected a GTT with "Stoploss trigger price should be a multiple of tick
    size 0.10" even after tick-rounding was added, because CIPLA's real tick
    is ₹0.10, not ₹0.05. Cross-checked against kite_instruments.csv: most of
    this universe is 0.05 (ANGELONE, HINDALCO, KOTAKBANK, ...), but CIPLA/
    BSE/NESTLEIND/TATACONSUM/MAXHEALTH are 0.10, and INDIGO is 0.50 — a flat
    assumption silently mis-rounds roughly a third of this universe's
    symbols. Reads the tick size Zerodha itself publishes per-symbol,
    falling back to 0.05 only if the symbol or the instrument cache file
    isn't available."""
    return _load_tick_sizes().get(tradingsymbol, NSE_TICK_SIZE)


def round_to_tick(price, tradingsymbol=None, tick=None):
    """Round to the nearest valid NSE tick for this specific symbol. Prices
    computed from ATR math (trade_monitor.py) come out to arbitrary
    precision (e.g. 5020.8481) — the exchange rejects anything not a
    multiple of the symbol's own tick size."""
    if price is None:
        return None
    if tick is None:
        tick = _tick_size_for(tradingsymbol) if tradingsymbol else NSE_TICK_SIZE
    return round(round(price / tick) * tick, 2)


def _token_expiry_utc(obtained_at_utc):
    """Kite access tokens expire at 6am IST on the day AFTER they were
    issued (regulatory requirement, not configurable)."""
    obtained_ist = obtained_at_utc.astimezone(IST)
    expiry_date = obtained_ist.date() + timedelta(days=1)
    expiry_ist = datetime(expiry_date.year, expiry_date.month, expiry_date.day, 6, 0, 0, tzinfo=IST)
    return expiry_ist.astimezone(timezone.utc)


def _load_env_file():
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
    fields = {
        "api_key": os.environ.get("ZERODHA_API_KEY"),
        "api_secret": os.environ.get("ZERODHA_API_SECRET"),
        "user_id": os.environ.get("ZERODHA_USER_ID"),
        "password": os.environ.get("ZERODHA_PASSWORD"),
        "totp_secret": os.environ.get("ZERODHA_TOTP_SECRET"),
    }
    missing = [k for k, v in fields.items() if not v]
    if missing:
        raise RuntimeError(
            f"Missing Zerodha credentials: {', '.join(missing)}. "
            f"Copy automation/.env.example to automation/.env and fill them in."
        )
    still_encrypted = [k for k, v in fields.items() if v.startswith("encrypted:")]
    if still_encrypted:
        raise RuntimeError(
            f"{', '.join(still_encrypted)} still encrypted (automation/.env is encrypted at rest) — "
            f"launch through automation/with_env.sh (or stock.sh, which already does this) instead of "
            f"running python directly, so dotenvx can decrypt it first."
        )
    return fields


def automated_login(api_key, user_id, password, totp_secret, debug=False):
    """Drives the official Kite login page headlessly and returns the
    request_token. Raises with a screenshot path on failure so you can see
    exactly what the page looked like.

    The registered Redirect URL (e.g. https://127.0.0.1) is a placeholder —
    nothing real is listening there. ignore_https_errors=True on the
    browser context lets that final navigation complete (or at least
    resolve far enough to update the address bar) instead of throwing a
    cert/connection error before we ever see the token — confirmed working:
    page.url reliably ends up as the full redirect URL with request_token
    in it once login+TOTP succeed."""
    from playwright.sync_api import sync_playwright

    totp_code = pyotp.TOTP(totp_secret).now()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not debug)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        try:
            page.goto(f"{LOGIN_URL}?v=3&api_key={api_key}", timeout=30000)

            page.wait_for_selector("#userid", timeout=15000)
            page.fill("#userid", user_id)
            page.fill("#password", password)
            page.click('button[type="submit"]', force=True)

            # TOTP step: rather than guess the exact selector Zerodha's 2FA
            # page uses (seen in practice: a dot-masked, likely
            # type="password" field that a plain text/number guess misses),
            # try every visible input on the page and verify the value
            # actually stuck (read it back) before trusting it — this is
            # resilient to whatever the real markup turns out to be.
            page.wait_for_timeout(1500)  # let the 2FA page finish rendering
            candidates = page.locator("input:visible")
            totp_filled = False
            for i in range(candidates.count()):
                inp = candidates.nth(i)
                try:
                    inp.fill(totp_code)
                    if inp.input_value() == totp_code:
                        totp_filled = True
                        break
                except Exception:
                    continue
            if not totp_filled:
                raise RuntimeError("Could not find/fill the TOTP input field on Zerodha's 2FA page "
                                    "(tried every visible <input> on the page)")
            page.click('button[type="submit"]', force=True)

            page.wait_for_url("**request_token=**", timeout=15000)
            url = page.url
        except Exception as e:
            shot = OUT_DIR / f"zerodha_login_failure_{int(time.time())}.png"
            try:
                page.screenshot(path=str(shot))
            except Exception:
                shot = None
            browser.close()
            raise RuntimeError(
                f"Zerodha automated login failed: {e}. "
                + (f"Screenshot saved to {shot} — inspect it and adjust the selectors in "
                   f"zerodha_api.py:automated_login()." if shot else "")
            )
        browser.close()

    if "request_token=" not in url:
        raise RuntimeError(f"Login flow completed but no request_token was found in the redirect URL: {url}")
    request_token = url.split("request_token=")[1].split("&")[0]
    return request_token


class KiteSession:
    def __init__(self, debug_login=False):
        creds = credentials()
        self.api_key = creds["api_key"]
        self.api_secret = creds["api_secret"]
        self.user_id = creds["user_id"]
        self.password = creds["password"]
        self.totp_secret = creds["totp_secret"]
        self.debug_login = debug_login
        self.access_token = None
        self._load_cached_token()

    def _load_cached_token(self):
        """Kite access tokens are valid until 6am IST the next day —
        reload one from disk if it's still within that window instead of
        driving the browser login again. This is what makes it safe to
        create a fresh KiteSession() per process (e.g. across stock.sh
        restarts) without hammering Zerodha's login page every time."""
        if not TOKEN_CACHE_FILE.exists():
            return
        try:
            cached = json.loads(TOKEN_CACHE_FILE.read_text())
            if cached.get("api_key") != self.api_key:
                return
            expiry = datetime.fromisoformat(cached["expiry_utc"])
            if datetime.now(timezone.utc) < expiry:
                self.access_token = cached["access_token"]
        except Exception:
            pass  # any corruption/format issue -> just fall back to a real login

    def _save_cached_token(self, obtained_at_utc):
        TOKEN_CACHE_FILE.write_text(json.dumps({
            "api_key": self.api_key,
            "access_token": self.access_token,
            "obtained_at_utc": obtained_at_utc.isoformat(),
            "expiry_utc": _token_expiry_utc(obtained_at_utc).isoformat(),
        }, indent=2))

    def login(self):
        request_token = automated_login(self.api_key, self.user_id, self.password,
                                          self.totp_secret, debug=self.debug_login)
        checksum = hashlib.sha256((self.api_key + request_token + self.api_secret).encode()).hexdigest()
        r = requests.post(f"{BASE}/session/token",
                           data={"api_key": self.api_key, "request_token": request_token, "checksum": checksum},
                           timeout=15)
        if r.status_code != 200:
            raise RuntimeError(f"Zerodha token exchange failed: HTTP {r.status_code} — {r.text[:300]}")
        self.access_token = r.json()["data"]["access_token"]
        self._save_cached_token(datetime.now(timezone.utc))

    def ensure_login(self):
        if not self.access_token:
            self.login()

    def _headers(self):
        return {"Authorization": f"token {self.api_key}:{self.access_token}",
                "X-Kite-Version": "3"}

    def _request(self, method, path, **kwargs):
        self.ensure_login()
        r = requests.request(method, f"{BASE}{path}", headers=self._headers(), timeout=15, **kwargs)
        if r.status_code in (401, 403):
            self.login()
            r = requests.request(method, f"{BASE}{path}", headers=self._headers(), timeout=15, **kwargs)
        if r.status_code >= 400:
            try:
                detail = r.json()
            except Exception:
                detail = r.text
            raise RuntimeError(f"Zerodha {method} {path} failed: HTTP {r.status_code} — {str(detail)[:300]}")
        return r.json().get("data")

    def margins(self):
        return self._request("GET", "/user/margins")

    def positions(self):
        return self._request("GET", "/portfolio/positions")

    def order_history(self, order_id):
        """Returns the list of status transitions for one order — the last
        entry is its current state (COMPLETE / REJECTED / CANCELLED / OPEN / ...)."""
        return self._request("GET", f"/orders/{order_id}")

    def orders(self):
        """Today's full order book (one entry per order, latest status only).
        place_order() tags every automated order "autotrader" — any order
        here with a different tag (or none) was placed some other way, e.g.
        manually in Kite's own app. This is the only reliable signal this
        codebase has for telling automated and manual orders apart."""
        return self._request("GET", "/orders")

    def place_order(self, exchange, tradingsymbol, transaction_type, quantity,
                     order_type="MARKET", product="MIS", price=None, trigger_price=None,
                     variety="regular", tag="autotrader"):
        body = {
            "exchange": exchange, "tradingsymbol": tradingsymbol,
            "transaction_type": transaction_type, "quantity": quantity,
            "order_type": order_type, "product": product, "validity": "DAY", "tag": tag,
        }
        if price is not None:
            body["price"] = round_to_tick(price, tradingsymbol)
        if trigger_price is not None:
            body["trigger_price"] = round_to_tick(trigger_price, tradingsymbol)
        if order_type in ("MARKET", "SL-M"):
            # Exchange-mandated as of 2026: a MARKET/SL-M order placed via the
            # API without this is rejected outright ("Market orders without
            # market protection are not allowed via API"). -1 = let the
            # system apply the standard exchange-default protection band,
            # same as what Kite's own UI applies automatically — not a
            # hand-picked percentage, so it can't drift from the exchange's
            # own default.
            body["market_protection"] = -1
        return self._request("POST", f"/orders/{variety}", data=body)

    def place_gtt_oco(self, exchange, tradingsymbol, last_price, quantity,
                       exit_transaction_type, stop_price, target_price, product="MIS"):
        """Two-leg GTT: whichever of stop_price/target_price is hit first
        fires that leg and cancels the other (Zerodha's documented OCO
        mechanism).

        All four prices are rounded to THIS SYMBOL's own NSE tick size before
        being sent — trade_monitor's ATR-derived stop/target come out to
        arbitrary precision (e.g. 5020.8481, 1400.0755), which Zerodha
        rejects outright as an invalid exchange price. Tick size is NOT a
        uniform ₹0.05 across NSE (see _tick_size_for) — CIPLA is 0.10,
        INDIGO is 0.50 — so this must be looked up per tradingsymbol, not
        hardcoded.

        THE ACTUAL, DOMINANT BUG (found 2026-08-25 after ₹0.05-tick-rounding
        alone didn't stop the failures — INDIGO, CIPLA (x3), and ANGELONE all
        still failed post-fix): Kite Connect's /gtt/triggers endpoint is
        FORM-ENCODED, with `condition`/`orders`/`type` as top-level form
        fields where `condition` and `orders` are themselves JSON-stringified
        — not a nested JSON request body (unlike /orders/*, which really is
        form data with flat scalar fields, hence no bug there). Sending
        `json=body` posted a raw `application/json` body Kite's form parser
        never read `condition`/`orders` out of, so EVERY GTT attempt failed
        with "Invalid trigger data" regardless of price. Net effect until
        that fix: no Zerodha entry ever actually got stop/target protection;
        each failure triggered the emergency-flatten fallback (an immediate
        opposite MARKET order), which is what looked like "the stop-loss is
        way too tight" (e.g. ANGELONE entry 300.85 flattened at 300.65 —
        that gap is just fill slippage on the panic-exit, not a configured
        stop distance; the real computed stop was 287.15, never placed).

        Then (2026-08-26) CIPLA failed AGAIN post-fix, this time with a
        clearer error — "Stoploss trigger price should be a multiple of tick
        size 0.10" — revealing the flat ₹0.05 assumption was itself wrong
        for CIPLA specifically, hence the per-symbol lookup here now."""
        stop_price, target_price, last_price = (
            round_to_tick(stop_price, tradingsymbol), round_to_tick(target_price, tradingsymbol),
            round_to_tick(last_price, tradingsymbol))
        condition = {
            "exchange": exchange, "tradingsymbol": tradingsymbol,
            "trigger_values": sorted([stop_price, target_price]),
            "last_price": last_price,
        }
        orders = [
            {"exchange": exchange, "tradingsymbol": tradingsymbol,
             "transaction_type": exit_transaction_type, "quantity": quantity,
             "order_type": "LIMIT", "product": product, "price": stop_price},
            {"exchange": exchange, "tradingsymbol": tradingsymbol,
             "transaction_type": exit_transaction_type, "quantity": quantity,
             "order_type": "LIMIT", "product": product, "price": target_price},
        ]
        body = {"type": "two-leg", "condition": json.dumps(condition), "orders": json.dumps(orders)}
        return self._request("POST", "/gtt/triggers", data=body)

    def gtt_triggers(self):
        """All GTT triggers (active + recent). Used to find the stop/target
        levels associated with an open position — the position object itself
        doesn't carry them, they live on the separate GTT order."""
        return self._request("GET", "/gtt/triggers")

    def delete_gtt(self, trigger_id):
        """Cancels a GTT trigger — used to replace a position's stop/target
        (e.g. moving the stop to breakeven) since Kite has no in-place GTT
        modify; the caller deletes the old trigger then places a new one via
        place_gtt_oco."""
        return self._request("DELETE", f"/gtt/triggers/{trigger_id}")

    def cancel_gtt(self, trigger_id):
        self.ensure_login()
        r = requests.delete(f"{BASE}/gtt/triggers/{trigger_id}", headers=self._headers(), timeout=15)
        if r.status_code == 401:
            self.login()
            r = requests.delete(f"{BASE}/gtt/triggers/{trigger_id}", headers=self._headers(), timeout=15)
        r.raise_for_status()
        return r.json().get("data")


def main():
    p = argparse.ArgumentParser(description="Zerodha Kite Connect connectivity test")
    p.add_argument("--test", action="store_true")
    p.add_argument("--debug-login", action="store_true",
                    help="Run the login browser non-headless and keep a screenshot on failure — use this to fix selectors")
    args = p.parse_args()

    try:
        sess = KiteSession(debug_login=args.debug_login)
        sess.login()
    except Exception as e:
        print(f"FAILED: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Login OK — authenticated as {sess.user_id[:3]}***")
    if args.test:
        try:
            m = sess.margins()
            print(f"Equity margin net: {m.get('equity', {}).get('net')}")
        except Exception as e:
            print(f"Margins check failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
