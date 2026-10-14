import os
from dotenv import load_dotenv
from google import genai

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
LOCAL_SQLITE_PATH = os.getenv("LOCAL_SQLITE_PATH", "contacts_history.db")

# The bot owner: always allowed, and approves everyone else (docs/SPEC.md).
_admin = os.getenv("ADMIN_TELEGRAM_ID", "").strip()
ADMIN_TELEGRAM_ID = int(_admin) if _admin.isdigit() else None

# An active event tags every scan until it is turned off or expires.
EVENT_HOURS = 12

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN is missing in environment variables.")
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY is missing in environment variables.")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
