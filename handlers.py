from database import (
    check_duplicate_contact,
    clean_phone_number,
    record_saved_contact,
)
from extractor import extract_contact_info
from formatters import build_formatted_name, generate_vcard, render_preview
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes


def merge_card_data(existing: dict, new_data: dict) -> dict:
    """Merges newly scanned side into existing data without overwriting good fields."""
    merged = existing.copy()
    for key, val in new_data.items():
        if val and not merged.get(key):
            merged[key] = val
        elif val and key in ["company", "job_title", "address"] and len(str(val)) > len(str(merged.get(key, ""))):
            merged[key] = val
    return merged


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 שלום! שלח לי תמונה של כרטיס ביקור (צד אחד או שני צדדים).\n"
        "אחלץ את הפרטים, אבדוק כפילויות ואייצר כרטיס איש קשר ישירות למכשיר."
    )


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏓 Pong! הבוט פעיל ומוכן לסריקה.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    waiting_second_side = context.user_data.get("waiting_second_side", False)

    status_msg = await update.message.reply_text(
        "🔍 סורק כרטיס ביקור..." if not waiting_second_side else "🔍 סורק צד שני וממזג נתונים..."
    )
    photo_file = await update.message.photo[-1].get_file()
    photo_bytes = await photo_file.download_as_bytearray()

    try:
        new_data = extract_contact_info(bytes(photo_bytes))

        if waiting_second_side and context.user_data.get("pending_contact"):
            data = merge_card_data(context.user_data["pending_contact"], new_data)
            context.user_data["waiting_second_side"] = False
        else:
            data = new_data

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
        await status_msg.edit_text(f"❌ שגיאה בעיבוד התמונה: {str(e)}")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    contact_data = context.user_data.get("pending_contact")
    duplicate_info = context.user_data.get("duplicate_info")

    if not contact_data and query.data != "cancel":
        await query.edit_message_text(
            "⚠️ תוקף הפעולה פג. אנא שלח את תמונת הכרטיס מחדש."
        )
        return

    # 1. Save Default
    if query.data == "save_default":
        phone = clean_phone_number(contact_data.get("phone", ""))
        if not phone:
            await query.edit_message_text(
                "❌ לא זוהה מספר טלפון. לחץ על 'ערוך פרטים' כדי להוסיף מספר, או סרוק את הצד השני."
            )
            return

        formatted_name = build_formatted_name(contact_data)
        vcard_text = generate_vcard(contact_data)

        await context.bot.send_contact(
            chat_id=query.message.chat_id,
            phone_number=phone,
            first_name=formatted_name,
            last_name="",
            vcard=vcard_text,
        )

        record_saved_contact(user_id, contact_data)

        await query.edit_message_text(
            f"✅ איש הקשר `{formatted_name}` נוצר בהצלחה! לחץ עליו למעלה לשמירה בטלפון.",
            parse_mode="Markdown",
        )
        context.user_data.clear()

    # 2. Scan Second Side
    elif query.data == "scan_second_side":
        context.user_data["waiting_second_side"] = True
        await query.edit_message_text(
            "📷 **שלח עכשיו תמונה של הצד השני של הכרטיס:**\n"
            "הבוט ימזג את המידע מהצד השני עם הנתונים שכבר נסרקו.",
            parse_mode="Markdown",
        )

    # 3. Assign Event Name
    elif query.data == "ask_event":
        context.user_data["waiting_for_event"] = True
        context.user_data["editing_field"] = None
        await query.edit_message_text(
            "✍️ **הקלד את שם הכנס / האירוע בצ'אט:**\n"
            "(לדוגמה: `Grape Conference 2026` או `Tashkent B2B`)",
            parse_mode="Markdown",
        )

    # 4. Edit Menu
    elif query.data == "open_edit_menu":
        edit_keyboard = [
            [
                InlineKeyboardButton(
                    "📞 ערוך טלפון", callback_data="edit_field_phone"
                ),
                InlineKeyboardButton(
                    "👤 ערוך שם", callback_data="edit_field_name"
                ),
            ],
            [
                InlineKeyboardButton(
                    "🏢 ערוך חברה/עסק", callback_data="edit_field_company"
                ),
                InlineKeyboardButton(
                    "💼 ערוך תפקיד", callback_data="edit_field_job_title"
                ),
            ],
            [
                InlineKeyboardButton(
                    "✉️ ערוך אימייל", callback_data="edit_field_email"
                ),
                InlineKeyboardButton(
                    "🌐 ערוך אתר", callback_data="edit_field_website"
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔙 חזרה לתצוגה", callback_data="back_to_preview"
                )
            ],
        ]
        await query.edit_message_text(
            "✏️ **בחר שדה לעריכה:**",
            reply_markup=InlineKeyboardMarkup(edit_keyboard),
            parse_mode="Markdown",
        )

    # 5. Field Selected
    elif query.data.startswith("edit_field_"):
        field = query.data.replace("edit_field_", "")
        context.user_data["editing_field"] = field

        current_val = (
            f"{contact_data.get('first_name', '')} {contact_data.get('last_name', '')}".strip()
            if field == "name"
            else contact_data.get(field, "")
        )

        await query.edit_message_text(
            f"✏️ **עריכת {field}:**\n"
            f"ערך נוכחי: `{current_val or 'ריק'}`\n\n"
            "👉 השב בצ'אט עם הערך החדש:",
            parse_mode="Markdown",
        )

    # 6. Back to Preview
    elif query.data == "back_to_preview":
        text, reply_markup = render_preview(contact_data, duplicate_info)
        await query.edit_message_text(
            text, reply_markup=reply_markup, parse_mode="Markdown"
        )

    # 7. Cancel
    elif query.data == "cancel":
        context.user_data.clear()
        await query.edit_message_text("🚫 הפעולה בוטלה.")


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
            "אין כרטיס פעיל כרגע. שלח תמונה של כרטיס ביקור כדי להתחיל."
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
            f"✅ השדה עודכן בהצלחה!\n\n" + text,
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
                "❌ לא נמצא מספר טלפון. אנא ערוך פרטים או סרוק את הצד השני."
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
            f"✅ איש הקשר `{formatted_name}` נוצר ונשמר בהיסטוריה!",
            parse_mode="Markdown",
        )
        context.user_data.clear()
