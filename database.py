"""Accounts database (docs/SPEC.md): who is approved, each user's Google connection, the active event,
and pending sign-in links. Contacts are NOT stored here; they live in each user's Google account (cards.py).

SQLite by default (a file on a Railway volume); PostgreSQL when DATABASE_URL is set.
"""

from datetime import datetime, timedelta
import re
import sqlite3

import psycopg2

import config


def get_db_connection():
    if config.DATABASE_URL:
        return psycopg2.connect(config.DATABASE_URL)
    return sqlite3.connect(config.LOCAL_SQLITE_PATH)


def _pg() -> bool:
    return bool(config.DATABASE_URL)


def _sql(query: str) -> str:
    """Queries are written with '?' placeholders; PostgreSQL wants '%s'."""
    return query.replace("?", "%s") if _pg() else query


def _run(query: str, params: tuple = (), fetch: str | None = None):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(_sql(query), params)
        out = None
        if fetch == "all":
            names = [d[0] for d in cur.description]
            out = [dict(zip(names, row)) for row in cur.fetchall()]
        elif fetch == "one":
            row = cur.fetchone()
            out = dict(zip([d[0] for d in cur.description], row)) if row else None
        elif fetch == "rowcount":
            out = cur.rowcount
        conn.commit()
        cur.close()
        return out
    finally:
        conn.close()


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def init_db():
    big = "BIGINT" if _pg() else "INTEGER"
    for stmt in (
        f"""CREATE TABLE IF NOT EXISTS users (
    telegram_id {big} PRIMARY KEY,
    name TEXT,
    username TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    requested_at TEXT,
    decided_at TEXT,
    last_note TEXT,
    google_email TEXT,
    refresh_token_enc TEXT,
    sheet_id TEXT,
    contact_group TEXT
)""",
        f"""CREATE TABLE IF NOT EXISTS active_events (
    owner_id {big} PRIMARY KEY,
    name TEXT NOT NULL,
    expires_at TEXT NOT NULL
)""",
        f"""CREATE TABLE IF NOT EXISTS oauth_states (
    state TEXT PRIMARY KEY,
    telegram_id {big} NOT NULL,
    expires_at TEXT NOT NULL
)""",
    ):
        _run(stmt)
    # Columns added after a table first existed: CREATE TABLE IF NOT EXISTS does not add them.
    for column in ("last_note", "google_email", "refresh_token_enc", "sheet_id", "contact_group"):
        _add_column("users", column, "TEXT")


def _add_column(table: str, column: str, kind: str):
    if _pg():
        _run(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {kind}")
        return
    existing = {r["name"] for r in _run(f"PRAGMA table_info({table})", fetch="all")}
    if column not in existing:
        _run(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


def clean_phone_number(phone_str: str) -> str:
    """Cleans phone numbers into a standard international format."""
    if not phone_str:
        return ""
    return re.sub(r"[^\d+]", "", phone_str)


# ---------------------------------------------------------------- users and approval

def get_user(telegram_id: int) -> dict | None:
    return _run("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,), "one")


def _ensure_user(telegram_id: int):
    """The admin may never have asked for access; give them a row when something is stored for them."""
    if not get_user(telegram_id):
        _run("INSERT INTO users (telegram_id, status, requested_at) VALUES (?, 'approved', ?)", (telegram_id, now_str()))


def is_approved(telegram_id: int) -> bool:
    if config.ADMIN_TELEGRAM_ID and telegram_id == config.ADMIN_TELEGRAM_ID:
        return True
    u = get_user(telegram_id)
    return bool(u and u["status"] == "approved")


def request_access(telegram_id: int, name: str, username: str | None) -> str:
    """Creates a pending request if there is none. Returns the user's status."""
    u = get_user(telegram_id)
    if u:
        return u["status"]
    _run(
        "INSERT INTO users (telegram_id, name, username, status, requested_at) VALUES (?, ?, ?, 'pending', ?)",
        (telegram_id, name, username, now_str()),
    )
    return "pending"


def decide_access(telegram_id: int, approve: bool) -> bool:
    return bool(_run(
        "UPDATE users SET status = ?, decided_at = ? WHERE telegram_id = ?",
        ("approved" if approve else "denied", now_str(), telegram_id),
        "rowcount",
    ))


def get_last_note(telegram_id: int) -> str | None:
    u = get_user(telegram_id)
    return u["last_note"] if u else None


def set_last_note(telegram_id: int, note: str):
    _ensure_user(telegram_id)
    _run("UPDATE users SET last_note = ? WHERE telegram_id = ?", (note, telegram_id))


# ---------------------------------------------------------------- Google connection

def save_google(telegram_id: int, email: str, refresh_token_enc: str):
    _ensure_user(telegram_id)
    _run(
        "UPDATE users SET google_email = ?, refresh_token_enc = ? WHERE telegram_id = ?",
        (email, refresh_token_enc, telegram_id),
    )


def set_google_ids(telegram_id: int, sheet_id: str | None = None, contact_group: str | None = None):
    if sheet_id is not None:
        _run("UPDATE users SET sheet_id = ? WHERE telegram_id = ?", (sheet_id, telegram_id))
    if contact_group is not None:
        _run("UPDATE users SET contact_group = ? WHERE telegram_id = ?", (contact_group, telegram_id))


def clear_google(telegram_id: int):
    """Forgets the connection. The sheet id is kept, so reconnecting continues the same sheet."""
    _run("UPDATE users SET google_email = NULL, refresh_token_enc = NULL WHERE telegram_id = ?", (telegram_id,))


def create_oauth_state(state: str, telegram_id: int, minutes: int = 10):
    _run("DELETE FROM oauth_states WHERE expires_at < ?", (now_str(),))
    expires = (datetime.now() + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    _run("INSERT INTO oauth_states (state, telegram_id, expires_at) VALUES (?, ?, ?)", (state, telegram_id, expires))


def consume_oauth_state(state: str) -> int | None:
    """Single use: returns the Telegram id the link was made for, or None if unknown or expired."""
    row = _run("SELECT telegram_id, expires_at FROM oauth_states WHERE state = ?", (state,), "one")
    _run("DELETE FROM oauth_states WHERE state = ?", (state,))
    if not row or row["expires_at"] < datetime.now().strftime("%Y-%m-%d %H:%M:%S"):
        return None
    return int(row["telegram_id"])


# ---------------------------------------------------------------- active event

def set_event(owner_id: int, name: str | None):
    _run("DELETE FROM active_events WHERE owner_id = ?", (owner_id,))
    if name:
        expires = (datetime.now() + timedelta(hours=config.EVENT_HOURS)).strftime("%Y-%m-%d %H:%M")
        _run("INSERT INTO active_events (owner_id, name, expires_at) VALUES (?, ?, ?)", (owner_id, name, expires))


def get_event(owner_id: int) -> str | None:
    row = _run("SELECT name, expires_at FROM active_events WHERE owner_id = ?", (owner_id,), "one")
    if not row:
        return None
    if row["expires_at"] < now_str():
        set_event(owner_id, None)
        return None
    return row["name"]
