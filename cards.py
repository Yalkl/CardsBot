"""Where cards live (docs/SPEC.md): in the user's own Google account.

- SheetStore: the list of record, a Google Sheet the bot creates in the user's Drive ("drive.file": the bot can
  open only files it created). The bot answers questions from it, so edits made in the sheet count.
- ContactsClient: Google Contacts, so every card reaches the phone by itself.

Google's client library is synchronous; handlers call these through asyncio.to_thread.
Tests replace `open_user` with in-memory fakes.
"""

import re
import uuid

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import database as db
import google_auth
from formatters import build_display_name, contact_notes, english_name

CONTACT_FIELDS = (
    "first_name", "last_name", "company", "job_title", "country",
    "phone", "phone_office", "email", "website", "telegram", "address",
    "first_name_he", "last_name_he", "job_title_he", "company_he",
)

TAB = "אנשי קשר"
SHEET_TITLE = "CardsBot – אנשי קשר"
GROUP_NAME = "כרטיסי ביקור"

# Sheet columns, in order. Rows are read by header text, so moving columns in the sheet is fine.
COLUMNS = [
    ("display_name", "שם בטלפון"),
    ("scanned_at", "נסרק"),
    ("event", "אירוע"),
    ("note", "הערה"),
    ("first_name_he", "שם פרטי"),
    ("last_name_he", "שם משפחה"),
    ("job_title_he", "תפקיד"),
    ("company_he", "חברה (עברית)"),
    ("first_name", "First name"),
    ("last_name", "Last name"),
    ("job_title", "Title"),
    ("company", "Company"),
    ("country", "מדינה"),
    ("phone", "נייד"),
    ("phone_office", "טלפון משרד"),
    ("email", "מייל"),
    ("website", "אתר"),
    ("telegram", "טלגרם"),
    ("address", "כתובת"),
    ("google_contact", "מזהה בגוגל"),
    ("id", "מזהה"),
]
LABEL_TO_KEY = {label: key for key, label in COLUMNS}
LABEL_TO_KEY["טלפון"] = "phone"  # the mobile column's header before office phones were added (14.10.2026)


def _col_letter(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


class SheetStore:
    def __init__(self, creds, telegram_id: int):
        self.telegram_id = telegram_id
        self.svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self.sheet_id = (db.get_user(telegram_id) or {}).get("sheet_id")

    def _create(self) -> str:
        body = {
            "properties": {"title": SHEET_TITLE},
            "sheets": [{"properties": {"title": TAB, "rightToLeft": True, "gridProperties": {"frozenRowCount": 1}}}],
        }
        sheet = self.svc.spreadsheets().create(body=body, fields="spreadsheetId").execute()
        sid = sheet["spreadsheetId"]
        self.svc.spreadsheets().values().update(
            spreadsheetId=sid, range=f"'{TAB}'!A1", valueInputOption="RAW", body={"values": [[label for _, label in COLUMNS]]}
        ).execute()
        db.set_google_ids(self.telegram_id, sheet_id=sid)
        self.sheet_id = sid
        return sid

    def ensure(self) -> str:
        """The sheet id; creates the sheet the first time (or again, if the user deleted it)."""
        if self.sheet_id:
            try:
                self.svc.spreadsheets().get(spreadsheetId=self.sheet_id, fields="spreadsheetId").execute()
                return self.sheet_id
            except HttpError as e:
                if e.resp.status not in (403, 404):
                    raise
        return self._create()

    def url(self) -> str:
        return f"https://docs.google.com/spreadsheets/d/{self.ensure()}"

    def _rows(self) -> tuple[list[str], list[list[str]]]:
        sid = self.ensure()
        values = self.svc.spreadsheets().values().get(spreadsheetId=sid, range=f"'{TAB}'").execute().get("values", [])
        if not values:
            return [], []
        return values[0], values[1:]

    def all(self) -> list[dict]:
        """Every card, newest first. Rows without an id (typed by hand) get one the next time they are saved."""
        header, rows = self._rows()
        keys = [LABEL_TO_KEY.get(h.strip(), "") for h in header]
        out = []
        for i, row in enumerate(rows, start=2):
            card = {k: (row[j] if j < len(row) else "") for j, k in enumerate(keys) if k}
            if not any(card.get(f) for f in ("first_name", "first_name_he", "company", "phone", "phone_office", "email")):
                continue
            card["_row"] = i
            card["id"] = card.get("id") or f"r{i}"  # a row typed by hand: addressed by its row until saved again
            out.append(card)
        out.sort(key=lambda c: c.get("scanned_at") or "", reverse=True)
        return out

    def get(self, card_id: str) -> dict | None:
        return next((c for c in self.all() if c.get("id") == card_id), None)

    def _row_values(self, card: dict, header: list[str]) -> list[str]:
        card = {**card, "display_name": build_display_name(card)}
        keys = [LABEL_TO_KEY.get(h.strip(), "") for h in header] or [k for k, _ in COLUMNS]
        return [str(card.get(k) or "") if k else "" for k in keys]

    def _header(self) -> list[str]:
        """The header row; columns added in a newer version are appended at the end, so old sheets keep working."""
        header, _ = self._rows()
        known = {LABEL_TO_KEY.get(h.strip()) for h in header}
        missing = [label for key, label in COLUMNS if key not in known]
        if header and missing:
            header = header + missing
            self.svc.spreadsheets().values().update(
                spreadsheetId=self.sheet_id, range=f"'{TAB}'!A1", valueInputOption="RAW", body={"values": [header]}
            ).execute()
        return header

    def insert(self, card: dict) -> str:
        card = {**card, "id": card.get("id") or uuid.uuid4().hex[:10]}
        header = self._header()
        self.svc.spreadsheets().values().append(
            spreadsheetId=self.sheet_id, range=f"'{TAB}'!A1", valueInputOption="RAW",
            insertDataOption="INSERT_ROWS", body={"values": [self._row_values(card, header)]},
        ).execute()
        return card["id"]

    def update(self, card_id: str, card: dict) -> bool:
        header = self._header()
        current = self.get(card_id)
        if not current:
            return False
        row = current["_row"]
        values = self._row_values({**card, "id": card_id}, header)
        self.svc.spreadsheets().values().update(
            spreadsheetId=self.sheet_id, range=f"'{TAB}'!A{row}:{_col_letter(len(values))}{row}",
            valueInputOption="RAW", body={"values": [values]},
        ).execute()
        return True


class ContactsClient:
    def __init__(self, creds, telegram_id: int):
        self.telegram_id = telegram_id
        self.svc = build("people", "v1", credentials=creds, cache_discovery=False)
        self.group = (db.get_user(telegram_id) or {}).get("contact_group")

    def _ensure_group(self) -> str:
        if self.group:
            return self.group
        groups = self.svc.contactGroups().list(pageSize=200).execute().get("contactGroups", [])
        found = next((g["resourceName"] for g in groups if g.get("name") == GROUP_NAME), None)
        if not found:
            found = self.svc.contactGroups().create(body={"contactGroup": {"name": GROUP_NAME}}).execute()["resourceName"]
        db.set_google_ids(self.telegram_id, contact_group=found)
        self.group = found
        return found

    @staticmethod
    def _person(card: dict) -> dict:
        phone = db.clean_phone_number(card.get("phone", ""))
        office = db.clean_phone_number(card.get("phone_office", ""))
        website = (card.get("website") or "").strip()
        if website and not website.startswith("http"):
            website = f"https://{website}"
        addr = ", ".join(x for x in ((card.get("address") or "").strip(), (card.get("country") or "").strip()) if x)
        person = {
            "names": [{"givenName": build_display_name(card)}],
            "biographies": [{"value": contact_notes(card), "contentType": "TEXT_PLAIN"}],
        }
        if english_name(card):
            person["nicknames"] = [{"value": english_name(card)}]
        if card.get("company") or card.get("job_title"):
            person["organizations"] = [{"name": card.get("company") or "", "title": card.get("job_title") or ""}]
        numbers = [{"value": phone, "type": "mobile"}] if phone else []
        if office and office != phone:
            numbers.append({"value": office, "type": "work"})
        if numbers:
            person["phoneNumbers"] = numbers
        if card.get("email"):
            person["emailAddresses"] = [{"value": card["email"], "type": "work"}]
        if website:
            person["urls"] = [{"value": website, "type": "work"}]
        if addr:
            person["addresses"] = [{"formattedValue": addr, "type": "work"}]
        return person

    def create(self, card: dict) -> str:
        person = self._person(card)
        person["memberships"] = [{"contactGroupMembership": {"contactGroupResourceName": self._ensure_group()}}]
        return self.svc.people().createContact(body=person).execute()["resourceName"]

    def update(self, resource: str, card: dict) -> str:
        """Updates the contact; if it was deleted from Google meanwhile, creates it again."""
        try:
            etag = self.svc.people().get(resourceName=resource, personFields="names").execute()["etag"]
        except HttpError as e:
            if e.resp.status == 404:
                return self.create(card)
            raise
        body = {**self._person(card), "etag": etag}
        self.svc.people().updateContact(
            resourceName=resource,
            updatePersonFields="names,nicknames,organizations,phoneNumbers,emailAddresses,urls,addresses,biographies",
            body=body,
        ).execute()
        return resource


def open_user(telegram_id: int) -> tuple[SheetStore, ContactsClient]:
    """Raises google_auth.GoogleNotConnected if the user has no Google connection."""
    creds = google_auth.credentials_for(telegram_id)
    return SheetStore(creds, telegram_id), ContactsClient(creds, telegram_id)


# ---------------------------------------------------------------- matching and search (pure)

def find_duplicate(cards: list[dict], data: dict) -> dict | None:
    """Same person already saved? By mobile or office number (last 8 digits), then email, then full English name."""
    def tails(c: dict) -> set[str]:
        out = set()
        for f in ("phone", "phone_office"):
            d = re.sub(r"\D", "", c.get(f) or "")
            if len(d) >= 8:
                out.add(d[-8:])
        return out

    mine = tails(data)
    email = (data.get("email") or "").strip().lower()
    full = english_name(data).lower()
    for card in cards:
        match = None
        if mine & tails(card):
            match = "טלפון"
        elif email and (card.get("email") or "").strip().lower() == email:
            match = "מייל"
        elif full and len(full) > 3 and full == english_name(card).lower():
            match = "שם"
        if match:
            return {"id": card.get("id"), "row": card, "match_type": match}
    return None


SEARCH_FIELDS = CONTACT_FIELDS + ("event", "note", "display_name")


def _hits(card: dict, terms: list[str]) -> int:
    haystack = " | ".join(str(card.get(f) or "") for f in SEARCH_FIELDS).lower()
    digits = re.sub(r"\D", "", f"{card.get('phone') or ''} {card.get('phone_office') or ''}")
    n = 0
    for t in terms:
        t = t.strip().lower()
        if len(t) < 2:
            continue
        t_digits = re.sub(r"\D", "", t)
        if t in haystack or (len(t_digits) >= 4 and t_digits in digits):
            n += 1
    return n


def search(cards: list[dict], terms: list[str], date_from: str | None = None, date_to: str | None = None,
           limit: int | None = 10, groups: list[list[str]] | None = None) -> list[dict]:
    """Cards in the date range (inclusive), best match first; no terms and no groups = everyone in the range.

    groups: each inner list is one requirement with its synonyms; a card must match every group (AND of ORs).
    terms (when no groups): a card must match at least one term.
    """
    groups = [g for g in (groups or []) if g] or ([terms] if terms else [])
    scored = []
    for card in cards:
        day = (card.get("scanned_at") or "")[:10]
        if date_from and day < date_from:
            continue
        if date_to and day > date_to:
            continue
        hits = [_hits(card, g) for g in groups]
        if all(hits):
            scored.append((sum(hits), card))
    scored.sort(key=lambda s: s[0], reverse=True)  # stable: newest first within a score
    out = [c for _, c in scored]
    return out[:limit] if limit else out


def search_relaxed(cards: list[dict], groups: list[list[str]], date_from: str | None = None, date_to: str | None = None) -> list[dict]:
    """The closest cards when nothing matches every group: ranked by how many groups they match."""
    scored = []
    for card in cards:
        day = (card.get("scanned_at") or "")[:10]
        if (date_from and day < date_from) or (date_to and day > date_to):
            continue
        matched = sum(1 for g in groups if _hits(card, g))
        if matched:
            scored.append((matched, _hits(card, [t for g in groups for t in g]), card))
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    return [c for *_, c in scored]
