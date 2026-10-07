from database import (
    check_duplicate_contact,
    clean_phone_number,
    record_saved_contact,
)
from extractor import extract_contact_info
from formatters import build_formatted_name, generate_vcard, render_preview
from sheets import append_contact, notes_enabled_for, sheets_enabled_for
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

NOTE_MAX_LEN = 500


def _last_notes(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """Last note per user, kept in bot memory so "Same as last" survives user_data.clear()."""
    return context.bot_data.setdefault("last_note", {})


async def ask_for_note(message, context: ContextTypes.DEFAULT_TYPE, user_id: int, edit: bool):
    """Second step of saving: one short question, answer by text or a button."""
    context.user_data["waiting_for_note"] = True
    context.user_data["editing_field"] = None
    context.user_data["waiting_for_event"] = False
    context.user_data["waiting_second_side"] = False

    keyboard = []
    last = _last_notes(context).get(user_id)
    if last:
        short = last if len(last) <= 30 else last[:29] + "…"
        keyboard.append([InlineKeyboardButton(f"🔁 Same as last: {short}", callback_data="note_repeat")])
    keyboard.append([InlineKeyboardButton("⏭ Skip", callback_data="note_skip")])
    keyboard.append([InlineKeyboardButton("🔙 Back to Preview", callback_data="back_to_preview")])

    text = "📝 Note? Where you met, what you talked about.\nType it in chat, or tap Skip."
    if edit:
        sent = await message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        sent = await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    context.user_data["note_prompt"] = (sent.chat_id, sent.message_id)


async def finalize_save(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user, note: str | None) -> str:
    """Sends the contact card, records it, and appends it to the sheet when enabled.

    Returns the confirmation text. Clears the pending session.
    """
    contact_data = context.user_data["pending_contact"]
    event_name = context.user_data.get("pending_event")
    phone = clean_phone_number(contact_data.get("phone", ""))

    formatted_name = build_formatted_name(contact_data, event_name=event_name)
    vcard_text = generate_vcard(contact_data, event_name=event_name, note=note)

    await context.bot.send_contact(
        chat_id=chat_id,
        phone_number=phone,
        first_name=formatted_name,
        last_name="",
        vcard=vcard_text,
    )
    record_saved_contact(user.id, contact_data)

    if note:
        _last_notes(context)[user.id] = note

    result = f"✅ Contact {formatted_name} generated! Tap above to save it to your phone."
    if sheets_enabled_for(user.id):
        ok = await append_contact(user.first_name or str(user.id), contact_data, event_name, note)
        result += "\n📊 Added to the shared sheet." if ok else "\n⚠️ Could not add to the shared sheet. The contact card above is fine."

    context.user_data.clear()
    return result


async def _close_note_prompt(context: ContextTypes.DEFAULT_TYPE):
    """Removes the buttons from the note question once it has been answered."""
    prompt = context.user_data.get("note_prompt")
    if not prompt:
        return
    try:
        await context.bot.edit_message_reply_markup(chat_id=prompt[0], message_id=prompt[1], reply_markup=None)
    except Exception:
        pass


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


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sheet = "on" if sheets_enabled_for(user_id) else ("notes only" if notes_enabled_for(user_id) else "off")
    await update.message.reply_text(f"🆔 Your Telegram ID: {user_id}\n📊 Shared sheet: {sheet}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    waiting_second_side = context.user_data.get("waiting_second_side", False)

    # Next card arrived while the previous one waits for a note: save the previous one without a note.
    if context.user_data.get("waiting_for_note") and context.user_data.get("pending_contact"):
        await _close_note_prompt(context)
        saved_text = await finalize_save(context, update.message.chat_id, update.effective_user, None)
        await update.message.reply_text(saved_text + "\n(Saved without a note.)")
        waiting_second_side = False

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
        context.user_data["waiting_for_note"] = False

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

        if notes_enabled_for(user_id):
            await ask_for_note(query.message, context, user_id, edit=True)
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

    # 1b. Note step answered by a button
    elif query.data in ("note_skip", "note_repeat"):
        if not context.user_data.get("waiting_for_note"):
            await query.edit_message_text("⚠️ Session expired. Please send the business card photo again.")
            return
        note = _last_notes(context).get(user_id) if query.data == "note_repeat" else None
        result = await finalize_save(context, query.message.chat_id, update.effective_user, note)
        await query.edit_message_text(result)

    # 2. Scan Second Side
    elif query.data == "scan_second_side":
        context.user_data["waiting_second_side"] = True
        context.user_data["editing_field"] = None
        context.user_data["waiting_for_event"] = False

        back_keyboard = [[InlineKeyboardButton("🔙 Back to Preview", callback_data="back_to_preview")]]
        await query.edit_message_text(
            "📷 **Please send a photo of the back side of the card:**\n"
            "The bot will automatically merge the new information with the existing card.",
            reply_markup=InlineKeyboardMarkup(back_keyboard),
            parse_mode="Markdown",
        )

    # 3. Assign Event Name
    elif query.data == "ask_event":
        context.user_data["waiting_for_event"] = True
        context.user_data["editing_field"] = None
        context.user_data["waiting_second_side"] = False

        back_keyboard = [[InlineKeyboardButton("🔙 Back to Preview", callback_data="back_to_preview")]]
        await query.edit_message_text(
            "✍️ **Please type the Event / Conference name in chat:**\n"
            "(e.g., `Grape Conference 2026` or `Tashkent B2B`)",
            reply_markup=InlineKeyboardMarkup(back_keyboard),
            parse_mode="Markdown",
        )

    # 4. Edit Menu
    elif query.data == "open_edit_menu":
        context.user_data["editing_field"] = None
        context.user_data["waiting_for_event"] = False
        context.user_data["waiting_second_side"] = False

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
        context.user_data["waiting_for_event"] = False
        context.user_data["waiting_second_side"] = False

        current_val = (
            f"{contact_data.get('first_name', '')} {contact_data.get('last_name', '')}".strip()
            if field == "name"
            else contact_data.get(field, "")
        )

        back_keyboard = [
            [InlineKeyboardButton("🔙 Back to Edit Menu", callback_data="open_edit_menu")],
            [InlineKeyboardButton("🔙 Back to Preview", callback_data="back_to_preview")],
        ]

        await query.edit_message_text(
            f"✏️ **Editing {field.capitalize()}:**\n"
            f"Current value: `{current_val or 'Empty'}`\n\n"
            "👉 Please reply with the new value in chat:",
            reply_markup=InlineKeyboardMarkup(back_keyboard),
            parse_mode="Markdown",
        )

    # 6. Back to Preview
    elif query.data == "back_to_preview":
        context.user_data["editing_field"] = None
        context.user_data["waiting_for_event"] = False
        context.user_data["waiting_second_side"] = False
        context.user_data["waiting_for_note"] = False
        context.user_data.pop("pending_event", None)

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

    # Handle Note Input
    if context.user_data.get("waiting_for_note"):
        await _close_note_prompt(context)
        result = await finalize_save(context, update.message.chat_id, update.effective_user, user_input[:NOTE_MAX_LEN])
        await update.message.reply_text(result)
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

        if notes_enabled_for(user_id):
            context.user_data["pending_event"] = event_name
            await ask_for_note(update.message, context, user_id, edit=False)
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
