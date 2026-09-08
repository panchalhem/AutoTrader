#!/usr/bin/env bash
# Run on macOS, from the desktop/ folder:
#   ./build-backend.sh
#
# Freezes automation/dashboard.py (+ its automation/ dependencies) into a
# self-contained "dashboard" binary under desktop/backend/mac/dashboard/, so
# the packaged Electron app needs no system Python. Requires Python 3.11+ on
# THIS build machine (not the end user's), with pip available.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
AUTOMATION="$REPO_ROOT/automation"
OUT_DIR="$SCRIPT_DIR/backend/mac"
BUILD_VENV="$SCRIPT_DIR/.build-venv-mac"

# dashboard.py's only third-party import is pandas (strategy_config.py and
# trading_settings.py, its two internal deps, are stdlib-only) — no need for
# the full repo requirements.txt here.
echo "Setting up a throwaway build venv..."
rm -rf "$BUILD_VENV"
python3 -m venv "$BUILD_VENV"
"$BUILD_VENV/bin/pip" install --upgrade pip
"$BUILD_VENV/bin/pip" install pandas==3.0.5 pyinstaller

echo "Running PyInstaller (onedir)..."
rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

"$BUILD_VENV/bin/pyinstaller" \
  --name dashboard \
  --onedir \
  --noconfirm \
  --clean \
  --distpath "$OUT_DIR" \
  --workpath "$SCRIPT_DIR/build/pyinstaller-mac" \
  --specpath "$SCRIPT_DIR/build" \
  --paths "$AUTOMATION" \
  --collect-submodules pandas \
  "$AUTOMATION/dashboard.py"

echo "Backend built at $OUT_DIR/dashboard/dashboard"
rm -rf "$BUILD_VENV"
