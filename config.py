import os
from dotenv import load_dotenv
from google import genai

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
LOCAL_SQLITE_PATH = "contacts_history.db"

# Optional: append saved contacts to a shared Google Sheet (see docs/SHEETS_SETUP.md).
SHEETS_WEBHOOK_URL = os.getenv("SHEETS_WEBHOOK_URL", "").strip()
SHEETS_WEBHOOK_SECRET = os.getenv("SHEETS_WEBHOOK_SECRET", "").strip()
# Telegram user IDs (comma-separated) that get the note step and the sheet export.
SHEETS_USER_IDS = {
    int(x) for x in os.getenv("SHEETS_USER_IDS", "").replace(" ", "").split(",") if x.isdigit()
}

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN is missing in environment variables.")
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY is missing in environment variables.")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)