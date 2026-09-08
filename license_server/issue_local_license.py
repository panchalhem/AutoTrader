#!/usr/bin/env python3
"""One-off CLI to issue yourself a license token without running the full
admin-panel server — for personal/local use of the desktop app. Reuses the
same db.create_client/create_license + JWT signing the admin panel uses, so
the license is a normal record (revocable later), not a side-channel token.

Usage:
    .venv/bin/python license_server/issue_local_license.py "Your Name" --days 3650
"""
import argparse
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jwt  # noqa: E402

from license_server import db, keys  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("name")
    p.add_argument("--email", default=None)
    p.add_argument("--plan", default="standard")
    p.add_argument("--seats", type=int, default=1)
    p.add_argument("--days", type=int, default=3650, help="validity in days (default 10 years)")
    args = p.parse_args()

    client_id = db.create_client(args.name, args.email)
    client = db.get_client(client_id)

    license_id = str(uuid.uuid4())
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(days=args.days)
    db.create_license(license_id, client_id, args.plan, args.seats, issued_at, expires_at)

    token = jwt.encode(
        {
            "jti": license_id,
            "sub": client_id,
            "client_name": client["name"],
            "plan": args.plan,
            "seats": args.seats,
            "iat": issued_at,
            "exp": expires_at,
        },
        keys.load_private_key(),
        algorithm="RS256",
    )

    print(f"client_id:   {client_id}")
    print(f"license_id:  {license_id}")
    print(f"expires_at:  {expires_at.isoformat()}")
    print()
    print("License token (paste this into the desktop app's activation screen):")
    print(token)


if __name__ == "__main__":
    main()
