"""Text shown in Telegram and the contact files (docs/SPEC.md). Messages use HTML; user text is always escaped."""

from datetime import datetime
from html import escape
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from database import clean_phone_number


def _s(data: dict, key: str) -> str:
    return (data.get(key) or "").strip()


def english_name(data: dict) -> str:
    return f"{_s(data, 'first_name')} {_s(data, 'last_name')}".strip()


def hebrew_name(data: dict) -> str:
    return f"{_s(data, 'first_name_he')} {_s(data, 'last_name_he')}".strip()


def build_display_name(data: dict) -> str:
    """The name shown on the phone: "ניקיטה איבנוב · סגן שגריר · שגרירות בולגריה · Nikita Ivanov".

    Hebrew name, title and institution when there are, otherwise the original; brand names stay as on the card.
    """
    he = hebrew_name(data)
    en = english_name(data)
    title = _s(data, "job_title_he") or _s(data, "job_title")
    org = _s(data, "company_he") or _s(data, "company")
    parts = [he or en, title, org]
    if he and en:
        parts.append(en)
    return " · ".join(p for p in parts if p) or "איש קשר"


def _vc(text: str) -> str:
    """Escapes free text for a vCard 3.0 value."""
    return text.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\r\n", "\\n").replace("\n", "\\n")


def contact_notes(card: dict, include_note: bool = True) -> str:
    """The notes text of a contact: scan date, event, the user's note, WhatsApp and Telegram links."""
    digits = re.sub(r"\D", "", clean_phone_number(card.get("phone", "")))
    tg = _s(card, "telegram").lstrip("@")
    when = (card.get("scanned_at") or datetime.now().strftime("%Y-%m-%d"))[:10]
    notes = [f"נסרק {when}"]
    if _s(card, "event"):
        notes.append(f"אירוע: {_s(card, 'event')}")
    if _s(card, "note") and include_note:
        notes.append(f"הערה: {_s(card, 'note')}")
    if digits:
        notes.append(f"WhatsApp: https://wa.me/{digits}")
    if tg:
        notes.append(f"Telegram: https://t.me/{tg}")
    return "\n".join(notes)


def generate_vcard(card: dict, include_note: bool = True) -> str:
    """vCard 3.0: the display name on top, the real fields below (company and title as on the card)."""
    data = card
    display = build_display_name(data)
    phone = clean_phone_number(data.get("phone", ""))
    website = _s(data, "website")
    if website and not website.startswith("http"):
        website = f"https://{website}"

    lines = ["BEGIN:VCARD", "VERSION:3.0", f"N:;{_vc(display)};;;", f"FN:{_vc(display)}"]
    if english_name(data):
        lines.append(f"NICKNAME:{_vc(english_name(data))}")
    if _s(data, "company"):
        lines.append(f"ORG:{_vc(_s(data, 'company'))}")
    if _s(data, "job_title"):
        lines.append(f"TITLE:{_vc(_s(data, 'job_title'))}")
    office = clean_phone_number(data.get("phone_office", ""))
    if phone:
        lines.append(f"TEL;TYPE=CELL:{phone}")
    if office and office != phone:
        lines.append(f"TEL;TYPE=WORK,VOICE:{office}")
    if _s(data, "email"):
        lines.append(f"EMAIL;TYPE=INTERNET:{_s(data, 'email')}")
    if website:
        lines.append(f"URL;TYPE=WORK:{website}")
    addr = ", ".join(x for x in (_s(data, "address"), _s(data, "country")) if x)
    if addr:
        lines.append(f"ADR;TYPE=WORK:;;{_vc(addr)};;;;")
    lines.append(f"NOTE:{_vc(contact_notes(card, include_note))}")
    lines.append("END:VCARD")
    return "\r\n".join(lines)


def card_summary(data: dict) -> str:
    """The preview body: display name and the fields, HTML-escaped."""
    phone = clean_phone_number(data.get("phone", ""))
    rows = [f"<b>{escape(build_display_name(data))}</b>"]
    for icon, value in (
        ("📱", phone),
        ("☎️", clean_phone_number(data.get("phone_office", ""))),
        ("✉️", _s(data, "email")),
        ("🌐", _s(data, "website")),
        ("✈️", _s(data, "telegram")),
        ("🌍", ", ".join(x for x in (_s(data, "address"), _s(data, "country")) if x)),
    ):
        if value:
            rows.append(f"{icon} {escape(value)}")
    return "\n".join(rows)


def render_preview(data: dict, duplicate: dict | None = None, event: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    parts = []
    if duplicate:
        row = duplicate["row"]
        parts.append(f"⚠️ <b>כבר שמור</b> (לפי {duplicate['match_type']}): {escape(build_display_name(row))} · נסרק {escape((row.get('scanned_at') or '')[:10])}\n")
    if not clean_phone_number(data.get("phone", "")) and not clean_phone_number(data.get("phone_office", "")):
        parts.append("💡 לא נמצא טלפון. אם הוא בצד השני של הכרטיס, לחצו \"צד שני\".\n")
    parts.append(card_summary(data))
    if event:
        parts.append(f"🏷️ אירוע: {escape(event)}")

    if duplicate:
        first_row = [
            InlineKeyboardButton("🔄 עדכן את הקיים", callback_data="save_update"),
            InlineKeyboardButton("➕ שמור כחדש", callback_data="save_default"),
        ]
    else:
        first_row = [InlineKeyboardButton("✅ שמור", callback_data="save_default")]
    keyboard = [
        first_row,
        [
            InlineKeyboardButton("✏️ תיקון", callback_data="open_edit_menu"),
            InlineKeyboardButton("📷 צד שני", callback_data="scan_second_side"),
        ],
        [InlineKeyboardButton("❌ ביטול", callback_data="cancel")],
    ]
    return "\n".join(parts), InlineKeyboardMarkup(keyboard)


EDIT_FIELDS = [
    ("name_he", "👤 שם בעברית"),
    ("name", "🔤 שם באנגלית"),
    ("job_title_he", "💼 תפקיד"),
    ("company", "🏢 חברה"),
    ("phone", "📱 נייד"),
    ("phone_office", "☎️ טלפון משרד"),
    ("email", "✉️ מייל"),
]


def edit_menu() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(label, callback_data=f"edit_field_{key}")] for key, label in EDIT_FIELDS]
    rows.append([InlineKeyboardButton("🔙 חזרה", callback_data="back_to_preview")])
    return InlineKeyboardMarkup(rows)


def render_results(rows: list[dict], header: str) -> tuple[str, InlineKeyboardMarkup | None]:
    """Search results: one line per person with company, date, event and note; a button per person and "share all"."""
    if not rows:
        return f"{header}\n\nלא מצאתי אף אחד.", None
    lines = [header, ""]
    for i, r in enumerate(rows, 1):
        meta = " · ".join(x for x in ((r.get("scanned_at") or "")[:10], r.get("event") or "") if x)
        lines.append(f"{i}. <b>{escape(build_display_name(r))}</b>")
        if r.get("phone"):
            lines.append(f"   📱 {escape(r['phone'])}")
        if r.get("phone_office"):
            lines.append(f"   ☎️ {escape(r['phone_office'])}")
        if meta:
            lines.append(f"   <i>{escape(meta)}</i>")
        if r.get("note"):
            lines.append(f"   📝 {escape(r['note'])}")
    buttons = [InlineKeyboardButton(f"📇 {i}", callback_data=f"send_{r['id']}") for i, r in enumerate(rows, 1)]
    keyboard = [buttons[i : i + 5] for i in range(0, len(buttons), 5)]
    if len(rows) > 1:
        keyboard.append([InlineKeyboardButton(f"📤 שתף את כל ה-{len(rows)}", callback_data="share_all")])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)
