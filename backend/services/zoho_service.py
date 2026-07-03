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
    Fetch all active recurring invoice profiles from Zoho.
    Returns a list of raw recurring invoice dicts.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{settings.zoho_api_base}/recurringinvoices",
            headers=await _headers(db),
            params={
                "organization_id": await get_org_id(db),
                "filter_by": "Status.Active",
                "per_page": 200,
            },
        )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError:
            logger.error(f"List recurring invoices failed: {resp.text}")
            raise
        invoices = resp.json().get("recurring_invoices", [])
        for inv in invoices:
            inv["recurring_invoice_url"] = recurring_invoice_url(inv.get("recurring_invoice_id", ""))
        logger.info(f"Fetched {len(invoices)} active recurring invoice(s)")
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
    Compute invoice statistics from Zoho.
    Note: Zoho India rejects Status.Overdue filter — overdue is detected client-side
    by checking due_date < today on sent invoices.
    """
    from datetime import date
    org_id = await get_org_id(db)
    headers = await _headers(db)

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Fetch all unpaid (sent) invoices — includes overdue ones on Zoho India
        r_sent = await client.get(
            f"{settings.zoho_api_base}/invoices",
            headers=headers,
            params={"organization_id": org_id, "filter_by": "Status.Sent", "per_page": 200},
        )
        # Fetch paid invoices — for revenue history and this-month totals
        r_paid = await client.get(
            f"{settings.zoho_api_base}/invoices",
            headers=headers,
            params={"organization_id": org_id, "filter_by": "Status.Paid", "per_page": 200},
        )
        # Fetch active recurring profiles
        r_recurring = await client.get(
            f"{settings.zoho_api_base}/recurringinvoices",
            headers=headers,
            params={"organization_id": org_id, "filter_by": "Status.Active", "per_page": 200},
        )

    sent_raw  = r_sent.json().get("invoices", []) if r_sent.is_success else []
    paid_list = r_paid.json().get("invoices", []) if r_paid.is_success else []
    recurring = r_recurring.json().get("recurring_invoices", []) if r_recurring.is_success else []

    today = date.today()
    today_str = today.isoformat()
    this_month_prefix = today.strftime("%Y-%m")

    # Split sent_raw into overdue (due_date passed) vs current sent
    overdue_list = [inv for inv in sent_raw if (inv.get("due_date") or "9999") < today_str]
    sent_list    = [inv for inv in sent_raw if (inv.get("due_date") or "9999") >= today_str]

    overdue_amount = sum(float(inv.get("balance", 0)) for inv in overdue_list)
    sent_amount    = sum(float(inv.get("balance", 0)) for inv in sent_list)
    outstanding    = overdue_amount + sent_amount

    collected_this_month = sum(
        float(inv.get("total", 0))
        for inv in paid_list
        if (inv.get("last_payment_date") or inv.get("invoice_date") or "").startswith(this_month_prefix)
    )

    paid_count_this_month = sum(
        1 for inv in paid_list
        if (inv.get("last_payment_date") or inv.get("invoice_date") or "").startswith(this_month_prefix)
    )

    # Build 6-month revenue history from paid invoices
    from collections import defaultdict
    monthly = defaultdict(float)
    for inv in paid_list:
        dt = inv.get("last_payment_date") or inv.get("invoice_date") or ""
        if len(dt) >= 7:
            monthly[dt[:7]] += float(inv.get("total", 0))

    # Build sorted last 6 months
    import calendar
    revenue_history = []
    for i in range(5, -1, -1):
        month_num = (today.month - i - 1) % 12 + 1
        year_offset = (today.month - i - 1) // 12
        yr = today.year - year_offset
        key = f"{yr}-{month_num:02d}"
        label = f"{calendar.month_abbr[month_num]} {yr}"
        revenue_history.append({"month": label, "amount": round(monthly.get(key, 0), 2)})

    logger.info(
        f"Stats: outstanding={outstanding:.2f}, overdue={len(overdue_list)}, "
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
    }

