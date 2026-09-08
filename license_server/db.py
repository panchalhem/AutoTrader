"""SQLite-backed store for clients and licenses. Deliberately not
Postgres/an ORM for this v1 — single-writer admin usage, easy to swap for
Postgres later (Phase 3's plan already flags that) without changing callers,
since every function here takes/returns plain dicts.
"""

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DB_FILE = Path(__file__).resolve().parent / "data" / "licenses.db"


@contextmanager
def _conn():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS licenses (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                plan TEXT NOT NULL,
                seats INTEGER NOT NULL DEFAULT 1,
                issued_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (client_id) REFERENCES clients(id)
            )
        """)


def create_client(name, email=None):
    client_id = str(uuid.uuid4())
    with _conn() as conn:
        conn.execute(
            "INSERT INTO clients (id, name, email, created_at) VALUES (?, ?, ?, ?)",
            (client_id, name, email, datetime.now(timezone.utc).isoformat()),
        )
    return client_id


def list_clients():
    with _conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM clients ORDER BY created_at DESC")]


def get_client(client_id):
    with _conn() as conn:
        row = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
        return dict(row) if row else None


def create_license(license_id, client_id, plan, seats, issued_at, expires_at):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO licenses (id, client_id, plan, seats, issued_at, expires_at, revoked) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            (license_id, client_id, plan, seats, issued_at.isoformat(), expires_at.isoformat()),
        )


def get_license(license_id):
    with _conn() as conn:
        row = conn.execute("SELECT * FROM licenses WHERE id = ?", (license_id,)).fetchone()
        return dict(row) if row else None


def list_licenses():
    with _conn() as conn:
        rows = conn.execute("""
            SELECT licenses.*, clients.name AS client_name
            FROM licenses JOIN clients ON clients.id = licenses.client_id
            ORDER BY licenses.issued_at DESC
        """).fetchall()
        return [dict(r) for r in rows]


def revoke_license(license_id):
    with _conn() as conn:
        cur = conn.execute("UPDATE licenses SET revoked = 1 WHERE id = ?", (license_id,))
        return cur.rowcount > 0
