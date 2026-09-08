#!/usr/bin/env bash
# Watchdog for the live trading loops (auto_trader.py / zerodha_trader.py).
#
# There is no other supervision of these processes today: stock.sh is a
# manual launcher that dies with the terminal/session that started it, and
# nothing restarts it automatically. Cron this script every few minutes
# (see crontab: */5 * * * * automation/watchdog.sh) and it will bring both
# loops back up via stock.sh --live if either one has died.
#
# Reuses stock.sh's own PID-file + liveness-check convention (PID alive AND
# cmdline still matches the expected script) rather than inventing a new
# detection mechanism — a recycled PID belonging to some unrelated process
# must never be mistaken for "still running."
#
# Restarts into LIVE mode (--live --confirm "I UNDERSTAND"), matching how
# stock.sh is currently run manually (see `ps aux` at the time this was
# added: auto_trader.py --env live --live --confirm "I UNDERSTAND"). This
# was a deliberate, confirmed choice (2026-09-02) — an unattended crash
# recovery resumes real-money trading automatically, not dry-run. If that's
# ever unwanted, flip this script's MODE_ARGS below, not stock.sh itself.
set -uo pipefail

REPO_DIR="/mnt/g/adhoc/stocktrade"
OUT_DIR="$REPO_DIR/automation/output"
STOCK_SH="$REPO_DIR/automation/stock.sh"
CAPITAL_PID_FILE="$OUT_DIR/capital_run.pid"
ZERODHA_PID_FILE="$OUT_DIR/zerodha_run.pid"

is_alive() {
    local pid_file="$1" name_pattern="$2"
    [ -f "$pid_file" ] || return 1
    local pid
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    [ -n "${pid:-}" ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -qE "$name_pattern"
}

if is_alive "$CAPITAL_PID_FILE" "auto_trader\.py" && is_alive "$ZERODHA_PID_FILE" "zerodha_trader\.py"; then
    echo "$(date -Is) watchdog: both loops alive, nothing to do."
    exit 0
fi

echo "$(date -Is) watchdog: at least one loop is down — restarting via stock.sh --live."
# stock.sh is safe to re-run any time: it kills any stale/mismatched
# previous run before starting fresh, so it's fine to call it even if only
# one of the two loops actually died.
"$STOCK_SH" --live --confirm "I UNDERSTAND" < /dev/null
