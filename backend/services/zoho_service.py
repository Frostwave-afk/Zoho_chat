import time
import logging
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.db.models import ContactCache, ProcessedEmail
from backend.auth.zoho_auth import get_zoho_access_token

logger = logging.getLogger(__name__)
settings = get_settings()

_CACHE_TTL = 86400  # 24 hours

# Org-ID cache — reset whenever the user reconnects Zoho
_zoho_org_id: Optional[str] = None

_ZOHO_APP_BASE = {
    "com": "https://invoice.zoho.com",
    "in":  "https://invoice.zoho.in",
    "eu":  "https://invoice.zoho.eu",
    "au":  "https://invoice.zoho.com.au",
    "jp":  "https://invoice.zoho.jp",
}


def clear_org_id_cache() -> None:
    """Call on logout / reconnect so the next request fetches a fresh org ID."""
    global _zoho_org_id
    _zoho_org_id = None


def _invoice_url(invoice_id: str) -> str:
    base = _ZOHO_APP_BASE.get(settings.ZOHO_REGION, "https://invoice.zoho.com")
    return f"{base}/app#/invoices/{invoice_id}"


async def _headers(db: AsyncSession) -> dict:
    token = await get_zoho_access_token(db)
    if not token:
        raise RuntimeError("Zoho not connected — please connect your Zoho account first.")
    return {"Authorization": f"Zoho-oauthtoken {token}"}


async def get_org_id(db: AsyncSession) -> str:
    """Return the Zoho organization_id for the connected account.
    Fetched once from /organizations and cached in memory for the session.
    """
    global _zoho_org_id
    if _zoho_org_id:
        return _zoho_org_id

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{settings.zoho_api_base}/organizations",
            headers=await _headers(db),
        )
        resp.raise_for_status()
        orgs = resp.json().get("organizations", [])

    if not orgs:
        raise RuntimeError("No Zoho organization found for this account.")

    _zoho_org_id = str(orgs[0]["organization_id"])
    logger.info(f"Zoho org ID resolved: {_zoho_org_id}")
    return _zoho_org_id


async def search_contact_by_name(name: str, db: AsyncSession) -> list[dict]:
    """Look up Zoho contacts by name. Checks contact_cache first (TTL 24h)."""
    name_lower = name.lower().strip()

    # Cache hit
    cached = await db.get(ContactCache, name_lower)
    if cached and (time.time() - cached.cached_at) < _CACHE_TTL:
        return [{
            "contact_id": cached.zoho_contact_id,
            "contact_name": name,
            "email": cached.zoho_email,
        }]

    # Live Zoho lookup
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{settings.zoho_api_base}/contacts",
            headers=await _headers(db),
            params={
                "organization_id": await get_org_id(db),
                "search_text": name,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    contacts = data.get("contacts", [])

    # Update cache for all returned contacts
    for c in contacts:
        c_name_lower = c.get("contact_name", "").lower().strip()
        email = (
            c.get("email")
            or next((p.get("email") for p in c.get("contact_persons", [])), None)
        )
        existing = await db.get(ContactCache, c_name_lower)
        if existing:
            existing.zoho_contact_id = c["contact_id"]
            existing.zoho_email = email
            existing.cached_at = int(time.time())
        else:
            db.add(ContactCache(
                name_lower=c_name_lower,
                zoho_contact_id=c["contact_id"],
                zoho_email=email,
                cached_at=int(time.time()),
            ))
    await db.commit()

    return [
        {
            "contact_id": c["contact_id"],
            "contact_name": c.get("contact_name", name),
            "email": (
                c.get("email")
                or next((p.get("email") for p in c.get("contact_persons", [])), None)
            ),
        }
        for c in contacts
    ]


async def search_contact_by_email(email: str, db: AsyncSession) -> list[dict]:
    """Look up Zoho contacts by email address."""
    email = email.strip().lower()
    if not email:
        return []

    cached = await db.execute(
        select(ContactCache).where(ContactCache.zoho_email == email)
    )
    cached_rows = cached.scalars().all()
    if cached_rows:
        return [
            {
                "contact_id": row.zoho_contact_id,
                "contact_name": " ".join(part.capitalize() for part in row.name_lower.split()),
                "email": row.zoho_email,
            }
            for row in cached_rows
        ]

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{settings.zoho_api_base}/contacts",
            headers=await _headers(db),
            params={
                "organization_id": await get_org_id(db),
                "search_text": email,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    contacts = data.get("contacts", [])
    matches: list[dict] = []

    for c in contacts:
        candidate_email = (
            c.get("email")
            or next((p.get("email") for p in c.get("contact_persons", [])), None)
        )
        if (candidate_email or "").strip().lower() != email:
            continue

        contact_name = c.get("contact_name", "")
        if contact_name:
            name_lower = contact_name.lower().strip()
            existing = await db.get(ContactCache, name_lower)
            if existing:
                existing.zoho_contact_id = c["contact_id"]
                existing.zoho_email = candidate_email
                existing.cached_at = int(time.time())
            else:
                db.add(ContactCache(
                    name_lower=name_lower,
                    zoho_contact_id=c["contact_id"],
                    zoho_email=candidate_email,
                    cached_at=int(time.time()),
                ))

        matches.append({
            "contact_id": c["contact_id"],
            "contact_name": contact_name or email,
            "email": candidate_email,
        })

    await db.commit()
    return matches


async def create_contact(name: str, email: Optional[str], db: AsyncSession) -> str:
    """Create a new Zoho contact and return the new contact_id."""
    payload: dict = {"contact_name": name}
    if email:
        payload["contact_persons"] = [{"email": email, "is_primary_contact": True}]

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{settings.zoho_api_base}/contacts",
            headers=await _headers(db),
            params={"organization_id": await get_org_id(db)},
            json=payload,
        )
        resp.raise_for_status()
        contact = resp.json().get("contact", {})

    contact_id = contact["contact_id"]
    # Cache immediately so future lookups don't hit the API
    name_lower = name.lower().strip()
    existing = await db.get(ContactCache, name_lower)
    if existing:
        existing.zoho_contact_id = contact_id
        existing.zoho_email = email
        existing.cached_at = int(time.time())
    else:
        db.add(ContactCache(
            name_lower=name_lower,
            zoho_contact_id=contact_id,
            zoho_email=email,
            cached_at=int(time.time()),
        ))
    await db.commit()
    logger.info(f"Created new Zoho contact: {name} ({contact_id})")
    return contact_id



async def create_invoice(
    contact_id: str,
    task_description: str,
    amount: float,
    currency: str,
    db: AsyncSession,
    item_name: Optional[str] = None,
    line_items: Optional[list] = None,  # pass for combined multi-item invoices
) -> dict:
    """POST a new invoice to Zoho Invoice and return the created invoice object.
    If line_items is provided directly, it is used as-is (combined invoice path).
    Otherwise a single line item is built from task_description/amount/item_name.
    """
    if line_items is None:
        # Single-item path (existing behaviour)
        if not item_name:
            first_sentence = task_description.split(".")[0].split("!")[0].split("?")[0].strip()
            first_words    = " ".join(task_description.split()[:6])
            raw_title      = first_sentence if len(first_sentence) <= len(first_words) else first_words
            item_name      = raw_title[:60].rstrip() + ("\u2026" if len(raw_title) > 60 else "")

        line_items = [{
            "name":        item_name,
            "description": task_description,
            "rate":        amount,
            "quantity":    1,
        }]

    payload = {
        "customer_id": contact_id,
        "line_items":  line_items,
    }
    logger.info(f"Sending payload to Zoho: {payload}")
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{settings.zoho_api_base}/invoices",
            headers=await _headers(db),
            params={"organization_id": await get_org_id(db)},
            json=payload,
        )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            logger.error(f"Zoho invoice creation failed. Body: {resp.text}")
            raise e
        invoice = resp.json().get("invoice", {})
        logger.info(f"Zoho invoice created: {invoice.get('invoice_id')} | items={len(line_items)}")
        invoice["invoice_url"] = _invoice_url(invoice["invoice_id"])
        return invoice


async def mark_email_processed(
    gmail_message_id: str,
    zoho_invoice_id: str,
    db: AsyncSession,
) -> None:
    """Record that a Gmail message has been invoiced (prevents double-billing)."""
    existing = await db.get(ProcessedEmail, gmail_message_id)
    if not existing:
        db.add(ProcessedEmail(
            gmail_message_id=gmail_message_id,
            zoho_invoice_id=zoho_invoice_id,
            created_at=int(time.time()),
        ))
        await db.commit()


async def update_contact_email(contact_id: str, email: str, db: AsyncSession) -> None:
    """Patch a Zoho contact to add an email address (contact_persons)."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.put(
                f"{settings.zoho_api_base}/contacts/{contact_id}",
                headers=await _headers(db),
                params={"organization_id": await get_org_id(db)},
                json={"contact_persons": [{"email": email, "is_primary_contact": True}]},
            )
        if resp.is_success:
            logger.info(f"Updated Zoho contact {contact_id} with email {email}")
        else:
            logger.warning(f"Could not update contact email: {resp.status_code} — {resp.text[:200]}")
    except Exception as e:
        logger.warning(f"update_contact_email failed: {e}")


async def send_invoice_email(
    invoice_id: str,
    db: AsyncSession,
    to_email: Optional[str] = None,
) -> tuple[bool, str]:
    """
    Send a Zoho invoice to the customer via email.
    Returns (True, "") on success or (False, "reason") on failure.
    """
    try:
        # Zoho Invoice email API — to_mail_ids must be a plain list of strings.
        # Using an object format [{"user_name": ..., "email": ...}] returns HTTP 400.
        body: dict = {
            "send_customer_emails": True,
        }
        if to_email:
            body["to_mail_ids"] = [to_email]

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{settings.zoho_api_base}/invoices/{invoice_id}/email",
                headers=await _headers(db),
                params={"organization_id": await get_org_id(db)},
                json=body,
            )
            if not resp.is_success:
                reason = f"HTTP {resp.status_code} — {resp.text[:200]}"
                logger.error(f"Zoho email API error for {invoice_id}: {reason}")
                return False, reason
        logger.info(f"Invoice email sent for {invoice_id} → {to_email or 'default contact email'}")
        return True, ""
    except RuntimeError as e:
        # Zoho not connected (raised by _headers)
        logger.error(f"Zoho not connected when sending invoice email: {e}")
        return False, str(e)
    except Exception as e:
        logger.error(f"Failed to send invoice email for {invoice_id}: {e}")
        return False, str(e)
