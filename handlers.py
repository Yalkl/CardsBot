"""Telegram handlers (docs/SPEC.md, phase A): approval, scan → preview → save with a note, event mode,
open questions, and sharing a group of contacts as one file."""

import asyncio
import io
from datetime import date
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import config
import database as db
from extractor import extract_contact_info, parse_question
from formatters import build_display_name, edit_menu, generate_vcard, render_preview, render_results

HTML = "HTML"
NOTE_MAX_LEN = 500

WELCOME = (
    "👋 שלחו תמונה של כרטיס ביקור (צד אחד או שניים), והוא יישמר כאיש קשר עם הערה.\n\n"
    "🏷️ <b>/event</b> שם האירוע: כל כרטיס שתסרקו עד סוף היום יסומן באירוע הזה. <b>/event off</b> מכבה.\n"
    "🔎 אפשר פשוט לשאול: \"את מי פגשנו שבוע שעבר בכנס הגרמני?\"\n"
    "🆔 <b>/myid</b>: המספר שלכם בטלגרם."
)

# user_data keys that belong to one card being saved; cleared after each save.
SESSION_KEYS = ("pending_contact", "duplicate", "waiting_second_side", "editing_field", "waiting_for_note", "save_mode", "note_prompt")


def clear_session(context: ContextTypes.DEFAULT_TYPE):
    for k in SESSION_KEYS:
        context.user_data.pop(k, None)


def merge_card_data(existing: dict, new_data: dict) -> dict:
    """Merges newly scanned side into existing data without overwriting valid fields."""
    merged = existing.copy()
    for key, val in new_data.items():
        if val and not merged.get(key):
            merged[key] = val
        elif val and key in ["company", "job_title", "address"] and len(str(val)) > len(str(merged.get(key, ""))):
            merged[key] = val
    return merged


# ---------------------------------------------------------------- access

async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True if the user may use the bot. Otherwise asks the admin once and tells the user."""
    user = update.effective_user
    if db.is_approved(user.id):
        return True
    message = update.effective_message
    if not config.ADMIN_TELEGRAM_ID:
        await message.reply_text("הבוט עוד לא הוגדר.")
        return False
    existing = db.get_user(user.id)
    status = db.request_access(user.id, user.full_name or "", user.username)
    if status == "denied":
        await message.reply_text("אין לך גישה לבוט.")
        return False
    if not existing:
        who = escape(user.full_name or "")
        if user.username:
            who += f" (@{escape(user.username)})"
        await context.bot.send_message(
            chat_id=config.ADMIN_TELEGRAM_ID,
            text=f"🔐 בקשת גישה לבוט: {who}\nID: <code>{user.id}</code>",
            parse_mode=HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ אשר", callback_data=f"approve_{user.id}"),
                InlineKeyboardButton("🚫 דחה", callback_data=f"deny_{user.id}"),
            ]]),
        )
    await message.reply_text("🔐 בקשת הגישה נשלחה. תקבלו הודעה כשהיא תאושר.")
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await guard(update, context):
        await update.message.reply_text(WELCOME, parse_mode=HTML)


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏓 הבוט פעיל.")


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    status = "מנהל" if uid == config.ADMIN_TELEGRAM_ID else ("מאושר" if db.is_approved(uid) else "לא מאושר")
    await update.message.reply_text(f"🆔 <code>{uid}</code>\nסטטוס: {status}", parse_mode=HTML)


# ---------------------------------------------------------------- event mode

async def event_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context):
        return
    uid = update.effective_user.id
    name = " ".join(context.args or []).strip()[:100]
    if not name:
        current = db.get_event(uid)
        text = f"🏷️ אירוע פעיל: <b>{escape(current)}</b>\nלכיבוי: /event off" if current else "אין אירוע פעיל.\nלהפעלה: /event שם האירוע"
        await update.message.reply_text(text, parse_mode=HTML)
        return
    if name.lower() in ("off", "stop", "כבה", "סיום"):
        db.set_event(uid, None)
        await update.message.reply_text("🏷️ האירוע כובה.")
        return
    db.set_event(uid, name)
    await update.message.reply_text(
        f"🏷️ מעכשיו כל כרטיס יסומן: <b>{escape(name)}</b>\n(עד /event off או {config.EVENT_HOURS} שעות)", parse_mode=HTML
    )


# ---------------------------------------------------------------- scan and save

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context):
        return
    user = update.effective_user
    message = update.message

    # Next card arrived while the previous one waits for a note: save the previous one without a note.
    if context.user_data.get("waiting_for_note") and context.user_data.get("pending_contact"):
        await close_note_prompt(context)
        await message.reply_text(await finalize_save(context, message.chat_id, user, None) + "\n(נשמר בלי הערה)", parse_mode=HTML)

    second_side = context.user_data.get("waiting_second_side") and context.user_data.get("pending_contact")
    status = await message.reply_text("🔍 קורא את הצד השני…" if second_side else "🔍 קורא את הכרטיס…")
    photo_file = await message.photo[-1].get_file()
    photo_bytes = bytes(await photo_file.download_as_bytearray())
    try:
        new_data = await asyncio.to_thread(extract_contact_info, photo_bytes)
    except Exception:
        await status.edit_text("❌ לא הצלחתי לקרוא את הכרטיס. נסו שוב, אולי עם תמונה חדה יותר.")
        return

    data = merge_card_data(context.user_data["pending_contact"], new_data) if second_side else new_data
    clear_session(context)
    context.user_data["pending_contact"] = data
    context.user_data["duplicate"] = db.find_duplicate(user.id, data)
    text, markup = render_preview(data, context.user_data["duplicate"], db.get_event(user.id))
    await status.delete()
    await message.reply_text(text, reply_markup=markup, parse_mode=HTML)


async def ask_for_note(message, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Second step of saving: one short question, answered by text or a button."""
    context.user_data["waiting_for_note"] = True
    context.user_data["editing_field"] = None
    keyboard = []
    last = db.get_last_note(user_id)
    if last:
        short = last if len(last) <= 30 else last[:29] + "…"
        keyboard.append([InlineKeyboardButton(f"🔁 כמו הקודם: {short}", callback_data="note_repeat")])
    keyboard.append([InlineKeyboardButton("⏭ דלג", callback_data="note_skip")])
    keyboard.append([InlineKeyboardButton("🔙 חזרה", callback_data="back_to_preview")])
    sent = await message.edit_text("📝 הערה? איפה נפגשתם, על מה דיברתם.\nכתבו בצ'אט, או לחצו דלג.", reply_markup=InlineKeyboardMarkup(keyboard))
    context.user_data["note_prompt"] = (sent.chat_id, sent.message_id)


async def close_note_prompt(context: ContextTypes.DEFAULT_TYPE):
    prompt = context.user_data.get("note_prompt")
    if prompt:
        try:
            await context.bot.edit_message_reply_markup(chat_id=prompt[0], message_id=prompt[1], reply_markup=None)
        except Exception:
            pass


async def send_card(context: ContextTypes.DEFAULT_TYPE, chat_id: int, data: dict, event=None, note=None, scanned_at=None):
    """A contact the phone can save in one tap; a .vcf file when there is no phone number."""
    vcard = generate_vcard(data, event=event, note=note, scanned_at=scanned_at)
    phone = db.clean_phone_number(data.get("phone", ""))
    if phone:
        await context.bot.send_contact(chat_id=chat_id, phone_number=phone, first_name=build_display_name(data), last_name="", vcard=vcard)
    else:
        doc = io.BytesIO(vcard.encode("utf-8"))
        doc.name = "contact.vcf"
        await context.bot.send_document(chat_id=chat_id, document=doc)


async def finalize_save(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user, note: str | None) -> str:
    """Stores the card (new or update), sends the contact, clears the session. Returns the confirmation text."""
    data = context.user_data["pending_contact"]
    event = db.get_event(user.id)
    dup = context.user_data.get("duplicate")
    if context.user_data.get("save_mode") == "update" and dup:
        db.update_card(user.id, dup["id"], data, event, note)
        verb = "עודכן"
    else:
        db.insert_card(user.id, data, event, note)
        verb = "נשמר"
    if note:
        db.set_last_note(user.id, note)
    await send_card(context, chat_id, data, event, note)
    clear_session(context)
    return f"✅ {verb}: <b>{escape(build_display_name(data))}</b>\nלחצו על איש הקשר למעלה כדי לשמור אותו בטלפון."


EDIT_PROMPTS = {
    "name_he": "שם בעברית (פרטי ומשפחה)",
    "name": "שם באנגלית (First Last)",
    "job_title_he": "תפקיד",
    "company": "חברה, כפי שתוצג",
    "phone": "טלפון",
    "email": "מייל",
}


def apply_edit(data: dict, field: str, value: str):
    if field in ("name_he", "name"):
        first, _, last = value.partition(" ")
        suffix = "_he" if field == "name_he" else ""
        data[f"first_name{suffix}"] = first
        data[f"last_name{suffix}"] = last
    elif field == "company":
        data["company_he"] = value  # shown in the name; the original company stays in its own field
        if not data.get("company"):
            data["company"] = value
    elif field == "phone":
        data["phone"] = db.clean_phone_number(value)
    else:
        data[field] = value


# ---------------------------------------------------------------- buttons

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    action = query.data or ""

    # Admin decides on an access request.
    if action.startswith(("approve_", "deny_")):
        if user.id != config.ADMIN_TELEGRAM_ID:
            return
        target = int(action.split("_", 1)[1])
        approve = action.startswith("approve_")
        db.decide_access(target, approve)
        await query.edit_message_text(f"{'✅ אושר' if approve else '🚫 נדחה'}: {target}")
        try:
            await context.bot.send_message(chat_id=target, text=("✅ הגישה אושרה.\n\n" + WELCOME) if approve else "🚫 בקשת הגישה נדחתה.", parse_mode=HTML)
        except Exception:
            pass
        return

    if not db.is_approved(user.id):
        return

    # Results of a question: send one contact, or share them all.
    if action.startswith("send_"):
        card = db.get_card(user.id, int(action.split("_", 1)[1]))
        if card:
            await send_card(context, query.message.chat_id, card, card.get("event"), card.get("note"), card.get("scanned_at"))
        return
    if action == "share_all":
        await query.message.reply_text(
            "📤 לשתף עם ההערות שלך?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("בלי הערות", callback_data="share_plain"),
                InlineKeyboardButton("עם הערות", callback_data="share_notes"),
            ]]),
        )
        return
    if action in ("share_plain", "share_notes"):
        cards = [c for c in (db.get_card(user.id, i) for i in context.user_data.get("last_results", [])) if c]
        if not cards:
            await query.edit_message_text("⚠️ אין רשימה לשתף. שאלו שוב.")
            return
        with_notes = action == "share_notes"
        vcf = "\r\n".join(generate_vcard(c, c.get("event"), c.get("note"), c.get("scanned_at"), include_note=with_notes) for c in cards)
        events = {c.get("event") for c in cards if c.get("event")}
        title = (events.pop() if len(events) == 1 else "כרטיסי ביקור")
        doc = io.BytesIO(vcf.encode("utf-8"))
        doc.name = f"{title} – {len(cards)} אנשי קשר.vcf"
        summary = "\n".join(f"• {escape(build_display_name(c))}" for c in cards)
        await query.edit_message_text(f"📤 {len(cards)} אנשי קשר{' עם הערות' if with_notes else ''}. אפשר להעביר את הקובץ בוואטסאפ או בטלגרם:")
        await context.bot.send_document(chat_id=query.message.chat_id, document=doc, caption=summary[:1000], parse_mode=HTML)
        return

    # Everything below belongs to the card being saved.
    data = context.user_data.get("pending_contact")
    if not data:
        if action != "cancel":
            await query.edit_message_text("⚠️ הסשן פג. שלחו שוב את התמונה.")
        return

    if action in ("save_default", "save_update"):
        context.user_data["save_mode"] = "update" if action == "save_update" else "new"
        await ask_for_note(query.message, context, user.id)
    elif action in ("note_skip", "note_repeat"):
        if not context.user_data.get("waiting_for_note"):
            return
        note = db.get_last_note(user.id) if action == "note_repeat" else None
        await query.edit_message_text(await finalize_save(context, query.message.chat_id, user, note), parse_mode=HTML)
    elif action == "scan_second_side":
        context.user_data["waiting_second_side"] = True
        await query.edit_message_text(
            "📷 שלחו תמונה של הצד השני. הפרטים יתמזגו.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 חזרה", callback_data="back_to_preview")]]),
        )
    elif action == "open_edit_menu":
        context.user_data["editing_field"] = None
        await query.edit_message_text("✏️ מה לתקן?", reply_markup=edit_menu())
    elif action.startswith("edit_field_"):
        field = action.removeprefix("edit_field_")
        context.user_data["editing_field"] = field
        await query.edit_message_text(
            f"✏️ כתבו בצ'אט: {EDIT_PROMPTS.get(field, field)}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 חזרה", callback_data="open_edit_menu")]]),
        )
    elif action == "back_to_preview":
        for k in ("editing_field", "waiting_second_side", "waiting_for_note", "save_mode"):
            context.user_data.pop(k, None)
        text, markup = render_preview(data, context.user_data.get("duplicate"), db.get_event(user.id))
        await query.edit_message_text(text, reply_markup=markup, parse_mode=HTML)
    elif action == "cancel":
        clear_session(context)
        await query.edit_message_text("🚫 בוטל.")


# ---------------------------------------------------------------- text: edits, notes, questions

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context):
        return
    user = update.effective_user
    text = update.message.text.strip()
    data = context.user_data.get("pending_contact")

    if data and context.user_data.get("editing_field"):
        apply_edit(data, context.user_data.pop("editing_field"), text)
        context.user_data["duplicate"] = db.find_duplicate(user.id, data)
        preview, markup = render_preview(data, context.user_data["duplicate"], db.get_event(user.id))
        await update.message.reply_text("✅ עודכן\n\n" + preview, reply_markup=markup, parse_mode=HTML)
        return

    if data and context.user_data.get("waiting_for_note"):
        await close_note_prompt(context)
        await update.message.reply_text(await finalize_save(context, update.message.chat_id, user, text[:NOTE_MAX_LEN]), parse_mode=HTML)
        return

    await answer_question(update, context, text)


async def ask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context):
        return
    question = " ".join(context.args or []).strip()
    if not question:
        await update.message.reply_text("🔎 מה לחפש? למשל: /ask את מי פגשנו אתמול")
        return
    await answer_question(update, context, question)


def describe_filter(f: dict) -> str:
    parts = []
    if f.get("date_from") or f.get("date_to"):
        a, b = f.get("date_from"), f.get("date_to")
        fmt = lambda d: f"{d[8:10]}.{d[5:7]}"  # noqa: E731
        parts.append(fmt(a) if a == b else f"{fmt(a) if a else '…'}–{fmt(b) if b else '…'}")
    if f.get("terms"):
        parts.append(", ".join(f["terms"][:6]))
    return " · ".join(parts)


async def answer_question(update: Update, context: ContextTypes.DEFAULT_TYPE, question: str):
    user = update.effective_user
    status = await update.message.reply_text("🔎 מחפש…")
    f = await asyncio.to_thread(parse_question, question, date.today())
    rows = db.search_cards(user.id, f["terms"], f["date_from"], f["date_to"])
    context.user_data["last_results"] = [r["id"] for r in rows]
    header = f"🔎 <i>{escape(describe_filter(f))}</i> · {len(rows)} אנשים" if rows else f"🔎 <i>{escape(describe_filter(f))}</i>"
    text, markup = render_results(rows, header)
    await status.edit_text(text, reply_markup=markup, parse_mode=HTML)
