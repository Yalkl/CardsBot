from config import TELEGRAM_BOT_TOKEN
from database import init_db
import handlers
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)


def main():
    # Initialize Database table
    init_db()

    # Build Telegram Bot Application
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Register Handlers
    app.add_handler(CommandHandler("start", handlers.start))
    app.add_handler(CommandHandler("ping", handlers.ping))
    app.add_handler(MessageHandler(filters.PHOTO, handlers.handle_photo))
    app.add_handler(CallbackQueryHandler(handlers.handle_callback))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND, handlers.handle_text_input
        )
    )

    print(
        "🤖 Telegram Business Card Bot (Modular Structure) is running..."
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()