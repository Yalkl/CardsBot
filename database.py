from datetime import datetime
import re
import sqlite3
from config import DATABASE_URL, LOCAL_SQLITE_PATH
import psycopg2

# Card fields stored for each contact, in display order.
CONTACT_FIELDS = (
    "first_name", "last_name", "company", "job_title", "country",
    "phone", "email", "website", "telegram", "address",
)


def get_db_connection():
    """Returns PostgreSQL connection if DATABASE_URL is set, else SQLite connection."""
    if DATABASE_URL:
        return psycopg2.connect(DATABASE_URL)
    return sqlite3.connect(LOCAL_SQLITE_PATH)


def _sql(query: str) -> str:
    """Queries are written with '?' placeholders; PostgreSQL wants '%s'."""
    return query.replace("?", "%s") if DATABASE_URL else query


def _fetch_dicts(cursor) -> list[dict]:
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def init_db():
    """Initializes user_contacts (per-user scan history) and contacts (shared team list)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    pk = "SERIAL PRIMARY KEY" if DATABASE_URL else "INTEGER PRIMARY KEY AUTOINCREMENT"
    uid = "BIGINT" if DATABASE_URL else "INTEGER"
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS user_contacts (
            id {pk},
            user_id {uid} NOT NULL,
            full_name TEXT,
            phone TEXT,
            email TEXT,
            scanned_date TEXT
        );
    """
    )
    fields = ",\n".join(f"            {f} TEXT" for f in CONTACT_FIELDS)
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS contacts (
            id {pk},
            workspace TEXT NOT NULL,
            scanned_by_id {uid} NOT NULL,
            scanned_by_name TEXT,
            scanned_at TEXT NOT NULL,
{fields},
            event TEXT,
            note TEXT,
            saved_to_phone INTEGER NOT NULL DEFAULT 0
        );
    """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS contacts_workspace_idx ON contacts (workspace);")
    conn.commit()
    cursor.close()
    conn.close()


def clean_phone_number(phone_str: str) -> str:
    """Cleans phone numbers into a standard international format."""
    if not phone_str:
        return ""
    return re.sub(r"[^\d+]", "", phone_str)


def _match_type(digits: str, email: str, full_name: str, row_name: str, row_phone: str, row_email: str):
    """Returns 'Phone', 'Email' or 'Name' when the row looks like the same person, else None."""
    row_digits = re.sub(r"\D", "", clean_phone_number(row_phone))
    row_clean_name = row_name.strip().lower() if row_name else ""

    # 1. Phone Match (last 8 digits)
    if digits and row_digits:
        if (
            digits == row_digits
            or digits.endswith(row_digits[-8:])
            or row_digits.endswith(digits[-8:])
        ):
            return "Phone"

    # 2. Email Match
    if email and row_email and email == row_email.strip().lower():
        return "Email"

    # 3. Name Match
    if full_name and len(full_name) > 3 and full_name in row_clean_name:
        return "Name"
    return None


def _match_keys(first_name: str, last_name: str, phone: str, email: str):
    digits = re.sub(r"\D", "", clean_phone_number(phone))
    cleaned_email = email.strip().lower() if email else ""
    target_first = first_name.strip().lower() if first_name else ""
    target_last = last_name.strip().lower() if last_name else ""
    return digits, cleaned_email, f"{target_first} {target_last}".strip()


def check_duplicate_contact(
    user_id: int, first_name: str, last_name: str, phone: str, email: str
):
    """Checks if this specific user already scanned a contact by Phone, Email, or Name."""
    keys = _match_keys(first_name, last_name, phone, email)

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        _sql("SELECT full_name, phone, email, scanned_date FROM user_contacts WHERE user_id = ?"),
        (user_id,),
    )
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    for row_name, row_phone, row_email, row_date in rows:
        match = _match_type(*keys, row_name, row_phone, row_email)
        if match:
            return {"name": row_name, "phone": row_phone, "email": row_email, "date": row_date, "match_type": match}
    return None


def record_saved_contact(user_id: int, data: dict):
    """Saves the scanned contact into database for future duplicate checks."""
    full_name = f"{data.get('first_name', '')} {data.get('last_name', '')}".strip()
    phone = clean_phone_number(data.get("phone", ""))
    email = data.get("email", "").strip()
    today_str = datetime.now().strftime("%d/%m/%Y %H:%M")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        _sql("INSERT INTO user_contacts (user_id, full_name, phone, email, scanned_date) VALUES (?, ?, ?, ?, ?)"),
        (user_id, full_name, phone, email, today_str),
    )
    conn.commit()
    cursor.close()
    conn.close()


# ---------- Shared team contacts ----------

def insert_contact(workspace: str, user_id: int, user_name: str, data: dict, event: str | None, note: str | None) -> int:
    """Stores a full contact in the shared list. Returns its id."""
    values = [(data.get(f) or "").strip() for f in CONTACT_FIELDS]
    values[CONTACT_FIELDS.index("phone")] = clean_phone_number(data.get("phone", ""))
    cols = ", ".join(("workspace", "scanned_by_id", "scanned_by_name", "scanned_at") + CONTACT_FIELDS + ("event", "note"))
    marks = ", ".join(["?"] * (4 + len(CONTACT_FIELDS) + 2))
    params = (workspace, user_id, user_name, datetime.now().strftime("%Y-%m-%d %H:%M"), *values, event or "", note or "")

    conn = get_db_connection()
    cursor = conn.cursor()
    if DATABASE_URL:
        cursor.execute(_sql(f"INSERT INTO contacts ({cols}) VALUES ({marks}) RETURNING id"), params)
        new_id = cursor.fetchone()[0]
    else:
        cursor.execute(f"INSERT INTO contacts ({cols}) VALUES ({marks})", params)
        new_id = cursor.lastrowid
    conn.commit()
    cursor.close()
    conn.close()
    return new_id


def _select(query: str, params: tuple) -> list[dict]:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(_sql(query), params)
    rows = _fetch_dicts(cursor)
    cursor.close()
    conn.close()
    return rows


def get_contact(workspace: str, contact_id: int) -> dict | None:
    rows = _select("SELECT * FROM contacts WHERE workspace = ? AND id = ?", (workspace, contact_id))
    return rows[0] if rows else None


def all_contacts(workspace: str) -> list[dict]:
    return _select("SELECT * FROM contacts WHERE workspace = ? ORDER BY scanned_at DESC, id DESC", (workspace,))


def list_unsaved(workspace: str, user_id: int, limit: int = 20) -> list[dict]:
    """Contacts this user scanned but has not marked as saved to their phone, newest first."""
    rows = _select(
        "SELECT * FROM contacts WHERE workspace = ? AND scanned_by_id = ? AND saved_to_phone = 0 "
        "ORDER BY scanned_at DESC, id DESC",
        (workspace, user_id),
    )
    return rows[:limit]


def mark_saved(workspace: str, contact_id: int, user_id: int) -> bool:
    """Marks a contact as saved to the scanner's phone. Only the person who scanned it can mark it."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        _sql("UPDATE contacts SET saved_to_phone = 1 WHERE workspace = ? AND id = ? AND scanned_by_id = ?"),
        (workspace, contact_id, user_id),
    )
    changed = cursor.rowcount > 0
    conn.commit()
    cursor.close()
    conn.close()
    return changed


def find_team_duplicate(workspace: str, data: dict) -> dict | None:
    """Same matching as check_duplicate_contact, against the shared list (so Tamar sees Yehuda's scans)."""
    keys = _match_keys(data.get("first_name", ""), data.get("last_name", ""), data.get("phone", ""), data.get("email", ""))
    for row in all_contacts(workspace):
        row_name = f"{row['first_name'] or ''} {row['last_name'] or ''}".strip() or (row["company"] or "")
        match = _match_type(*keys, row_name, row["phone"], row["email"])
        if match:
            return {
                "id": row["id"],
                "name": row_name,
                "date": row["scanned_at"],
                "match_type": match,
                "by": row["scanned_by_name"],
                "by_id": row["scanned_by_id"],
                "saved": bool(row["saved_to_phone"]),
            }
    return None


SEARCH_FIELDS = CONTACT_FIELDS + ("event", "note", "scanned_by_name")


def search_contacts(workspace: str, terms: list[str], limit: int = 10) -> list[dict]:
    """Ranks contacts by how many search terms appear in any field (case-insensitive substring).

    The list is small (hundreds of people), so filtering in Python keeps SQLite and PostgreSQL identical.
    """
    terms = [t.strip().lower() for t in terms if t and len(t.strip()) >= 2]
    if not terms:
        return []
    scored = []
    for row in all_contacts(workspace):
        haystack = " | ".join(str(row.get(f) or "") for f in SEARCH_FIELDS).lower()
        digits = re.sub(r"\D", "", row.get("phone") or "")
        score = 0
        for t in terms:
            t_digits = re.sub(r"\D", "", t)
            if t in haystack or (len(t_digits) >= 4 and t_digits == t.replace("+", "").replace("-", "").replace(" ", "") and t_digits in digits):
                score += 1
        if score:
            scored.append((score, row))
    scored.sort(key=lambda s: s[0], reverse=True)  # stable: newest first within the same score
    return [row for _, row in scored[:limit]]
