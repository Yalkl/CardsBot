import config
from database import init_db
import handlers
import web
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)


async def post_init(app: Application):
    # The Google sign-in callback runs in the same event loop as the bot.
    app.bot_data["web_runner"] = await web.start_server(app.bot, config.PORT)


async def post_shutdown(app: Application):
    runner = app.bot_data.get("web_runner")
    if runner:
        await runner.cleanup()


def main():
    init_db()
    app = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()

    app.add_handler(CommandHandler("start", handlers.start))
    app.add_handler(CommandHandler("ping", handlers.ping))
    app.add_handler(CommandHandler("myid", handlers.myid))
    app.add_handler(CommandHandler("connect", handlers.connect))
    app.add_handler(CommandHandler("disconnect", handlers.disconnect))
    app.add_handler(CommandHandler("sheet", handlers.sheet_cmd))
    app.add_handler(CommandHandler("event", handlers.event_cmd))
    app.add_handler(CommandHandler("ask", handlers.ask_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, handlers.handle_photo))
    app.add_handler(CallbackQueryHandler(handlers.handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_text_input))

    print("CardsBot is running")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
