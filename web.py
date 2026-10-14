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


def make_app(bot) -> web.Application:
    app = web.Application()
    app[BOT_KEY] = bot
    app.router.add_get("/oauth/callback", oauth_callback)
    app.router.add_get("/health", health)
    return app


async def start_server(bot, port: int) -> web.AppRunner:
    runner = web.AppRunner(make_app(bot))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    return runner
