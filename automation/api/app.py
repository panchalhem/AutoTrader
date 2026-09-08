"""FastAPI service exposing the trading automation's read/settings surface.

Phase 1 of the desktop-app conversion: this wraps dashboard.py's existing
data-gathering functions (build_snapshot, build_positions_snapshot, the
control_* functions) behind a typed REST API instead of the hand-rolled
http.server Handler, so a future desktop UI (or anything else) can consume
it over HTTP. dashboard.py itself is untouched and keeps working as-is —
this is an additive service, run separately (default port 8788).

Run: automation/with_env.sh .venv/bin/uvicorn api.app:app --host 127.0.0.1 --port 8788
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import secrets  # noqa: E402
import strategy_config  # noqa: E402
import trading_settings  # noqa: E402
from fastapi import Depends, FastAPI, HTTPException  # noqa: E402
from fastapi.security import HTTPBasic, HTTPBasicCredentials  # noqa: E402

import dashboard  # noqa: E402 — reuses build_snapshot/build_positions_snapshot/control_*

app = FastAPI(title="Trading Automation API", version="0.1.0")
security = HTTPBasic()


def require_auth(credentials: HTTPBasicCredentials = Depends(security)):
    ok_user = secrets.compare_digest(credentials.username, dashboard.DASHBOARD_USER)
    ok_pw = secrets.compare_digest(credentials.password, dashboard.DASHBOARD_PASSWORD)
    if not (ok_user and ok_pw):
        raise HTTPException(status_code=401, detail="Invalid credentials", headers={"WWW-Authenticate": "Basic"})
    return credentials.username


@app.get("/api/data")
def get_snapshot(_=Depends(require_auth)):
    return dashboard.build_snapshot()


@app.get("/api/positions")
def get_positions(_=Depends(require_auth)):
    return dashboard.build_positions_snapshot()


@app.get("/api/control/status")
def get_control_status(_=Depends(require_auth)):
    return dashboard.control_status()


@app.get("/api/settings/strategies")
def get_strategy_settings(_=Depends(require_auth)):
    return strategy_config.status()


@app.get("/api/settings/trading")
def get_trading_settings(_=Depends(require_auth)):
    return trading_settings.status()


@app.post("/api/control/traders")
def post_control_traders(payload: dict, _=Depends(require_auth)):
    result = dashboard.control_traders(payload)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


@app.post("/api/control/strategy")
def post_control_strategy(payload: dict, _=Depends(require_auth)):
    result = dashboard.control_strategy(payload)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


@app.post("/api/control/settings")
def post_control_settings(payload: dict, _=Depends(require_auth)):
    result = dashboard.control_settings(payload)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


@app.post("/api/close")
def post_close(payload: dict, _=Depends(require_auth)):
    result = dashboard.close_position(payload)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result
