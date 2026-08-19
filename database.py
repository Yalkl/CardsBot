from datetime import datetime
import re
import sqlite3
from config import DATABASE_URL, LOCAL_SQLITE_PATH
import psycopg2


def get_db_connection():
    """Returns PostgreSQL connection if DATABASE_URL is set, else SQLite connection."""
    if DATABASE_URL:
        return psycopg2.connect(DATABASE_URL)
    return sqlite3.connect(LOCAL_SQLITE_PATH)


def init_db():
    """Initializes user_contacts table."""
    conn = get_db_connection()
    cursor = conn.cursor()
    if DATABASE_URL:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS user_contacts (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                full_name TEXT,
                phone TEXT,
                email TEXT,
                scanned_date TEXT
            );
        """
        )
    else:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS user_contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                full_name TEXT,
                phone TEXT,
                email TEXT,
                scanned_date TEXT
            );
        """
        )
    conn.commit()
    cursor.close()
    conn.close()


def clean_phone_number(phone_str: str) -> str:
    """Cleans phone numbers into a standard international format."""
    if not phone_str:
        return ""
    return re.sub(r"[^\d+]", "", phone_str)


def check_duplicate_contact(
    user_id: int, first_name: str, last_name: str, phone: str, email: str
):
    """Checks if this specific user already scanned a contact by Phone, Email, or Name."""
    cleaned_phone = clean_phone_number(phone)
    digits_only = re.sub(r"\D", "", cleaned_phone)
    cleaned_email = email.strip().lower() if email else ""

    target_first = first_name.strip().lower() if first_name else ""
    target_last = last_name.strip().lower() if last_name else ""
    target_full = f"{target_first} {target_last}".strip()

    conn = get_db_connection()
    cursor = conn.cursor()

    param_placeholder = "%s" if DATABASE_URL else "?"
    cursor.execute(
        f"SELECT full_name, phone, email, scanned_date FROM user_contacts WHERE user_id = {param_placeholder}",
        (user_id,),
    )
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    for row_name, row_phone, row_email, row_date in rows:
        row_digits = re.sub(r"\D", "", clean_phone_number(row_phone))
        row_clean_name = row_name.strip().lower() if row_name else ""

        # 1. Phone Match (last 8 digits)
        if digits_only and row_digits:
            if (
                digits_only == row_digits
                or digits_only.endswith(row_digits[-8:])
                or row_digits.endswith(digits_only[-8:])
            ):
                return {
                    "name": row_name,
                    "phone": row_phone,
                    "date": row_date,
                    "match_type": "Phone",
                }

        # 2. Email Match
        if (
            cleaned_email
            and row_email
            and cleaned_email == row_email.strip().lower()
        ):
            return {
                "name": row_name,
                "email": row_email,
                "date": row_date,
                "match_type": "Email",
            }

        # 3. Name Match
        if target_full and len(target_full) > 3 and target_full in row_clean_name:
            return {
                "name": row_name,
                "phone": row_phone,
                "date": row_date,
                "match_type": "Name",
            }

    return None


def record_saved_contact(user_id: int, data: dict):
    """Saves the scanned contact into database for future duplicate checks."""
    full_name = f"{data.get('first_name', '')} {data.get('last_name', '')}".strip()
    phone = clean_phone_number(data.get("phone", ""))
    email = data.get("email", "").strip()
    today_str = datetime.now().strftime("%d/%m/%Y %H:%M")

    conn = get_db_connection()
    cursor = conn.cursor()

    param_placeholder = "%s, %s, %s, %s, %s" if DATABASE_URL else "?, ?, ?, ?, ?"
    cursor.execute(
        f"""
        INSERT INTO user_contacts (user_id, full_name, phone, email, scanned_date)
        VALUES ({param_placeholder})
    """,
        (user_id, full_name, phone, email, today_str),
    )
    conn.commit()
    cursor.close()
    conn.close()