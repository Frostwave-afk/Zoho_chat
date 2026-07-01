import time
import logging
import asyncio
from typing import Optional

from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.db.models import OAuthToken
from backend.utils import encrypt_token, decrypt_token

logger = logging.getLogger(__name__)
settings = get_settings()

# Store the Flow object between /start and /callback so PKCE code_verifier is preserved
_pending_flows: dict[str, Flow] = {}

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

_CLIENT_CONFIG = {
    "web": {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [settings.GOOGLE_REDIRECT_URI],
    }
}


def get_gmail_auth_url() -> tuple[str, str]:
    """Generate the Google OAuth consent URL. Returns (url, state)."""
    flow = Flow.from_client_config(_CLIENT_CONFIG, scopes=SCOPES)
    flow.redirect_uri = settings.GOOGLE_REDIRECT_URI
    url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="select_account consent",  # always show account picker + consent
    )
    # Preserve the Flow so the PKCE code_verifier survives the redirect round-trip
    _pending_flows[state] = flow
    return url, state


async def exchange_gmail_code(code: str, state: str, db: AsyncSession) -> None:
    """Exchange Google auth code for tokens and persist (encrypted) to DB."""
    # Retrieve the stored Flow (contains PKCE code_verifier)
    flow = _pending_flows.pop(state, None)
    if flow is None:
        # Fallback for edge cases (e.g. server restarted mid-flow)
        flow = Flow.from_client_config(_CLIENT_CONFIG, scopes=SCOPES)
        flow.redirect_uri = settings.GOOGLE_REDIRECT_URI
    await asyncio.to_thread(flow.fetch_token, code=code)
    creds = flow.credentials

    expires_at = int(creds.expiry.timestamp()) if creds.expiry else int(time.time()) + 3600

    existing = await db.get(OAuthToken, "gmail")
    enc_access = encrypt_token(creds.token, settings.SECRET_KEY)
    enc_refresh = encrypt_token(creds.refresh_token, settings.SECRET_KEY) if creds.refresh_token else None

    if existing:
        existing.access_token = enc_access
        existing.refresh_token = enc_refresh
        existing.expires_at = expires_at
    else:
        db.add(OAuthToken(
            service="gmail",
            access_token=enc_access,
            refresh_token=enc_refresh,
            expires_at=expires_at,
        ))
    await db.commit()
    logger.info("Gmail tokens stored.")


async def get_gmail_credentials(db: AsyncSession) -> Optional[Credentials]:
    """Return valid Credentials, auto-refreshing if close to expiry. None if not connected."""
    row = await db.get(OAuthToken, "gmail")
    if not row:
        return None

    access_token = decrypt_token(row.access_token, settings.SECRET_KEY)
    refresh_token = decrypt_token(row.refresh_token, settings.SECRET_KEY) if row.refresh_token else None

    creds = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )

    if row.expires_at - int(time.time()) < 60:
        logger.info("Gmail token expiring soon — refreshing.")
        await asyncio.to_thread(creds.refresh, Request())
        row.access_token = encrypt_token(creds.token, settings.SECRET_KEY)
        row.expires_at = int(creds.expiry.timestamp()) if creds.expiry else int(time.time()) + 3600
        await db.commit()

    return creds


async def is_gmail_connected(db: AsyncSession) -> bool:
    return await db.get(OAuthToken, "gmail") is not None
