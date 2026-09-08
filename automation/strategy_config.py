#!/usr/bin/env python3
"""
strategy_config.py — the single on/off switch for every strategy in this
pipeline: BREAKOUT_INTRADAY, BREAKOUT_SWING, INBOUNDTRADEALGO.

One JSON file, automation/data/strategy_config.json, one shape:
    {"<STRATEGY_NAME>": {"enabled": true|false}}

Checked at every stage a strategy touches — the daily backtest gate
(trade_monitor.build_watchlist), the live signal loop (trade_monitor.
run_once/run_swing_check/run_inbound_check), AND live order execution
(auto_trader.py, zerodha_trader.py). Flip one strategy off here and it
disappears from all three at once — not researched, not shown as a live
signal, and no broker will open a NEW position under it — with no code
change and no restart needed beyond the next scheduled gate rebuild /
5-minute check.

Positions already open when a strategy is disabled are left alone: they
keep whatever stop/target they were opened with and get closed/exited
normally (stop, target, or max-hold, whichever comes first) — disabling a
strategy stops new entries, it does not abandon or force-close existing
ones.

Default (file missing, or a strategy's key missing from it): enabled. A
strategy has to be explicitly turned off, never opts out by omission.

Usage:
    import strategy_config as sc
    if sc.is_enabled("INBOUNDTRADEALGO"): ...
    sc.set_enabled("INBOUNDTRADEALGO", False)   # disable it
    sc.status()   # -> {"BREAKOUT_INTRADAY": True, "BREAKOUT_SWING": True, "INBOUNDTRADEALGO": False}
"""
import json
import sys
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
CONFIG_FILE = DATA_DIR / "strategy_config.json"

STRATEGY_NAMES = ["BREAKOUT_INTRADAY", "BREAKOUT_SWING", "INBOUNDTRADEALGO"]


def _load():
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


def is_enabled(name):
    return bool(_load().get(name, {}).get("enabled", True))


def set_enabled(name, enabled):
    cfg = _load()
    cfg.setdefault(name, {})["enabled"] = bool(enabled)
    DATA_DIR.mkdir(exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def status():
    """{name: enabled} for every known strategy — for reporting/dashboard."""
    return {name: is_enabled(name) for name in STRATEGY_NAMES}


# CLI shim (added 2026-09-03) — lets stock.sh (and anything else) toggle a
# strategy or check status without a human/AI writing a one-off `python -c`
# each time:
#   python strategy_config.py status            -> "NAME: true/false" lines
#   python strategy_config.py enable NAME
#   python strategy_config.py disable NAME
# No new logic — thin wrapper around is_enabled/set_enabled/status above.
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: strategy_config.py {status|enable|disable} [NAME]", file=sys.stderr)
        sys.exit(1)
    action = sys.argv[1]
    if action == "status":
        for name, enabled in status().items():
            print(f"{name}: {str(enabled).lower()}")
    elif action in ("enable", "disable"):
        if len(sys.argv) < 3:
            print(f"Usage: strategy_config.py {action} NAME", file=sys.stderr)
            sys.exit(1)
        name = sys.argv[2]
        if name not in STRATEGY_NAMES:
            print(f"Unknown strategy {name!r} — must be one of {STRATEGY_NAMES}", file=sys.stderr)
            sys.exit(1)
        set_enabled(name, action == "enable")
        print(f"{name}: {str(action == 'enable').lower()}")
    else:
        print(f"Unknown action {action!r} — must be status/enable/disable", file=sys.stderr)
        sys.exit(1)
