import os
from dotenv import load_dotenv
from google import genai

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
LOCAL_SQLITE_PATH = "contacts_history.db"

# Telegram user IDs (comma-separated) that share one contact list: they get the note step,
# the saved-to-phone tracking, and /find, /unsaved, /export. Everyone else keeps the basic flow.
TEAM_USER_IDS = {
    int(x) for x in os.getenv("TEAM_USER_IDS", "").replace(" ", "").split(",") if x.isdigit()
}
TEAM_WORKSPACE = "team"

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN is missing in environment variables.")
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY is missing in environment variables.")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)