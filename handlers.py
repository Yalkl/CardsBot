"""Telegram handlers (docs/SPEC.md): approval, Google connection, scan → preview → save with a note
(to Google Contacts and the user's sheet), event mode, open questions, and sharing contacts as one file."""

import asyncio
import io
import logging
from datetime import date, datetime
from html import escape

from google.auth.exceptions import RefreshError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import cards
import config
import database as db
import google_auth
from extractor import GeminiBusy, extract_contact_info, parse_question, transcribe_audio
from formatters import build_display_name, edit_menu, generate_vcard, render_preview, render_results

HTML = "HTML"
log = logging.getLogger("cardsbot")
BUSY = "⏳ Gemini עמוס כרגע (מגבלת שימוש לדקה). נסו שוב בעוד דקה."
NOTE_MAX_LEN = 500

WELCOME = (
    "👋 שלחו תמונה של כרטיס ביקור (צד אחד או שניים). הוא יישמר באנשי הקשר של גוגל ובטבלה בדרייב שלכם, עם הערה.\n\n"
    "🏷️ <b>/event</b> שם האירוע: כל כרטיס שתסרקו עד סוף היום יסומן באירוע. <b>/event off</b> מכבה.\n"
    "🔎 אפשר פשוט לשאול: \"את מי פגשנו שבוע שעבר בכנס הגרמני?\"\n"
    "✏️ ולעדכן: \"ניקיטה עבר לכתובת הרצל 5, טשקנט\" או \"תוסיף הערה לאנה: שלחתי דוגמאות\".\n"
    "🎙️ בכל מקום שכותבים אפשר גם לשלוח הקלטה קולית.\n"
    "📊 <b>/sheet</b>: קישור לטבלה. 🔌 <b>/connect</b> / <b>/disconnect</b>: חיבור לגוגל. 🆔 <b>/myid</b>"
)
NEED_CONNECT = "🔌 קודם מתחברים לגוגל: /connect"

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


async def google_for(update_or_message, user_id: int):
    """(sheet store, contacts client), or None after telling the user to connect."""
    message = getattr(update_or_message, "effective_message", update_or_message)
    try:
        return cards.open_user(user_id)
    except google_auth.GoogleNotConnected:
        await message.reply_text(NEED_CONNECT)
        return None


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    """Last resort: log the error type. Pressing a button twice ("message is not modified") is not an error."""
    err = context.error
    if "Message is not modified" in str(err):
        return
    log.error("unhandled %s: %s", type(err).__name__, str(err)[:200])


async def google_failed(message, user_id: int, err: Exception):
    """A Google call failed: a revoked or expired connection needs /connect; anything else, try again."""
    log.warning("google call failed: %s", type(err).__name__)
    if isinstance(err, (RefreshError, google_auth.GoogleNotConnected)):
        db.clear_google(user_id)
        await message.reply_text("🔌 החיבור לגוגל פג או בוטל. התחברו מחדש: /connect")
    else:
        await message.reply_text("⚠️ גוגל לא ענה כרגע. נסו שוב בעוד רגע.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context):
        return
    await update.message.reply_text(WELCOME, parse_mode=HTML)
    u = db.get_user(update.effective_user.id) or {}
    if not u.get("refresh_token_enc"):
        await connect(update, context)


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏓 הבוט פעיל.")


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    status = "מנהל" if uid == config.ADMIN_TELEGRAM_ID else ("מאושר" if db.is_approved(uid) else "לא מאושר")
    email = (db.get_user(uid) or {}).get("google_email")
    google = f"מחובר כ-{escape(email)}" if email else "לא מחובר"
    await update.message.reply_text(f"🆔 <code>{uid}</code>\nסטטוס: {status}\nגוגל: {google}", parse_mode=HTML)


# ---------------------------------------------------------------- Google connection

async def connect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context):
        return
    if not config.google_configured():
        await update.message.reply_text("החיבור לגוגל עוד לא הוגדר בשרת.")
        return
    url = google_auth.new_login_url(update.effective_user.id)
    await update.message.reply_text(
        "🔌 חיבור לגוגל: הכרטיסים יישמרו באנשי הקשר שלכם ובטבלה בדרייב.\n"
        "הבוט מקבל גישה רק לאנשי הקשר ולקבצים שהוא עצמו יוצר, לא לשאר הדרייב.\n"
        "הקישור תקף ל-10 דקות. אם גוגל מזהירה ש\"האפליקציה לא מאומתת\": Advanced ← Go to CardsBot.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔐 התחברות עם גוגל", url=url)]]),
    )


async def disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context):
        return
    await google_auth.revoke(update.effective_user.id)
    await update.message.reply_text("🔌 נותק מגוגל. אנשי הקשר והטבלה נשארים אצלכם בגוגל.")


async def sheet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context):
        return
    g = await google_for(update, update.effective_user.id)
    if not g:
        return
    try:
        url = await asyncio.to_thread(g[0].url)
    except Exception as e:
        await google_failed(update.message, update.effective_user.id, e)
        return
    await update.message.reply_text(f"📊 הטבלה שלכם: {url}")


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
    g = await google_for(update, user.id)
    if not g:
        return
    store, contacts = g

    # Next card arrived while the previous one waits for a note: save the previous one without a note.
    if context.user_data.get("waiting_for_note") and context.user_data.get("pending_contact"):
        await close_note_prompt(context)
        text, markup = await finalize_save(context, message, user, None)
        await message.reply_text(text + "\n(נשמר בלי הערה)", reply_markup=markup, parse_mode=HTML)

    second_side = context.user_data.get("waiting_second_side") and context.user_data.get("pending_contact")
    status = await message.reply_text("🔍 קורא את הצד השני…" if second_side else "🔍 קורא את הכרטיס…")
    photo_file = await message.photo[-1].get_file()
    photo_bytes = bytes(await photo_file.download_as_bytearray())
    try:
        new_data = await asyncio.to_thread(extract_contact_info, photo_bytes)
    except GeminiBusy:
        await status.edit_text(BUSY)
        return
    except Exception as e:
        log.warning("card read failed: %s", type(e).__name__)
        await status.edit_text("❌ לא הצלחתי לקרוא את הכרטיס. נסו שוב, אולי עם תמונה חדה יותר.")
        return

    data = merge_card_data(context.user_data["pending_contact"], new_data) if second_side else new_data
    clear_session(context)
    context.user_data["pending_contact"] = data
    try:
        existing = await asyncio.to_thread(store.all)
    except Exception:
        existing = []  # the duplicate check is a help, not a must
    context.user_data["duplicate"] = cards.find_duplicate(existing, data)
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


async def send_card(context: ContextTypes.DEFAULT_TYPE, chat_id: int, card: dict):
    """A contact the phone can save in one tap; a .vcf file when there is no phone number."""
    vcard = generate_vcard(card)
    phone = db.clean_phone_number(card.get("phone", "")) or db.clean_phone_number(card.get("phone_office", ""))
    if phone:
        await context.bot.send_contact(chat_id=chat_id, phone_number=phone, first_name=build_display_name(card), last_name="", vcard=vcard)
    else:
        doc = io.BytesIO(vcard.encode("utf-8"))
        doc.name = "contact.vcf"
        await context.bot.send_document(chat_id=chat_id, document=doc)


def _save_to_google(store, contacts, card: dict, existing: dict | None) -> tuple[str, bool]:
    """Writes the card to the sheet and to Google Contacts. Returns (card id, contacts ok).

    The sheet is the record: if it fails, this raises and nothing counts as saved. Contacts is retried later.
    """
    if existing:
        merged = {**existing, **{k: v for k, v in card.items() if v}}
        merged["note"] = " | ".join(n for n in (existing.get("note"), card.get("note")) if n)
        card = merged
    contact_ok = True
    try:
        resource = existing.get("google_contact") if existing else ""
        card["google_contact"] = contacts.update(resource, card) if resource else contacts.create(card)
    except Exception:
        contact_ok = False
        card["google_contact"] = card.get("google_contact") or ""
    if existing:
        store.update(existing["id"], card)
        return existing["id"], contact_ok
    return store.insert(card), contact_ok


async def finalize_save(context: ContextTypes.DEFAULT_TYPE, message, user, note: str | None):
    """Saves the pending card in Google. Returns (text, keyboard) for the confirmation.

    On failure the card stays pending, so the user can press save again.
    """
    data = context.user_data["pending_contact"]
    dup = context.user_data.get("duplicate")
    card = {**data, "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "event": db.get_event(user.id) or "", "note": note or ""}
    existing = dup["row"] if (context.user_data.get("save_mode") == "update" and dup) else None
    try:
        store, contacts = cards.open_user(user.id)
        card_id, contact_ok = await asyncio.to_thread(_save_to_google, store, contacts, card, existing)
    except Exception as e:
        context.user_data["waiting_for_note"] = False
        if isinstance(e, (RefreshError, google_auth.GoogleNotConnected)):
            db.clear_google(user.id)
            return "🔌 החיבור לגוגל פג. התחברו מחדש (/connect) ואז לחצו שמור שוב.", _back_keyboard()
        return "⚠️ השמירה לגוגל נכשלה. הכרטיס עדיין כאן, נסו שוב.", _back_keyboard()

    if note:
        db.set_last_note(user.id, note)
    clear_session(context)
    verb = "עודכן" if existing else "נשמר"
    status = "אנשי קשר ✓ · טבלה ✓" if contact_ok else "טבלה ✓ · אנשי קשר ✗"
    markup = None if contact_ok else InlineKeyboardMarkup([[InlineKeyboardButton("🔁 נסה שוב לשמור באנשי קשר", callback_data=f"retry_{card_id}")]])
    return f"✅ {verb}: <b>{escape(build_display_name(card))}</b>\n{status}", markup


def _back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 חזרה לכרטיס", callback_data="back_to_preview")]])


EDIT_PROMPTS = {
    "name_he": "שם בעברית (פרטי ומשפחה)",
    "name": "שם באנגלית (First Last)",
    "job_title_he": "תפקיד",
    "company": "חברה, כפי שתוצג",
    "phone": "נייד",
    "phone_office": "טלפון משרד",
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
    elif field in ("phone", "phone_office"):
        data[field] = db.clean_phone_number(value)
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
            await context.bot.send_message(
                chat_id=target, text=("✅ הגישה אושרה. שלחו /start כדי להתחיל.") if approve else "🚫 בקשת הגישה נדחתה."
            )
        except Exception:
            pass
        return

    if not db.is_approved(user.id):
        return

    # Things that read the user's saved cards.
    if action == "upd_no":
        context.user_data.pop("pending_update", None)
        await query.edit_message_text("🚫 העדכון בוטל.")
        return
    if action.startswith(("send_", "retry_", "upd_pick_", "upd_yes_")) or action in ("share_plain", "share_notes"):
        g = await google_for(query.message, user.id)
        if not g:
            return
        store, contacts = g
        try:
            all_cards = await asyncio.to_thread(store.all)
        except Exception as e:
            await google_failed(query.message, user.id, e)
            return
        by_id = {c["id"]: c for c in all_cards}
        if action.startswith(("upd_pick_", "upd_yes_")):
            pending = context.user_data.get("pending_update")
            card = by_id.get(action.split("_", 2)[2])
            if not pending or not card:
                await query.edit_message_text("⚠️ העדכון פג. כתבו אותו שוב.")
                return
            if action.startswith("upd_pick_"):
                text, markup = update_proposal(card, pending)
                await query.edit_message_text(text, reply_markup=markup, parse_mode=HTML)
                return
            context.user_data.pop("pending_update", None)
            try:
                updated, contact_ok = await asyncio.to_thread(_apply_update, store, contacts, card, pending)
            except Exception as e:
                await google_failed(query.message, user.id, e)
                return
            status = "אנשי קשר ✓ · טבלה ✓" if contact_ok else "טבלה ✓ · אנשי קשר ✗"
            markup = None if contact_ok else InlineKeyboardMarkup([[InlineKeyboardButton("🔁 נסה שוב לשמור באנשי קשר", callback_data=f"retry_{card['id']}")]])
            await query.edit_message_text(f"✅ עודכן: <b>{escape(build_display_name(updated))}</b>\n{status}", reply_markup=markup, parse_mode=HTML)
            return
        if action.startswith("send_"):
            card = by_id.get(action.split("_", 1)[1])
            if card:
                await send_card(context, query.message.chat_id, card)
        elif action.startswith("retry_"):
            card = by_id.get(action.split("_", 1)[1])
            if not card:
                await query.edit_message_text("⚠️ הכרטיס לא נמצא בטבלה.")
                return
            try:
                await asyncio.to_thread(_retry_contact, store, contacts, card)
                await query.edit_message_text(f"✅ נשמר באנשי קשר: <b>{escape(build_display_name(card))}</b>", parse_mode=HTML)
            except Exception:
                await query.message.reply_text("⚠️ עדיין לא הצליח. נסו שוב מאוחר יותר.")
        else:
            chosen = [by_id[i] for i in context.user_data.get("last_results", []) if i in by_id]
            if not chosen:
                await query.edit_message_text("⚠️ אין רשימה לשתף. שאלו שוב.")
                return
            with_notes = action == "share_notes"
            vcf = "\r\n".join(generate_vcard(c, include_note=with_notes) for c in chosen)
            events = {c.get("event") for c in chosen if c.get("event")}
            title = events.pop() if len(events) == 1 else "כרטיסי ביקור"
            doc = io.BytesIO(vcf.encode("utf-8"))
            doc.name = f"{title} – {len(chosen)} אנשי קשר.vcf"
            summary = "\n".join(f"• {escape(build_display_name(c))}" for c in chosen)
            await query.edit_message_text(f"📤 {len(chosen)} אנשי קשר{' עם הערות' if with_notes else ''}. אפשר להעביר את הקובץ בוואטסאפ או בטלגרם:")
            await context.bot.send_document(chat_id=query.message.chat_id, document=doc, caption=summary[:1000], parse_mode=HTML)
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
        text, markup = await finalize_save(context, query.message, user, note)
        await query.edit_message_text(text, reply_markup=markup, parse_mode=HTML)
    elif action == "scan_second_side":
        context.user_data["waiting_second_side"] = True
        await query.edit_message_text("📷 שלחו תמונה של הצד השני. הפרטים יתמזגו.", reply_markup=_back_keyboard())
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


def _retry_contact(store, contacts, card: dict):
    card = {**card, "google_contact": contacts.create(card)}
    store.update(card["id"], card)


# ---------------------------------------------------------------- text: edits, notes, questions

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context):
        return
    await process_text(update, context, update.message.text.strip())


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A voice message counts as if its words were typed: a note, a field edit, a question or an update."""
    if not await guard(update, context):
        return
    voice = update.message.voice or update.message.audio
    status = await update.message.reply_text("🎙️ מקשיב…")
    try:
        f = await voice.get_file()
        audio = bytes(await f.download_as_bytearray())
        text = await asyncio.to_thread(transcribe_audio, audio, getattr(voice, "mime_type", None) or "audio/ogg")
    except GeminiBusy:
        await status.edit_text(BUSY)
        return
    except Exception as e:
        log.warning("transcription failed: %s", type(e).__name__)
        await status.edit_text("⚠️ לא הצלחתי לתמלל. נסו שוב או כתבו.")
        return
    if not text:
        await status.edit_text("🎙️ לא שמעתי דיבור. נסו שוב.")
        return
    await status.edit_text(f"🎙️ <i>{escape(text)}</i>", parse_mode=HTML)
    await process_text(update, context, text[:1000])


async def process_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user = update.effective_user
    data = context.user_data.get("pending_contact")

    if data and context.user_data.get("editing_field"):
        apply_edit(data, context.user_data.pop("editing_field"), text)
        dup = context.user_data.get("duplicate")
        preview, markup = render_preview(data, dup, db.get_event(user.id))
        await update.message.reply_text("✅ עודכן\n\n" + preview, reply_markup=markup, parse_mode=HTML)
        return

    if data and context.user_data.get("waiting_for_note"):
        await close_note_prompt(context)
        reply, markup = await finalize_save(context, update.message, user, text[:NOTE_MAX_LEN])
        await update.message.reply_text(reply, reply_markup=markup, parse_mode=HTML)
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
        fmt = lambda d: f"{d[8:10]}.{d[5:7]}" if d else "…"  # noqa: E731
        parts.append(fmt(a) if a == b else f"{fmt(a)}–{fmt(b)}")
    if f.get("terms"):
        parts.append(", ".join(f["terms"][:6]))
    return " · ".join(parts)


FIELD_LABELS = {
    "first_name_he": "שם פרטי", "last_name_he": "שם משפחה", "first_name": "First name", "last_name": "Last name",
    "job_title": "Title", "job_title_he": "תפקיד", "company": "חברה", "company_he": "חברה (עברית)", "country": "מדינה",
    "phone": "נייד", "phone_office": "טלפון משרד", "email": "מייל", "website": "אתר", "telegram": "טלגרם", "address": "כתובת", "event": "אירוע",
}


def update_proposal(card: dict, pending: dict) -> tuple[str, InlineKeyboardMarkup]:
    """What will change, old → new, with confirm / cancel."""
    lines = [f"✏️ לעדכן את <b>{escape(build_display_name(card))}</b>?", ""]
    for field, new in pending["changes"].items():
        old = card.get(field) or "—"
        lines.append(f"• {FIELD_LABELS.get(field, field)}: {escape(old)} ← <b>{escape(new)}</b>")
    if pending.get("note"):
        if pending.get("note_mode") == "replace":
            lines.append(f"• הערה (במקום הקיימת): <b>{escape(pending['note'])}</b>")
        else:
            lines.append(f"• הוספה להערה: <b>{escape(pending['note'])}</b>")
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ עדכן", callback_data=f"upd_yes_{card['id']}"),
        InlineKeyboardButton("❌ ביטול", callback_data="upd_no"),
    ]])
    return "\n".join(lines), markup


def _apply_update(store, contacts, card: dict, pending: dict) -> tuple[dict, bool]:
    """Writes the confirmed change to the sheet and the Google contact. Returns (updated card, contacts ok)."""
    updated = {**card, **pending["changes"]}
    if "company" in pending["changes"] and "company_he" not in pending["changes"]:
        updated["company_he"] = ""  # the old Hebrew name belonged to the old company
    for f in ("phone", "phone_office"):
        if f in pending["changes"]:
            updated[f] = db.clean_phone_number(updated[f])
    if pending.get("note"):
        if pending.get("note_mode") == "replace":
            updated["note"] = pending["note"]
        else:
            dated = f"{date.today():%d.%m}: {pending['note']}"
            updated["note"] = " | ".join(n for n in (card.get("note"), dated) if n)
    contact_ok = True
    try:
        resource = card.get("google_contact")
        updated["google_contact"] = contacts.update(resource, updated) if resource else contacts.create(updated)
    except Exception:
        contact_ok = False
    store.update(card["id"], updated)
    return updated, contact_ok


async def propose_update(message, context: ContextTypes.DEFAULT_TYPE, f: dict, all_cards: list[dict]):
    pending = {"changes": f["changes"], "note": f.get("note"), "note_mode": f.get("note_mode", "append")}
    candidates = cards.search(all_cards, f["who"], limit=5)
    if not candidates:
        await message.edit_text(f"🔎 לא מצאתי את מי לעדכן ({escape(', '.join(f['who'][:4]))}). נסו עם שם או חברה.", parse_mode=HTML)
        return
    context.user_data["pending_update"] = pending
    if len(candidates) == 1:
        text, markup = update_proposal(candidates[0], pending)
        await message.edit_text(text, reply_markup=markup, parse_mode=HTML)
        return
    rows = [[InlineKeyboardButton(build_display_name(c)[:60], callback_data=f"upd_pick_{c['id']}")] for c in candidates]
    rows.append([InlineKeyboardButton("❌ ביטול", callback_data="upd_no")])
    await message.edit_text("✏️ את מי לעדכן?", reply_markup=InlineKeyboardMarkup(rows))


async def answer_question(update: Update, context: ContextTypes.DEFAULT_TYPE, question: str):
    user = update.effective_user
    g = await google_for(update, user.id)
    if not g:
        return
    status = await update.message.reply_text("🔎 מחפש…")
    try:
        f, all_cards = await asyncio.gather(
            asyncio.to_thread(parse_question, question, date.today()),
            asyncio.to_thread(g[0].all),
        )
    except Exception as e:
        await status.delete()
        await google_failed(update.message, user.id, e)
        return
    if f.get("action") == "update":
        await propose_update(status, context, f, all_cards)
        return
    rows = cards.search(all_cards, f["terms"], f["date_from"], f["date_to"])
    context.user_data["last_results"] = [r["id"] for r in rows]
    desc = escape(describe_filter(f))
    header = f"🔎 <i>{desc}</i> · {len(rows)} אנשים" if rows else f"🔎 <i>{desc}</i>"
    text, markup = render_results(rows, header)
    await status.edit_text(text, reply_markup=markup, parse_mode=HTML)
