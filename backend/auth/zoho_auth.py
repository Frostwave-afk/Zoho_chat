import time
import logging
from typing import Optional
from urllib.parse import urlencode

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.db.models import OAuthToken
from backend.utils import encrypt_token, decrypt_token

logger = logging.getLogger(__name__)
settings = get_settings()

ZOHO_SCOPES = ",".join([
    "ZohoInvoice.invoices.CREATE",
    "ZohoInvoice.invoices.READ",
    "ZohoInvoice.contacts.READ",
    "ZohoInvoice.contacts.CREATE",
    "ZohoInvoice.settings.READ",
])


def get_zoho_auth_url() -> str:
    """Build the Zoho OAuth consent URL."""
    params = urlencode({
        "scope": ZOHO_SCOPES,
        "client_id": settings.ZOHO_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": settings.ZOHO_REDIRECT_URI,
        "access_type": "offline",
        "prompt": "consent",          # always show consent → Zoho returns refresh_token
    })
    return f"{settings.zoho_auth_base}/oauth/v2/auth?{params}"


async def exchange_zoho_code(code: str, db: AsyncSession) -> None:
    """Exchange Zoho auth code for tokens and persist (encrypted) to DB."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{settings.zoho_auth_base}/oauth/v2/token",
            params={
                "code": code,
                "client_id": settings.ZOHO_CLIENT_ID,
                "client_secret": settings.ZOHO_CLIENT_SECRET,
                "redirect_uri": settings.ZOHO_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    access_token = data["access_token"]
    refresh_token = data.get("refresh_token")
    expires_in = int(data.get("expires_in", 3600))

    existing = await db.get(OAuthToken, "zoho")
    enc_access = encrypt_token(access_token, settings.SECRET_KEY)
    enc_refresh = encrypt_token(refresh_token, settings.SECRET_KEY) if refresh_token else None

    if existing:
        existing.access_token = enc_access
        # Only overwrite refresh_token when Zoho actually returned one;
        # re-auth without prompt=consent may omit it.
        if enc_refresh:
            existing.refresh_token = enc_refresh
        existing.expires_at = int(time.time()) + expires_in
    else:
        db.add(OAuthToken(
            service="zoho",
            access_token=enc_access,
            refresh_token=enc_refresh,
            expires_at=int(time.time()) + expires_in,
        ))
    await db.commit()
    logger.info("Zoho tokens stored.")


async def get_zoho_access_token(db: AsyncSession) -> Optional[str]:
    """Return a valid Zoho access token, auto-refreshing if close to expiry."""
    row = await db.get(OAuthToken, "zoho")
    if not row:
        return None

    if row.expires_at - int(time.time()) < 60:
        logger.info("Zoho token expiring soon — refreshing.")
        if not row.refresh_token:
            logger.warning("No Zoho refresh token available — re-auth required.")
            return None

        refresh_token = decrypt_token(row.refresh_token, settings.SECRET_KEY)
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{settings.zoho_auth_base}/oauth/v2/token",
                params={
                    "refresh_token": refresh_token,
                    "client_id": settings.ZOHO_CLIENT_ID,
                    "client_secret": settings.ZOHO_CLIENT_SECRET,
                    "grant_type": "refresh_token",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        row.access_token = encrypt_token(data["access_token"], settings.SECRET_KEY)
        row.expires_at = int(time.time()) + int(data.get("expires_in", 3600))
        await db.commit()
        return data["access_token"]

    return decrypt_token(row.access_token, settings.SECRET_KEY)


async def is_zoho_connected(db: AsyncSession) -> bool:
    return await db.get(OAuthToken, "zoho") is not None
