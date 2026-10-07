"""Append-only export of saved contacts to a Google Sheet, through an Apps Script web app.

The bot can only add rows: it never gets read access to the sheet, so even if the bot's
server leaks, the contact list does not. See apps_script/Code.gs and docs/SHEETS_SETUP.md.
"""

import asyncio
import json
import urllib.request
from datetime import datetime

from config import SHEETS_USER_IDS, SHEETS_WEBHOOK_SECRET, SHEETS_WEBHOOK_URL
from database import clean_phone_number


def notes_enabled_for(user_id: int) -> bool:
    """Users on the list get the note step after saving; everyone else keeps the original flow."""
    return user_id in SHEETS_USER_IDS


def sheets_enabled_for(user_id: int) -> bool:
    return bool(SHEETS_WEBHOOK_URL and SHEETS_WEBHOOK_SECRET and notes_enabled_for(user_id))


def _post(payload: dict) -> bool:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        SHEETS_WEBHOOK_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # Apps Script answers with a redirect to the result; urllib follows it.
    with urllib.request.urlopen(req, timeout=20) as resp:
        result = json.loads(resp.read().decode("utf-8") or "{}")
    return bool(result.get("ok"))


async def append_contact(scanned_by: str, data: dict, event_name: str | None, note: str | None) -> bool:
    """Adds one row. Returns False on any failure; never raises and never logs contact data."""
    phone = clean_phone_number(data.get("phone", ""))
    digits = "".join(ch for ch in phone if ch.isdigit())
    row = {
        "secret": SHEETS_WEBHOOK_SECRET,
        "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "scanned_by": scanned_by,
        "name": f"{data.get('first_name', '')} {data.get('last_name', '')}".strip(),
        "company": data.get("company", ""),
        "job_title": data.get("job_title", ""),
        "country": data.get("country", ""),
        "phone": phone,
        "email": data.get("email", ""),
        "whatsapp": f"https://wa.me/{digits}" if digits else "",
        "telegram": data.get("telegram", ""),
        "website": data.get("website", ""),
        "address": data.get("address", ""),
        "event": event_name or "",
        "note": note or "",
    }
    try:
        return await asyncio.to_thread(_post, row)
    except Exception:
        return False
