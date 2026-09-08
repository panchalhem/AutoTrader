"""Central license server: issues/revokes signed license tokens for the
desktop trading app, and lets you (the vendor) manage clients from the
admin panel (admin.html, served at /admin/panel).

Run: .venv/bin/uvicorn license_server.server:app --host 0.0.0.0 --port 8790

Licenses are RS256-signed JWTs (see keys.py) — the desktop app can verify
signature + expiry completely offline with the bundled public key, and
additionally calls POST /license/validate when it has network to catch
revocation before that JWT expires.
"""

import os
import secrets
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jwt  # noqa: E402
from fastapi import Depends, FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.security import HTTPBasic, HTTPBasicCredentials  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from license_server import db, keys  # noqa: E402

ROOT = Path(__file__).resolve().parent
ADMIN_PASSWORD_FILE = ROOT / "data" / ".admin_password"
ADMIN_USER = "admin"


def _load_or_create_admin_password():
    env_pw = os.environ.get("LICENSE_ADMIN_PASSWORD")
    if env_pw:
        return env_pw
    if ADMIN_PASSWORD_FILE.exists():
        return ADMIN_PASSWORD_FILE.read_text().strip()
    pw = secrets.token_urlsafe(18)
    ADMIN_PASSWORD_FILE.parent.mkdir(parents=True, exist_ok=True)
    ADMIN_PASSWORD_FILE.write_text(pw)
    return pw


ADMIN_PASSWORD = _load_or_create_admin_password()
security = HTTPBasic()

app = FastAPI(title="License Server", version="0.1.0")
db.init_db()


def require_admin(credentials: HTTPBasicCredentials = Depends(security)):
    ok_user = secrets.compare_digest(credentials.username, ADMIN_USER)
    ok_pw = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
    if not (ok_user and ok_pw):
        raise HTTPException(status_code=401, detail="Invalid credentials", headers={"WWW-Authenticate": "Basic"})
    return credentials.username


class CreateClientRequest(BaseModel):
    name: str
    email: str | None = None


class IssueLicenseRequest(BaseModel):
    client_id: str
    plan: str = "standard"
    seats: int = 1
    validity_days: int = 365


class ValidateRequest(BaseModel):
    token: str


@app.post("/admin/clients")
def create_client(payload: CreateClientRequest, _=Depends(require_admin)):
    client_id = db.create_client(payload.name, payload.email)
    return {"id": client_id, "name": payload.name}


@app.get("/admin/clients")
def get_clients(_=Depends(require_admin)):
    return db.list_clients()


@app.post("/admin/licenses")
def issue_license(payload: IssueLicenseRequest, _=Depends(require_admin)):
    client = db.get_client(payload.client_id)
    if not client:
        raise HTTPException(status_code=404, detail="No such client")
    license_id = str(uuid.uuid4())
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(days=payload.validity_days)
    db.create_license(license_id, payload.client_id, payload.plan, payload.seats, issued_at, expires_at)
    token = jwt.encode(
        {
            "jti": license_id,
            "sub": payload.client_id,
            "client_name": client["name"],
            "plan": payload.plan,
            "seats": payload.seats,
            "iat": issued_at,
            "exp": expires_at,
        },
        keys.load_private_key(),
        algorithm="RS256",
    )
    return {"license_id": license_id, "token": token, "expires_at": expires_at.isoformat()}


@app.get("/admin/licenses")
def get_licenses(_=Depends(require_admin)):
    return db.list_licenses()


@app.post("/admin/licenses/{license_id}/revoke")
def revoke_license(license_id: str, _=Depends(require_admin)):
    if not db.revoke_license(license_id):
        raise HTTPException(status_code=404, detail="No such license")
    return {"ok": True}


@app.get("/license/public-key")
def get_public_key():
    """Unauthenticated — the whole point is any installed desktop app can
    fetch (and cache) this to verify licenses offline afterwards."""
    return {"public_key": keys.load_public_key()}


@app.post("/license/validate")
def validate_license(payload: ValidateRequest):
    """Unauthenticated by design (a license token itself is the credential).
    Checks signature + expiry via the JWT, then revocation against the DB —
    a revoked license fails here even if the JWT itself hasn't expired yet."""
    try:
        claims = jwt.decode(payload.token, keys.load_public_key(), algorithms=["RS256"])
    except jwt.ExpiredSignatureError:
        return {"valid": False, "reason": "expired"}
    except jwt.InvalidTokenError:
        return {"valid": False, "reason": "invalid_signature"}

    license_row = db.get_license(claims["jti"])
    if not license_row:
        return {"valid": False, "reason": "unknown_license"}
    if license_row["revoked"]:
        return {"valid": False, "reason": "revoked"}

    return {
        "valid": True,
        "client_name": claims.get("client_name"),
        "plan": claims.get("plan"),
        "seats": claims.get("seats"),
        "expires_at": claims.get("exp"),
    }


@app.get("/admin/panel")
def admin_panel(_=Depends(require_admin)):
    return FileResponse(ROOT / "admin.html")
