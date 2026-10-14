"""The bot's small web server (docs/SPEC.md): Google sends the user back here after sign-in.

Runs inside the bot's own event loop (started from bot.py's post_init), on Railway's PORT.
"""

import asyncio
from html import escape

from aiohttp import web

import cards
import database as db
import google_auth

BOT_KEY = web.AppKey("bot", object)
NEEDED = {"https://www.googleapis.com/auth/contacts", "https://www.googleapis.com/auth/drive.file"}


def _page(title: str, body: str, status: int = 200) -> web.Response:
    html = f"""<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>CardsBot</title>
<style>body{{font-family:system-ui,sans-serif;max-width:28rem;margin:15vh auto;padding:0 1rem;text-align:center;line-height:1.6}}</style>
</head><body><h1>{escape(title)}</h1><p>{escape(body)}</p></body></html>"""
    return web.Response(text=html, content_type="text/html", status=status)


async def oauth_callback(request: web.Request) -> web.Response:
    bot = request.app[BOT_KEY]
    state = request.query.get("state", "")
    telegram_id = db.consume_oauth_state(state) if state else None
    if telegram_id is None:
        return _page("הקישור לא תקף", "הקישור פג או כבר נוצל. שלחו /connect בבוט לקישור חדש.", 400)
    if request.query.get("error") or not request.query.get("code"):
        await bot.send_message(chat_id=telegram_id, text="🔌 ההתחברות לגוגל בוטלה. אפשר לנסות שוב: /connect")
        return _page("ההתחברות בוטלה", "אפשר לחזור לטלגרם ולנסות שוב.")
    try:
        email, refresh_token, scopes = await google_auth.exchange_code(request.query["code"])
    except Exception:
        await bot.send_message(chat_id=telegram_id, text="⚠️ ההתחברות לגוגל נכשלה. נסו שוב: /connect")
        return _page("משהו השתבש", "חזרו לטלגרם ונסו שוב.", 502)
    if not NEEDED.issubset(set(scopes)):
        await bot.send_message(
            chat_id=telegram_id,
            text="⚠️ צריך לאשר את שתי ההרשאות (אנשי קשר וקבצים בדרייב), אחרת אין איפה לשמור. נסו שוב: /connect",
        )
        return _page("חסרות הרשאות", "צריך לסמן את שתי ההרשאות. חזרו לטלגרם ונסו שוב.", 400)

    db.save_google(telegram_id, email, google_auth.encrypt(refresh_token))
    try:
        store, _ = cards.open_user(telegram_id)
        url = await asyncio.to_thread(store.url)  # creates the sheet the first time
        sheet_line = f"\n📊 הטבלה שלכם: {url}"
    except Exception:
        sheet_line = ""
    await bot.send_message(chat_id=telegram_id, text=f"✅ מחובר לגוגל כ-{email}. אפשר לשלוח כרטיס.{sheet_line}")
    return _page("מחובר ✓", f"מחובר כ-{email}. אפשר לחזור לטלגרם.")


async def health(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


# Google requires a home page and a privacy policy for an app in production (Google Auth Platform → Branding).
HOME_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>CardsBot</title>
<style>body{font-family:system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem;line-height:1.6}</style></head><body>
<h1>CardsBot</h1>
<p>A private Telegram bot that reads a business card photo and saves the person to the user's own Google Contacts,
with a row in a Google Sheet in the user's own Drive. Access is by invitation only.</p>
<p><a href="/privacy">Privacy policy</a></p>
</body></html>"""

PRIVACY_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>CardsBot – Privacy policy</title>
<style>body{font-family:system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem;line-height:1.6}</style></head><body>
<h1>CardsBot – Privacy policy</h1>
<p>Last updated: 14 October 2026</p>
<h2>What the bot does</h2>
<p>CardsBot is a private Telegram bot, used by invitation only. When a user sends a photo of a business card, the bot reads the
text and, with the user's permission, saves the contact in the user's own Google account.</p>
<h2>Google data the bot uses</h2>
<ul>
<li><b>Google Contacts</b> (contacts scope): to create and update the contacts the user scans. The bot does not read or change other contacts.</li>
<li><b>Google Drive</b> (drive.file scope): to create one Google Sheet that lists the scanned cards, and to read and update that sheet only.
The bot cannot see any other file in the user's Drive.</li>
<li><b>Email address</b>: to show the user which Google account is connected.</li>
</ul>
<h2>What is stored, and where</h2>
<ul>
<li>The contacts and the sheet stay in the user's own Google account.</li>
<li>The bot's server stores only: the user's Telegram id, approval status, connected email, the id of the sheet it created,
and the Google refresh token, encrypted.</li>
<li>Card photos are not stored. They are sent to Google Gemini to read the text, and then discarded.</li>
<li>Search questions are sent to Google Gemini only to turn them into a search filter. The contact list is never sent.</li>
</ul>
<h2>Sharing</h2>
<p>Data is not sold or shared with anyone. It is used only to provide the features above, to the user who scanned the card.
The use of information received from Google APIs adheres to the Google API Services User Data Policy, including the Limited Use requirements.</p>
<h2>Removing access and data</h2>
<p>Send <code>/disconnect</code> to the bot to revoke its access and delete the stored token, or remove CardsBot at
<a href="https://myaccount.google.com/permissions">myaccount.google.com/permissions</a>. Contacts and the sheet remain in your Google account
and can be deleted there.</p>
<h2>Contact</h2>
<p>yudalkl@gmail.com</p>
</body></html>"""


async def home(_request: web.Request) -> web.Response:
    return web.Response(text=HOME_HTML, content_type="text/html")


async def privacy(_request: web.Request) -> web.Response:
    return web.Response(text=PRIVACY_HTML, content_type="text/html")


def make_app(bot) -> web.Application:
    app = web.Application()
    app[BOT_KEY] = bot
    app.router.add_get("/oauth/callback", oauth_callback)
    app.router.add_get("/health", health)
    app.router.add_get("/", home)
    app.router.add_get("/privacy", privacy)
    return app


async def start_server(bot, port: int) -> web.AppRunner:
    runner = web.AppRunner(make_app(bot))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    return runner
