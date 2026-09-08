#!/usr/bin/env bash
# Runs any command with automation/.env's encrypted credentials decrypted
# into its environment via dotenvx. automation/.env is encrypted at rest
# (dotenvx encrypt) — the private key lives in .env.keys at the repo root,
# which is gitignored and must never be committed or shared.
#
# Usage:
#   automation/with_env.sh ./.venv/bin/python automation/capital_api.py --test
#   automation/with_env.sh ./.venv/bin/python automation/zerodha_api.py --test --debug-login
set -euo pipefail
REPO_DIR="/mnt/g/adhoc/stocktrade"
exec dotenvx run -f "$REPO_DIR/automation/.env" -fk "$REPO_DIR/.env.keys" -- "$@"
