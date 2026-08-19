from datetime import datetime
import io
import json
import os
import re
import sqlite3
from dotenv import load_dotenv
from google import genai
from google.genai import types
from PIL import Image
import psycopg2
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Load environment variables
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
LOCAL_SQLITE_PATH = "contacts_history.db"

gemini_client = genai.Client(api_key=GEMINI_API_KEY)


# ==========================================
# Database Management (PostgreSQL / SQLite)
# ==========================================


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


# ==========================================
# Business Card Extraction & Formatting
# ==========================================


def extract_contact_info(image_bytes: bytes) -> dict:
    """Extracts structured fields from business card using Gemini 3.6 Flash."""
    image = Image.open(io.BytesIO(image_bytes))
    prompt = """
    Extract contact information from this business card image.
    Return ONLY a JSON object with these exact keys:
    {
      "first_name": "",
      "last_name": "",
      "country": "",
      "company": "",
      "job_title": "",
      "phone": "",
      "email": "",
      "address": ""
    }

    CRITICAL RULES:
    1. PHONE NUMBERS: Extract only ONE primary mobile or direct phone number with international country code.
    2. DIPLOMATIC REPRESENTATION: If the card represents an Embassy/Consulate (e.g. "Embassy of Italy in Tashkent"), country MUST be the represented country ("Italy").
    3. Missing fields should be empty strings.
    """
    response = gemini_client.models.generate_content(
        model="gemini-3.6-flash",
        contents=[image, prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.1,
        ),
    )
    return json.loads(response.text)


def build_formatted_name(data: dict, event_name: str = None) -> str:
    """Constructs display name: BC [Event] | Name - Country - Title."""
    full_name = f"{data.get('first_name', '')} {data.get('last_name', '')}".strip()
    name_elements = [full_name]
    if data.get("country"):
        name_elements.append(data.get("country"))
    if data.get("job_title"):
        name_elements.append(data.get("job_title"))
    base_info = " - ".join([el for el in name_elements if el])

    if event_name:
        return f"BC [{event_name}] | {base_info}"
    return f"BC | {base_info}"


def generate_vcard(data: dict, event_name: str = None) -> str:
    """Generates standard vCard 3.0 string with metadata and direct WhatsApp link."""
    formatted_name = build_formatted_name(data, event_name)
    phone = clean_phone_number(data.get("phone", ""))
    digits_only = re.sub(r"\D", "", phone)
    wa_link = f"https://wa.me/{digits_only}" if digits_only else "N/A"
    today_str = datetime.now().strftime("%d/%m/%Y")

    notes = [
        f"📅 Scanned Date: {today_str}",
        f"💬 WhatsApp: {wa_link}",
    ]
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
        lines.append(f"TEL;TYPE=CELL:{phone}")
    if data.get("email"):
        lines.append(f"EMAIL;TYPE=INTERNET:{data.get('email')}")
    if data.get("address") or data.get("country"):
        full_addr = f"{data.get('address', '')}, {data.get('country', '')}".strip(", ")
        lines.append(f"ADR;TYPE=WORK:;;{full_addr};;;;")
    if digits_only:
        lines.append(f"URL:https://wa.me/{digits_only}")
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

    dup_alert = ""
    if duplicate_info:
        dup_alert = (
            f"⚠️ **Duplicate Found in your History!**\n"
            f"You previously scanned this contact on **{duplicate_info['date']}** "
            f"as `{duplicate_info['name']}` ({duplicate_info['match_type']} matched).\n\n---\n"
        )

    text = (
        f"{dup_alert}"
        "📋 **Scanned Business Card Details:**\n\n"
        f"🏷️ **Contact Name:** `{name_preview}`\n"
        f"👤 **Person:** {data.get('first_name', '')} {data.get('last_name', '')}\n"
        f"🏢 **Company:** {data.get('company') or 'Not specified'}\n"
        f"💼 **Title:** {data.get('job_title') or 'Not specified'}\n"
        f"🌍 **Country:** {data.get('country') or 'Not specified'}\n"
        f"📞 **Phone:** `{phone or 'Not detected'}`\n"
        f"💬 **WhatsApp:** {wa_preview}\n"
        f"✉️ **Email:** {data.get('email') or 'Not specified'}\n"
        f"📍 **Address:** {data.get('address') or 'Not specified'}\n\n"
        "How would you like to proceed?"
    )

    btn_label = (
        "🔄 Update / Send Contact Anyway"
        if duplicate_info
        else "✅ Save Default (BC | ...)"
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


# ==========================================
# Telegram Bot Handlers
# ==========================================


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome! Send me a business card photo.\n"
        "I will extract the details, check your scan history for duplicates, and generate a ready-to-save Contact Card directly to your device."
    )


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏓 Pong! Bot is alive, active, and ready.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    status_msg = await update.message.reply_text(
        "🔍 Scanning business card & checking duplicates..."
    )
    photo_file = await update.message.photo[-1].get_file()
    photo_bytes = await photo_file.download_as_bytearray()

    try:
        data = extract_contact_info(bytes(photo_bytes))
        context.user_data["pending_contact"] = data
        context.user_data["editing_field"] = None
        context.user_data["waiting_for_event"] = False

        duplicate_info = check_duplicate_contact(
            user_id,
            data.get("first_name", ""),
            data.get("last_name", ""),
            data.get("phone", ""),
            data.get("email", ""),
        )
        context.user_data["duplicate_info"] = duplicate_info

        text, reply_markup = render_preview(data, duplicate_info)
        await status_msg.delete()
        await update.message.reply_text(
            text, reply_markup=reply_markup, parse_mode="Markdown"
        )
    except Exception as e:
        await status_msg.edit_text(f"❌ Error processing image: {str(e)}")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    contact_data = context.user_data.get("pending_contact")
    duplicate_info = context.user_data.get("duplicate_info")

    if not contact_data and query.data != "cancel":
        await query.edit_message_text(
            "⚠️ Session expired. Please send the business card photo again."
        )
        return

    # 1. Save Contact & Update History
    if query.data == "save_default":
        phone = clean_phone_number(contact_data.get("phone", ""))
        if not phone:
            await query.edit_message_text(
                "❌ No phone number detected. Tap 'Edit Details' to add a phone number."
            )
            return

        formatted_name = build_formatted_name(contact_data)
        vcard_text = generate_vcard(contact_data)

        # Send native Telegram Contact Card
        await context.bot.send_contact(
            chat_id=query.message.chat_id,
            phone_number=phone,
            first_name=formatted_name,
            last_name="",
            vcard=vcard_text,
        )

        # Save to DB
        record_saved_contact(user_id, contact_data)

        await query.edit_message_text(
            f"✅ Contact `{formatted_name}` generated! Tap the card above to save it to your phone.",
            parse_mode="Markdown",
        )
        context.user_data.clear()

    # 2. Ask Event Name
    elif query.data == "ask_event":
        context.user_data["waiting_for_event"] = True
        context.user_data["editing_field"] = None
        await query.edit_message_text(
            "✍️ **Please type the Event / Conference name in chat:**\n"
            "(e.g., `Grape Conference 2026` or `Tashkent B2B`)",
            parse_mode="Markdown",
        )

    # 3. Edit Menu
    elif query.data == "open_edit_menu":
        edit_keyboard = [
            [
                InlineKeyboardButton(
                    "📞 Edit Phone", callback_data="edit_field_phone"
                ),
                InlineKeyboardButton(
                    "👤 Edit Name", callback_data="edit_field_name"
                ),
            ],
            [
                InlineKeyboardButton(
                    "🏢 Edit Company", callback_data="edit_field_company"
                ),
                InlineKeyboardButton(
                    "💼 Edit Title", callback_data="edit_field_job_title"
                ),
            ],
            [
                InlineKeyboardButton(
                    "✉️ Edit Email", callback_data="edit_field_email"
                ),
                InlineKeyboardButton(
                    "🌍 Edit Country", callback_data="edit_field_country"
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔙 Back to Preview", callback_data="back_to_preview"
                )
            ],
        ]
        await query.edit_message_text(
            "✏️ **Select a field to modify:**",
            reply_markup=InlineKeyboardMarkup(edit_keyboard),
            parse_mode="Markdown",
        )

    # 4. Field Selected
    elif query.data.startswith("edit_field_"):
        field = query.data.replace("edit_field_", "")
        context.user_data["editing_field"] = field

        current_val = (
            f"{contact_data.get('first_name', '')} {contact_data.get('last_name', '')}".strip()
            if field == "name"
            else contact_data.get(field, "")
        )

        await query.edit_message_text(
            f"✏️ **Editing {field.replace('_', ' ').capitalize()}:**\n"
            f"Current value: `{current_val}`\n\n"
            "👉 Please reply with the new value in chat:",
            parse_mode="Markdown",
        )

    # 5. Back to Preview
    elif query.data == "back_to_preview":
        text, reply_markup = render_preview(contact_data, duplicate_info)
        await query.edit_message_text(
            text, reply_markup=reply_markup, parse_mode="Markdown"
        )

    # 6. Cancel
    elif query.data == "cancel":
        context.user_data.clear()
        await query.edit_message_text("🚫 Operation cancelled.")


async def handle_text_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Handles text replies for editing fields or entering event names."""
    user_id = update.effective_user.id
    user_input = update.message.text.strip()
    contact_data = context.user_data.get("pending_contact")
    editing_field = context.user_data.get("editing_field")
    waiting_for_event = context.user_data.get("waiting_for_event")

    if not contact_data:
        await update.message.reply_text(
            "No active card session. Send a photo of a business card to begin."
        )
        return

    # Handle Field Editing
    if editing_field:
        if editing_field == "name":
            parts = user_input.split(" ", 1)
            contact_data["first_name"] = parts[0]
            contact_data["last_name"] = parts[1] if len(parts) > 1 else ""
        elif editing_field == "phone":
            contact_data["phone"] = clean_phone_number(user_input)
        else:
            contact_data[editing_field] = user_input

        context.user_data["editing_field"] = None

        # Re-check duplicate with updated values
        duplicate_info = check_duplicate_contact(
            user_id,
            contact_data.get("first_name", ""),
            contact_data.get("last_name", ""),
            contact_data.get("phone", ""),
            contact_data.get("email", ""),
        )
        context.user_data["duplicate_info"] = duplicate_info

        text, reply_markup = render_preview(contact_data, duplicate_info)
        await update.message.reply_text(
            f"✅ **{editing_field.replace('_', ' ').capitalize()} updated!**\n\n"
            + text,
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )
        return

    # Handle Event Input
    if waiting_for_event:
        event_name = user_input
        phone = clean_phone_number(contact_data.get("phone", ""))
        if not phone:
            await update.message.reply_text(
                "❌ No phone number found on card. Please edit details and add a phone number first."
            )
            return

        formatted_name = build_formatted_name(contact_data, event_name=event_name)
        vcard_text = generate_vcard(contact_data, event_name=event_name)

        await context.bot.send_contact(
            chat_id=update.message.chat_id,
            phone_number=phone,
            first_name=formatted_name,
            last_name="",
            vcard=vcard_text,
        )

        record_saved_contact(user_id, contact_data)

        await update.message.reply_text(
            f"✅ Contact `{formatted_name}` generated and saved to history! Tap above to save.",
            parse_mode="Markdown",
        )
        context.user_data.clear()


# ==========================================
# Main Execution
# ==========================================


def main():
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input)
    )

    print("🤖 Telegram Business Card Bot (Postgres/SQLite + Duplicates) is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
