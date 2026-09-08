# Run on Windows, from the desktop/ folder (or repo root — path below adjusts):
#   powershell -ExecutionPolicy Bypass -File build-backend.ps1
#
# Freezes automation/dashboard.py (+ its automation/ dependencies) into a
# self-contained dashboard.exe under desktop/backend/win/dashboard/, so the
# packaged Electron app needs no system Python. Requires a Python 3.11+
# install on THIS build machine (not the end user's) with pip available.

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$Automation = Join-Path $RepoRoot "automation"
$OutDir = Join-Path $PSScriptRoot "backend\win"

# dashboard.py's only third-party import is pandas (strategy_config.py and
# trading_settings.py, its two internal deps, are stdlib-only) — installing
# the full repo requirements.txt here is unnecessary AND breaks the build:
# it pulls in uvloop, which has no Windows wheel and fails the whole
# `pip install -r` before anything (including pandas) gets installed.
Write-Host "Setting up a throwaway build venv..."
$BuildVenv = Join-Path $PSScriptRoot ".build-venv-win"
if (Test-Path $BuildVenv) { Remove-Item -Recurse -Force $BuildVenv }
python -m venv $BuildVenv
& "$BuildVenv\Scripts\pip.exe" install --upgrade pip
& "$BuildVenv\Scripts\pip.exe" install pandas==3.0.5 pyinstaller
if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)" }

Write-Host "Running PyInstaller (onedir)..."
if (Test-Path $OutDir) { Remove-Item -Recurse -Force $OutDir }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

& "$BuildVenv\Scripts\pyinstaller.exe" `
  --name dashboard `
  --onedir `
  --noconfirm `
  --clean `
  --distpath $OutDir `
  --workpath (Join-Path $PSScriptRoot "build\pyinstaller-win") `
  --specpath (Join-Path $PSScriptRoot "build") `
  --paths $Automation `
  --collect-submodules pandas `
  (Join-Path $Automation "dashboard.py")

Write-Host "Backend built at $OutDir\dashboard\dashboard.exe"
Remove-Item -Recurse -Force $BuildVenv
