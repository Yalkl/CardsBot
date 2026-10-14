"""Phase A tests (docs/SPEC.md): fake Telegram objects, fake Gemini, a temporary SQLite database.

Run:  python -m unittest discover -s tests -v
"""

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace

ADMIN, OTHER, STRANGER = 111, 222, 333
_tmp = tempfile.mkdtemp()
os.environ.update(TELEGRAM_BOT_TOKEN="test", GEMINI_API_KEY="test", ADMIN_TELEGRAM_ID=str(ADMIN), LOCAL_SQLITE_PATH=os.path.join(_tmp, "t.db"))
# SQLite by default; TEST_DATABASE_URL runs the same tests on PostgreSQL (a throwaway local database).
if os.environ.get("TEST_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
else:
    os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import database as db  # noqa: E402
import extractor  # noqa: E402
import handlers  # noqa: E402

NIKITA = {
    "first_name": "Nikita", "last_name": "Ivanov", "company": "Embassy of Bulgaria", "job_title": "Deputy Ambassador",
    "country": "Bulgaria", "phone": "+359 88 123 4567", "email": "n.ivanov@mfa.bg", "website": "", "telegram": "", "address": "",
    "first_name_he": "ניקיטה", "last_name_he": "איבנוב", "job_title_he": "סגן שגריר", "company_he": "שגרירות בולגריה",
}
ANNA = {**{k: "" for k in NIKITA}, "first_name": "Anna", "last_name": "Muller", "company": "Siemens", "job_title": "Sales Manager",
        "country": "Germany", "phone": "+49 151 2345678", "first_name_he": "אנה", "last_name_he": "מולר", "job_title_he": "מנהלת מכירות"}

log: list = []
next_card: list = []


def fake_extract(_bytes):
    return dict(next_card.pop(0))


handlers.extract_contact_info = fake_extract


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
    """One context per user, like python-telegram-bot's user_data."""

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
    return [b.callback_data for row in markup.inline_keyboard for b in row] if markup else []


def last(kind, chat=None):
    return next(e for e in reversed(log) if e[0] == kind and (chat is None or e[1] == chat))


def run(coro):
    return asyncio.run(coro)


class PhaseA(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()

    def setUp(self):
        log.clear()
        next_card.clear()
        for t in ("cards", "users", "active_events"):
            db._run(f"DELETE FROM {t}")
        self.admin, self.other, self.stranger = Ctx(), Ctx(), Ctx()

    # ---- access

    def test_stranger_is_blocked_and_admin_asked_once(self):
        run(command(handlers.start, self.stranger, STRANGER))
        self.assertIn("בקשת הגישה נשלחה", last("reply", STRANGER)[2])
        ask = last("message", ADMIN)
        self.assertIn(f"approve_{STRANGER}", buttons(ask[3]))
        run(photo(self.stranger, STRANGER, NIKITA))
        self.assertEqual(len([e for e in log if e[0] == "message" and e[1] == ADMIN]), 1, "admin asked only once")
        self.assertEqual(next_card, [NIKITA], "card was not read for a stranger")

    def test_admin_approves_then_user_can_scan(self):
        run(command(handlers.start, self.stranger, STRANGER))
        run(press(self.admin, ADMIN, f"approve_{STRANGER}"))
        self.assertIn("הגישה אושרה", last("message", STRANGER)[2])
        run(photo(self.stranger, STRANGER, NIKITA))
        self.assertIn("save_default", buttons(last("reply", STRANGER)[3]))

    def test_denied_user_stays_out_and_only_admin_can_decide(self):
        run(command(handlers.start, self.stranger, STRANGER))
        run(press(self.other, OTHER, f"approve_{STRANGER}"))  # not the admin
        self.assertFalse(db.is_approved(STRANGER))
        run(press(self.admin, ADMIN, f"deny_{STRANGER}"))
        run(command(handlers.start, self.stranger, STRANGER))
        self.assertIn("אין לך גישה", last("reply", STRANGER)[2])

    # ---- scan and save

    def test_scan_save_with_note_hebrew_display_name(self):
        run(photo(self.admin, ADMIN, NIKITA))
        preview = last("reply", ADMIN)
        self.assertIn("ניקיטה איבנוב · סגן שגריר · שגרירות בולגריה · Nikita Ivanov", preview[2])
        run(press(self.admin, ADMIN, "save_default"))
        self.assertIn("הערה?", last("edit", ADMIN)[2])
        run(text(self.admin, ADMIN, "רוצה לבוא לביקור באביב"))
        contact = last("contact", ADMIN)
        self.assertEqual(contact[2], "ניקיטה איבנוב · סגן שגריר · שגרירות בולגריה · Nikita Ivanov")
        self.assertIn("ORG:Embassy of Bulgaria", contact[3])
        self.assertIn("הערה: רוצה לבוא לביקור באביב", contact[3])
        cards = db.all_cards(ADMIN)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["note"], "רוצה לבוא לביקור באביב")
        self.assertEqual(cards[0]["phone"], "+359881234567")
        self.assertEqual(db.get_last_note(ADMIN), "רוצה לבוא לביקור באביב")
        self.assertNotIn("pending_contact", self.admin.user_data)

    def test_brand_company_stays_in_original(self):
        run(photo(self.admin, ADMIN, ANNA))
        self.assertIn("אנה מולר · מנהלת מכירות · Siemens · Anna Muller", last("reply", ADMIN)[2])

    def test_note_repeat_and_skip(self):
        db.set_last_note(ADMIN, "כנס היין")
        run(photo(self.admin, ADMIN, NIKITA))
        run(press(self.admin, ADMIN, "save_default"))
        self.assertIn("note_repeat", buttons(last("edit", ADMIN)[3]))
        run(press(self.admin, ADMIN, "note_repeat"))
        run(photo(self.admin, ADMIN, ANNA))
        run(press(self.admin, ADMIN, "save_default"))
        run(press(self.admin, ADMIN, "note_skip"))
        notes = sorted((c["note"] or "") for c in db.all_cards(ADMIN))
        self.assertEqual(notes, ["", "כנס היין"])

    def test_next_photo_saves_previous_without_note(self):
        run(photo(self.admin, ADMIN, NIKITA))
        run(press(self.admin, ADMIN, "save_default"))
        run(photo(self.admin, ADMIN, ANNA))
        self.assertEqual(len(db.all_cards(ADMIN)), 1)
        self.assertTrue(any("נשמר בלי הערה" in e[2] for e in log if e[0] == "reply"))
        self.assertEqual(self.admin.user_data["pending_contact"]["first_name"], "Anna")

    def test_event_mode_tags_scans_until_off(self):
        run(command(handlers.event_cmd, self.admin, ADMIN, "כנס", "גרמניה", "2026"))
        run(photo(self.admin, ADMIN, ANNA))
        self.assertIn("אירוע: כנס גרמניה 2026", last("reply", ADMIN)[2])
        run(press(self.admin, ADMIN, "save_default"))
        run(press(self.admin, ADMIN, "note_skip"))
        run(command(handlers.event_cmd, self.admin, ADMIN, "off"))
        run(photo(self.admin, ADMIN, NIKITA))
        run(press(self.admin, ADMIN, "save_default"))
        run(press(self.admin, ADMIN, "note_skip"))
        events = {c["first_name"]: c["event"] for c in db.all_cards(ADMIN)}
        self.assertEqual(events, {"Anna": "כנס גרמניה 2026", "Nikita": ""})

    def test_event_expires(self):
        db.set_event(ADMIN, "ישן")
        db._run("UPDATE active_events SET expires_at = ? WHERE owner_id = ?", ((datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M"), ADMIN))
        self.assertIsNone(db.get_event(ADMIN))

    def test_duplicate_offers_update_and_update_keeps_one_card(self):
        run(photo(self.admin, ADMIN, NIKITA))
        run(press(self.admin, ADMIN, "save_default"))
        run(text(self.admin, ADMIN, "פגישה ראשונה"))
        run(photo(self.admin, ADMIN, {**NIKITA, "email": "new@mfa.bg"}))
        preview = last("reply", ADMIN)
        self.assertIn("כבר שמור", preview[2])
        self.assertIn("save_update", buttons(preview[3]))
        run(press(self.admin, ADMIN, "save_update"))
        run(text(self.admin, ADMIN, "פגישה שנייה"))
        cards = db.all_cards(ADMIN)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["email"], "new@mfa.bg")
        self.assertEqual(cards[0]["note"], "פגישה ראשונה | פגישה שנייה")

    def test_edit_hebrew_name(self):
        run(photo(self.admin, ADMIN, NIKITA))
        run(press(self.admin, ADMIN, "edit_field_name_he"))
        run(text(self.admin, ADMIN, "ניקיטה איוואנוב"))
        self.assertIn("ניקיטה איוואנוב · סגן שגריר", last("reply", ADMIN)[2])

    def test_card_without_phone_is_sent_as_file(self):
        run(photo(self.admin, ADMIN, {**ANNA, "phone": ""}))
        run(press(self.admin, ADMIN, "save_default"))
        run(press(self.admin, ADMIN, "note_skip"))
        self.assertEqual(last("document", ADMIN)[2], "contact.vcf")

    def test_user_text_is_escaped(self):
        run(photo(self.admin, ADMIN, {**NIKITA, "first_name_he": "<b>x</b>"}))
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", last("reply", ADMIN)[2])

    # ---- questions and sharing

    def _save(self, ctx, uid, card, note=None):
        run(photo(ctx, uid, card))
        run(press(ctx, uid, "save_default"))
        if note:
            run(text(ctx, uid, note))
        else:
            run(press(ctx, uid, "note_skip"))

    def test_question_with_dates_and_terms_then_share(self):
        run(command(handlers.event_cmd, self.admin, ADMIN, "כנס", "גרמניה"))
        self._save(self.admin, ADMIN, ANNA, "דיברנו על הפצה")
        self._save(self.admin, ADMIN, NIKITA, "רוצה לבוא באביב")
        # An old card from another month must not show up for "this week".
        db._run("UPDATE cards SET scanned_at = '2026-01-05 10:00' WHERE first_name = 'Nikita'")
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

    def test_send_one_result(self):
        self._save(self.admin, ADMIN, NIKITA)
        card_id = db.all_cards(ADMIN)[0]["id"]
        log.clear()
        run(press(self.admin, ADMIN, f"send_{card_id}"))
        self.assertIn("ניקיטה", last("contact", ADMIN)[2])

    def test_users_see_only_their_own_cards(self):
        db.request_access(OTHER, "Other", None)
        db.decide_access(OTHER, True)
        self._save(self.admin, ADMIN, NIKITA)
        handlers.parse_question = lambda q, d=None: {"date_from": None, "date_to": None, "terms": ["Nikita"]}
        run(text(self.other, OTHER, "ניקיטה"))
        self.assertIn("לא מצאתי", last("edit", OTHER)[2])
        card_id = db.all_cards(ADMIN)[0]["id"]
        run(press(self.other, OTHER, f"send_{card_id}"))
        self.assertFalse(any(e[0] == "contact" and e[1] == OTHER for e in log), "cannot fetch someone else's card")

    def test_no_results(self):
        handlers.parse_question = lambda q, d=None: {"date_from": None, "date_to": None, "terms": ["nobody"]}
        run(text(self.admin, ADMIN, "מי זה"))
        self.assertIn("לא מצאתי", last("edit", ADMIN)[2])


class QuestionParser(unittest.TestCase):
    def _gemini(self, response_text=None, error=None):
        def gen(**_kw):
            if error:
                raise error
            return SimpleNamespace(text=response_text)
        extractor.gemini_client = SimpleNamespace(models=SimpleNamespace(generate_content=gen))

    def test_valid_output(self):
        self._gemini('{"date_from": "2026-10-04", "date_to": "2026-10-10", "terms": ["Germany", "גרמניה"]}')
        f = extractor.parse_question("את מי פגשנו שבוע שעבר בכנס הגרמני?", date(2026, 10, 14))
        self.assertEqual(f, {"date_from": "2026-10-04", "date_to": "2026-10-10", "terms": ["Germany", "גרמניה"]})

    def test_bad_dates_dropped(self):
        self._gemini('{"date_from": "last week", "date_to": null, "terms": ["Germany"]}')
        self.assertEqual(extractor.parse_question("x")["date_from"], None)

    def test_failure_falls_back_to_words(self):
        self._gemini(error=RuntimeError("down"))
        f = extractor.parse_question("הבחור הרומני")
        self.assertEqual(f, {"date_from": None, "date_to": None, "terms": ["הבחור", "הרומני"]})


if __name__ == "__main__":
    unittest.main()
