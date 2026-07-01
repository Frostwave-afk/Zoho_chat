import logging
import time
from datetime import date
from decimal import Decimal
from typing import Optional

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.db.models import InvoiceCache
from backend.services.zoho_service import _headers, _invoice_url, get_org_id

logger = logging.getLogger(__name__)
settings = get_settings()

_CACHE_TTL = 900  # 15 minutes (used for background card refreshes only)

# All statuses to pull from Zoho on each sync.
# "unpaid" = drafted/not-yet-sent invoices; included so nothing is missed.
_SYNC_STATUSES = ("overdue", "sent", "partially_paid", "paid", "unpaid")

# Normalize whatever status string Zoho returns → canonical value stored in DB
_STATUS_ALIASES: dict[str, str] = {
    "partial":        "partially_paid",
    "partiallypaid":  "partially_paid",
    "partially paid": "partially_paid",
}


def _normalize_status(raw: str, fallback: str) -> str:
    s = (raw or fallback).strip().lower()
    return _STATUS_ALIASES.get(s, s)


def _parse_due_date(raw: Optional[str]) -> Optional[date]:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _row_to_dict(row: InvoiceCache) -> dict:
    due = row.due_date
    days_overdue = None
    if row.status == "overdue" and due:
        days_overdue = max(0, (date.today() - due).days)

    return {
        "invoice_id":    row.invoice_id,
        "customer_name": row.customer_name,
        "status":        row.status,
        "due_date":      due.isoformat() if due else None,
        "balance":       float(row.balance or 0),
        "total":         float(row.total or 0),
        "currency_code": row.currency_code or "INR",
        "zoho_view_url": row.zoho_view_url,
        "days_overdue":  days_overdue,
    }


async def _fetch_invoices_by_status(db: AsyncSession, status: str) -> list[dict]:
    """Paginate through Zoho Invoice list for a single status filter."""
    invoices: list[dict] = []
    page = 1

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            resp = await client.get(
                f"{settings.zoho_api_base}/invoices",
                headers=await _headers(db),
                params={
                    "organization_id": await get_org_id(db),
                    "status": status,
                    "page": page,
                    "per_page": 200,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            invoices.extend(data.get("invoices", []))

            page_ctx = data.get("page_context") or {}
            if not page_ctx.get("has_more_page"):
                break
            page += 1

    return invoices


async def sync_invoices_from_zoho(db: AsyncSession) -> int:
    """Fetch ALL relevant invoices from Zoho and replace the local cache.
    Returns the number of rows stored.
    """
    now = int(time.time())
    seen: set[str] = set()
    rows: list[InvoiceCache] = []

    for status in _SYNC_STATUSES:
        try:
            batch = await _fetch_invoices_by_status(db, status)
        except Exception as e:
            logger.error(f"Zoho invoice sync failed for status={status}: {e}")
            raise RuntimeError(
                f"Could not sync invoices from Zoho (status={status}). "
                "Check that Zoho is connected and the API region is correct."
            ) from e

        for inv in batch:
            invoice_id = str(inv.get("invoice_id", ""))
            if not invoice_id or invoice_id in seen:
                continue
            seen.add(invoice_id)

            rows.append(InvoiceCache(
                invoice_id=invoice_id,
                customer_name=inv.get("customer_name") or "Unknown",
                # Normalize status variants (e.g. "partial" → "partially_paid")
                status=_normalize_status(inv.get("status", ""), status),
                due_date=_parse_due_date(inv.get("due_date")),
                balance=Decimal(str(inv.get("balance") or 0)),
                total=Decimal(str(inv.get("total") or 0)),
                currency_code=inv.get("currency_code") or "INR",
                zoho_view_url=_invoice_url(invoice_id),
                last_synced=now,
            ))

    await db.execute(delete(InvoiceCache))
    for row in rows:
        db.add(row)
    await db.commit()

    logger.info(f"Invoice cache synced — {len(rows)} invoice(s) from Zoho.")
    return len(rows)


async def is_cache_stale(db: AsyncSession) -> bool:
    """True when cache is empty or the newest row is older than TTL."""
    result = await db.execute(select(func.max(InvoiceCache.last_synced)))
    latest = result.scalar()
    if latest is None:
        return True
    return (time.time() - latest) > _CACHE_TTL


async def ensure_fresh_cache(db: AsyncSession, force: bool = False) -> None:
    """Sync from Zoho when cache is stale.

    Pass force=True to always pull fresh data regardless of TTL.
    All user-facing payment queries should use force=True so they are
    never served stale data — the cache is only for background card display.
    """
    if force or await is_cache_stale(db):
        await sync_invoices_from_zoho(db)


async def get_overdue(db: AsyncSession) -> list[dict]:
    """All overdue invoices, most overdue first."""
    result = await db.execute(
        select(InvoiceCache)
        .where(InvoiceCache.status == "overdue")
        .order_by(InvoiceCache.due_date.asc().nulls_last())
    )
    return [_row_to_dict(r) for r in result.scalars()]


async def get_pending(db: AsyncSession) -> list[dict]:
    """Sent or partially-paid invoices with a remaining balance.
    Includes 'unpaid' (drafted but not yet sent) so nothing slips through.
    """
    result = await db.execute(
        select(InvoiceCache)
        .where(
            InvoiceCache.status.in_(("sent", "partially_paid", "unpaid")),
            InvoiceCache.balance > 0,
        )
        .order_by(InvoiceCache.due_date.asc().nulls_last())
    )
    return [_row_to_dict(r) for r in result.scalars()]


async def get_client_payments(db: AsyncSession, client_name: str) -> list[dict]:
    """LIKE match on customer_name (case-insensitive)."""
    pattern = f"%{client_name.lower().strip()}%"
    result = await db.execute(
        select(InvoiceCache)
        .where(func.lower(InvoiceCache.customer_name).like(pattern))
        .order_by(InvoiceCache.due_date.desc().nulls_last())
    )
    return [_row_to_dict(r) for r in result.scalars()]


async def get_payment_summary(db: AsyncSession) -> dict:
    """Aggregate counts and totals from cache."""
    overdue_rows = await get_overdue(db)
    pending_rows = await get_pending(db)

    paid_result = await db.execute(
        select(InvoiceCache).where(InvoiceCache.status == "paid")
    )
    paid_rows = [_row_to_dict(r) for r in paid_result.scalars()]

    all_result = await db.execute(select(InvoiceCache))
    all_rows   = [_row_to_dict(r) for r in all_result.scalars()]

    total_owed = sum(r["balance"] for r in overdue_rows + pending_rows)
    total_received = sum(
        max(float(r.get("total") or 0) - float(r.get("balance") or 0), 0)
        for r in all_rows
    )
    currency = (
        (overdue_rows or pending_rows or paid_rows or all_rows)[0]["currency_code"]
        if (overdue_rows or pending_rows or paid_rows or all_rows)
        else "INR"
    )

    return {
        "overdue_count":    len(overdue_rows),
        "pending_count":    len(pending_rows),
        "fully_paid_count": len(paid_rows),
        "total_owed":       total_owed,
        "total_received":   total_received,
        "currency_code":    currency,
    }
