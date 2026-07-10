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


def recurring_invoice_url(recurring_invoice_id: str) -> str:
    base = _ZOHO_APP_BASE.get(settings.ZOHO_REGION, "https://invoice.zoho.com")
    return f"{base}/app#/recurringinvoices/{recurring_invoice_id}"



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
    estimate_id: Optional[str] = None,
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
    if estimate_id:
        payload["estimate_id"] = estimate_id
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
        async with httpx.AsyncClient() as client:
            headers = await _headers(db)
            org_id = await get_org_id(db)

            # If to_email is not provided, fetch the invoice details from Zoho first to get the email
            if not to_email:
                inv_resp = await client.get(
                    f"{settings.zoho_api_base}/invoices/{invoice_id}",
                    headers=headers,
                    params={"organization_id": org_id},
                )
                if inv_resp.is_success:
                    inv_data = inv_resp.json().get("invoice", {})
                    # Try direct email field on invoice
                    to_email = (inv_data.get("email") or "").strip()
                    # Fallback to customer's contact persons if not set on the invoice itself
                    if not to_email:
                        for cp in inv_data.get("contact_persons", []):
                            if cp.get("is_primary_contact"):
                                to_email = (cp.get("email") or "").strip()
                                break
                        if not to_email and inv_data.get("contact_persons"):
                            to_email = (inv_data["contact_persons"][0].get("email") or "").strip()
                else:
                    logger.warning(f"Could not fetch invoice details for {invoice_id} to resolve email: {inv_resp.text}")

            body: dict = {
                "send_customer_emails": True,
            }
            if to_email:
                body["to_mail_ids"] = [to_email]
            else:
                return False, "Could not resolve recipient email address for the invoice"

            resp = await client.post(
                f"{settings.zoho_api_base}/invoices/{invoice_id}/email",
                headers=headers,
                params={"organization_id": org_id},
                json=body,
            )
            if not resp.is_success:
                reason = f"HTTP {resp.status_code} — {resp.text[:200]}"
                logger.error(f"Zoho email API error for {invoice_id}: {reason}")
                return False, reason
        logger.info(f"Invoice email sent for {invoice_id} → {to_email}")
        return True, ""
    except RuntimeError as e:
        # Zoho not connected (raised by _headers)
        logger.error(f"Zoho not connected when sending invoice email: {e}")
        return False, str(e)
    except Exception as e:
        logger.error(f"Failed to send invoice email for {invoice_id}: {e}")
        return False, str(e)


# ── Payment Reminder API ─────────────────────────────────────────────────────

async def bulk_remind_invoices(
    invoice_ids: list[str],
    db: AsyncSession,
) -> dict:
    """
    Send payment reminders for a list of invoice IDs using Zoho's bulk endpoint.

    Endpoint: POST /invoices/paymentreminder
    - invoice_ids are passed as a comma-separated query param (Zoho v3 spec).
    - Max 10 IDs per call (Zoho bulk limit) — callers must chunk.
    - organization_id is sent both as query param AND as header (gotcha #2 from
      CONTEXT.md — the reminder endpoint behaves like the email endpoint).

    Returns:
        {
            "succeeded": ["id1", "id2", ...],  # IDs that were reminded successfully
            "failed":    [("id3", "reason"), ...]  # IDs that Zoho reported as errors
        }
    """
async def _fix_invoice_contact_person(invoice_id: str, db: AsyncSession) -> bool:
    """
    Looks up the invoice's customer, finds their primary contact person ID,
    and updates the invoice to link that contact person.
    Returns True if successfully updated, False otherwise.
    """
    try:
        org_id = await get_org_id(db)
        headers = await _headers(db)
        headers["X-com-zoho-invoice-organizationid"] = org_id
        
        async with httpx.AsyncClient(timeout=15.0) as client:
            # 1. Get the invoice to find customer_id
            inv_resp = await client.get(
                f"{settings.zoho_api_base}/invoices/{invoice_id}",
                headers=headers,
                params={"organization_id": org_id},
            )
            if not inv_resp.is_success:
                return False
            
            invoice_data = inv_resp.json().get("invoice", {})
            customer_id = invoice_data.get("customer_id")
            if not customer_id:
                return False
                
            # 2. Get the customer detail to find primary contact person ID
            cust_resp = await client.get(
                f"{settings.zoho_api_base}/contacts/{customer_id}",
                headers=headers,
                params={"organization_id": org_id},
            )
            if not cust_resp.is_success:
                return False
                
            contact_data = cust_resp.json().get("contact", {})
            contact_persons = contact_data.get("contact_persons", [])
            if not contact_persons:
                return False
                
            # Find primary contact person, or fallback to first contact person
            cp_id = None
            for cp in contact_persons:
                if cp.get("is_primary_contact"):
                    cp_id = cp.get("contact_person_id")
                    break
            if not cp_id:
                cp_id = contact_persons[0].get("contact_person_id")
                
            if not cp_id:
                return False
                
            # 3. Update the invoice to associate this contact person
            update_resp = await client.put(
                f"{settings.zoho_api_base}/invoices/{invoice_id}",
                headers=headers,
                params={"organization_id": org_id},
                json={"contact_persons": [cp_id]},
            )
            if update_resp.is_success:
                logger.info(f"Successfully auto-linked contact person {cp_id} to invoice {invoice_id}")
                return True
    except Exception as e:
        logger.error(f"Failed to auto-heal contact person on invoice {invoice_id}: {e}")
    return False


async def bulk_remind_invoices(invoice_ids: list[str], db: AsyncSession) -> dict:
    """Send payment reminders for overdue invoices in bulk (max 10)."""
    if not invoice_ids:
        return {"succeeded": [], "failed": []}

    # Clamp to Zoho's 10-ID bulk limit (callers should chunk, but be safe)
    ids_slice = invoice_ids[:10]
    ids_param = ",".join(ids_slice)

    succeeded: list[str] = []
    failed: list[tuple[str, str]] = []

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            headers = await _headers(db)
            org_id = await get_org_id(db)
            # Also send org_id in header for compatibility with Zoho India region
            headers["X-com-zoho-invoice-organizationid"] = org_id

            resp = await client.post(
                f"{settings.zoho_api_base}/invoices/paymentreminder",
                headers=headers,
                params={
                    "organization_id": org_id,
                    "invoice_ids": ids_param,
                },
            )

            if not resp.is_success:
                reason = f"HTTP {resp.status_code} — {resp.text[:300]}"
                logger.error(f"Zoho bulk reminder API error: {reason}")
                return {"succeeded": [], "failed": [(iid, reason) for iid in ids_slice]}

            data = resp.json()
            info = data.get("info", {})

            # Parse partial failures from email_errors_info
            error_ids: set[str] = set()
            retry_ids: list[str] = []
            
            for err in (info.get("email_errors_info") or []):
                raw_ids = err.get("ids")
                msg = str(err.get("message") or "Unknown error")
                
                # Check if it is a missing contact person error
                is_contact_person_err = "no contact person" in msg.lower()
                
                failed_eids = []
                if isinstance(raw_ids, list):
                    failed_eids = [str(eid).strip() for eid in raw_ids if str(eid).strip()]
                else:
                    failed_eids = [str(eid).strip() for eid in str(raw_ids or "").split(",") if str(eid).strip()]
                
                for eid in failed_eids:
                    if is_contact_person_err:
                        retry_ids.append(eid)
                    else:
                        error_ids.add(eid)
                        failed.append((eid, msg))

            # Auto-heal invoices with missing contact persons
            if retry_ids:
                healed_ids = []
                for eid in retry_ids:
                    healed = await _fix_invoice_contact_person(eid, db)
                    if healed:
                        healed_ids.append(eid)
                    else:
                        error_ids.add(eid)
                        failed.append((eid, "Unable to send payment reminder as no contact person was found (auto-heal failed)"))
                
                # Re-trigger payment reminder for the healed ones
                if healed_ids:
                    logger.info(f"Retrying payment reminders for auto-healed invoices: {healed_ids}")
                    retry_param = ",".join(healed_ids)
                    retry_resp = await client.post(
                        f"{settings.zoho_api_base}/invoices/paymentreminder",
                        headers=headers,
                        params={"organization_id": org_id, "invoice_ids": retry_param},
                    )
                    if retry_resp.is_success:
                        retry_data = retry_resp.json()
                        retry_info = retry_data.get("info", {})
                        
                        for err in (retry_info.get("email_errors_info") or []):
                            retry_raw_ids = err.get("ids")
                            retry_msg = str(err.get("message") or "Unknown error")
                            
                            retry_failed_eids = []
                            if isinstance(retry_raw_ids, list):
                                retry_failed_eids = [str(reid).strip() for reid in retry_raw_ids if str(reid).strip()]
                            else:
                                retry_failed_eids = [str(reid).strip() for reid in str(retry_raw_ids or "").split(",") if str(reid).strip()]
                                
                            for reid in retry_failed_eids:
                                error_ids.add(reid)
                                failed.append((reid, retry_msg))
                    else:
                        retry_reason = f"Retry HTTP {retry_resp.status_code}"
                        for reid in healed_ids:
                            error_ids.add(reid)
                            failed.append((reid, retry_reason))

            succeeded = [iid for iid in ids_slice if iid not in error_ids]

            logger.info(
                f"Bulk reminder: {len(succeeded)} sent, {len(failed)} failed. "
                f"Zoho code={data.get('code')} message={data.get('message')!r}"
            )

    except RuntimeError as e:
        logger.error(f"Zoho not connected when sending bulk reminders: {e}")
        return {"succeeded": [], "failed": [(iid, str(e)) for iid in ids_slice]}
    except Exception as e:
        logger.error(f"bulk_remind_invoices failed: {e}")
        return {"succeeded": [], "failed": [(iid, str(e)) for iid in ids_slice]}

    return {"succeeded": succeeded, "failed": failed}


async def record_payment(
    customer_id: str,
    invoice_id: str,
    amount: float,
    payment_date: str,
    payment_mode: str,
    db: AsyncSession,
) -> dict:
    """
    Record an offline payment against an invoice in Zoho.
    POST /customerpayments
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        headers = await _headers(db)
        org_id = await get_org_id(db)
        headers["X-com-zoho-invoice-organizationid"] = org_id

        # Map to standard Zoho manual payment modes:
        # cash, check, creditcard, banktransfer, bankremittance, others
        mode_mapped = payment_mode.lower().strip()
        if mode_mapped == "upi":
            mode_mapped = "banktransfer"
        elif mode_mapped not in ("cash", "check", "creditcard", "banktransfer", "bankremittance", "others"):
            mode_mapped = "others"

        body = {
            "customer_id": customer_id,
            "payment_mode": mode_mapped,
            "amount": amount,
            "date": payment_date,
            "invoices": [
                {
                    "invoice_id": invoice_id,
                    "amount_applied": amount
                }
            ]
        }

        resp = await client.post(
            f"{settings.zoho_api_base}/customerpayments",
            headers=headers,
            params={"organization_id": org_id},
            json=body,
        )
        if not resp.is_success:
            reason = f"HTTP {resp.status_code} — {resp.text[:300]}"
            logger.error(f"Zoho record_payment error: {reason}")
            raise RuntimeError(f"Zoho API error: {reason}")

        return resp.json().get("payment", {})


# ── Recurring Invoice API ─────────────────────────────────────────────────────

async def create_recurring_invoice(
    contact_id: str,
    item_name: str,
    amount: float,
    currency: str,
    frequency: str,          # "monthly" | "weekly" | "yearly" | "daily"
    start_date: str,         # "YYYY-MM-DD"
    db: AsyncSession,
    task_description: Optional[str] = None,
    end_date: Optional[str] = None,
    recurrence_name: Optional[str] = None,
) -> dict:
    """
    Create a Zoho recurring invoice profile.
    Returns the created recurring_invoice dict from Zoho.
    """
    # Zoho accepts: weeks, months, years, days (plural)
    freq_map = {
        "monthly": "months", "month": "months", "months": "months",
        "weekly":  "weeks",  "week":  "weeks",  "weeks":  "weeks",
        "yearly":  "years",  "year":  "years",  "yearly": "years", "annual": "years", "years": "years",
        "daily":   "days",   "day":   "days",   "days":   "days",
    }
    zoho_freq = freq_map.get(frequency.lower(), "months")

    name = recurrence_name or f"{item_name} — {zoho_freq.capitalize()} Invoice"

    payload: dict = {
        "customer_id":          contact_id,
        "recurrence_name":      name,
        "recurrence_frequency": zoho_freq,
        "repeat_every":         1,
        "start_date":           start_date,
        "line_items": [{
            "name":        item_name,
            "description": task_description or item_name,
            "rate":        amount,
            "quantity":    1,
        }],
    }
    if end_date:
        payload["end_date"] = end_date


    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{settings.zoho_api_base}/recurringinvoices",
            headers=await _headers(db),
            params={"organization_id": await get_org_id(db)},
            json=payload,
        )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError:
            logger.error(f"Recurring invoice creation failed: {resp.text}")
            raise
        data = resp.json().get("recurring_invoice", {})
        data["recurring_invoice_url"] = recurring_invoice_url(data.get("recurring_invoice_id", ""))
        logger.info(f"Recurring invoice created: {data.get('recurring_invoice_id')} — {name}")
        return data


async def list_recurring_invoices(db: AsyncSession) -> list[dict]:
    """
    Fetch all active recurring invoice profiles.
    Reads from the local cached RecurringCache table.
    Ensures the cache is fresh by calling ensure_fresh_cache(db) first.
    """
    from backend.services.zoho_payments import ensure_fresh_cache
    from backend.db.models import RecurringCache
    from sqlalchemy import select

    await ensure_fresh_cache(db)

    result = await db.execute(select(RecurringCache))
    profiles = result.scalars().all()

    invoices = []
    for prof in profiles:
        invoices.append({
            "recurring_invoice_id": prof.profile_id,
            "customer_name": prof.customer_name,
            "status": prof.status,
            "total": float(prof.amount or 0),
            "recurring_invoice_url": recurring_invoice_url(prof.profile_id),
        })
    logger.info(f"Fetched {len(invoices)} active recurring invoice(s) from cache")
    return invoices



async def stop_recurring_invoice(recurring_invoice_id: str, db: AsyncSession) -> bool:
    """
    Stop (pause) a Zoho recurring invoice profile.
    Returns True on success, False on failure.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{settings.zoho_api_base}/recurringinvoices/{recurring_invoice_id}/status/stop",
            headers=await _headers(db),
            params={"organization_id": await get_org_id(db)},
        )
        if resp.is_success:
            logger.info(f"Recurring invoice {recurring_invoice_id} stopped.")
            return True
        logger.error(f"Stop recurring invoice failed: {resp.status_code} — {resp.text[:200]}")
        return False


async def list_all_invoices(db: AsyncSession, status_filter: str | None = None) -> list[dict]:
    """
    Fetch all invoices from Zoho (sent, paid, overdue, draft).
    Optionally filter by status: 'sent', 'paid', 'overdue', 'draft', 'void'.
    Returns a list of invoice dicts with url attached.
    Note: sort_column is omitted — Zoho India rejects 'invoice_date' as a sort value.
    """
    params: dict = {
        "organization_id": await get_org_id(db),
        "per_page": 200,
    }
    if status_filter and status_filter != "all":
        status_map = {
            "sent":    "Status.Sent",
            "paid":    "Status.Paid",
            "draft":   "Status.Draft",
            "void":    "Status.Void",
            # Note: Status.Overdue returns 400 on Zoho India — handle client-side below
        }
        zoho_filter = status_map.get(status_filter.lower())
        if zoho_filter:
            params["filter_by"] = zoho_filter

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(
            f"{settings.zoho_api_base}/invoices",
            headers=await _headers(db),
            params=params,
        )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError:
            logger.error(f"list_all_invoices failed: {resp.text[:300]}")
            raise

    from datetime import date as _date
    today_str = _date.today().isoformat()

    invoices = resp.json().get("invoices", [])
    base = _ZOHO_APP_BASE.get(settings.ZOHO_REGION, "https://invoice.zoho.com")
    for inv in invoices:
        inv["invoice_url"] = f"{base}/app#/invoices/{inv.get('invoice_id', '')}"
        # Compute overdue client-side if Zoho status says 'sent' but due date has passed
        if inv.get("status", "").lower() == "sent":
            due = inv.get("due_date") or ""
            if due and due < today_str:
                inv["status"] = "overdue"

    # If caller asked for overdue, filter it here (since Zoho India rejects Status.Overdue)
    if status_filter and status_filter.lower() == "overdue":
        invoices = [inv for inv in invoices if inv.get("status", "").lower() == "overdue"]

    logger.info(f"Fetched {len(invoices)} invoice(s) (filter={status_filter})")
    return invoices


async def get_invoice_stats(db: AsyncSession) -> dict:
    """
    Compute invoice statistics.
    Reads from the local SQL cache (InvoiceCache) to avoid unnecessary Zoho API calls.
    Refreshes the cache on-demand if it is older than the 15-minute TTL.
    """
    from datetime import date
    from collections import defaultdict
    import calendar
    from backend.services.zoho_payments import ensure_fresh_cache
    from backend.db.models import InvoiceCache, RecurringCache
    from sqlalchemy import select

    # 1. Ensure the database cache is fresh (15-minute TTL fallback)
    await ensure_fresh_cache(db)

    # 2. Query all invoices and recurring profiles from the cache
    result_invoices = await db.execute(select(InvoiceCache))
    invoices = result_invoices.scalars().all()

    result_recurring = await db.execute(select(RecurringCache))
    recurring = result_recurring.scalars().all()

    today = date.today()
    today_str = today.isoformat()
    this_month_prefix = today.strftime("%Y-%m")

    # Split cached invoices into overdue, sent, paid
    # Statuses in DB: 'overdue', 'sent', 'partially_paid', 'paid', 'unpaid', 'void'
    overdue_list = [inv for inv in invoices if inv.status == "overdue"]
    sent_list    = [inv for inv in invoices if inv.status in ("sent", "partially_paid", "unpaid")]
    paid_list    = [inv for inv in invoices if inv.status == "paid"]

    # Calculate per-customer breakdown of paid, overdue, and sent amounts
    customer_data = defaultdict(lambda: {"paid": 0.0, "overdue": 0.0, "sent": 0.0})
    for inv in paid_list:
        cname = inv.customer_name or "Unknown"
        customer_data[cname]["paid"] += float(inv.total or 0)

    for inv in overdue_list:
        cname = inv.customer_name or "Unknown"
        customer_data[cname]["overdue"] += float(inv.balance or 0)

    for inv in sent_list:
        cname = inv.customer_name or "Unknown"
        customer_data[cname]["sent"] += float(inv.balance or 0)

    customer_breakdown = [
        {
            "customer_name": name,
            "paid": round(vals["paid"], 2),
            "overdue": round(vals["overdue"], 2),
            "sent": round(vals["sent"], 2),
        }
        for name, vals in customer_data.items()
    ]

    overdue_amount = sum(float(inv.balance or 0) for inv in overdue_list)
    sent_amount    = sum(float(inv.balance or 0) for inv in sent_list)
    outstanding    = overdue_amount + sent_amount

    collected_this_month = sum(
        float(inv.total or 0)
        for inv in paid_list
        if (inv.last_payment_date and inv.last_payment_date.strftime("%Y-%m") == this_month_prefix)
        or (inv.invoice_date and inv.invoice_date.strftime("%Y-%m") == this_month_prefix)
    )

    paid_count_this_month = sum(
        1 for inv in paid_list
        if (inv.last_payment_date and inv.last_payment_date.strftime("%Y-%m") == this_month_prefix)
        or (inv.invoice_date and inv.invoice_date.strftime("%Y-%m") == this_month_prefix)
    )

    # Build 6-month revenue history from paid invoices
    monthly = defaultdict(float)
    for inv in paid_list:
        dt_val = inv.last_payment_date or inv.invoice_date
        if dt_val:
            key = dt_val.strftime("%Y-%m")
            monthly[key] += float(inv.total or 0)

    # Build sorted last 6 months
    revenue_history = []
    for i in range(5, -1, -1):
        month_num = (today.month - i - 1) % 12 + 1
        year_offset = (today.month - i - 1) // 12
        yr = today.year - year_offset
        key = f"{yr}-{month_num:02d}"
        label = f"{calendar.month_abbr[month_num]} {yr}"
        revenue_history.append({"month": label, "amount": round(monthly.get(key, 0), 2)})

    logger.info(
        f"Stats (Cached): outstanding={outstanding:.2f}, overdue={len(overdue_list)}, "
        f"sent={len(sent_list)}, paid_all={len(paid_list)}, recurring={len(recurring)}"
    )

    return {
        "outstanding_amount":     round(outstanding, 2),
        "collected_this_month":   round(collected_this_month, 2),
        "overdue_count":          len(overdue_list),
        "overdue_amount":         round(overdue_amount, 2),
        "sent_count":             len(sent_list),
        "paid_count_this_month":  paid_count_this_month,
        "paid_total_count":       len(paid_list),
        "recurring_count":        len(recurring),
        "revenue_history":        revenue_history,
        "customer_breakdown":     customer_breakdown,
    }


async def create_estimate(
    customer_id: str,
    line_items: list,
    currency: str,
    db: AsyncSession,
) -> dict:
    """POST a new estimate to Zoho Invoice and return the created estimate object."""
    payload = {
        "customer_id": customer_id,
        "line_items": line_items,
        "currency_code": currency,
    }
    logger.info(f"Sending estimate payload to Zoho: {payload}")
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{settings.zoho_api_base}/estimates",
            headers=await _headers(db),
            params={"organization_id": await get_org_id(db)},
            json=payload,
        )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            logger.error(f"Zoho estimate creation failed. Body: {resp.text}")
            raise e
        estimate = resp.json().get("estimate", {})
        logger.info(f"Zoho estimate created: {estimate.get('estimate_id')} | items={len(line_items)}")
        base = _ZOHO_APP_BASE.get(settings.ZOHO_REGION, "https://invoice.zoho.com")
        estimate["estimate_url"] = f"{base}/app#/estimates/{estimate.get('estimate_id', '')}"
        return estimate


async def send_estimate_email(
    estimate_id: str,
    db: AsyncSession,
    to_email: Optional[str] = None,
) -> tuple[bool, str]:
    """
    Send a Zoho estimate to the customer via email.
    Returns (True, "") on success or (False, "reason") on failure.
    """
    try:
        async with httpx.AsyncClient() as client:
            headers = await _headers(db)
            org_id = await get_org_id(db)

            # If to_email is not provided, fetch estimate details first
            if not to_email:
                est_resp = await client.get(
                    f"{settings.zoho_api_base}/estimates/{estimate_id}",
                    headers=headers,
                    params={"organization_id": org_id},
                )
                if est_resp.is_success:
                    est_data = est_resp.json().get("estimate", {})
                    to_email = (est_data.get("email") or "").strip()
                    if not to_email:
                        for cp in est_data.get("contact_persons", []):
                            if cp.get("is_primary_contact"):
                                to_email = (cp.get("email") or "").strip()
                                break
                        if not to_email and est_data.get("contact_persons"):
                            to_email = (est_data["contact_persons"][0].get("email") or "").strip()
                else:
                    logger.warning(f"Could not fetch estimate details for {estimate_id} to resolve email: {est_resp.text}")

            if not to_email:
                return False, "Could not resolve recipient email address for the estimate"

            body: dict = {
                "to_mail_ids": [to_email],
            }
            resp = await client.post(
                f"{settings.zoho_api_base}/estimates/{estimate_id}/email",
                headers=headers,
                params={"organization_id": org_id},
                json=body,
            )
            if not resp.is_success:
                reason = f"HTTP {resp.status_code} — {resp.text[:200]}"
                logger.error(f"Zoho email API error for estimate {estimate_id}: {reason}")
                return False, reason
        logger.info(f"Estimate email sent for {estimate_id} → {to_email}")
        return True, ""
    except Exception as e:
        logger.error(f"Failed to send estimate email for {estimate_id}: {e}")
        return False, str(e)


async def convert_estimate_to_invoice(
    estimate_id: str,
    db: AsyncSession,
    line_items: Optional[list] = None,
) -> dict:
    """
    Fetch estimate details, check duplicate status, and call the existing create_invoice function.
    """
    async with httpx.AsyncClient() as client:
        headers = await _headers(db)
        org_id = await get_org_id(db)
        resp = await client.get(
            f"{settings.zoho_api_base}/estimates/{estimate_id}",
            headers=headers,
            params={"organization_id": org_id},
        )
        resp.raise_for_status()
        estimate = resp.json().get("estimate", {})
        
        status = estimate.get("status", "").lower()
        if status in ("invoiced", "partially_invoiced"):
            raise ValueError(f"Estimate is already {status}.")

        customer_id = estimate.get("customer_id")
        currency = estimate.get("currency_code") or "INR"

        if line_items is None:
            raw_items = estimate.get("line_items") or []
            line_items = []
            for item in raw_items:
                line_items.append({
                    "name": item.get("name") or "Service",
                    "description": item.get("description") or "",
                    "rate": float(item.get("rate") or 0.0),
                    "quantity": int(item.get("quantity") or 1),
                })

        invoice = await create_invoice(
            contact_id=customer_id,
            task_description="",
            amount=0.0,
            currency=currency,
            db=db,
            line_items=line_items,
            estimate_id=estimate_id,
        )
        return invoice


async def list_estimates(
    db: AsyncSession,
    filter_by: Optional[str] = None,
) -> list[dict]:
    """List estimates from Zoho, optionally filtered by status (e.g. Status.Sent)."""
    params: dict = {"organization_id": await get_org_id(db)}
    if filter_by:
        params["filter_by"] = filter_by
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{settings.zoho_api_base}/estimates",
            headers=await _headers(db),
            params=params,
        )
        resp.raise_for_status()
        return resp.json().get("estimates", [])


async def get_estimate(estimate_id: str, db: AsyncSession) -> dict:
    """Fetch full estimate details from Zoho."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{settings.zoho_api_base}/estimates/{estimate_id}",
            headers=await _headers(db),
            params={"organization_id": await get_org_id(db)},
        )
        resp.raise_for_status()
        return resp.json().get("estimate", {})


async def update_estimate_status(
    estimate_id: str,
    status: str,
    db: AsyncSession,
) -> dict:
    """Mark an estimate as accepted, declined, or sent via Zoho status API."""
    allowed = {"accepted", "declined", "sent"}
    if status not in allowed:
        raise ValueError(f"Unsupported estimate status: {status}")
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{settings.zoho_api_base}/estimates/{estimate_id}/status/{status}",
            headers=await _headers(db),
            params={"organization_id": await get_org_id(db)},
        )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            logger.error(f"Zoho estimate status update failed for {estimate_id}: {resp.text}")
            raise e
        return resp.json().get("estimate", {})

