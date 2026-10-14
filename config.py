import os
from dotenv import load_dotenv
from google import genai

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
# Small accounts database (who is approved, Google connection, active event). No contacts in it:
# contacts live in each user's own Google account (docs/SPEC.md). On Railway this file sits on a volume.
LOCAL_SQLITE_PATH = os.getenv("LOCAL_SQLITE_PATH", "cardsbot.db")

# The bot owner: always allowed, and approves everyone else.
_admin = os.getenv("ADMIN_TELEGRAM_ID", "").strip()
ADMIN_TELEGRAM_ID = int(_admin) if _admin.isdigit() else None

# An active event tags every scan until it is turned off or expires.
EVENT_HOURS = 12

# Google sign-in (docs/SPEC.md, "Google"). PUBLIC_URL is this service's public address, e.g. https://cardsbot.up.railway.app
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
# Fernet key that encrypts the stored Google refresh tokens.
TOKEN_ENC_KEY = os.getenv("TOKEN_ENC_KEY", "").strip()
PORT = int(os.getenv("PORT", "8080"))

GOOGLE_SCOPES = [
    "openid",
    "email",
    "https://www.googleapis.com/auth/contacts",
    "https://www.googleapis.com/auth/drive.file",
]

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN is missing in environment variables.")
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY is missing in environment variables.")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)


def google_configured() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and PUBLIC_URL and TOKEN_ENC_KEY)
