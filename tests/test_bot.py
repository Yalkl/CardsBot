"""Tests (docs/SPEC.md): fake Telegram, fake Gemini, fake Google; a temporary accounts database.

Run:  python -m unittest discover -s tests -v
SQLite by default; TEST_DATABASE_URL runs the same tests on PostgreSQL (a throwaway local database).
"""

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest import mock

ADMIN, OTHER, STRANGER = 111, 222, 333
_tmp = tempfile.mkdtemp()
os.environ.update(
    TELEGRAM_BOT_TOKEN="test", GEMINI_API_KEY="test", ADMIN_TELEGRAM_ID=str(ADMIN), LOCAL_SQLITE_PATH=os.path.join(_tmp, "t.db"),
    GOOGLE_CLIENT_ID="cid", GOOGLE_CLIENT_SECRET="csecret", PUBLIC_URL="https://bot.example",
    TOKEN_ENC_KEY="x4Q1aJ0r4nRk3G7mY2b9sVfL8cT5uWzE1hN6pD3qK0o=",
)
if os.environ.get("TEST_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
else:
    os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from google.auth.exceptions import RefreshError  # noqa: E402

import cards  # noqa: E402
import database as db  # noqa: E402
import extractor  # noqa: E402
import google_auth  # noqa: E402
import handlers  # noqa: E402
import web  # noqa: E402

NIKITA = {
    "first_name": "Nikita", "last_name": "Ivanov", "company": "Embassy of Bulgaria", "job_title": "Deputy Ambassador",
    "country": "Bulgaria", "phone": "+359 88 123 4567", "email": "n.ivanov@mfa.bg", "website": "", "telegram": "", "address": "",
    "first_name_he": "ניקיטה", "last_name_he": "איבנוב", "job_title_he": "סגן שגריר", "company_he": "שגרירות בולגריה",
}
ANNA = {**{k: "" for k in NIKITA}, "first_name": "Anna", "last_name": "Muller", "company": "Siemens", "job_title": "Sales Manager",
        "country": "Germany", "phone": "+49 151 2345678", "first_name_he": "אנה", "last_name_he": "מולר", "job_title_he": "מנהלת מכירות"}

log: list = []
next_card: list = []


# ---------------------------------------------------------------- fake Google (for handler tests)

class FakeGoogle:
    """One user's Google: the sheet rows and the contacts, in memory."""

    def __init__(self):
        self.rows: list[dict] = []
        self.contacts: dict[str, dict] = {}
        self.fail_contacts = False
        self.fail_sheet = False
        self.refresh_error = False
        self.n = 0

    # SheetStore interface
    def all(self):
        if self.refresh_error:
            raise RefreshError("revoked")
        return sorted((dict(r) for r in self.rows), key=lambda c: c.get("scanned_at") or "", reverse=True)

    def insert(self, card):
        if self.fail_sheet:
            raise RuntimeError("sheets down")
        self.n += 1
        row = {**card, "id": f"c{self.n}"}
        self.rows.append(row)
        return row["id"]

    def update(self, card_id, card):
        for i, r in enumerate(self.rows):
            if r["id"] == card_id:
                self.rows[i] = {**card, "id": card_id}
                return True
        return False

    def url(self):
        return "https://docs.google.com/spreadsheets/d/fake"

    # ContactsClient interface
    def create(self, card):
        if self.fail_contacts:
            raise RuntimeError("people down")
        rid = f"people/{len(self.contacts) + 1}"
        self.contacts[rid] = dict(card)
        return rid


class FakeContacts:
    def __init__(self, g):
        self.g = g

    def create(self, card):
        return self.g.create(card)

    def update(self, resource, card):
        self.g.contacts[resource] = dict(card)
        return resource


google: dict[int, FakeGoogle] = {}


def fake_open_user(uid):
    u = db.get_user(uid)
    if not u or not u.get("refresh_token_enc"):
        raise google_auth.GoogleNotConnected()
    g = google.setdefault(uid, FakeGoogle())
    return g, FakeContacts(g)


def connect(uid):
    db.save_google(uid, f"u{uid}@gmail.com", google_auth.encrypt("refresh-token"))


handlers.extract_contact_info = lambda _b: dict(next_card.pop(0))
handlers.cards.open_user = fake_open_user


# ---------------------------------------------------------------- fake Telegram

class Msg:
    _id = 0

    def __init__(self, chat_id, text=None):
        Msg._id += 1
        self.message_id, self.chat_id, self.text = Msg._id, chat_id, text
        self.photo = [SimpleNamespace(get_file=self._file)]

    async def _file(self):
        async def dl():
            return bytearray(b"img")
        return SimpleNamespace(download_as_bytearray=dl)

    async def reply_text(self, text, **kw):
        log.append(("reply", self.chat_id, text, kw.get("reply_markup")))
        return Msg(self.chat_id)

    async def edit_text(self, text, **kw):
        log.append(("edit", self.chat_id, text, kw.get("reply_markup")))
        return self

    async def delete(self):
        pass


class Bot:
    async def send_contact(self, **kw):
        log.append(("contact", kw["chat_id"], kw["first_name"], kw["vcard"]))

    async def send_document(self, **kw):
        log.append(("document", kw["chat_id"], kw["document"].name, kw["document"].getvalue().decode("utf-8"), kw.get("caption")))

    async def send_message(self, **kw):
        log.append(("message", kw["chat_id"], kw["text"], kw.get("reply_markup")))

    async def edit_message_reply_markup(self, **kw):
        pass


class Ctx:
    def __init__(self):
        self.user_data, self.bot, self.args = {}, Bot(), []


def user(uid):
    return SimpleNamespace(id=uid, full_name=f"User {uid}", username=f"u{uid}")


def upd(uid, text=None):
    m = Msg(uid, text)
    return SimpleNamespace(effective_user=user(uid), message=m, effective_message=m)


async def photo(ctx, uid, card):
    next_card.append(card)
    await handlers.handle_photo(upd(uid), ctx)


async def text(ctx, uid, t):
    await handlers.handle_text_input(upd(uid, t), ctx)


async def voice(ctx, uid, spoken):
    """A voice message; the fake transcription returns `spoken`."""
    handlers.transcribe_audio = lambda audio, mime=None: spoken
    u = upd(uid)

    async def get_file():
        async def dl():
            return bytearray(b"ogg")
        return SimpleNamespace(download_as_bytearray=dl)

    u.message.voice = SimpleNamespace(get_file=get_file, mime_type="audio/ogg")
    u.message.audio = None
    await handlers.handle_voice(u, ctx)


def search_filter(terms=None, date_from=None, date_to=None):
    return {"action": "search", "date_from": date_from, "date_to": date_to, "terms": terms or [], "who": [], "changes": {}, "note": None, "note_mode": "append"}


def update_filter(who, changes=None, note=None, note_mode="append"):
    return {"action": "update", "date_from": None, "date_to": None, "terms": [], "who": who, "changes": changes or {}, "note": note, "note_mode": note_mode}


async def press(ctx, uid, data):
    m = Msg(uid)

    async def answer():
        pass

    async def edit(t, **kw):
        log.append(("edit", uid, t, kw.get("reply_markup")))

    q = SimpleNamespace(data=data, answer=answer, message=m, edit_message_text=edit)
    await handlers.handle_callback(SimpleNamespace(effective_user=user(uid), callback_query=q, effective_message=m), ctx)


async def command(fn, ctx, uid, *args):
    ctx.args = list(args)
    await fn(upd(uid, "/cmd"), ctx)


def buttons(markup):
    return [getattr(b, "callback_data", None) or getattr(b, "url", None) for row in markup.inline_keyboard for b in row] if markup else []


def last(kind, chat=None):
    return next(e for e in reversed(log) if e[0] == kind and (chat is None or e[1] == chat))


def run(coro):
    return asyncio.run(coro)


def reset_db():
    for t in ("users", "active_events", "oauth_states"):
        db._run(f"DELETE FROM {t}")


class Bot_(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()

    def setUp(self):
        log.clear()
        next_card.clear()
        google.clear()
        reset_db()
        connect(ADMIN)
        self.admin, self.other, self.stranger = Ctx(), Ctx(), Ctx()

    def g(self, uid=ADMIN) -> FakeGoogle:
        return google.setdefault(uid, FakeGoogle())

    def _save(self, ctx, uid, card, note=None):
        run(photo(ctx, uid, card))
        run(press(ctx, uid, "save_default"))
        if note:
            run(text(ctx, uid, note))
        else:
            run(press(ctx, uid, "note_skip"))

    # ---- access

    def test_stranger_is_blocked_and_admin_asked_once(self):
        run(command(handlers.start, self.stranger, STRANGER))
        self.assertIn("בקשת הגישה נשלחה", last("reply", STRANGER)[2])
        self.assertIn(f"approve_{STRANGER}", buttons(last("message", ADMIN)[3]))
        run(photo(self.stranger, STRANGER, NIKITA))
        self.assertEqual(len([e for e in log if e[0] == "message" and e[1] == ADMIN]), 1)
        self.assertEqual(next_card, [NIKITA], "card was not read for a stranger")

    def test_approved_user_must_connect_google_before_scanning(self):
        run(command(handlers.start, self.stranger, STRANGER))
        run(press(self.admin, ADMIN, f"approve_{STRANGER}"))
        self.assertIn("הגישה אושרה", last("message", STRANGER)[2])
        run(command(handlers.start, self.stranger, STRANGER))
        login = last("reply", STRANGER)
        self.assertTrue(buttons(login[3])[0].startswith("https://accounts.google.com/"), "start offers the Google sign-in")
        run(photo(self.stranger, STRANGER, NIKITA))
        self.assertEqual(last("reply", STRANGER)[2], handlers.NEED_CONNECT)
        connect(STRANGER)
        run(photo(self.stranger, STRANGER, NIKITA))
        self.assertIn("save_default", buttons(last("reply", STRANGER)[3]))

    def test_only_admin_decides(self):
        run(command(handlers.start, self.stranger, STRANGER))
        run(press(self.other, OTHER, f"approve_{STRANGER}"))
        self.assertFalse(db.is_approved(STRANGER))
        run(press(self.admin, ADMIN, f"deny_{STRANGER}"))
        run(command(handlers.start, self.stranger, STRANGER))
        self.assertIn("אין לך גישה", last("reply", STRANGER)[2])

    # ---- scan and save to Google

    def test_scan_save_with_note_goes_to_sheet_and_contacts(self):
        run(command(handlers.event_cmd, self.admin, ADMIN, "כנס", "גרמניה"))
        run(photo(self.admin, ADMIN, NIKITA))
        self.assertIn("ניקיטה איבנוב · סגן שגריר · שגרירות בולגריה · Nikita Ivanov", last("reply", ADMIN)[2])
        run(press(self.admin, ADMIN, "save_default"))
        self.assertIn("הערה?", last("edit", ADMIN)[2])
        run(text(self.admin, ADMIN, "רוצה לבוא לביקור באביב"))
        done = last("reply", ADMIN)
        self.assertIn("נשמר", done[2])
        self.assertIn("אנשי קשר ✓ · טבלה ✓", done[2])
        g = self.g()
        self.assertEqual(len(g.rows), 1)
        row = g.rows[0]
        self.assertEqual((row["note"], row["event"], row["google_contact"]), ("רוצה לבוא לביקור באביב", "כנס גרמניה", "people/1"))
        self.assertEqual(g.contacts["people/1"]["first_name_he"], "ניקיטה")
        self.assertEqual(db.get_last_note(ADMIN), "רוצה לבוא לביקור באביב")
        self.assertNotIn("pending_contact", self.admin.user_data)
        self.assertFalse(any(e[0] == "contact" for e in log), "no contact card needed: Google syncs to the phone")

    def test_brand_company_stays_in_original(self):
        run(photo(self.admin, ADMIN, ANNA))
        self.assertIn("אנה מולר · מנהלת מכירות · Siemens · Anna Muller", last("reply", ADMIN)[2])

    def test_contacts_failure_keeps_sheet_row_and_retry_works(self):
        self.g().fail_contacts = True
        self._save(self.admin, ADMIN, NIKITA)
        done = last("edit", ADMIN)
        self.assertIn("טבלה ✓ · אנשי קשר ✗", done[2])
        retry = buttons(done[3])[0]
        self.assertEqual(self.g().rows[0]["google_contact"], "")
        self.g().fail_contacts = False
        run(press(self.admin, ADMIN, retry))
        self.assertIn("נשמר באנשי קשר", last("edit", ADMIN)[2])
        self.assertEqual(self.g().rows[0]["google_contact"], "people/1")

    def test_sheet_failure_keeps_the_card_to_try_again(self):
        self.g().fail_sheet = True
        run(photo(self.admin, ADMIN, NIKITA))
        run(press(self.admin, ADMIN, "save_default"))
        run(press(self.admin, ADMIN, "note_skip"))
        self.assertIn("השמירה לגוגל נכשלה", last("edit", ADMIN)[2])
        self.assertIn("pending_contact", self.admin.user_data)
        self.g().fail_sheet = False
        run(press(self.admin, ADMIN, "save_default"))
        run(press(self.admin, ADMIN, "note_skip"))
        self.assertEqual(len(self.g().rows), 1)

    def test_revoked_google_asks_to_reconnect(self):
        self.g().refresh_error = True
        handlers.parse_question = lambda q, d=None: {"date_from": None, "date_to": None, "terms": []}
        run(text(self.admin, ADMIN, "מי"))
        self.assertIn("התחברו מחדש", last("reply", ADMIN)[2])
        self.assertIsNone(db.get_user(ADMIN)["refresh_token_enc"])

    def test_note_repeat_skip_and_fast_scanning(self):
        db.set_last_note(ADMIN, "כנס היין")
        run(photo(self.admin, ADMIN, NIKITA))
        run(press(self.admin, ADMIN, "save_default"))
        self.assertIn("note_repeat", buttons(last("edit", ADMIN)[3]))
        run(press(self.admin, ADMIN, "note_repeat"))
        run(photo(self.admin, ADMIN, ANNA))
        run(press(self.admin, ADMIN, "save_default"))
        run(photo(self.admin, ADMIN, {**ANNA, "first_name": "Jurgen", "phone": "+49 170 0000001", "first_name_he": "יורגן"}))
        self.assertTrue(any("נשמר בלי הערה" in e[2] for e in log if e[0] == "reply"))
        notes = sorted(r["note"] for r in self.g().rows)
        self.assertEqual(notes, ["", "כנס היין"])

    def test_event_mode_and_expiry(self):
        run(command(handlers.event_cmd, self.admin, ADMIN, "כנס", "גרמניה", "2026"))
        self._save(self.admin, ADMIN, ANNA)
        run(command(handlers.event_cmd, self.admin, ADMIN, "off"))
        self._save(self.admin, ADMIN, NIKITA)
        self.assertEqual({r["first_name"]: r["event"] for r in self.g().rows}, {"Anna": "כנס גרמניה 2026", "Nikita": ""})
        db.set_event(ADMIN, "ישן")
        db._run("UPDATE active_events SET expires_at = ? WHERE owner_id = ?", ((datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M"), ADMIN))
        self.assertIsNone(db.get_event(ADMIN))

    def test_duplicate_update_keeps_one_row_and_one_contact(self):
        self._save(self.admin, ADMIN, NIKITA, "פגישה ראשונה")
        run(photo(self.admin, ADMIN, {**NIKITA, "email": "new@mfa.bg"}))
        preview = last("reply", ADMIN)
        self.assertIn("כבר שמור", preview[2])
        self.assertIn("save_update", buttons(preview[3]))
        run(press(self.admin, ADMIN, "save_update"))
        run(text(self.admin, ADMIN, "פגישה שנייה"))
        g = self.g()
        self.assertEqual(len(g.rows), 1)
        self.assertEqual(g.rows[0]["email"], "new@mfa.bg")
        self.assertEqual(g.rows[0]["note"], "פגישה ראשונה | פגישה שנייה")
        self.assertEqual(list(g.contacts), ["people/1"])
        self.assertEqual(g.contacts["people/1"]["email"], "new@mfa.bg")

    def test_edit_hebrew_name_and_escaping(self):
        run(photo(self.admin, ADMIN, NIKITA))
        run(press(self.admin, ADMIN, "edit_field_name_he"))
        run(text(self.admin, ADMIN, "ניקיטה <איוואנוב>"))
        self.assertIn("ניקיטה &lt;איוואנוב&gt; · סגן שגריר", last("reply", ADMIN)[2])

    # ---- questions and sharing

    def test_question_with_dates_and_terms_then_share(self):
        run(command(handlers.event_cmd, self.admin, ADMIN, "כנס", "גרמניה"))
        self._save(self.admin, ADMIN, ANNA, "דיברנו על הפצה")
        self._save(self.admin, ADMIN, NIKITA, "רוצה לבוא באביב")
        next(r for r in self.g().rows if r["first_name"] == "Nikita")["scanned_at"] = "2026-01-05 10:00"
        today = date.today().isoformat()
        handlers.parse_question = lambda q, d=None: {"date_from": today, "date_to": today, "terms": ["Germany", "גרמניה"]}
        run(text(self.admin, ADMIN, "את מי פגשנו היום בכנס הגרמני?"))
        result = last("edit", ADMIN)
        self.assertIn("אנה מולר", result[2])
        self.assertNotIn("ניקיטה", result[2])
        self.assertIn("דיברנו על הפצה", result[2])

        handlers.parse_question = lambda q, d=None: {"date_from": None, "date_to": None, "terms": []}
        run(text(self.admin, ADMIN, "כולם"))
        self.assertIn("share_all", buttons(last("edit", ADMIN)[3]))
        run(press(self.admin, ADMIN, "share_all"))
        run(press(self.admin, ADMIN, "share_plain"))
        doc = last("document", ADMIN)
        self.assertEqual(doc[3].count("BEGIN:VCARD"), 2)
        self.assertNotIn("הערה:", doc[3])
        self.assertIn("2 אנשי קשר", doc[2])
        run(press(self.admin, ADMIN, "share_notes"))
        self.assertIn("הערה: רוצה לבוא באביב", last("document", ADMIN)[3])
        run(press(self.admin, ADMIN, f"send_{self.g().rows[0]['id']}"))
        self.assertIn("אנה", last("contact", ADMIN)[2])

    def test_users_see_only_their_own_cards(self):
        db.request_access(OTHER, "Other", None)
        db.decide_access(OTHER, True)
        connect(OTHER)
        self._save(self.admin, ADMIN, NIKITA)
        handlers.parse_question = lambda q, d=None: {"date_from": None, "date_to": None, "terms": ["Nikita"]}
        run(text(self.other, OTHER, "ניקיטה"))
        self.assertIn("לא מצאתי", last("edit", OTHER)[2])
        run(press(self.other, OTHER, f"send_{self.g().rows[0]['id']}"))  # someone else's card id
        self.assertFalse(any(e[0] == "contact" and e[1] == OTHER for e in log))

    # ---- updating a saved contact by message

    def test_update_by_message_shows_change_then_applies_to_sheet_and_contact(self):
        self._save(self.admin, ADMIN, NIKITA, "פגישה ראשונה")
        handlers.parse_question = lambda q, d=None: update_filter(["Nikita", "ניקיטה"], {"address": "הרצל 5, טשקנט"}, "עבר משרד")
        run(text(self.admin, ADMIN, "ניקיטה עבר לכתובת הרצל 5 טשקנט, עבר משרד"))
        proposal = last("edit", ADMIN)
        self.assertIn("לעדכן את", proposal[2])
        self.assertIn("כתובת: — ← <b>הרצל 5, טשקנט</b>", proposal[2])
        self.assertIn("הוספה להערה", proposal[2])
        card_id = self.g().rows[0]["id"]
        self.assertEqual(buttons(proposal[3]), [f"upd_yes_{card_id}", "upd_no"])
        self.assertEqual(self.g().rows[0]["address"], "", "nothing changes before confirming")
        run(press(self.admin, ADMIN, f"upd_yes_{card_id}"))
        self.assertIn("עודכן", last("edit", ADMIN)[2])
        row = self.g().rows[0]
        self.assertEqual(row["address"], "הרצל 5, טשקנט")
        self.assertEqual(row["note"], f"פגישה ראשונה | {date.today():%d.%m}: עבר משרד")
        self.assertEqual(self.g().contacts["people/1"]["address"], "הרצל 5, טשקנט")

    def test_update_new_company_drops_old_hebrew_name_and_replace_note(self):
        self._save(self.admin, ADMIN, NIKITA, "ישן")
        handlers.parse_question = lambda q, d=None: update_filter(["Nikita"], {"company": "Uzbek Wine"}, "מחליף", "replace")
        run(text(self.admin, ADMIN, "ניקיטה עבר ל-Uzbek Wine, תחליף את ההערה ל: מחליף"))
        run(press(self.admin, ADMIN, f"upd_yes_{self.g().rows[0]['id']}"))
        row = self.g().rows[0]
        self.assertEqual((row["company"], row["company_he"], row["note"]), ("Uzbek Wine", "", "מחליף"))
        self.assertIn("Uzbek Wine", last("edit", ADMIN)[2])

    def test_update_with_several_matches_asks_who(self):
        self._save(self.admin, ADMIN, ANNA)
        self._save(self.admin, ADMIN, {**ANNA, "first_name": "Anna", "last_name": "Weber", "phone": "+49 170 1111111", "last_name_he": "וובר"})
        handlers.parse_question = lambda q, d=None: update_filter(["Anna"], {"phone": "+49 171 2222222"})
        run(text(self.admin, ADMIN, "לאנה יש טלפון חדש"))
        choose = last("edit", ADMIN)
        self.assertIn("את מי לעדכן", choose[2])
        picks = [b for b in buttons(choose[3]) if b.startswith("upd_pick_")]
        self.assertEqual(len(picks), 2)
        run(press(self.admin, ADMIN, picks[0]))
        target = picks[0].removeprefix("upd_pick_")
        run(press(self.admin, ADMIN, f"upd_yes_{target}"))
        phones = {r["id"]: r["phone"] for r in self.g().rows}
        self.assertEqual(phones[target], "+491712222222")
        self.assertEqual(sum(1 for p in phones.values() if p == "+491712222222"), 1)

    def test_update_cancel_and_no_match(self):
        self._save(self.admin, ADMIN, NIKITA)
        handlers.parse_question = lambda q, d=None: update_filter(["Nikita"], {"email": "x@y.z"})
        run(text(self.admin, ADMIN, "מייל חדש לניקיטה"))
        run(press(self.admin, ADMIN, "upd_no"))
        self.assertIn("בוטל", last("edit", ADMIN)[2])
        self.assertEqual(self.g().rows[0]["email"], "n.ivanov@mfa.bg")
        handlers.parse_question = lambda q, d=None: update_filter(["Boris"], {"email": "x@y.z"})
        run(text(self.admin, ADMIN, "מייל חדש לבוריס"))
        self.assertIn("לא מצאתי את מי לעדכן", last("edit", ADMIN)[2])

    # ---- voice

    def test_voice_note_while_saving(self):
        run(photo(self.admin, ADMIN, NIKITA))
        run(press(self.admin, ADMIN, "save_default"))
        run(voice(self.admin, ADMIN, "רוצה לבוא לביקור באביב"))
        self.assertIn("🎙️ <i>רוצה לבוא לביקור באביב</i>", [e[2] for e in log if e[0] == "edit"])
        self.assertEqual(self.g().rows[0]["note"], "רוצה לבוא לביקור באביב")

    def test_voice_question_and_update(self):
        self._save(self.admin, ADMIN, NIKITA)
        handlers.parse_question = lambda q, d=None: search_filter(["Nikita"])
        run(voice(self.admin, ADMIN, "מה המספר של ניקיטה"))
        self.assertIn("ניקיטה", last("edit", ADMIN)[2])
        handlers.parse_question = lambda q, d=None: update_filter(["Nikita"], {"address": "טשקנט"})
        run(voice(self.admin, ADMIN, "ניקיטה עבר לטשקנט"))
        self.assertIn("לעדכן את", last("edit", ADMIN)[2])

    def test_voice_without_speech(self):
        run(voice(self.admin, ADMIN, ""))
        self.assertIn("לא שמעתי", last("edit", ADMIN)[2])

    def test_voice_from_stranger_is_ignored(self):
        run(voice(self.stranger, STRANGER, "שלום"))
        self.assertIn("בקשת הגישה נשלחה", last("reply", STRANGER)[2])

    # ---- mobile and office numbers

    def test_mobile_and_office_numbers(self):
        card = {**NIKITA, "phone_office": "+359 2 948 0000"}
        run(photo(self.admin, ADMIN, card))
        preview = last("reply", ADMIN)[2]
        self.assertIn("📱 +359881234567", preview)
        self.assertIn("☎️ +35929480000", preview)
        run(press(self.admin, ADMIN, "save_default"))
        run(press(self.admin, ADMIN, "note_skip"))
        self.assertEqual(self.g().rows[0]["phone_office"], "+359 2 948 0000")
        # Same person by the office number only: still a duplicate.
        run(photo(self.admin, ADMIN, {**ANNA, "phone": "", "phone_office": "+359 2 948 0000", "first_name": "Other"}))
        self.assertIn("כבר שמור", last("reply", ADMIN)[2])

    def test_card_with_only_office_number_is_sent_as_contact(self):
        self._save(self.admin, ADMIN, {**NIKITA, "phone": "", "phone_office": "+359 2 948 0000"})
        run(press(self.admin, ADMIN, f"send_{self.g().rows[0]['id']}"))
        self.assertIn("TEL;TYPE=WORK,VOICE:+35929480000", last("contact", ADMIN)[3])

    def test_edit_office_number(self):
        run(photo(self.admin, ADMIN, NIKITA))
        run(press(self.admin, ADMIN, "edit_field_phone_office"))
        run(text(self.admin, ADMIN, "+359 (2) 111-2222"))
        self.assertIn("☎️ +35921112222", last("reply", ADMIN)[2])

    def test_slow_telegram_download_is_retried_then_reported(self):
        from telegram.error import TimedOut
        tries = []

        async def slow_file():
            tries.append(1)
            raise TimedOut()

        handlers._pause = _no_sleep
        u = upd(ADMIN)
        u.message.photo = [SimpleNamespace(get_file=slow_file)]
        run(handlers.handle_photo(u, self.admin))
        self.assertEqual(len(tries), 3)
        self.assertIn("שלחו אותה שוב", last("edit", ADMIN)[2])

        # The second try works: the card is read normally.
        calls = []

        async def flaky_file():
            calls.append(1)
            if len(calls) == 1:
                raise TimedOut()
            async def dl():
                return bytearray(b"img")
            return SimpleNamespace(download_as_bytearray=dl)

        next_card.append(NIKITA)
        u = upd(ADMIN)
        u.message.photo = [SimpleNamespace(get_file=flaky_file)]
        run(handlers.handle_photo(u, self.admin))
        self.assertIn("save_default", buttons(last("reply", ADMIN)[3]))

    def test_busy_gemini_says_so_instead_of_blaming_the_photo(self):
        def busy(_b):
            raise extractor.GeminiBusy()
        handlers.extract_contact_info = busy
        try:
            run(handlers.handle_photo(upd(ADMIN), self.admin))
        finally:
            handlers.extract_contact_info = lambda _b: dict(next_card.pop(0))
        self.assertIn("Gemini עמוס", last("edit", ADMIN)[2])

    def test_sheet_command(self):
        run(command(handlers.sheet_cmd, self.admin, ADMIN))
        self.assertIn("docs.google.com/spreadsheets", last("reply", ADMIN)[2])


# ---------------------------------------------------------------- the Google layers themselves

class FakeRequest:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeSheetsService:
    """Enough of the Sheets API for SheetStore: one spreadsheet, one tab of string cells."""

    def __init__(self):
        self.values: list[list[str]] = []
        self.created = 0

    def spreadsheets(self):
        svc = self

        class S:
            def create(self, body, fields):
                svc.created += 1
                return FakeRequest(lambda: {"spreadsheetId": "sheet1"})

            def get(self, spreadsheetId, fields):
                return FakeRequest(lambda: {"spreadsheetId": spreadsheetId})

            def values(self):
                class V:
                    def update(self, spreadsheetId, range, valueInputOption, body):
                        def do():
                            row = int("".join(ch for ch in range.split("!")[1].split(":")[0] if ch.isdigit()))
                            while len(svc.values) < row:
                                svc.values.append([])
                            svc.values[row - 1] = body["values"][0]
                            svc.last_update_range = range
                            return {}
                        return FakeRequest(do)

                    def append(self, spreadsheetId, range, valueInputOption, insertDataOption, body):
                        return FakeRequest(lambda: svc.values.append(body["values"][0]) or {})

                    def get(self, spreadsheetId, range):
                        return FakeRequest(lambda: {"values": [list(r) for r in svc.values]})
                return V()
        return S()


class SheetStoreTests(unittest.TestCase):
    def setUp(self):
        db.init_db()
        reset_db()
        connect(ADMIN)
        self.svc = FakeSheetsService()
        with mock.patch.object(cards, "build", return_value=self.svc):
            self.store = cards.SheetStore(None, ADMIN)

    def test_creates_sheet_once_with_hebrew_header(self):
        self.store.ensure()
        self.store.ensure()
        self.assertEqual(self.svc.created, 1)
        self.assertEqual(self.svc.values[0][0], "שם בטלפון")
        self.assertEqual(db.get_user(ADMIN)["sheet_id"], "sheet1")

    def test_insert_read_update_roundtrip_keeps_phone_as_text(self):
        cid = self.store.insert({**NIKITA, "scanned_at": "2026-10-13 10:00", "event": "כנס", "note": "הערה"})
        card = self.store.get(cid)
        self.assertEqual(card["display_name"], "ניקיטה איבנוב · סגן שגריר · שגרירות בולגריה · Nikita Ivanov")
        self.assertEqual(card["phone"], "+359 88 123 4567")
        self.assertTrue(self.store.update(cid, {**card, "note": "עודכן"}))
        self.assertEqual(self.store.get(cid)["note"], "עודכן")
        self.assertTrue(self.svc.last_update_range.endswith("2"))

    def test_old_sheet_gets_the_office_column_added(self):
        self.store.ensure()
        old_header = [label for key, label in cards.COLUMNS if key != "phone_office"]
        old_header[old_header.index("נייד")] = "טלפון"  # the header before office numbers existed
        self.svc.values = [old_header, [""] * len(old_header)]
        self.svc.values[1][old_header.index("טלפון")] = "+972 50 111 2222"
        self.svc.values[1][old_header.index("מזהה")] = "old1"
        self.assertEqual(self.store.get("old1")["phone"], "+972 50 111 2222", "old rows still read")
        cid = self.store.insert({**NIKITA, "phone_office": "+359 2 948 0000", "scanned_at": "2026-10-14 10:00"})
        self.assertEqual(self.svc.values[0][-1], "טלפון משרד")
        card = self.store.get(cid)
        self.assertEqual((card["phone"], card["phone_office"]), ("+359 88 123 4567", "+359 2 948 0000"))

    def test_reads_by_header_even_if_columns_moved_and_rows_typed_by_hand(self):
        self.store.ensure()
        header = self.svc.values[0]
        self.svc.values[0] = list(reversed(header))  # the user reordered the columns
        hand = [""] * len(header)
        hand[header.index("מייל")] = "a@b.c"
        hand[header.index("שם פרטי")] = "דנה"
        self.svc.values.append(list(reversed(hand)))
        card = self.store.all()[0]
        self.assertEqual((card["email"], card["first_name_he"], card["id"]), ("a@b.c", "דנה", "r2"))


class ContactsTests(unittest.TestCase):
    def test_person_fields(self):
        p = cards.ContactsClient._person({**NIKITA, "scanned_at": "2026-10-13 10:00", "event": "כנס", "note": "לחזור אליו", "website": "mfa.bg"})
        self.assertEqual(p["names"][0]["givenName"], "ניקיטה איבנוב · סגן שגריר · שגרירות בולגריה · Nikita Ivanov")
        self.assertEqual(p["organizations"][0], {"name": "Embassy of Bulgaria", "title": "Deputy Ambassador"})
        self.assertEqual(p["phoneNumbers"], [{"value": "+359881234567", "type": "mobile"}])
        both = cards.ContactsClient._person({**NIKITA, "phone_office": "+359 2 948 0000"})
        self.assertEqual(both["phoneNumbers"][1], {"value": "+35929480000", "type": "work"})
        self.assertEqual(p["urls"][0]["value"], "https://mfa.bg")
        self.assertIn("הערה: לחזור אליו", p["biographies"][0]["value"])
        self.assertIn("אירוע: כנס", p["biographies"][0]["value"])


class GoogleAuthTests(unittest.TestCase):
    def setUp(self):
        db.init_db()
        reset_db()

    def test_login_link_state_is_single_use_and_tied_to_user(self):
        url = google_auth.new_login_url(ADMIN)
        self.assertIn("contacts", url)
        self.assertIn("drive.file", url)
        self.assertIn("access_type=offline", url)
        state = url.split("state=")[1]
        self.assertEqual(db.consume_oauth_state(state), ADMIN)
        self.assertIsNone(db.consume_oauth_state(state), "second use refused")

    def test_expired_state_refused(self):
        db.create_oauth_state("old", ADMIN, minutes=-1)
        self.assertIsNone(db.consume_oauth_state("old"))

    def test_token_encryption(self):
        enc = google_auth.encrypt("secret-refresh")
        self.assertNotIn("secret", enc)
        self.assertEqual(google_auth.decrypt(enc), "secret-refresh")

    def test_email_from_id_token(self):
        import base64, json
        payload = base64.urlsafe_b64encode(json.dumps({"email": "y@gmail.com"}).encode()).decode().rstrip("=")
        self.assertEqual(google_auth._email_from_id_token(f"h.{payload}.s"), "y@gmail.com")


class OAuthCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient, TestServer

        db.init_db()
        reset_db()
        log.clear()
        self.client = TestClient(TestServer(web.make_app(Bot())))
        await self.client.start_server()
        self.ensure = mock.patch.object(web.cards, "open_user", side_effect=lambda uid: (SimpleNamespace(url=lambda: "https://sheet"), None))
        self.ensure.start()

    async def asyncTearDown(self):
        self.ensure.stop()
        await self.client.close()

    async def _login(self, scopes):
        state = google_auth.new_login_url(ADMIN).split("state=")[1]
        with mock.patch.object(google_auth, "exchange_code", return_value=("y@gmail.com", "rt", scopes)):
            return state, await self.client.get(f"/oauth/callback?state={state}&code=abc")

    async def test_success_saves_encrypted_token_and_tells_the_user(self):
        state, r = await self._login(list(web.NEEDED) + ["openid"])
        self.assertEqual(r.status, 200)
        u = db.get_user(ADMIN)
        self.assertEqual(u["google_email"], "y@gmail.com")
        self.assertEqual(google_auth.decrypt(u["refresh_token_enc"]), "rt")
        self.assertIn("מחובר לגוגל", last("message", ADMIN)[2])
        r2 = await self.client.get(f"/oauth/callback?state={state}&code=abc")
        self.assertEqual(r2.status, 400, "a link works once")

    async def test_missing_permission_is_refused(self):
        _, r = await self._login(["openid", "https://www.googleapis.com/auth/contacts"])
        self.assertEqual(r.status, 400)
        self.assertIsNone((db.get_user(ADMIN) or {}).get("refresh_token_enc"))

    async def test_home_and_privacy_pages(self):
        r = await self.client.get("/")
        self.assertEqual(r.status, 200)
        self.assertIn("/privacy", await r.text())
        r = await self.client.get("/privacy")
        self.assertIn("drive.file", await r.text())

    async def test_unknown_state_and_cancel(self):
        r = await self.client.get("/oauth/callback?state=nope&code=abc")
        self.assertEqual(r.status, 400)
        state = google_auth.new_login_url(ADMIN).split("state=")[1]
        r = await self.client.get(f"/oauth/callback?state={state}&error=access_denied")
        self.assertIn("בוטלה", last("message", ADMIN)[2])


class GeminiFallback(unittest.TestCase):
    def setUp(self):
        self.calls = []
        extractor.time.sleep = lambda _s: None

    def _client(self, outcomes):
        """outcomes: per model, an Exception to raise or a text to return."""
        def gen(model, contents, config=None):
            self.calls.append(model)
            out = outcomes[len(self.calls) - 1]
            if isinstance(out, Exception):
                raise out
            return SimpleNamespace(text=out)
        extractor.gemini_client = SimpleNamespace(models=SimpleNamespace(generate_content=gen))

    def test_next_model_when_quota_is_reached(self):
        self._client([RuntimeError("429 RESOURCE_EXHAUSTED"), '{"first_name": "Barbara", "phone": null}'])
        data = extractor.extract_contact_info(_png())
        self.assertEqual(self.calls, list(extractor.MODELS[:2]))
        self.assertEqual(data, {"first_name": "Barbara", "phone": ""})

    def test_all_busy_raises_gemini_busy(self):
        self._client([RuntimeError("429")] * len(extractor.MODELS))
        with self.assertRaises(extractor.GeminiBusy):
            extractor.extract_contact_info(_png())

    def test_other_errors_do_not_try_more_models(self):
        self._client([ValueError("bad request 400")])
        with self.assertRaises(ValueError):
            extractor.extract_contact_info(_png())
        self.assertEqual(len(self.calls), 1)

    def test_list_response_is_unwrapped(self):
        self._client(['[{"first_name": "Barbara"}]'])
        self.assertEqual(extractor.extract_contact_info(_png())["first_name"], "Barbara")


async def _no_sleep(_s):
    return None


def _png() -> bytes:
    from PIL import Image
    import io as _io
    buf = _io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(buf, format="PNG")
    return buf.getvalue()


class QuestionParser(unittest.TestCase):
    def _gemini(self, response_text=None, error=None):
        def gen(**_kw):
            if error:
                raise error
            return SimpleNamespace(text=response_text)
        extractor.gemini_client = SimpleNamespace(models=SimpleNamespace(generate_content=gen))

    def test_valid_search(self):
        self._gemini('{"action": "search", "date_from": "2026-10-04", "date_to": "2026-10-10", "terms": ["Germany", "גרמניה"]}')
        f = extractor.parse_question("את מי פגשנו שבוע שעבר בכנס הגרמני?", date(2026, 10, 14))
        self.assertEqual(f, search_filter(["Germany", "גרמניה"], "2026-10-04", "2026-10-10"))

    def test_valid_update_keeps_only_known_fields(self):
        self._gemini('{"action": "update", "who": ["Nikita", "ניקיטה"], "changes": {"address": "הרצל 5", "salary": "1M", "phone": ""}, "note": "עבר משרד", "note_mode": "weird"}')
        f = extractor.parse_question("ניקיטה עבר להרצל 5")
        self.assertEqual(f, update_filter(["Nikita", "ניקיטה"], {"address": "הרצל 5"}, "עבר משרד"))

    def test_update_without_who_or_change_becomes_a_search(self):
        self._gemini('{"action": "update", "who": [], "changes": {"address": "x"}, "terms": ["x"]}')
        self.assertEqual(extractor.parse_question("x")["action"], "search")

    def test_bad_dates_dropped(self):
        self._gemini('{"date_from": "last week", "date_to": null, "terms": ["Germany"]}')
        self.assertIsNone(extractor.parse_question("x")["date_from"])

    def test_failure_falls_back_to_words(self):
        self._gemini(error=RuntimeError("down"))
        self.assertEqual(extractor.parse_question("הבחור הרומני"), search_filter(["הבחור", "הרומני"]))


if __name__ == "__main__":
    unittest.main()
