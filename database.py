"""Storage (docs/SPEC.md): PostgreSQL when DATABASE_URL is set, SQLite otherwise (local runs and tests).

Every contact belongs to the Telegram user who scanned it (owner_id); every read is filtered by it.
"""

from datetime import datetime, timedelta
import re
import sqlite3

import psycopg2

import config

# Card fields, as extracted (Latin script) plus the Hebrew display fields.
CONTACT_FIELDS = (
    "first_name", "last_name", "company", "job_title", "country",
    "phone", "email", "website", "telegram", "address",
    "first_name_he", "last_name_he", "job_title_he", "company_he",
)


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
    pk = "SERIAL PRIMARY KEY" if _pg() else "INTEGER PRIMARY KEY AUTOINCREMENT"
    big = "BIGINT" if _pg() else "INTEGER"
    fields = ",\n".join(f"    {f} TEXT" for f in CONTACT_FIELDS)
    for stmt in (
        f"""CREATE TABLE IF NOT EXISTS users (
    telegram_id {big} PRIMARY KEY,
    name TEXT,
    username TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    requested_at TEXT,
    decided_at TEXT,
    last_note TEXT
)""",
        f"""CREATE TABLE IF NOT EXISTS cards (
    id {pk},
    owner_id {big} NOT NULL,
    scanned_at TEXT NOT NULL,
{fields},
    event TEXT,
    note TEXT,
    google_resource TEXT
)""",
        "CREATE INDEX IF NOT EXISTS cards_owner_idx ON cards (owner_id)",
        f"""CREATE TABLE IF NOT EXISTS active_events (
    owner_id {big} PRIMARY KEY,
    name TEXT NOT NULL,
    expires_at TEXT NOT NULL
)""",
    ):
        _run(stmt)


def clean_phone_number(phone_str: str) -> str:
    """Cleans phone numbers into a standard international format."""
    if not phone_str:
        return ""
    return re.sub(r"[^\d+]", "", phone_str)


# ---------------------------------------------------------------- users and approval

def get_user(telegram_id: int) -> dict | None:
    return _run("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,), "one")


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
    """Sets approved/denied. Returns False if there is no such request."""
    return bool(_run(
        "UPDATE users SET status = ?, decided_at = ? WHERE telegram_id = ?",
        ("approved" if approve else "denied", now_str(), telegram_id),
        "rowcount",
    ))


def get_last_note(telegram_id: int) -> str | None:
    u = get_user(telegram_id)
    return u["last_note"] if u else None


def set_last_note(telegram_id: int, note: str):
    if not get_user(telegram_id):  # the admin may never have requested access
        _run("INSERT INTO users (telegram_id, status, requested_at) VALUES (?, 'approved', ?)", (telegram_id, now_str()))
    _run("UPDATE users SET last_note = ? WHERE telegram_id = ?", (note, telegram_id))


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


# ---------------------------------------------------------------- cards

def insert_card(owner_id: int, data: dict, event: str | None, note: str | None) -> int:
    values = [(data.get(f) or "").strip() for f in CONTACT_FIELDS]
    values[CONTACT_FIELDS.index("phone")] = clean_phone_number(data.get("phone", ""))
    cols = ", ".join(("owner_id", "scanned_at") + CONTACT_FIELDS + ("event", "note"))
    marks = ", ".join(["?"] * (2 + len(CONTACT_FIELDS) + 2))
    params = (owner_id, now_str(), *values, event or "", note or "")
    if _pg():
        return _run(f"INSERT INTO cards ({cols}) VALUES ({marks}) RETURNING id", params, "one")["id"]
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"INSERT INTO cards ({cols}) VALUES ({marks})", params)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_card(owner_id: int, card_id: int, data: dict, event: str | None, note: str | None) -> bool:
    """Updates an existing card with the new scan: non-empty new values win; the new note is appended."""
    old = get_card(owner_id, card_id)
    if not old:
        return False
    merged = {f: (data.get(f) or "").strip() or (old.get(f) or "") for f in CONTACT_FIELDS}
    merged["phone"] = clean_phone_number(merged["phone"])
    notes = [n for n in (old.get("note"), note) if n]
    sets = ", ".join(f"{f} = ?" for f in CONTACT_FIELDS)
    _run(
        f"UPDATE cards SET {sets}, event = ?, note = ?, scanned_at = ? WHERE owner_id = ? AND id = ?",
        (*[merged[f] for f in CONTACT_FIELDS], event or old.get("event") or "", " | ".join(notes), now_str(), owner_id, card_id),
    )
    return True


def get_card(owner_id: int, card_id: int) -> dict | None:
    return _run("SELECT * FROM cards WHERE owner_id = ? AND id = ?", (owner_id, card_id), "one")


def all_cards(owner_id: int) -> list[dict]:
    return _run("SELECT * FROM cards WHERE owner_id = ? ORDER BY scanned_at DESC, id DESC", (owner_id,), "all")


def find_duplicate(owner_id: int, data: dict) -> dict | None:
    """Same person already saved? By phone (last 8 digits), then email, then full name."""
    digits = re.sub(r"\D", "", clean_phone_number(data.get("phone", "")))
    email = (data.get("email") or "").strip().lower()
    full = f"{data.get('first_name', '')} {data.get('last_name', '')}".strip().lower()
    for row in all_cards(owner_id):
        row_digits = re.sub(r"\D", "", row.get("phone") or "")
        row_full = f"{row.get('first_name') or ''} {row.get('last_name') or ''}".strip().lower()
        match = None
        if digits and row_digits and (digits[-8:] == row_digits[-8:]):
            match = "טלפון"
        elif email and (row.get("email") or "").strip().lower() == email:
            match = "מייל"
        elif full and len(full) > 3 and full == row_full:
            match = "שם"
        if match:
            return {"id": row["id"], "row": row, "match_type": match}
    return None


SEARCH_FIELDS = CONTACT_FIELDS + ("event", "note")


def search_cards(owner_id: int, terms: list[str], date_from: str | None = None, date_to: str | None = None, limit: int = 10) -> list[dict]:
    """Cards in the date range (inclusive, YYYY-MM-DD) ranked by how many terms appear in any field.

    With no terms, every card in the range matches. The list is small, so this runs in Python and
    behaves the same on SQLite and PostgreSQL.
    """
    terms = [t.strip().lower() for t in terms if t and len(t.strip()) >= 2]
    scored = []
    for row in all_cards(owner_id):
        day = (row.get("scanned_at") or "")[:10]
        if date_from and day < date_from:
            continue
        if date_to and day > date_to:
            continue
        if not terms:
            scored.append((0, row))
            continue
        haystack = " | ".join(str(row.get(f) or "") for f in SEARCH_FIELDS).lower()
        digits = re.sub(r"\D", "", row.get("phone") or "")
        score = sum(1 for t in terms if t in haystack or (len(re.sub(r"\D", "", t)) >= 4 and re.sub(r"\D", "", t) in digits))
        if score:
            scored.append((score, row))
    scored.sort(key=lambda s: s[0], reverse=True)  # stable: newest first within a score
    return [row for _, row in scored[:limit]]
