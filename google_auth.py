"""Google sign-in (docs/SPEC.md): one-time links, code exchange, encrypted refresh tokens.

Only the refresh token is kept, encrypted with TOKEN_ENC_KEY (Fernet). Without the key a copy of the
database gives no access to anyone's Google account.
"""

import base64
import json
import secrets
from urllib.parse import urlencode

import aiohttp
from cryptography.fernet import Fernet
from google.oauth2.credentials import Credentials

import config
import database as db

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"


class GoogleNotConnected(Exception):
    """The user has no (working) Google connection; they need /connect."""


def redirect_uri() -> str:
    return f"{config.PUBLIC_URL}/oauth/callback"


def _fernet() -> Fernet:
    return Fernet(config.TOKEN_ENC_KEY.encode())


def encrypt(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def decrypt(token_enc: str) -> str:
    return _fernet().decrypt(token_enc.encode()).decode()


def new_login_url(telegram_id: int) -> str:
    """A sign-in link valid for 10 minutes and one use, tied to this Telegram user."""
    state = secrets.token_urlsafe(32)
    db.create_oauth_state(state, telegram_id)
    params = {
        "client_id": config.GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join(config.GOOGLE_SCOPES),
        "access_type": "offline",
        "prompt": "consent",  # always returns a refresh token
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def _email_from_id_token(id_token: str) -> str:
    """The id_token comes straight from Google's token endpoint over TLS, so its payload is read as is."""
    payload = id_token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload)).get("email", "")


async def exchange_code(code: str) -> tuple[str, str, list[str]]:
    """Returns (email, refresh_token, granted scopes)."""
    data = {
        "code": code,
        "client_id": config.GOOGLE_CLIENT_ID,
        "client_secret": config.GOOGLE_CLIENT_SECRET,
        "redirect_uri": redirect_uri(),
        "grant_type": "authorization_code",
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(TOKEN_URL, data=data, timeout=aiohttp.ClientTimeout(total=15)) as r:
            body = await r.json()
            if r.status != 200 or "refresh_token" not in body:
                raise RuntimeError(f"token exchange failed: {r.status} {body.get('error', '')}")
    return _email_from_id_token(body.get("id_token", "")), body["refresh_token"], body.get("scope", "").split()


def credentials_for(telegram_id: int) -> Credentials:
    u = db.get_user(telegram_id)
    if not u or not u.get("refresh_token_enc"):
        raise GoogleNotConnected()
    return Credentials(
        token=None,
        refresh_token=decrypt(u["refresh_token_enc"]),
        token_uri=TOKEN_URL,
        client_id=config.GOOGLE_CLIENT_ID,
        client_secret=config.GOOGLE_CLIENT_SECRET,
        scopes=config.GOOGLE_SCOPES,
    )


async def revoke(telegram_id: int):
    """Revokes the token at Google (best effort) and forgets it."""
    u = db.get_user(telegram_id)
    if u and u.get("refresh_token_enc"):
        try:
            async with aiohttp.ClientSession() as s:
                await s.post(REVOKE_URL, data={"token": decrypt(u["refresh_token_enc"])}, timeout=aiohttp.ClientTimeout(total=10))
        except Exception:
            pass
    db.clear_google(telegram_id)
