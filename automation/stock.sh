#!/usr/bin/env bash
# Global control CLI for the trading pipeline. Rewritten 2026-09-03 (user
# request) from a start-only launcher into a subcommand CLI, so every
# mechanical operation this session needed an AI for — restart after a code
# change, force a watchlist rebuild, toggle a strategy, run a discovery scan
# — is now a single scriptable command, callable by a human, cron, or the
# dashboard's Control Panel (see dashboard.py's /api/control/* routes).
#
# Subcommands:
#   stock.sh start [--demo|--live] [--confirm "I UNDERSTAND"]
#                                     Starts auto_trader.py + zerodha_trader.py
#                                     (+ the dashboard, if not already up).
#                                     Same behavior as always: no mode flag in
#                                     an interactive terminal -> menu prompt;
#                                     non-interactive with no mode flag ->
#                                     dry-run (safe default).
#   stock.sh stop                    Kills the Capital.com/Zerodha loops.
#                                     Leaves the dashboard running.
#   stock.sh restart [--demo|--live] [--confirm ...]
#                                     stop, then start.
#   stock.sh restart-dashboard       Restarts ONLY the dashboard (e.g. after
#                                     editing dashboard.py) without touching
#                                     the live traders. Always launches
#                                     through the decrypt wrapper — never run
#                                     `python dashboard.py` directly, or
#                                     broker credentials stay encrypted and
#                                     every live-equity/position call fails.
#   stock.sh status                  Prints running/stopped + mode for each
#                                     process, halt state, each strategy's
#                                     enabled state, and the watchlist's last
#                                     rebuild time — stable "key: value" lines,
#                                     one per line, meant to be machine-parsed.
#   stock.sh rebuild-watchlist [--min-win-rate N]
#                                     Forces a fresh backtest-gate rebuild
#                                     (trade_monitor.py --rebuild-watchlist).
#                                     Runs in the foreground here — the
#                                     dashboard backgrounds this itself if it
#                                     doesn't want to block on it.
#   stock.sh discover [--source nse|us|both] [--batch-size N]
#                                     Runs weekend_discovery.py once, widening
#                                     the live instrument universe.
#   stock.sh enable <STRATEGY_NAME>  Flips one strategy on in
#   stock.sh disable <STRATEGY_NAME> automation/data/strategy_config.json.
#   stock.sh clear-halt --confirm "I UNDERSTAND"
#                                     Removes output/TRADING_HALTED, resuming
#                                     real-money trading. Refuses without the
#                                     exact confirm string — same friction as
#                                     --live below, deliberately not weakened.
#
# Bare invocations with no subcommand (or a first arg starting with "--")
# dispatch straight to `start`, for backward compatibility with how this
# script has always been called:
#   stock.sh                                   # interactive menu
#   stock.sh --demo
#   stock.sh --live --confirm "I UNDERSTAND"
#   stock.sh --once                            # any flag either trading
#                                               # script accepts still passes
#                                               # through to `start`
#
# --demo auto-confirms for Capital.com (fake money, no reason for friction).
# --live deliberately does NOT auto-confirm for either broker — you must
# pass --confirm "I UNDERSTAND" yourself, every time, so real-money trading
# is always a distinct, deliberate action, never a side effect of muscle
# memory or a typo. It's the same one confirm string for both, and for
# clear-halt above.
set -uo pipefail

REPO_DIR="/mnt/g/adhoc/stocktrade"
PY="$REPO_DIR/.venv/bin/python"
export PYTHONUNBUFFERED=1  # otherwise stdout buffers fully when not a tty (e.g. `stock.sh > log.txt &`),
                           # making a tailed log file look stalled for minutes at a time
CAPITAL_SCRIPT="$REPO_DIR/automation/auto_trader.py"
ZERODHA_SCRIPT="$REPO_DIR/automation/zerodha_trader.py"
DASHBOARD_SCRIPT="$REPO_DIR/automation/dashboard.py"
TRADE_MONITOR_SCRIPT="$REPO_DIR/automation/trade_monitor.py"
DISCOVERY_SCRIPT="$REPO_DIR/automation/weekend_discovery.py"
STRATEGY_CONFIG_SCRIPT="$REPO_DIR/automation/strategy_config.py"
OUT_DIR="$REPO_DIR/automation/output"
ENV_FILE="$REPO_DIR/automation/.env"
ENV_KEYS_FILE="$REPO_DIR/.env.keys"
HALT_FILE="$OUT_DIR/TRADING_HALTED"

# automation/.env is encrypted at rest (dotenvx encrypt) — decrypt into the
# child process's environment at launch time via dotenvx run, rather than
# ever writing plaintext credentials to disk. Falls back to a plain launch
# if dotenvx or the encrypted files aren't present (e.g. before you've run
# `dotenvx encrypt -f automation/.env` at all) — the Python scripts' own
# credentials() check still catches missing/invalid values either way.
if command -v dotenvx >/dev/null 2>&1 && [ -f "$ENV_FILE" ] && [ -f "$ENV_KEYS_FILE" ]; then
    RUN_WITH_ENV=(dotenvx run -f "$ENV_FILE" -fk "$ENV_KEYS_FILE" --)
else
    RUN_WITH_ENV=()
fi
CAPITAL_PID_FILE="$OUT_DIR/capital_run.pid"
ZERODHA_PID_FILE="$OUT_DIR/zerodha_run.pid"
DASHBOARD_PID_FILE="$OUT_DIR/dashboard_run.pid"
DASHBOARD_LOG="$OUT_DIR/dashboard_run.log"

mkdir -p "$OUT_DIR"

kill_previous() {
    local pid_file="$1" name_pattern="$2" label="$3"
    if [ -f "$pid_file" ]; then
        local old_pid
        old_pid="$(cat "$pid_file" 2>/dev/null || true)"
        if [ -n "${old_pid:-}" ] && kill -0 "$old_pid" 2>/dev/null \
           && tr '\0' ' ' < "/proc/$old_pid/cmdline" 2>/dev/null | grep -qE "$name_pattern"; then
            echo "Stopping previous $label run (pid $old_pid)..."
            kill "$old_pid" 2>/dev/null || true
            for _ in 1 2 3 4 5; do
                kill -0 "$old_pid" 2>/dev/null || break
                sleep 1
            done
            kill -0 "$old_pid" 2>/dev/null && kill -9 "$old_pid" 2>/dev/null || true
        fi
        rm -f "$pid_file"
    fi
}

# Ground-truth check, same convention kill_previous/dashboard.py's
# _process_info use: PID file's PID must still be alive AND its cmdline must
# still match the expected script — a recycled PID belonging to some
# unrelated process must never be mistaken for "still running."
is_alive() {
    local pid_file="$1" name_pattern="$2"
    [ -f "$pid_file" ] || return 1
    local pid
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    [ -n "${pid:-}" ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -qE "$name_pattern"
}

# Idempotent, unlike kill_previous above: if the dashboard is already up,
# leave it running as-is (no reason to bounce a healthy monitoring process
# just because stock.sh was re-run) — only start it if it's not. Covers
# both "I stopped the dashboard, start it again" and "it's my first run
# today" with the same check.
DASHBOARD_PORT=8787

start_dashboard_if_needed() {
    if [ -f "$DASHBOARD_PID_FILE" ]; then
        local old_pid
        old_pid="$(cat "$DASHBOARD_PID_FILE" 2>/dev/null || true)"
        if [ -n "${old_pid:-}" ] && kill -0 "$old_pid" 2>/dev/null \
           && tr '\0' ' ' < "/proc/$old_pid/cmdline" 2>/dev/null | grep -qE "dashboard\.py"; then
            echo "Dashboard already running (pid $old_pid) — http://127.0.0.1:$DASHBOARD_PORT"
            return
        fi
        rm -f "$DASHBOARD_PID_FILE"
    fi
    # Not tracked by our PID file, but something might already be bound to
    # the port anyway (a manual launch, or the first run after adding this
    # feature) — check before starting a second instance, which would just
    # fail with "address already in use".
    if command -v curl >/dev/null 2>&1 && curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$DASHBOARD_PORT/"; then
        echo "Dashboard already responding on http://127.0.0.1:$DASHBOARD_PORT (not one this script started — leaving it as-is)."
        return
    fi
    echo "Dashboard not running — starting it..."
    # setsid (not just nohup/&) — puts the dashboard in its own session and
    # process group, so it survives independently of stock.sh's lifecycle:
    # a Ctrl-C on stock.sh sends SIGINT to its whole foreground process
    # group, which would otherwise take an ordinarily-backgrounded child
    # down with it despite nohup/disown (nohup only blocks SIGHUP, and
    # disown only affects bash's own job table, neither changes actual
    # process-group membership).
    setsid "${RUN_WITH_ENV[@]}" "$PY" "$DASHBOARD_SCRIPT" --no-open >> "$DASHBOARD_LOG" 2>&1 < /dev/null &
    local dash_pid=$!
    disown "$dash_pid" 2>/dev/null || true
    echo "$dash_pid" > "$DASHBOARD_PID_FILE"
    echo "Dashboard started (pid $dash_pid) — http://127.0.0.1:$DASHBOARD_PORT"
}

cmd_start() {
    # Ask for an explicit mode choice when: (a) it's an interactive terminal —
    # a backgrounded/cron/scripted invocation has no one to answer a prompt, so
    # it must never block on one, and (b) --demo/--live wasn't already given
    # anywhere in the args (scan all of them, not just $1, so e.g.
    # `stock.sh start --once --live --confirm "I UNDERSTAND"` still skips the prompt).
    local mode_flag_given=false
    for arg in "$@"; do
        case "$arg" in
            --demo|--live) mode_flag_given=true ;;
        esac
    done

    if [ "$mode_flag_given" = false ] && [ -t 0 ]; then
        echo "========================================================================"
        echo "  Select trading mode — nothing starts until you choose:"
        echo "========================================================================"
        echo "    1) Demo     Capital.com DEMO account (fake money, real orders there)."
        echo "                Zerodha has no demo account, so it stays dry-run (no real orders)."
        echo "    2) Live     REAL MONEY orders on BOTH Capital.com and Zerodha."
        echo "    3) Dry-run  Visibility only — no orders placed on either broker. [default]"
        echo
        read -r -p "  Enter 1, 2, or 3 [3]: " mode_choice
        case "$mode_choice" in
            1)
                set -- --demo "$@"
                echo "  -> DEMO mode selected."
                ;;
            2)
                echo
                echo "  *** LIVE MODE PLACES REAL ORDERS WITH REAL MONEY ON BOTH BROKERS ***"
                read -r -p "  Type I UNDERSTAND to confirm, or anything else to abort: " confirm_text
                if [ "$confirm_text" != "I UNDERSTAND" ]; then
                    echo "  Confirmation text did not match \"I UNDERSTAND\" exactly — aborting, nothing started."
                    exit 1
                fi
                set -- --live --confirm "I UNDERSTAND" "$@"
                echo "  -> LIVE mode selected and confirmed."
                ;;
            3|"")
                echo "  -> Dry-run selected (no orders will be placed)."
                ;;
            *)
                echo "  Unrecognized choice \"$mode_choice\" — defaulting to dry-run (safest), nothing was assumed."
                ;;
        esac
        echo
    fi

    kill_previous "$CAPITAL_PID_FILE" "auto_trader\.py" "Capital.com"
    kill_previous "$ZERODHA_PID_FILE" "zerodha_trader\.py" "Zerodha"
    start_dashboard_if_needed

    local CAPITAL_ARGS=(--loop-seconds 300)
    local ZERODHA_ARGS=(--loop-seconds 300)
    case "${1:-}" in
        --demo)
            shift
            CAPITAL_ARGS+=(--env demo --live --confirm "I UNDERSTAND")
            ZERODHA_ARGS+=(--env demo)   # forces dry-run internally; no fake-money account to trade against
            ;;
        --live)
            shift
            CAPITAL_ARGS+=(--env live --live)   # --confirm "I UNDERSTAND" must come from you via "$@" below
            ZERODHA_ARGS+=(--env live --live)
            ;;
        *)
            ;;
    esac

    # setsid + disown, same as start_dashboard_if_needed above and for the
    # same reason: without it, these loops are plain children of this script,
    # and stock.sh's own EXIT trap (or the parent shell/session dying and
    # sending SIGHUP down the process group) kills them the moment stock.sh
    # exits — silently stopping live trading while the (correctly detached)
    # dashboard keeps running and looks healthy. Output is captured to its own
    # log file for the same reason dashboard_run.log exists: previously stdout
    # only survived if whoever launched stock.sh happened to redirect it
    # themselves.
    setsid "${RUN_WITH_ENV[@]}" "$PY" "$CAPITAL_SCRIPT" "${CAPITAL_ARGS[@]}" "$@" >> "$OUT_DIR/capital_run.log" 2>&1 < /dev/null &
    local capital_pid=$!
    disown "$capital_pid" 2>/dev/null || true
    echo "$capital_pid" > "$CAPITAL_PID_FILE"
    echo "Capital.com loop started (pid $capital_pid) — log: $OUT_DIR/capital_run.log"

    setsid "${RUN_WITH_ENV[@]}" "$PY" "$ZERODHA_SCRIPT" "${ZERODHA_ARGS[@]}" "$@" >> "$OUT_DIR/zerodha_run.log" 2>&1 < /dev/null &
    local zerodha_pid=$!
    disown "$zerodha_pid" 2>/dev/null || true
    echo "$zerodha_pid" > "$ZERODHA_PID_FILE"
    echo "Zerodha loop started (pid $zerodha_pid) — log: $OUT_DIR/zerodha_run.log"
}

cmd_stop() {
    kill_previous "$CAPITAL_PID_FILE" "auto_trader\.py" "Capital.com"
    kill_previous "$ZERODHA_PID_FILE" "zerodha_trader\.py" "Zerodha"
    echo "Stopped (dashboard left running — use restart-dashboard if you want it down/up too)."
}

cmd_restart() {
    cmd_stop
    cmd_start "$@"
}

# Added 2026-09-03 — restarting just the dashboard (e.g. after a dashboard.py
# code change) previously had no dedicated path: cmd_start restarts the
# LIVE TRADERS too, which is the wrong tool for "the dashboard needs a code
# reload." This kills any existing dashboard (regardless of PID-match state,
# unlike start_dashboard_if_needed's idempotent "leave it alone if healthy"
# check) and starts a fresh one via the same decrypt-wrapped launch line —
# never run dashboard.py directly with plain python, or it won't have
# CAPITAL_API_KEY/ZERODHA_* decrypted and every live-equity/position call
# will fail with a "still encrypted" error (capital_api.py/zerodha_api.py's
# own credentials() check).
cmd_restart_dashboard() {
    kill_previous "$DASHBOARD_PID_FILE" "dashboard\.py" "Dashboard"
    start_dashboard_if_needed
}

cmd_status() {
    local cap_alive=false zer_alive=false dash_alive=false
    is_alive "$CAPITAL_PID_FILE" "auto_trader\.py" && cap_alive=true
    is_alive "$ZERODHA_PID_FILE" "zerodha_trader\.py" && zer_alive=true
    is_alive "$DASHBOARD_PID_FILE" "dashboard\.py" && dash_alive=true

    echo "capital_running: $cap_alive"
    if [ "$cap_alive" = true ]; then
        local pid mode
        pid="$(cat "$CAPITAL_PID_FILE")"
        mode="dry-run"
        tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q -- '--live' && mode="live"
        tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q -- '--env demo' && mode="demo"
        echo "capital_pid: $pid"
        echo "capital_mode: $mode"
    fi

    echo "zerodha_running: $zer_alive"
    if [ "$zer_alive" = true ]; then
        local pid mode
        pid="$(cat "$ZERODHA_PID_FILE")"
        mode="dry-run"
        tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q -- '--live' && mode="live"
        tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q -- '--env demo' && mode="demo"
        echo "zerodha_pid: $pid"
        echo "zerodha_mode: $mode"
    fi

    echo "dashboard_running: $dash_alive"

    if [ -f "$HALT_FILE" ]; then
        echo "halted: true"
        echo "halt_text: $(tr '\n' ' ' < "$HALT_FILE")"
    else
        echo "halted: false"
    fi

    "$PY" "$STRATEGY_CONFIG_SCRIPT" status 2>/dev/null | while IFS= read -r line; do
        echo "strategy_$line"
    done

    "$PY" -c "
import json
from pathlib import Path
p = Path('$OUT_DIR') / 'watchlist_latest.json'
if p.exists():
    try:
        wl = json.loads(p.read_text())
        print(f\"watchlist_generated_at_local: {wl.get('generated_at_local')}\")
        print(f\"watchlist_as_of_date: {wl.get('as_of_date')}\")
    except Exception as e:
        print(f'watchlist_error: {e}')
else:
    print('watchlist_generated_at_local: never')
"
}

cmd_rebuild_watchlist() {
    local min_win_rate=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --min-win-rate) min_win_rate="$2"; shift 2 ;;
            *) shift ;;
        esac
    done
    local args=(automation/trade_monitor.py --rebuild-watchlist --once --quiet)
    [ -n "$min_win_rate" ] && args+=(--min-win-rate "$min_win_rate")
    cd "$REPO_DIR" && "$PY" "${args[@]}" >> "$OUT_DIR/watchlist_rebuild.log" 2>&1
    echo "Watchlist rebuild finished — see $OUT_DIR/watchlist_rebuild.log"
}

cmd_discover() {
    local source="both" batch_size=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --source) source="$2"; shift 2 ;;
            --batch-size) batch_size="$2"; shift 2 ;;
            *) shift ;;
        esac
    done
    local args=(automation/weekend_discovery.py --source "$source" --quiet)
    [ -n "$batch_size" ] && args+=(--batch-size "$batch_size")
    cd "$REPO_DIR" && "$PY" "${args[@]}" >> "$OUT_DIR/weekend_discovery.log" 2>&1
    echo "Discovery scan finished — see $OUT_DIR/weekend_discovery.log"
}

cmd_enable() {
    local name="${1:-}"
    [ -n "$name" ] || { echo "Usage: stock.sh enable <STRATEGY_NAME>" >&2; exit 1; }
    "$PY" "$STRATEGY_CONFIG_SCRIPT" enable "$name"
}

cmd_disable() {
    local name="${1:-}"
    [ -n "$name" ] || { echo "Usage: stock.sh disable <STRATEGY_NAME>" >&2; exit 1; }
    "$PY" "$STRATEGY_CONFIG_SCRIPT" disable "$name"
}

cmd_clear_halt() {
    local confirm=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --confirm) confirm="$2"; shift 2 ;;
            *) shift ;;
        esac
    done
    if [ "$confirm" != "I UNDERSTAND" ]; then
        echo "Refusing to clear the halt — pass --confirm \"I UNDERSTAND\" exactly. Nothing was changed." >&2
        exit 1
    fi
    if [ -f "$HALT_FILE" ]; then
        rm -f "$HALT_FILE"
        echo "Halt cleared — live trading can resume on both brokers."
    else
        echo "No halt file present — nothing to clear."
    fi
}

# --- Dispatch ---------------------------------------------------------------
case "${1:-}" in
    start)   shift; cmd_start "$@" ;;
    stop)    shift; cmd_stop "$@" ;;
    restart) shift; cmd_restart "$@" ;;
    restart-dashboard) shift; cmd_restart_dashboard "$@" ;;
    status)  shift; cmd_status "$@" ;;
    rebuild-watchlist) shift; cmd_rebuild_watchlist "$@" ;;
    discover) shift; cmd_discover "$@" ;;
    enable)  shift; cmd_enable "$@" ;;
    disable) shift; cmd_disable "$@" ;;
    clear-halt) shift; cmd_clear_halt "$@" ;;
    --*|"")
        # Backward compatibility — no subcommand, or the first token is
        # already a flag (--demo/--live/--once/...): treat the whole
        # invocation as `start` with those args, exactly like before this
        # script had subcommands at all.
        cmd_start "$@"
        ;;
    *)
        echo "Unknown subcommand: ${1:-}" >&2
        echo "Usage: stock.sh {start|stop|restart|restart-dashboard|status|rebuild-watchlist|discover|enable|disable|clear-halt} [args...]" >&2
        exit 1
        ;;
esac
