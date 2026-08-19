from datetime import datetime
import re
from database import clean_phone_number
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def build_formatted_name(data: dict, event_name: str = None) -> str:
    """Constructs display name: BC [Event] | Name/Business - Organization/Country - Title."""
    full_name = f"{data.get('first_name', '')} {data.get('last_name', '')}".strip()
    company = (data.get("company") or "").strip()
    country = (data.get("country") or "").strip()
    job_title = (data.get("job_title") or "").strip()

    # If no person name, the company/business is the primary name
    primary = full_name if full_name else company
    name_elements = [primary] if primary else []

    if full_name:
        entity = ""
        if company and country:
            if country.lower() in company.lower():
                entity = country
            else:
                entity = f"{company} ({country})"
        elif company:
            entity = company
        elif country:
            entity = country

        if entity:
            name_elements.append(entity)
    elif country:
        name_elements.append(country)

    if job_title:
        name_elements.append(job_title)

    base_info = " - ".join([el for el in name_elements if el]) or "Contact"

    if event_name:
        return f"BC [{event_name}] | {base_info}"
    return f"BC | {base_info}"


def generate_vcard(data: dict, event_name: str = None) -> str:
    """Generates standard vCard 3.0 string with website, WhatsApp, and Telegram."""
    formatted_name = build_formatted_name(data, event_name)
    phone = clean_phone_number(data.get("phone", ""))
    digits_only = re.sub(r"\D", "", phone)
    wa_link = f"https://wa.me/{digits_only}" if digits_only else ""

    tg_raw = (data.get("telegram") or "").strip().lstrip("@")
    tg_link = f"https://t.me/{tg_raw}" if tg_raw else ""

    website = (data.get("website") or "").strip()
    if website and not website.startswith("http"):
        website = f"https://{website}"

    today_str = datetime.now().strftime("%d/%m/%Y")

    notes = [f"📅 Scanned Date: {today_str}"]
    if wa_link:
        notes.append(f"💬 WhatsApp: {wa_link}")
    if tg_link:
        notes.append(f"✈️ Telegram: {tg_link}")
    if website:
        notes.append(f"🌐 Website: {website}")
    if event_name:
        notes.insert(0, f"🏷️ Event: {event_name}")
    note_content = "\\n".join(notes)

    lines = [
        "BEGIN:VCARD",
        "VERSION:3.0",
        f"N:;{formatted_name};;;",
        f"FN:{formatted_name}",
    ]
    if data.get("company"):
        lines.append(f"ORG:{data.get('company')}")
    if data.get("job_title"):
        lines.append(f"TITLE:{data.get('job_title')}")
    if phone:
        lines.append(f"TEL;TYPE=CELL,WORK:{phone}")
    if data.get("email"):
        lines.append(f"EMAIL;TYPE=INTERNET:{data.get('email')}")
    if website:
        lines.append(f"URL;TYPE=WORK:{website}")
    elif wa_link:
        lines.append(f"URL:{wa_link}")
    if data.get("address") or data.get("country"):
        full_addr = f"{data.get('address', '')}, {data.get('country', '')}".strip(", ")
        lines.append(f"ADR;TYPE=WORK:;;{full_addr};;;;")
    lines.append(f"NOTE:{note_content}")
    lines.append("END:VCARD")

    return "\n".join(lines)


def render_preview(
    data: dict, duplicate_info: dict = None
) -> tuple[str, InlineKeyboardMarkup]:
    """Builds the preview message and inline action buttons."""
    name_preview = build_formatted_name(data)
    phone = clean_phone_number(data.get("phone", ""))
    digits_only = re.sub(r"\D", "", phone)
    wa_preview = f"https://wa.me/{digits_only}" if digits_only else "Not detected"

    tg_handle = (data.get("telegram") or "").strip()
    tg_display = f"https://t.me/{tg_handle.lstrip('@')}" if tg_handle else "Not detected"
    website_display = data.get("website") or "Not detected"

    dup_alert = ""
    if duplicate_info:
        dup_alert = (
            f"⚠️ **Duplicate Found in your History!**\n"
            f"You previously scanned this contact on **{duplicate_info['date']}** "
            f"as `{duplicate_info['name']}` ({duplicate_info['match_type']} matched).\n\n---\n"
        )

    person_name = f"{data.get('first_name', '')} {data.get('last_name', '')}".strip()

    text = (
        f"{dup_alert}"
        "📋 **Scanned Business Card Details:**\n\n"
        f"🏷️ **Contact Name:** `{name_preview}`\n"
        f"🏢 **Business / Company:** {data.get('company') or 'Not specified'}\n"
        f"👤 **Person:** {person_name or 'None (Business Card)'}\n"
        f"💼 **Title:** {data.get('job_title') or 'Not specified'}\n"
        f"🌍 **Country:** {data.get('country') or 'Not specified'}\n"
        f"📞 **Phone:** `{phone or 'Not detected'}`\n"
        f"💬 **WhatsApp:** {wa_preview}\n"
        f"✈️ **Telegram:** {tg_display}\n"
        f"🌐 **Website:** {website_display}\n"
        f"✉️ **Email:** {data.get('email') or 'Not specified'}\n"
        f"📍 **Address:** {data.get('address') or 'Not specified'}\n\n"
        "How would you like to proceed?"
    )

    btn_label = (
        "🔄 Update / Send Contact Anyway"
        if duplicate_info
        else "✅ Save Default"
    )

    keyboard = [
        [InlineKeyboardButton(btn_label, callback_data="save_default")],
        [
            InlineKeyboardButton(
                "🏷️ Assign to Event / Conference", callback_data="ask_event"
            )
        ],
        [
            InlineKeyboardButton(
                "✏️ Edit Details", callback_data="open_edit_menu"
            )
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ]
    return text, InlineKeyboardMarkup(keyboard)
