#!/usr/bin/env python3
"""
trading_settings.py — the single dashboard-editable source of truth for
every numeric/boolean trading parameter that used to require a code edit
(and an AI session) to change: capital split, per-trade risk, the daily
loss cap, backtest-gate thresholds, and which instrument categories are
in the universe. Added 2026-09-03 (user request), mirrors
strategy_config.py's exact shape on purpose — one JSON file, get/set/status
functions, a CLI shim — so both config files are edited and inspected the
same way.

Reload semantics differ by setting (see RELOAD_SEMANTICS below) because
consuming code reads these at different points:
  - "immediate": read at the point of use every loop tick (auto_trader.py/
    zerodha_trader.py's size_position()/halt-check) — a change is picked up
    within one 5-minute cycle, no restart.
  - "next_rebuild": read at watchlist-rebuild time (trade_monitor.py's
    argparse defaults / SWING_MIN_* / inbound_trade_algo.py's MIN_*) — a
    change takes effect the next time the backtest gate rebuilds (the
    Control Panel's "Rebuild watchlist now" button runs that as a fresh
    process, so it's immediate FOR THAT REBUILD; the long-running traders'
    own automatic daily-rollover rebuild still uses whatever was in memory
    when THEY started, so restart them too if you want the new value to
    survive the next automatic rollover).
  - "next_rebuild_and_restart": same as above, but ALSO gates a whole
    instrument-category loop in trade_monitor.build_universe(), which only
    runs once at module import — a change is invisible to the long-running
    traders until they're restarted, full stop (this is the same
    stale-in-memory-universe issue fixed by a manual restart earlier
    2026-09-03 — a known, pre-existing architectural property, not
    something this module can paper over without the traders re-importing
    the universe every cycle, which is out of scope here).

Usage:
    import trading_settings as ts
    ts.get("per_trade_risk_pct")          # -> 0.005 (falls back to DEFAULTS)
    ts.set_value("per_trade_risk_pct", 0.01)   # validates range/type, writes file
    ts.get_all()                          # {name: value} for every known setting
    ts.status()                           # get_all() + reload-semantics tag per name
CLI:
    python trading_settings.py list
    python trading_settings.py get NAME
    python trading_settings.py set NAME VALUE
"""
import json
import sys
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
SETTINGS_FILE = DATA_DIR / "trading_settings.json"

# (default, min, max, type) — type is int or float; bool settings use
# (default, None, None, bool) since a range is meaningless for a flag.
_SPEC = {
    "intraday_capital_pct":        (0.20, 0.0, 1.0, float),
    "per_trade_risk_pct":          (0.005, 0.0005, 0.05, float),
    "daily_loss_cap_pct":          (0.04, 0.005, 0.20, float),
    "intraday_min_win_rate":       (0.65, 0.0, 1.0, float),
    "swing_min_win_rate":          (0.65, 0.0, 1.0, float),
    "intraday_min_trades":         (8, 1, 1000, int),
    "intraday_min_profit_factor":  (1.1, 0.5, 10.0, float),
    "intraday_min_expectancy":     (0.0, -2.0, 2.0, float),
    "swing_min_trades":            (8, 1, 1000, int),
    "swing_min_profit_factor":     (1.3, 0.5, 10.0, float),
    "swing_min_expectancy":        (0.05, -2.0, 2.0, float),
    "universe_forex_enabled":      (True, None, None, bool),
    "universe_commodities_enabled": (True, None, None, bool),
    "universe_crypto_enabled":     (True, None, None, bool),
    "universe_wide_us_enabled":    (True, None, None, bool),
}

DEFAULTS = {name: spec[0] for name, spec in _SPEC.items()}

RELOAD_SEMANTICS = {
    "intraday_capital_pct": "immediate",
    "per_trade_risk_pct": "immediate",
    "daily_loss_cap_pct": "immediate",
    "intraday_min_win_rate": "next_rebuild",
    "swing_min_win_rate": "next_rebuild",
    "intraday_min_trades": "next_rebuild",
    "intraday_min_profit_factor": "next_rebuild",
    "intraday_min_expectancy": "next_rebuild",
    "swing_min_trades": "next_rebuild",
    "swing_min_profit_factor": "next_rebuild",
    "swing_min_expectancy": "next_rebuild",
    "universe_forex_enabled": "next_rebuild_and_restart",
    "universe_commodities_enabled": "next_rebuild_and_restart",
    "universe_crypto_enabled": "next_rebuild_and_restart",
    "universe_wide_us_enabled": "next_rebuild_and_restart",
}

SETTING_NAMES = list(_SPEC.keys())


def _load():
    if not SETTINGS_FILE.exists():
        return {}
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return {}


def get(name):
    if name not in _SPEC:
        raise KeyError(f"unknown setting {name!r} — must be one of {SETTING_NAMES}")
    return _load().get(name, DEFAULTS[name])


def get_all():
    return {name: get(name) for name in SETTING_NAMES}


def status():
    """{name: {"value": ..., "reload": "immediate"|"next_rebuild"|"next_rebuild_and_restart"}}"""
    return {name: {"value": get(name), "reload": RELOAD_SEMANTICS[name]} for name in SETTING_NAMES}


def validate(name, value):
    """Raises ValueError with a clear message if invalid; returns the
    coerced value (correct type) if valid. Never partially applies — either
    the whole value is good or nothing is written."""
    if name not in _SPEC:
        raise ValueError(f"unknown setting {name!r} — must be one of {SETTING_NAMES}")
    _, lo, hi, typ = _SPEC[name]
    if typ is bool:
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be a boolean, got {value!r}")
        return value
    try:
        coerced = typ(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a {typ.__name__}, got {value!r}")
    if coerced < lo or coerced > hi:
        raise ValueError(f"{name}={coerced} out of range [{lo}, {hi}]")
    return coerced


def set_value(name, value):
    coerced = validate(name, value)
    cfg = _load()
    cfg[name] = coerced
    DATA_DIR.mkdir(exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(cfg, indent=2))
    return coerced


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: trading_settings.py {list|get|set} [NAME] [VALUE]", file=sys.stderr)
        sys.exit(1)
    action = sys.argv[1]
    if action == "list":
        for name, info in status().items():
            print(f"{name}: {info['value']} ({info['reload']})")
    elif action == "get":
        if len(sys.argv) < 3:
            print("Usage: trading_settings.py get NAME", file=sys.stderr)
            sys.exit(1)
        try:
            print(get(sys.argv[2]))
        except KeyError as e:
            print(e, file=sys.stderr)
            sys.exit(1)
    elif action == "set":
        if len(sys.argv) < 4:
            print("Usage: trading_settings.py set NAME VALUE", file=sys.stderr)
            sys.exit(1)
        name, raw = sys.argv[2], sys.argv[3]
        # Booleans arrive as "true"/"false" strings from a shell; everything
        # else is parsed as a float and validate() coerces to int if needed.
        value = raw.lower() in ("true", "1", "yes") if _SPEC.get(name, (None,) * 4)[3] is bool else raw
        try:
            if _SPEC.get(name, (None,) * 4)[3] is not bool:
                value = float(raw)
            new_val = set_value(name, value)
            print(f"{name}: {new_val} ({RELOAD_SEMANTICS[name]})")
        except (ValueError, KeyError) as e:
            print(e, file=sys.stderr)
            sys.exit(1)
    else:
        print(f"Unknown action {action!r} — must be list/get/set", file=sys.stderr)
        sys.exit(1)
