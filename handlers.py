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
    """Merges newly scanned side into existing data without overwriting valid fields."""
    merged = existing.copy()
    for key, val in new_data.items():
        if val and not merged.get(key):
            merged[key] = val
        elif val and key in ["company", "job_title", "address"] and len(str(val)) > len(str(merged.get(key, ""))):
            merged[key] = val
    return merged


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome! Send me a business card photo (one or both sides).\n"
        "I will extract the details, check your scan history for duplicates, and generate a ready-to-save Contact Card directly to your device."
    )


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏓 Pong! Bot is alive, active, and ready.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    waiting_second_side = context.user_data.get("waiting_second_side", False)

    status_msg = await update.message.reply_text(
        "🔍 Scanning business card..." if not waiting_second_side else "🔍 Scanning back side & merging details..."
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

    # 1. Save Default
    if query.data == "save_default":
        phone = clean_phone_number(contact_data.get("phone", ""))
        if not phone:
            await query.edit_message_text(
                "❌ No phone number detected. Tap 'Edit Details' to add a number, or scan the other side."
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
            f"✅ Contact `{formatted_name}` generated! Tap above to save it to your phone.",
            parse_mode="Markdown",
        )
        context.user_data.clear()

    # 2. Scan Second Side
    elif query.data == "scan_second_side":
        context.user_data["waiting_second_side"] = True
        await query.edit_message_text(
            "📷 **Please send a photo of the back side of the card:**\n"
            "The bot will automatically merge the new information with the existing card.",
            parse_mode="Markdown",
        )

    # 3. Assign Event Name
    elif query.data == "ask_event":
        context.user_data["waiting_for_event"] = True
        context.user_data["editing_field"] = None
        await query.edit_message_text(
            "✍️ **Please type the Event / Conference name in chat:**\n"
            "(e.g., `Grape Conference 2026` or `Tashkent B2B`)",
            parse_mode="Markdown",
        )

    # 4. Edit Menu
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
                    "🌐 Edit Website", callback_data="edit_field_website"
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
            f"✏️ **Editing {field.capitalize()}:**\n"
            f"Current value: `{current_val or 'Empty'}`\n\n"
            "👉 Please reply with the new value in chat:",
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
            f"✅ **{editing_field.capitalize()} updated!**\n\n" + text,
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
                "❌ No phone number found. Please edit details or scan the back side."
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
