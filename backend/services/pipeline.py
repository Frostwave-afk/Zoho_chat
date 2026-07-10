import uuid
import asyncio
import httpx
import logging
from collections import deque
from email.utils import parseaddr
from typing import Optional
import re

from sqlalchemy import delete, select, func
from sqlalchemy.ext.asyncio import AsyncSession
from backend.config import get_settings

settings = get_settings()

from backend.schemas import (
    ChatResponse, DraftInvoice, CreatedInvoice, PaymentInvoice,
    AmbiguousContact, InvoiceData, ApproveRequest, BatchDraft, BatchDraftItem,
    ManualInvoiceConversation, ManualInvoiceDraft, ManualInvoiceLineItem,
    RecurringConversation, RecurringInvoiceInfo, PaymentRecordDraft,
    EstimateDraft, EstimateLineItem, CreatedEstimate, AmbiguousEstimate, EstimateApproveRequest,
)
from backend.services.groq_service import parse_intent, extract_manual_invoice_request, extract_recurring_details
from backend.services.gemini_service import extract_invoice_data
from backend.services.gmail_service import search_gmail
from backend.services.zoho_payments import (
    ensure_fresh_cache, get_overdue, get_pending,
    get_client_payments, get_payment_summary,
)
from backend.utils import (
    format_payment_response, format_payment_summary, format_client_payment_response,
)
from backend.services.zoho_service import (
    search_contact_by_name, search_contact_by_email, create_contact, create_invoice,
    mark_email_processed, send_invoice_email, update_contact_email,
    clear_org_id_cache,
    create_recurring_invoice, list_recurring_invoices, stop_recurring_invoice,
    bulk_remind_invoices, record_payment,
    create_estimate, send_estimate_email, convert_estimate_to_invoice,
    list_estimates, get_estimate, update_estimate_status,
    _headers, get_org_id, _ZOHO_APP_BASE,
)
from backend.db.models import OAuthToken, ProcessedEmail, ContactCache, InvoiceCache

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")

def parse_relative_date(val: str) -> Optional[str]:
    """Parse relative date expressions (today, tomorrow, next week, next month) into YYYY-MM-DD."""
    from datetime import date as _date, timedelta as _timedelta
    val = val.lower().strip()
    today = _date.today()
    if "today" in val:
        return today.isoformat()
    if "tomorrow" in val or "tommorow" in val:
        return (today + _timedelta(days=1)).isoformat()
    if "next week" in val:
        return (today + _timedelta(days=7)).isoformat()
    if "next month" in val:
        return (today + _timedelta(days=30)).isoformat()
        
    m_days = re.search(r"in\s+(\d+)\s+days?", val)
    if m_days:
        return (today + _timedelta(days=int(m_days.group(1)))).isoformat()
        
    m_weeks = re.search(r"in\s+(\d+)\s+weeks?", val)
    if m_weeks:
        return (today + _timedelta(weeks=int(m_weeks.group(1)))).isoformat()
        
    if re.match(r"^\d{4}-\d{2}-\d{2}$", val):
        return val
    return None

_DONE_WORK_RE = re.compile(
    r"work on\s+(?:the\s+)?(?P<project>.+?)\s+is done",
    re.IGNORECASE | re.DOTALL,
)

# ── In-memory recurring conversation store ────────────────────────────────────
# key = telegram user_id (int) or "web" for the web frontend
_pending_recurring_conv: dict[str | int, RecurringConversation] = {}
# Stores last fetched active recurring list per user for stop-by-number
_pending_recurring_list: dict[str | int, list[dict]] = {}
_GENERIC_PLACEHOLDER_VALUES = {
    "john doe",
    "jane doe",
    "service",
    "product",
    "client",
    "customer",
    "example@example.com",
    "john.doe@example.com",
}


def _header_display_name(header_value: str) -> Optional[str]:
    name, addr = parseaddr(header_value or "")
    clean_name = (name or "").strip().strip('"').strip("'")
    if clean_name and clean_name.lower() != (addr or "").strip().lower():
        return clean_name
    return None


def _titleize_service_name(project_text: str) -> str:
    words = [w for w in re.split(r"\s+", project_text.strip()) if w]
    small_words = {"for", "the", "of", "and", "to", "a", "an", "on", "in"}
    titled: list[str] = []
    for idx, word in enumerate(words[:6]):
        lowered = word.lower().strip(",.!")
        if idx and lowered in small_words:
            titled.append(lowered)
        else:
            titled.append(lowered.capitalize())
    return " ".join(titled)


def _apply_invoice_text_fallbacks(data: InvoiceData, email_text: str) -> InvoiceData:
    updates: dict[str, object] = {}
    text = " ".join((email_text or "").split())
    match = _DONE_WORK_RE.search(text)

    if match:
        project = re.sub(r"\bthe\s+the\b", "the", match.group("project"), flags=re.IGNORECASE).strip(" .,!?:;")
        if project:
            if not data.task_description:
                updates["task_description"] = f"The work on the {project} is done."
            if not data.item_name:
                updates["item_name"] = _titleize_service_name(project)

    if not updates:
        return data

    missing = [field for field in data.missing_fields if field not in updates]
    updates["missing_fields"] = missing
    return data.model_copy(update=updates)


def _normalized_text(value: Optional[str]) -> str:
    return " ".join((value or "").strip().lower().split())


def _message_supports_manual_invoice_payload(message: str, parsed: dict) -> bool:
    text = _normalized_text(message)
    if not text:
        return False

    if any(placeholder == _normalized_text(parsed.get("client_name")) for placeholder in _GENERIC_PLACEHOLDER_VALUES):
        return False
    if any(placeholder == _normalized_text(parsed.get("client_email")) for placeholder in _GENERIC_PLACEHOLDER_VALUES):
        return False

    parsed_items = parsed.get("items") or []
    if parsed_items:
        has_amount_in_message = bool(re.search(r"(?:₹|inr|rs\.?|\$|usd|eur|gbp)?\s*\d[\d,]*(?:\.\d+)?", text, re.IGNORECASE))
        if not has_amount_in_message:
            return False
        for item in parsed_items:
            item_name = _normalized_text(item.get("item_name"))
            item_desc = _normalized_text(item.get("task_description"))
            if item_name in _GENERIC_PLACEHOLDER_VALUES or item_desc in _GENERIC_PLACEHOLDER_VALUES:
                return False
            meaningful_name = item_name and item_name in text
            meaningful_desc = item_desc and any(word in text for word in item_desc.split()[:3] if len(word) > 2)
            if not (meaningful_name or meaningful_desc):
                return False

    parsed_email = _normalized_text(parsed.get("client_email"))
    if parsed_email and parsed_email not in text:
        return False

    parsed_name = _normalized_text(parsed.get("client_name"))
    if parsed_name and parsed_name not in text:
        name_words = [word for word in parsed_name.split() if len(word) > 2]
        if not name_words or not any(word in text for word in name_words):
            return False

    return True

# In-memory draft store (single-user prototype — cleared on server restart)
_pending_drafts: dict[str, DraftInvoice] = {}
_pending_batches: dict[str, BatchDraft] = {}
_pending_manual_invoice_drafts: dict[str, ManualInvoiceDraft] = {}
_pending_estimate_drafts: dict[str, EstimateDraft] = {}
_manual_invoice_conversation: Optional[ManualInvoiceConversation] = None
_pending_payment_drafts: dict[str, PaymentRecordDraft] = {}
_pending_payment_disambiguations: dict[str | int, dict] = {}
_pending_estimate_disambiguations: dict[str | int, dict] = {}
_pending_payment_customer_prompts: dict[str | int, dict] = {}

# Tracks the last N invoices created this session so the user can say
# "send the invoice just created" or "send all invoices created today"
_recent_invoices: deque[CreatedInvoice] = deque(maxlen=20)

# Tracks estimates created/accepted this session for accept/send flows
_recent_estimates: deque[CreatedEstimate] = deque(maxlen=20)


def get_pending_draft(draft_id: str) -> Optional[DraftInvoice]:
    return _pending_drafts.get(draft_id)

def get_pending_batch(batch_id: str) -> Optional[BatchDraft]:
    return _pending_batches.get(batch_id)


def get_recent_invoices() -> list[CreatedInvoice]:
    return list(_recent_invoices)


def get_pending_manual_invoice(draft_id: str) -> Optional[ManualInvoiceDraft]:
    return _pending_manual_invoice_drafts.get(draft_id)

def get_pending_estimate(draft_id: str) -> Optional[EstimateDraft]:
    return _pending_estimate_drafts.get(draft_id)

def get_pending_payment_draft(draft_id: str) -> Optional[PaymentRecordDraft]:
    return _pending_payment_drafts.get(draft_id)


def _parse_amount_and_currency(text: str) -> tuple[Optional[float], Optional[str]]:
    raw = (text or "").strip().lower()
    currency = None
    if "₹" in raw or "inr" in raw or "rupee" in raw or "rs" in raw:
        currency = "INR"
    match = re.search(r"(\d[\d,]*(?:\.\d+)?)", raw.replace(",", ""))
    if not match:
        return None, currency
    try:
        return float(match.group(1)), currency
    except ValueError:
        return None, currency


def _parse_yes_no(text: str) -> Optional[bool]:
    raw = " ".join((text or "").strip().lower().split())
    if raw in {"yes", "y", "send", "send it", "send invoice", "email it", "email"}:
        return True
    if raw in {"no", "n", "draft", "save draft", "don't send", "do not send"}:
        return False
    return None


async def _resolve_manual_customer(
    client_name: Optional[str],
    client_email: Optional[str],
    db: AsyncSession,
) -> tuple[Optional[str], Optional[str], Optional[str], bool, Optional[str]]:
    """Return (name, email, contact_id, is_new_contact, clarification_reply)."""
    normalized_name = (client_name or "").strip() or None
    normalized_email = (client_email or "").strip() or None

    if normalized_email:
        email_matches = await search_contact_by_email(normalized_email, db)
        if len(email_matches) == 1:
            match = email_matches[0]
            return (
                normalized_name or match.get("contact_name"),
                normalized_email or match.get("email"),
                match["contact_id"],
                False,
                None,
            )

    if normalized_name:
        name_matches = await search_contact_by_name(normalized_name, db)
        if len(name_matches) == 1:
            match = name_matches[0]
            return (
                match.get("contact_name") or normalized_name,
                normalized_email or match.get("email"),
                match["contact_id"],
                False,
                None,
            )
        if len(name_matches) > 1:
            names = "\n".join(
                f"• {m['contact_name']} ({m.get('email') or 'no email'})" for m in name_matches
            )
            return (
                normalized_name,
                normalized_email,
                None,
                False,
                f"I found multiple Zoho contacts named **{normalized_name}**.\n\n{names}\n\nSend the exact email address for the one you want.",
            )
        if normalized_email:
            return normalized_name, normalized_email, None, True, None
        return (
            normalized_name,
            None,
            None,
            True,
            f"I couldn't find **{normalized_name}** in Zoho. Send their email address and I'll create them as a new customer.",
        )

    return None, normalized_email, None, False, "Who should this invoice be for?"


def _build_manual_invoice_draft_from_state(state: ManualInvoiceConversation) -> ManualInvoiceDraft:
    draft_id = str(uuid.uuid4())
    draft = ManualInvoiceDraft(
        draft_id=draft_id,
        client_name=state.client_name or "Unknown",
        client_email=state.client_email,
        currency=state.currency or "USD",
        zoho_contact_id=state.zoho_contact_id,
        is_new_contact=state.is_new_contact,
        line_items=state.line_items,
    )
    _pending_manual_invoice_drafts[draft_id] = draft
    return draft


def _build_estimate_draft_from_state(state: ManualInvoiceConversation) -> EstimateDraft:
    draft_id = str(uuid.uuid4())
    line_items = []
    for item in state.line_items:
        line_items.append(EstimateLineItem(
            item_name=item.item_name,
            description=item.task_description,
            amount=item.amount,
        ))
    draft = EstimateDraft(
        draft_id=draft_id,
        client_name=state.client_name or "Unknown",
        client_email=state.client_email,
        currency=state.currency or "INR",
        zoho_contact_id=state.zoho_contact_id,
        is_new_contact=state.is_new_contact,
        line_items=line_items,
    )
    _pending_estimate_drafts[draft_id] = draft
    return draft


def _manual_invoice_summary(draft: ManualInvoiceDraft) -> str:
    total = sum(item.amount for item in draft.line_items)
    lines = [f"Here's the manual invoice draft for **{draft.client_name}**:"]
    if draft.client_email:
        lines.append(f"Email: **{draft.client_email}**")
    if draft.is_new_contact:
        lines.append("This customer will be created in Zoho when you approve it.")
    lines.append("")
    for idx, item in enumerate(draft.line_items, 1):
        lines.append(
            f"{idx}. **{item.item_name}** — {draft.currency} {item.amount:,.2f}\n"
            f"   {item.task_description}"
        )
    lines.append(f"\nTotal: **{draft.currency} {total:,.2f}**")
    lines.append("Approve it below when you're happy with it.")
    return "\n".join(lines)


def _estimate_summary(draft: EstimateDraft) -> str:
    total = sum(item.amount for item in draft.line_items)
    lines = [f"Here's the estimate draft for **{draft.client_name}**:"]
    if draft.client_email:
        lines.append(f"Email: **{draft.client_email}**")
    if draft.is_new_contact:
        lines.append("This customer will be created in Zoho when you approve it.")
    lines.append("")
    for idx, item in enumerate(draft.line_items, 1):
        lines.append(
            f"{idx}. **{item.item_name}** — {draft.currency} {item.amount:,.2f}\n"
            f"   {item.description}"
        )
    lines.append(f"\nTotal: **{draft.currency} {total:,.2f}**")
    lines.append("Approve it below when you're happy with it.")
    return "\n".join(lines)


async def _advance_manual_invoice_conversation(db: AsyncSession) -> ChatResponse:
    global _manual_invoice_conversation
    state = _manual_invoice_conversation
    if not state:
        return ChatResponse(reply="Invoice/Estimate creation cancelled.", action="clarification_needed")

    noun = "estimate" if state.is_estimate else "invoice"
    verb = "quote" if state.is_estimate else "invoice"

    # 1. Check if client name is missing
    if not state.client_name:
        state.step = "awaiting_customer"
        return ChatResponse(
            reply=f"👤 Who is this {noun} for? (Enter name, or name + email for a new client)",
            action="clarification_needed",
        )

    # 2. Resolve customer details if not already done
    if not state.zoho_contact_id and not state.client_email:
        name, email, contact_id, is_new, clarification = await _resolve_manual_customer(
            state.client_name, state.client_email, db
        )
        state.client_name = name or state.client_name
        state.client_email = email or state.client_email
        state.zoho_contact_id = contact_id
        state.is_new_contact = is_new

        if clarification:
            if state.is_estimate:
                clarification = clarification.replace("invoice", "estimate")
            if is_new and not state.client_email:
                state.step = "awaiting_customer_email"
                return ChatResponse(reply=clarification, action="clarification_needed")
            state.step = "awaiting_customer"
            return ChatResponse(reply=clarification, action="clarification_needed")

    # 3. Check if new customer requires an email address
    if state.is_new_contact and not state.client_email:
        state.step = "awaiting_customer_email"
        return ChatResponse(
            reply=f"I couldn't find **{state.client_name}** in Zoho. Send their email address and I'll create them as a new customer (or type **skip** to continue without one).",
            action="clarification_needed",
        )

    # 4. Check if service/item name is missing
    if not state.pending_item_name:
        state.step = "awaiting_service_name"
        return ChatResponse(
            reply=f"📦 What service or work should I {verb} **{state.client_name}** for?",
            action="clarification_needed",
        )

    # 5. Check if amount is missing
    if state.pending_item_amount is None:
        state.step = "awaiting_item_amount"
        return ChatResponse(
            reply=f"💰 What's the amount for **{state.pending_item_name}**? (e.g. ₹5,000)",
            action="clarification_needed",
        )

    # 6. All fields satisfied! Instantiate ManualInvoiceLineItem, append to line_items and clear pending
    if not state.line_items:
        title_name = state.pending_item_name.strip().title()
        desc = state.pending_item_description or f"{title_name} Services"
        
        state.line_items = [
            ManualInvoiceLineItem(
                item_name=title_name,
                task_description=desc,
                amount=state.pending_item_amount
            )
        ]
        state.item_count = 1
        
    state.pending_item_name = None
    state.pending_item_description = None
    state.pending_item_amount = None

    # 7. Complete: build the manual draft/estimate draft card and reset state
    if state.is_estimate:
        draft = _build_estimate_draft_from_state(state)
        _manual_invoice_conversation = None
        return ChatResponse(
            reply=_estimate_summary(draft),
            action="estimate_pending",
            estimate_draft=draft,
        )
    else:
        draft = _build_manual_invoice_draft_from_state(state)
        _manual_invoice_conversation = None
        return ChatResponse(
            reply=_manual_invoice_summary(draft),
            action="manual_invoice_pending",
            manual_invoice_draft=draft,
        )


async def _handle_manual_invoice_conversation(
    message: str,
    db: AsyncSession,
) -> Optional[ChatResponse]:
    global _manual_invoice_conversation
    state = _manual_invoice_conversation
    if not state:
        return None

    text = (message or "").strip()

    # Cancel check
    clean_text = text.lower().strip(" .!?")
    if any(c in clean_text for c in ("cancel", "stop", "discard", "abort", "nevermind", "never mind", "cacell")):
        doc_type = "Estimate" if state.is_estimate else "Invoice"
        _manual_invoice_conversation = None
        return ChatResponse(
            reply=f"{doc_type} creation cancelled. Flow stopped.",
            action="clarification_needed",
        )

    if state.step == "awaiting_customer":
        proposed_name = text
        proposed_email = None
        email_match = _EMAIL_RE.search(text)
        if email_match:
            proposed_email = email_match.group()
            proposed_name = text.replace(proposed_email, "").strip()

        state.client_name = proposed_name
        state.client_email = proposed_email
        return await _advance_manual_invoice_conversation(db)

    if state.step == "awaiting_customer_email":
        if text.lower().strip() in ("skip", "none", "no email"):
            email = None
        else:
            email = text if _EMAIL_RE.fullmatch(text) else None
            if not email:
                return ChatResponse(
                    reply="Please send a valid email address, or type **skip** to continue without one.",
                    action="clarification_needed",
                )
        state.client_email = email
        state.zoho_contact_id = None  # force re-check
        return await _advance_manual_invoice_conversation(db)

    if state.step == "awaiting_service_name":
        item_name = text.strip()
        state.pending_item_name = item_name
        state.pending_item_description = f"{item_name} Services"
        return await _advance_manual_invoice_conversation(db)

    if state.step == "awaiting_item_amount":
        amount, currency = _parse_amount_and_currency(text)
        if amount is None:
            return ChatResponse(
                reply="Send the amount as a number, for example `5000`, `₹5000`, or `1200 USD`.",
                action="clarification_needed",
            )
        if currency:
            state.currency = currency
        state.pending_item_amount = amount
        return await _advance_manual_invoice_conversation(db)

    return None


async def clear_session_state(db: AsyncSession) -> None:
    """Wipe ALL in-memory caches and every DB cache table.
    Called on logout so a freshly-logged-in account starts completely clean.
    """
    # ── In-memory ──────────────────────────────────────────────────────────
    global _manual_invoice_conversation
    _pending_drafts.clear()
    _pending_batches.clear()
    _pending_manual_invoice_drafts.clear()
    _pending_estimate_drafts.clear()
    _pending_payment_drafts.clear()
    _pending_payment_disambiguations.clear()
    _pending_estimate_disambiguations.clear()
    _pending_payment_customer_prompts.clear()
    _recent_invoices.clear()
    _recent_estimates.clear()
    _manual_invoice_conversation = None
    clear_org_id_cache()          # force fresh org-ID fetch on next Zoho call

    # ── Database ───────────────────────────────────────────────────────────
    # OAuthToken rows are deleted by the auth_router before calling here;
    # we still include them for safety.
    for table in (OAuthToken, ProcessedEmail, ContactCache, InvoiceCache):
        await db.execute(delete(table))
    await db.commit()
    logger.info("Session cleared — all caches and tokens wiped.")


def _mark_invoice_sent(zoho_invoice_id: str) -> None:
    """Persist email_sent on the in-memory invoice record."""
    for i, inv in enumerate(_recent_invoices):
        if inv.zoho_invoice_id == zoho_invoice_id:
            _recent_invoices[i] = inv.model_copy(update={"email_sent": True})
            break


def _payment_invoices(rows: list[dict]) -> list[PaymentInvoice]:
    return [PaymentInvoice(**r) for r in rows]


async def _handle_payment_query(
    action: str,
    person_name: Optional[str],
    db: AsyncSession,
    emit=None,
) -> ChatResponse:
    """Shared path: always pull fresh data from Zoho, run query, format reply."""
    _emit = emit or (lambda _: None)
    try:
        await _emit("🔄 Syncing payment data from Zoho…")
        await ensure_fresh_cache(db, force=True)
    except RuntimeError as e:
        return ChatResponse(reply=str(e), action="error")

    if action == "payment_summary" and person_name:
        action = "check_specific_payment"

    if action == "check_overdue":
        rows = await get_overdue(db)
        if person_name:
            p_lower = person_name.lower().strip()
            rows = [r for r in rows if p_lower in r.get("customer_name", "").lower()]
        reply = format_payment_response(
            rows,
            title=f"**Overdue invoices for {person_name}:**" if person_name else "**Overdue invoices** (oldest first):",
            empty_message=f"Good news — **{person_name}** has no overdue invoices right now. ✅" if person_name else "Good news — you have no overdue invoices right now. ✅",
        )
        return ChatResponse(reply=reply, action="payment_status", payment_invoices=_payment_invoices(rows))

    if action == "check_pending":
        overdue_rows = await get_overdue(db)
        pending_rows = await get_pending(db)
        rows = overdue_rows + pending_rows
        if person_name:
            p_lower = person_name.lower().strip()
            rows = [r for r in rows if p_lower in r.get("customer_name", "").lower()]
        reply = format_payment_response(
            rows,
            title=f"**Unpaid invoices for {person_name}:**" if person_name else "**Unpaid invoices:**",
            empty_message=f"No unpaid invoices for **{person_name}** — everything has been paid. ✅" if person_name else "No unpaid invoices — everything has been paid. ✅",
        )
        return ChatResponse(reply=reply, action="payment_status", payment_invoices=_payment_invoices(rows))

    if action == "check_specific_payment":
        if not person_name:
            return ChatResponse(
                reply="Which client should I check? Try *\"Did Rahul pay?\"* or *\"Has Piyusha paid?\"*",
                action="clarification_needed",
            )
        rows = await get_client_payments(db, person_name)
        reply = format_client_payment_response(person_name, rows)
        return ChatResponse(reply=reply, action="payment_status", payment_invoices=_payment_invoices(rows))

    if action == "payment_summary":
        summary = await get_payment_summary(db)
        reply = format_payment_summary(summary)
        overdue = await get_overdue(db)
        pending = await get_pending(db)
        cards = _payment_invoices(overdue + pending)
        return ChatResponse(reply=reply, action="payment_status", payment_invoices=cards or None)

    return ChatResponse(reply="Unknown payment query.", action="error")


async def _handle_remind_overdue(
    person_name: Optional[str],
    days_overdue_min: int,
    db: AsyncSession,
    emit=None,
) -> ChatResponse:
    """Send payment reminders to overdue clients, with 5-day spam cooldown."""
    import time
    import math
    from sqlalchemy import update as sa_update

    _emit = emit or (lambda _: None)
    await _emit("🔄 Syncing fresh data from Zoho before sending reminders…")
    try:
        await ensure_fresh_cache(db, force=True)
    except RuntimeError as e:
        return ChatResponse(reply=str(e), action="error")

    all_overdue = await get_overdue(db)

    # Step 1 — filter by days_overdue threshold AND optional client_name
    p_lower = person_name.lower().strip() if person_name else None
    criteria_match = [
        r for r in all_overdue
        if (r.get("days_overdue") or 0) >= days_overdue_min
        and (p_lower is None or p_lower in r.get("customer_name", "").lower())
    ]

    # Case A: Nothing matches the basic overdue threshold / client filter
    if not criteria_match:
        if person_name:
            return ChatResponse(
                reply=f"No overdue invoices found for **{person_name}** that are "
                      f">= {days_overdue_min} days overdue. ✅",
                action="payment_status",
            )
        return ChatResponse(
            reply=f"No overdue invoices >= {days_overdue_min} days overdue right now. ✅",
            action="payment_status",
        )

    # Step 2 — apply 5-day cooldown filter
    cooldown_secs = 5 * 24 * 3600
    now_ts = time.time()
    eligible = [
        r for r in criteria_match
        if r.get("last_reminded_at") is None
        or r["last_reminded_at"] < now_ts - cooldown_secs
    ]
    on_cooldown = [r for r in criteria_match if r not in eligible]

    # Case B: All matching invoices are still within the 5-day cooldown
    if not eligible:
        # Find the one that will exit cooldown soonest
        soonest = min(
            on_cooldown,
            key=lambda r: r.get("last_reminded_at") or 0,
            default=None,
        )
        days_left = 0
        if soonest and soonest.get("last_reminded_at"):
            days_left = max(
                1,
                math.ceil((soonest["last_reminded_at"] + cooldown_secs - now_ts) / 86400),
            )
        subject = f" for **{person_name}**" if person_name else ""
        return ChatResponse(
            reply=(
                f"All matching overdue invoices{subject} were already reminded within "
                f"the last 5 days. \u23f3 You can send the next reminder in "
                f"**{days_left} day{'s' if days_left != 1 else ''}**."
            ),
            action="payment_status",
            payment_invoices=_payment_invoices(on_cooldown),
        )

    # Step 3 — send reminders in chunks of 10 (Zoho bulk limit)
    await _emit(f"📧 Sending reminders for {len(eligible)} invoice(s)…")
    eligible_ids = [r["invoice_id"] for r in eligible]
    all_succeeded: list[str] = []
    all_failed: list[tuple[str, str]] = []

    for chunk_start in range(0, len(eligible_ids), 10):
        chunk = eligible_ids[chunk_start:chunk_start + 10]
        result = await bulk_remind_invoices(chunk, db)
        all_succeeded.extend(result["succeeded"])
        all_failed.extend(result["failed"])

    # Step 4 — update last_reminded_at ONLY for succeeded IDs
    if all_succeeded:
        await db.execute(
            sa_update(InvoiceCache)
            .where(InvoiceCache.invoice_id.in_(all_succeeded))
            .values(last_reminded_at=int(now_ts))
        )
        await db.commit()

    # Step 5 — build reply
    succeeded_rows = [r for r in eligible if r["invoice_id"] in set(all_succeeded)]
    failed_ids     = {fid for fid, _ in all_failed}
    failed_rows    = [r for r in eligible if r["invoice_id"] in failed_ids]

    parts: list[str] = []
    if succeeded_rows:
        lines = [f"✅ Reminder sent to **{r['customer_name']}** "
                 f"({r.get('days_overdue', '?')} days overdue, "
                 f"\u20b9{float(r['balance']):,.2f} outstanding)" for r in succeeded_rows]
        parts.append(f"Reminded **{len(succeeded_rows)}** invoice(s):\n" + "\n".join(lines))

    if failed_rows:
        fail_lines = []
        for r in failed_rows:
            reason = next((msg for fid, msg in all_failed if fid == r["invoice_id"]), "unknown error")
            fail_lines.append(f"\u26a0\ufe0f **{r['customer_name']}** — {reason[:120]}")
        parts.append(f"\n{len(failed_rows)} reminder(s) failed:\n" + "\n".join(fail_lines))

    reply = "\n".join(parts) if parts else "No reminders were sent."
    return ChatResponse(
        reply=reply,
        action="invoice_sent" if all_succeeded else "error",
        payment_invoices=_payment_invoices(succeeded_rows + failed_rows) or None,
    )


async def _handle_record_payment(
    person_name: Optional[str],
    amount: Optional[float],
    payment_mode: str,
    payment_date: str,
    db: AsyncSession,
    session_key: str | int = "web",
    emit=None,
) -> ChatResponse:
    """
    Look up client's outstanding invoices.
    If multiple: return ambiguous_invoices.
    If exactly one: build a PaymentRecordDraft, store in _pending_payment_drafts, and ask user to confirm.
    """
    import time
    import httpx
    _emit = emit or (lambda _: None)
    if not person_name:
        _pending_payment_customer_prompts[session_key] = {
            "amount": amount,
            "payment_mode": payment_mode,
            "payment_date": payment_date,
        }
        return ChatResponse(
            reply="Who is the payment from? Please specify a client name (e.g. *\"record payment from Vismay\"*).",
            action="clarification_needed",
        )

    await _emit("🔄 Syncing payment data from Zoho...")
    try:
        await ensure_fresh_cache(db, force=True)
    except Exception as e:
        logger.error(f"Cache sync failed in record_payment: {e}")
        return ChatResponse(reply=f"Failed to sync data from Zoho: {e}", action="error")

    # Search for unpaid/partially-paid invoices for this client
    pattern = f"%{person_name.lower().strip()}%"
    result = await db.execute(
        select(InvoiceCache)
        .where(
            func.lower(InvoiceCache.customer_name).like(pattern),
            InvoiceCache.status.in_(("overdue", "sent", "partially_paid", "unpaid")),
            InvoiceCache.balance > 0,
        )
        .order_by(InvoiceCache.due_date.asc().nulls_last())
    )
    unpaid_invoices = result.scalars().all()

    if not unpaid_invoices:
        # Check if client even exists or has paid invoices
        contact_res = await db.execute(
            select(InvoiceCache)
            .where(func.lower(InvoiceCache.customer_name).like(pattern))
        )
        all_invoices = contact_res.scalars().all()
        if not all_invoices:
            return ChatResponse(
                reply=f"I couldn't find any contact or invoices matching **{person_name}** in your records.",
                action="clarification_needed",
            )
        else:
            cust_name = all_invoices[0].customer_name
            return ChatResponse(
                reply=f"Good news — **{cust_name}** has no unpaid invoices right now. ✅",
                action="payment_status",
            )

    customer_name = unpaid_invoices[0].customer_name

    # If multiple unpaid invoices, disambiguate
    if len(unpaid_invoices) > 1:
        # Resolve customer ID beforehand so we have it for selection
        contact_cache_res = await db.execute(
            select(ContactCache.zoho_contact_id).where(ContactCache.name_lower == customer_name.lower().strip())
        )
        customer_id = contact_cache_res.scalar()

        if not customer_id:
            try:
                contacts = await search_contact_by_name(customer_name, db)
                if contacts:
                    customer_id = contacts[0]["contact_id"]
            except Exception as e:
                logger.warning(f"Could not resolve customer_id for manual payment record: {e}")

        # Save in disambiguation dictionary
        _pending_payment_disambiguations[session_key] = {
            "customer_name": customer_name,
            "customer_id": customer_id or "",
            "amount": amount,
            "payment_mode": payment_mode,
            "payment_date": payment_date,
            "invoices": unpaid_invoices,
        }

        payment_invs = []
        reply_lines = [f"I found {len(unpaid_invoices)} unpaid invoices for **{customer_name}** — which one should I apply the payment to?\n"]
        for idx, inv in enumerate(unpaid_invoices, 1):
            currency = inv.currency_code or "INR"
            balance_fmt = f"₹{float(inv.balance):,.2f}" if currency == "INR" else f"{currency} {float(inv.balance):,.2f}"
            due_fmt = inv.due_date.isoformat() if inv.due_date else "no due date"
            reply_lines.append(f"{idx}. Invoice **#{inv.invoice_id}** — {balance_fmt} (due: {due_fmt})")
            
            days_overdue = None
            if inv.status == "overdue" and inv.due_date:
                from datetime import date as _date
                days_overdue = max(0, (_date.today() - inv.due_date).days)
                
            payment_invs.append(
                PaymentInvoice(
                    invoice_id=inv.invoice_id,
                    customer_name=inv.customer_name,
                    status=inv.status,
                    due_date=inv.due_date.isoformat() if inv.due_date else None,
                    balance=float(inv.balance),
                    total=float(inv.total),
                    currency_code=inv.currency_code or "INR",
                    zoho_view_url=inv.zoho_view_url,
                    days_overdue=days_overdue,
                    last_reminded_at=inv.last_reminded_at,
                )
            )
        return ChatResponse(
            reply="\n".join(reply_lines),
            action="clarification_needed",
            ambiguous_invoices=payment_invs,
        )

    # Exactly one matching unpaid invoice
    inv = unpaid_invoices[0]
    
    # Resolve customer ID
    contact_cache_res = await db.execute(
        select(ContactCache.zoho_contact_id).where(ContactCache.name_lower == customer_name.lower().strip())
    )
    customer_id = contact_cache_res.scalar()

    if not customer_id:
        try:
            contacts = await search_contact_by_name(customer_name, db)
            if contacts:
                customer_id = contacts[0]["contact_id"]
        except Exception as e:
            logger.warning(f"Could not resolve customer_id for manual payment record: {e}")

    if not customer_id:
        try:
            from backend.services.zoho_service import _headers as _z_headers, get_org_id as _z_get_org_id
            headers = await _z_headers(db)
            org_id = await _z_get_org_id(db)
            resp = await client.get(
                f"{settings.zoho_api_base}/invoices/{inv.invoice_id}",
                headers=headers,
                params={"organization_id": org_id},
            )
            if resp.is_success:
                invoice_detail = resp.json().get("invoice", {})
                customer_id = invoice_detail.get("customer_id")
        except Exception as e:
            logger.error(f"Failed to fetch customer_id from Zoho for invoice {inv.invoice_id}: {e}")

    if not customer_id:
        return ChatResponse(
            reply=f"Could not resolve the Zoho customer ID for **{customer_name}**. Please ensure Zoho is connected.",
            action="error",
        )

    # Determine amount
    actual_amount = float(inv.balance) if amount is None else amount
    
    # Overpayment check
    is_overpayment = actual_amount > float(inv.balance)
    overpayment_warn = ""
    if is_overpayment:
        overpayment_warn = f"\n\n⚠️ **Warning**: The payment amount (₹{actual_amount:,.2f}) exceeds the outstanding balance (₹{float(inv.balance):,.2f}) of this invoice. Approving this will create an overpayment credit in Zoho."

    draft_id = str(uuid.uuid4())[:8]
    
    draft = PaymentRecordDraft(
        draft_id=draft_id,
        invoice_id=inv.invoice_id,
        customer_id=customer_id,
        customer_name=customer_name,
        invoice_number=inv.invoice_id,
        amount=actual_amount,
        payment_mode=payment_mode,
        payment_date=payment_date,
        currency=inv.currency_code or "INR",
    )
    
    _pending_payment_drafts[draft_id] = draft

    currency_sym = "₹" if draft.currency == "INR" else f"{draft.currency} "
    reply = (
        f"I've prepared a draft to record a manual payment for **{customer_name}**:\n\n"
        f"• Invoice ID: **{draft.invoice_id}**\n"
        f"• Amount: **{currency_sym}{draft.amount:,.2f}**\n"
        f"• Method: **{draft.payment_mode.upper()}**\n"
        f"• Date: **{draft.payment_date}**"
        f"{overpayment_warn}\n\n"
        "Confirm below if this looks correct. ✓"
    )
    
    return ChatResponse(
        reply=reply,
        action="payment_record_pending",
        payment_record_draft=draft,
    )


def _should_auto_create(data: InvoiceData, contact_id: Optional[str]) -> bool:
    # Always show a draft card so the user can review item name, amount, description
    # before the invoice is created in Zoho.
    return False


# ── Friendly date labels for natural replies ─────────────────────────────────
_DATE_LABELS: dict[str, str] = {
    "today": "today",
    "yesterday": "yesterday",
    "this_week": "this week",
    "last_week": "last week",
    "last_monday": "last Monday",
    "last_tuesday": "last Tuesday",
    "last_wednesday": "last Wednesday",
    "last_thursday": "last Thursday",
    "last_friday": "last Friday",
    "last_saturday": "last Saturday",
    "last_sunday": "last Sunday",
    "this_monday": "this Monday",
    "this_tuesday": "this Tuesday",
    "this_wednesday": "this Wednesday",
    "this_thursday": "this Thursday",
    "this_friday": "this Friday",
    "this_saturday": "this Saturday",
    "this_sunday": "this Sunday",
}


# ─────────────────────────────────────────────────────────────────────────────
# Recurring invoice handlers
# ─────────────────────────────────────────────────────────────────────────────

def _recurring_confirm_text(conv: RecurringConversation) -> str:
    """Build the confirm-card summary for a recurring invoice draft."""
    from datetime import date as _date
    start = conv.start_date or "today"
    if start == "today":
        start = _date.today().isoformat()
    freq_emoji = {"monthly": "📅", "weekly": "📅", "yearly": "📅", "daily": "📅"}.get(conv.frequency or "monthly", "📅")
    lines = [
        "📋 **Recurring Invoice Draft**",
        "",
        f"👤 **Client:**  {conv.client_name or '—'}",
    ]
    if conv.client_email:
        lines.append(f"📧 **Email:**   {conv.client_email}")
    lines += [
        f"💰 **Amount:**  {conv.currency} {conv.amount:,.2f} / {conv.frequency or 'month'}",
        f"{freq_emoji} **Frequency:** {(conv.frequency or 'monthly').capitalize()}",
        f"📆 **Starts:**  {start}",
        f"🏁 **Ends:**    {conv.end_date or 'No end date'}",
    ]
    if conv.item_name:
        lines.append(f"📦 **Service:**  {conv.item_name}")
    return "\n".join(lines)


async def _advance_recurring_conversation(
    conv: RecurringConversation,
    db: AsyncSession,
    session_key: str | int,
) -> ChatResponse:
    # 1. Check if client name is missing
    if not conv.client_name:
        conv = conv.model_copy(update={"step": "client"})
        _pending_recurring_conv[session_key] = conv
        return ChatResponse(
            reply="Sure! Let's set up a recurring invoice.\n\n👤 Who is the client? (name or email)",
            action="clarification_needed",
        )

    # 2. Resolve client details if not already done
    if not conv.zoho_contact_id and not conv.client_email:
        name, email, contact_id, is_new, clarification = await _resolve_manual_customer(
            conv.client_name, conv.client_email, db
        )
        conv = conv.model_copy(update={
            "client_name": name or conv.client_name,
            "client_email": email or conv.client_email,
            "zoho_contact_id": contact_id,
            "is_new_contact": is_new,
        })
        if clarification:
            if is_new and not conv.client_email:
                conv = conv.model_copy(update={"step": "awaiting_customer_email"})
                _pending_recurring_conv[session_key] = conv
                return ChatResponse(reply=clarification, action="clarification_needed")
            conv = conv.model_copy(update={"step": "client"})
            _pending_recurring_conv[session_key] = conv
            return ChatResponse(reply=clarification, action="clarification_needed")

    # 3. Check if new customer requires an email address
    if conv.is_new_contact and not conv.client_email:
        conv = conv.model_copy(update={"step": "awaiting_customer_email"})
        _pending_recurring_conv[session_key] = conv
        return ChatResponse(
            reply=f"📧 **{conv.client_name}** isn't in your Zoho contacts yet. What's their email address so I can create them?\n\n_(Reply **skip** to proceed without an email)_",
            action="clarification_needed",
        )

    # 4. Check if item/service name is missing
    if not conv.item_name:
        conv = conv.model_copy(update={"step": "item_name"})
        _pending_recurring_conv[session_key] = conv
        return ChatResponse(
            reply="📦 What is the service or item name? (e.g. **Monthly Website Maintenance**)",
            action="clarification_needed",
        )

    # 5. Check if amount is missing
    if conv.amount is None:
        conv = conv.model_copy(update={"step": "amount"})
        _pending_recurring_conv[session_key] = conv
        return ChatResponse(
            reply=f"Got it — for **{conv.item_name}**. 💰 What's the amount and currency? (e.g. ₹15,000)",
            action="clarification_needed",
        )

    # 6. Check if frequency is missing
    if not conv.frequency:
        conv = conv.model_copy(update={"step": "frequency"})
        _pending_recurring_conv[session_key] = conv
        return ChatResponse(
            reply="🔁 How often should the invoice repeat?\n**monthly / weekly / yearly / daily**",
            action="clarification_needed",
        )

    # 7. Check if start_date is missing
    if not conv.start_date:
        conv = conv.model_copy(update={"step": "start_date"})
        _pending_recurring_conv[session_key] = conv
        return ChatResponse(
            reply="📆 When should it start? (e.g. **today**, **2025-07-01**)",
            action="clarification_needed",
        )

    # Complete! Show confirmation prompt
    conv = conv.model_copy(update={"step": "confirm"})
    _pending_recurring_conv[session_key] = conv
    return ChatResponse(
        reply=_recurring_confirm_text(conv) + "\n\nReply **confirm** to create or **cancel** to discard.",
        action="recurring_pending",
        recurring_draft=conv,
    )


async def _handle_recurring_create(
    message: str,
    db: AsyncSession,
    emit,
    session_key: str | int,
) -> ChatResponse:
    """
    Multi-step conversation to create a recurring invoice.
    Also handles one-shot creation when all fields are in the first message.
    """
    from datetime import date as _date
    conv = _pending_recurring_conv.get(session_key)

    if conv:
        step = conv.step
        msg = message.strip()

        # Check if the user wants to cancel mid-conversation (with typo support!)
        clean_msg = msg.lower().strip(" .!?")
        if any(c in clean_msg for c in ("cancel", "stop", "discard", "abort", "nevermind", "never mind", "cacell")):
            _pending_recurring_conv.pop(session_key, None)
            return ChatResponse(
                reply="Recurring invoice draft cancelled. ✗",
                action="clarification_needed",
            )

        if step == "client":
            raw_client = msg
            email_match = _EMAIL_RE.search(raw_client)
            proposed_email = None
            proposed_name = raw_client
            if email_match:
                proposed_email = email_match.group(0)
                proposed_name = raw_client.replace(proposed_email, "").strip() or proposed_email.split("@")[0]

            conv = conv.model_copy(update={
                "client_name": proposed_name,
                "client_email": proposed_email,
            })
            return await _advance_recurring_conversation(conv, db, session_key)

        if step == "awaiting_customer_email" or step == "confirm_email":
            email_match = _EMAIL_RE.search(msg)
            if not email_match and msg.lower().strip() != "skip":
                return ChatResponse(
                    reply="Please enter a valid email address, or say **skip** to proceed without an email.",
                    action="clarification_needed",
                )
            email = email_match.group(0) if email_match else None
            conv = conv.model_copy(update={
                "client_email": email,
                "zoho_contact_id": None,  # force re-resolve
            })
            return await _advance_recurring_conversation(conv, db, session_key)

        if step == "item_name":
            conv = conv.model_copy(update={"item_name": msg})
            return await _advance_recurring_conversation(conv, db, session_key)

        if step == "amount":
            nums = re.findall(r"[\d,]+(?:\.\d+)?", msg.replace(",", ""))
            currency = "INR" if any(c in msg for c in ("₹", "rs", "inr")) else (
                "USD" if "$" in msg else conv.currency
            )
            if nums:
                amount = float(nums[0].replace(",", ""))
                conv = conv.model_copy(update={"amount": amount, "currency": currency})
                return await _advance_recurring_conversation(conv, db, session_key)
            return ChatResponse(reply="I didn't catch the amount. Please enter a number (e.g. ₹15000 or 500).", action="clarification_needed")

        if step == "frequency":
            freq_map = {"monthly": "monthly", "month": "monthly", "weekly": "weekly", "week": "weekly",
                        "yearly": "yearly", "year": "yearly", "annual": "yearly", "daily": "daily", "day": "daily"}
            freq = freq_map.get(msg.lower().strip())
            if not freq:
                return ChatResponse(reply="Please choose: **monthly**, **weekly**, **yearly**, or **daily**.", action="clarification_needed")
            conv = conv.model_copy(update={"frequency": freq})
            return await _advance_recurring_conversation(conv, db, session_key)

        if step == "start_date":
            start = parse_relative_date(msg)
            if not start:
                return ChatResponse(
                    reply="I couldn't parse that date. Please enter a date in **YYYY-MM-DD** format, or say **today**, **tomorrow**, **next week**, or **next month**.",
                    action="clarification_needed"
                )
            conv = conv.model_copy(update={"start_date": start})
            return await _advance_recurring_conversation(conv, db, session_key)

        if step == "confirm":
            if any(w in msg.lower() for w in ("confirm", "yes", "create", "go", "ok", "do it", "proceed")):
                return await _execute_recurring_create(conv, db, emit, session_key)
            _pending_recurring_conv.pop(session_key, None)
            return ChatResponse(reply="Recurring invoice cancelled. ✗", action="clarification_needed")

    # ── No active conversation — try to extract from message ──────────────────
    await emit("🧠 Extracting recurring invoice details…")
    extracted = await extract_recurring_details(message)

    start_raw = extracted.get("start_date")
    if start_raw:
        parsed_start = parse_relative_date(start_raw)
        if parsed_start:
            start_raw = parsed_start
        else:
            start_raw = None

    conv = RecurringConversation(
        client_name=extracted.get("client_name"),
        client_email=extracted.get("client_email"),
        item_name=extracted.get("item_name"),
        task_description=extracted.get("task_description"),
        amount=extracted.get("amount"),
        currency=extracted.get("currency") or "INR",
        frequency=extracted.get("frequency"),
        start_date=start_raw,
        end_date=extracted.get("end_date"),
    )
    return await _advance_recurring_conversation(conv, db, session_key)



async def _execute_recurring_create(
    conv: RecurringConversation,
    db: AsyncSession,
    emit,
    session_key: str | int,
) -> ChatResponse:
    """Create the recurring invoice in Zoho and clear conversation state."""
    from datetime import date as _date
    _pending_recurring_conv.pop(session_key, None)

    await emit("📡 Looking up client in Zoho…")
    contact_id = conv.zoho_contact_id
    if not contact_id and conv.client_name:
        try:
            matches = await search_contact_by_name(conv.client_name, db)
            if matches:
                contact_id = matches[0]["contact_id"]
                if not conv.client_email:
                    conv = conv.model_copy(update={"client_email": matches[0].get("email")})
        except Exception:
            pass
    if not contact_id:
        try:
            contact_id = await create_contact(conv.client_name or "Unknown", conv.client_email, db)
        except Exception as e:
            return ChatResponse(reply=f"⚠️ Couldn't create/find Zoho contact: {e}", action="error")

    start = conv.start_date or _date.today().isoformat()
    item  = conv.item_name or conv.task_description or f"{conv.frequency or 'Monthly'} Service"

    await emit("📡 Creating recurring invoice in Zoho…")
    try:
        result = await create_recurring_invoice(
            contact_id=contact_id,
            item_name=item,
            amount=conv.amount or 0,
            currency=conv.currency,
            frequency=conv.frequency or "monthly",
            start_date=start,
            db=db,
            task_description=conv.task_description,
            end_date=conv.end_date,
        )
    except Exception as e:
        return ChatResponse(reply=f"⚠️ Failed to create recurring invoice: {e}", action="error")

    rec_id  = result.get("recurring_invoice_id", "")
    rec_name = result.get("recurrence_name", item)
    rec_url = result.get("recurring_invoice_url", "")
    return ChatResponse(
        reply=(
            f"✅ **Recurring invoice created!**\n\n"
            f"📋 **{rec_name}**\n"
            f"👤 {conv.client_name}  |  {conv.currency} {conv.amount:,.2f} / {conv.frequency}\n"
            f"📆 Starts: {start}"
            + (f"\n🏁 Ends: {conv.end_date}" if conv.end_date else "")
            + f"\n\n🔗 **[View Recurring Profile in Zoho]({rec_url})**"
        ),
        action="recurring_created",
    )


async def _handle_recurring_list(
    db: AsyncSession,
    emit,
    session_key: str | int,
) -> ChatResponse:
    """Fetch and display all active recurring invoices."""
    await emit("📡 Fetching active recurring invoices…")
    try:
        invoices = await list_recurring_invoices(db)
    except Exception as e:
        return ChatResponse(reply=f"⚠️ Couldn't fetch recurring invoices: {e}", action="error")

    if not invoices:
        _pending_recurring_list.pop(session_key, None)
        return ChatResponse(reply="You have no active recurring invoices.", action="recurring_list")

    _pending_recurring_list[session_key] = invoices

    lines = ["🔁 **Active Recurring Invoices**\n"]
    for i, inv in enumerate(invoices, 1):
        amount   = inv.get("total", inv.get("amount", 0))
        freq     = inv.get("recurrence_frequency", "monthly")
        customer = inv.get("customer_name", "Unknown")
        name     = inv.get("recurrence_name", customer)
        start    = inv.get("start_date", "")
        url      = inv.get("recurring_invoice_url", "")
        lines.append(
            f"**{i}.** [{name}]({url})\n"
            f"   👤 {customer}  💰 {inv.get('currency_code','INR')} {amount:,.2f} / {freq}"
            + (f"\n   📆 Since {start}" if start else "")
        )
    lines.append("\nTap **⏹ Stop** on a row, or reply with numbers to stop (e.g. **1 2**).")

    return ChatResponse(reply="\n".join(lines), action="recurring_list")


async def _handle_recurring_stop(
    message: str,
    db: AsyncSession,
    emit,
    session_key: str | int,
) -> ChatResponse:
    """Stop one or more recurring invoices by number or show the list first."""
    # Check if user gave numbers (e.g. "1 2" or "stop 1") referring to cached list
    cached = _pending_recurring_list.get(session_key)
    numbers = [int(t) for t in re.findall(r"\b(\d+)\b", message) if int(t) >= 1]

    if cached and numbers:
        stopped, failed = [], []
        for n in numbers:
            if 1 <= n <= len(cached):
                inv = cached[n - 1]
                rid = inv.get("recurring_invoice_id")
                name = inv.get("recurrence_name") or inv.get("customer_name", str(n))
                await emit(f"⏹ Stopping {name}…")
                ok = await stop_recurring_invoice(rid, db)
                (stopped if ok else failed).append(name)

        parts = []
        if stopped:
            parts.append(f"✅ Stopped {len(stopped)} recurring invoice(s): {', '.join(stopped)}")
        if failed:
            parts.append(f"⚠️ Failed to stop: {', '.join(failed)}")
        _pending_recurring_list.pop(session_key, None)
        return ChatResponse(reply="\n".join(parts) or "Nothing changed.", action="recurring_stopped")

    # No cached list or no numbers — show the list first
    list_resp = await _handle_recurring_list(db, emit, session_key)
    if list_resp.action != "recurring_list" or not _pending_recurring_list.get(session_key):
        return list_resp
    list_resp = list_resp.model_copy(update={
        "reply": list_resp.reply + "\n\n🛑 Reply with the **number(s)** you want to stop."
    })
    return list_resp



def _estimate_url(estimate_id: str) -> str:
    base = _ZOHO_APP_BASE.get(settings.ZOHO_REGION, "https://invoice.zoho.com")
    return f"{base}/app#/estimates/{estimate_id}"


def _format_estimate_amount(est: dict) -> str:
    currency = est.get("currency_code") or "INR"
    total = float(est.get("total") or 0.0)
    if currency == "INR":
        return f"₹{total:,.2f}"
    return f"{currency} {total:,.2f}"


def _estimate_matches_ref(est: dict, estimate_ref: str) -> bool:
    ref = estimate_ref.lower().replace(" ", "")
    number = (est.get("estimate_number") or "").lower().replace(" ", "")
    est_id = (est.get("estimate_id") or "").lower()
    return ref in number or ref in est_id or number.endswith(ref.lstrip("#"))


def _estimate_matches_person(est: dict, person_name: str) -> bool:
    return person_name.lower().strip() in (est.get("customer_name") or "").lower()


def _mark_estimate_status(estimate_id: str, status: str) -> None:
    for i, est in enumerate(_recent_estimates):
        if est.zoho_estimate_id == estimate_id:
            _recent_estimates[i] = est.model_copy(update={"status": status})
            break


async def _fetch_estimates_by_statuses(
    db: AsyncSession,
    statuses: set[str],
) -> list[dict]:
    """Fetch estimates from Zoho for one or more statuses, deduplicated by ID."""
    filter_map = {
        "sent": "Status.Sent",
        "accepted": "Status.Accepted",
        "declined": "Status.Declined",
        "draft": "Status.Draft",
    }
    seen: dict[str, dict] = {}
    for status in statuses:
        filter_by = filter_map.get(status)
        if not filter_by:
            continue
        try:
            rows = await list_estimates(db, filter_by=filter_by)
        except Exception as e:
            logger.warning(f"Could not list estimates with {filter_by}: {e}")
            continue
        for row in rows:
            est_id = row.get("estimate_id")
            if est_id and est_id not in seen:
                seen[est_id] = row
    return list(seen.values())


async def _resolve_estimate_candidates(
    db: AsyncSession,
    purpose: str,
    person_name: Optional[str] = None,
    estimate_ref: Optional[str] = None,
) -> list[dict]:
    """
    Find estimates relevant to accept/reject/convert/send actions.
    Checks session history first, then queries Zoho across relevant statuses.
    """
    allowed_by_purpose = {
        "accept": {"sent", "draft", "declined"},
        "reject": {"sent", "draft", "accepted"},
        "convert": {"accepted"},
        "send": {"accepted"},
    }
    allowed_statuses = allowed_by_purpose.get(purpose, {"accepted"})

    candidates: list[dict] = []
    seen_ids: set[str] = set()

    # Session estimates — re-fetch current status from Zoho when possible
    for recent in reversed(_recent_estimates):
        if recent.zoho_estimate_id in seen_ids:
            continue
        try:
            details = await get_estimate(recent.zoho_estimate_id, db)
            if not details:
                continue
            status = (details.get("status") or "").lower()
            if status in ("invoiced", "partially_invoiced", "expired"):
                continue
            if status not in allowed_statuses:
                continue
            if person_name and not _estimate_matches_person(details, person_name):
                continue
            if estimate_ref and not _estimate_matches_ref(details, estimate_ref):
                continue
            candidates.append(details)
            seen_ids.add(recent.zoho_estimate_id)
        except Exception as e:
            logger.warning(f"Could not refresh session estimate {recent.zoho_estimate_id}: {e}")

    zoho_rows = await _fetch_estimates_by_statuses(db, allowed_statuses)
    for row in zoho_rows:
        est_id = row.get("estimate_id")
        if not est_id or est_id in seen_ids:
            continue
        status = (row.get("status") or "").lower()
        if status in ("invoiced", "partially_invoiced", "expired"):
            continue
        if person_name and not _estimate_matches_person(row, person_name):
            continue
        if estimate_ref and not _estimate_matches_ref(row, estimate_ref):
            continue
        candidates.append(row)
        seen_ids.add(est_id)

    # Most recent first — Zoho list is usually newest-first; session hits are already ordered
    candidates.sort(
        key=lambda e: e.get("date") or e.get("created_time") or "",
        reverse=True,
    )
    return candidates


def _ambiguous_estimate_cards(estimates: list[dict]) -> list[AmbiguousEstimate]:
    cards = []
    for e in estimates:
        cards.append(AmbiguousEstimate(
            estimate_id=e["estimate_id"],
            estimate_number=e.get("estimate_number") or "",
            customer_name=e.get("customer_name") or "",
            status=e.get("status") or "",
            amount=float(e.get("total") or 0.0),
            currency=e.get("currency_code") or "INR",
            date=e.get("date"),
            estimate_url=_estimate_url(e.get("estimate_id", "")),
        ))
    return cards


def _estimate_disambiguation_reply(
    estimates: list[dict],
    purpose: str,
) -> str:
    verb = {
        "accept": "accept",
        "reject": "reject/decline",
        "convert": "convert to an invoice",
        "send": "convert and send an invoice for",
    }.get(purpose, "select")
    lines = [f"I found multiple estimates. Which one would you like to {verb}? Reply with the number:"]
    for idx, e in enumerate(estimates, 1):
        status = (e.get("status") or "unknown").capitalize()
        lines.append(
            f"{idx}. Estimate **#{e.get('estimate_number')}** for **{e.get('customer_name')}** "
            f"— {_format_estimate_amount(e)} ({status})"
        )
    return "\n".join(lines)


async def _build_invoice_draft_from_estimate(details: dict) -> ManualInvoiceDraft:
    raw_items = details.get("line_items") or []
    line_items = [
        ManualInvoiceLineItem(
            item_name=item.get("name") or "Service",
            task_description=item.get("description") or "",
            amount=float(item.get("rate") or 0.0),
        )
        for item in raw_items
    ]
    draft_id = str(uuid.uuid4())
    draft = ManualInvoiceDraft(
        draft_id=draft_id,
        client_name=details.get("customer_name") or "Unknown",
        client_email=details.get("email"),
        currency=details.get("currency_code") or "INR",
        zoho_contact_id=details.get("customer_id"),
        is_new_contact=False,
        line_items=line_items,
        estimate_id=details.get("estimate_id"),
    )
    _pending_manual_invoice_drafts[draft_id] = draft
    return draft


async def _convert_accepted_estimate_to_invoice(
    estimate_id: str,
    db: AsyncSession,
    send_email: bool = False,
) -> tuple[Optional[CreatedInvoice], str]:
    """Convert an accepted estimate to an invoice; optionally email it."""
    details = await get_estimate(estimate_id, db)
    status = (details.get("status") or "").lower()
    if status in ("invoiced", "partially_invoiced"):
        return None, f"Estimate **#{details.get('estimate_number')}** has already been converted to an invoice."
    if status != "accepted":
        return None, (
            f"Estimate **#{details.get('estimate_number')}** is **{status}** — "
            "accept it first, then ask me to send the invoice."
        )

    invoice = await convert_estimate_to_invoice(estimate_id=estimate_id, db=db)
    client_email = details.get("email")
    total_amount = float(details.get("total") or 0.0)
    email_sent = False
    if send_email:
        email_sent, _ = await send_invoice_email(
            invoice.get("invoice_id", ""),
            db,
            to_email=client_email,
        )

    created = CreatedInvoice(
        zoho_invoice_id=invoice.get("invoice_id", ""),
        invoice_number=invoice.get("invoice_number", ""),
        client_name=details.get("customer_name") or "Unknown",
        client_email=client_email,
        amount=total_amount,
        currency=details.get("currency_code") or "INR",
        invoice_url=invoice.get("invoice_url"),
        email_sent=email_sent,
    )
    _recent_invoices.append(created)
    return created, ""


async def _handle_estimate_disambiguation_pick(
    disambig: dict,
    selected_idx: int,
    db: AsyncSession,
) -> ChatResponse:
    est = disambig["estimates"][selected_idx]
    mode = disambig.get("mode", "convert")
    estimate_id = est["estimate_id"]

    if mode == "accept":
        try:
            updated = await update_estimate_status(estimate_id, "accepted", db)
            _mark_estimate_status(estimate_id, "accepted")
            number = updated.get("estimate_number") or est.get("estimate_number")
            customer = updated.get("customer_name") or est.get("customer_name")
            return ChatResponse(
                reply=(
                    f"Estimate **#{number}** for **{customer}** is now **accepted** ✅\n\n"
                    "Say **\"send the invoice\"** or **\"convert to invoice\"** when you're ready to bill."
                ),
                action="estimate_accepted",
            )
        except Exception as e:
            logger.error(f"Failed to accept estimate {estimate_id}: {e}")
            return ChatResponse(reply=f"Could not accept estimate: {e}", action="error")

    if mode == "reject":
        try:
            updated = await update_estimate_status(estimate_id, "declined", db)
            _mark_estimate_status(estimate_id, "declined")
            number = updated.get("estimate_number") or est.get("estimate_number")
            customer = updated.get("customer_name") or est.get("customer_name")
            return ChatResponse(
                reply=f"Estimate **#{number}** for **{customer}** has been **declined**. ✗",
                action="estimate_declined",
            )
        except Exception as e:
            logger.error(f"Failed to decline estimate {estimate_id}: {e}")
            return ChatResponse(reply=f"Could not decline estimate: {e}", action="error")

    if mode == "send":
        created, err = await _convert_accepted_estimate_to_invoice(estimate_id, db, send_email=True)
        if err:
            return ChatResponse(reply=err, action="clarification_needed")
        return ChatResponse(
            reply=f"Invoice **#{created.invoice_number}** created and sent to **{created.client_name}**. 📧",
            action="invoice_created",
            invoices_created=[created],
        )

    # convert (default)
    try:
        details = await get_estimate(estimate_id, db)
    except Exception as e:
        logger.error(f"Failed to fetch estimate details for {estimate_id}: {e}")
        return ChatResponse(reply=f"Could not fetch estimate details: {e}", action="error")

    status = (details.get("status") or "").lower()
    if status in ("invoiced", "partially_invoiced"):
        return ChatResponse(
            reply=f"Estimate **#{details.get('estimate_number')}** has already been converted to an invoice.",
            action="clarification_needed",
        )

    draft = await _build_invoice_draft_from_estimate(details)
    return ChatResponse(
        reply=(
            f"Found accepted estimate **#{details.get('estimate_number')}** for **{draft.client_name}**. "
            "Here is the draft invoice for your final approval:"
        ),
        action="manual_invoice_pending",
        manual_invoice_draft=draft,
    )


async def _handle_accept_estimate(
    person_name: Optional[str],
    estimate_ref: Optional[str],
    db: AsyncSession,
    session_key: str | int,
) -> ChatResponse:
    candidates = await _resolve_estimate_candidates(
        db, purpose="accept", person_name=person_name, estimate_ref=estimate_ref,
    )

    if not candidates:
        # Helpful hint if sent estimates exist but none matched filters
        sent = await _fetch_estimates_by_statuses(db, {"sent"})
        if person_name:
            sent = [e for e in sent if _estimate_matches_person(e, person_name)]
        if sent:
            hint = (
                f"I couldn't find a sent estimate to accept for **{person_name}**."
                if person_name
                else "I couldn't find a sent estimate to accept."
            )
            return ChatResponse(
                reply=f"{hint} There are **{len(sent)}** sent estimate(s) in Zoho — try specifying the estimate number (e.g. **QT-000004**).",
                action="clarification_needed",
            )
        if person_name:
            return ChatResponse(
                reply=f"I couldn't find any sent estimates for **{person_name}** to accept.",
                action="clarification_needed",
            )
        return ChatResponse(
            reply="There are no sent estimates waiting to be accepted in your Zoho account right now.",
            action="clarification_needed",
        )

    if len(candidates) > 1:
        _pending_estimate_disambiguations[session_key] = {
            "estimates": candidates,
            "mode": "accept",
        }
        return ChatResponse(
            reply=_estimate_disambiguation_reply(candidates, "accept"),
            action="clarification_needed",
            ambiguous_estimates=_ambiguous_estimate_cards(candidates),
            disambiguation_mode="accept",
        )

    est = candidates[0]
    status = (est.get("status") or "").lower()
    if status == "accepted":
        return ChatResponse(
            reply=(
                f"Estimate **#{est.get('estimate_number')}** for **{est.get('customer_name')}** "
                "is already **accepted** ✅\n\n"
                "Say **\"send the invoice\"** or **\"convert to invoice\"** to proceed."
            ),
            action="clarification_needed",
        )

    try:
        updated = await update_estimate_status(est["estimate_id"], "accepted", db)
        _mark_estimate_status(est["estimate_id"], "accepted")
        number = updated.get("estimate_number") or est.get("estimate_number")
        customer = updated.get("customer_name") or est.get("customer_name")
        return ChatResponse(
            reply=(
                f"Estimate **#{number}** for **{customer}** is now **accepted** ✅\n\n"
                "Say **\"send the invoice\"** or **\"convert to invoice\"** when you're ready to bill."
            ),
            action="estimate_accepted",
        )
    except Exception as e:
        logger.error(f"Failed to accept estimate {est.get('estimate_id')}: {e}")
        return ChatResponse(reply=f"Could not accept estimate: {e}", action="error")


async def _handle_reject_estimate(
    person_name: Optional[str],
    estimate_ref: Optional[str],
    db: AsyncSession,
    session_key: str | int,
) -> ChatResponse:
    candidates = await _resolve_estimate_candidates(
        db, purpose="reject", person_name=person_name, estimate_ref=estimate_ref,
    )

    if not candidates:
        if person_name:
            return ChatResponse(
                reply=f"I couldn't find any estimates for **{person_name}** that can be declined.",
                action="clarification_needed",
            )
        return ChatResponse(
            reply="There are no estimates in your Zoho account that can be declined right now.",
            action="clarification_needed",
        )

    if len(candidates) > 1:
        _pending_estimate_disambiguations[session_key] = {
            "estimates": candidates,
            "mode": "reject",
        }
        return ChatResponse(
            reply=_estimate_disambiguation_reply(candidates, "reject"),
            action="clarification_needed",
            ambiguous_estimates=_ambiguous_estimate_cards(candidates),
            disambiguation_mode="reject",
        )

    est = candidates[0]
    status = (est.get("status") or "").lower()
    if status == "declined":
        return ChatResponse(
            reply=f"Estimate **#{est.get('estimate_number')}** is already **declined**.",
            action="clarification_needed",
        )
    if status in ("invoiced", "partially_invoiced"):
        return ChatResponse(
            reply=f"Estimate **#{est.get('estimate_number')}** has already been converted to an invoice and can't be declined.",
            action="clarification_needed",
        )

    try:
        updated = await update_estimate_status(est["estimate_id"], "declined", db)
        _mark_estimate_status(est["estimate_id"], "declined")
        number = updated.get("estimate_number") or est.get("estimate_number")
        customer = updated.get("customer_name") or est.get("customer_name")
        return ChatResponse(
            reply=f"Estimate **#{number}** for **{customer}** has been **declined**. ✗",
            action="estimate_declined",
        )
    except Exception as e:
        logger.error(f"Failed to decline estimate {est.get('estimate_id')}: {e}")
        return ChatResponse(reply=f"Could not decline estimate: {e}", action="error")


async def _handle_convert_estimate(
    person_name: Optional[str],
    db: AsyncSession,
    session_key: str | int,
) -> ChatResponse:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{settings.zoho_api_base}/estimates",
                headers=await _headers(db),
                params={
                    "organization_id": await get_org_id(db),
                    "filter_by": "Status.Accepted",
                }
            )
            resp.raise_for_status()
            estimates = resp.json().get("estimates", [])
    except Exception as e:
        logger.error(f"Failed to fetch estimates from Zoho: {e}")
        return ChatResponse(
            reply=f"Could not fetch estimates: {e}",
            action="error",
        )

    if person_name:
        person_name_clean = person_name.lower().strip()
        estimates = [
            e for e in estimates
            if person_name_clean in (e.get("customer_name") or "").lower()
        ]

    if not estimates:
        if person_name:
            return ChatResponse(
                reply=f"I couldn't find any accepted estimates for **{person_name}**.",
                action="clarification_needed",
            )
        else:
            return ChatResponse(
                reply="There are no accepted estimates in your Zoho account right now.",
                action="clarification_needed",
            )

    # If exactly one estimate matches
    if len(estimates) == 1:
        est = estimates[0]
        estimate_id = est["estimate_id"]
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{settings.zoho_api_base}/estimates/{estimate_id}",
                    headers=await _headers(db),
                    params={"organization_id": await get_org_id(db)}
                )
                resp.raise_for_status()
                details = resp.json().get("estimate", {})
        except Exception as e:
            logger.error(f"Failed to fetch estimate details for {estimate_id}: {e}")
            return ChatResponse(
                reply=f"Could not fetch estimate details: {e}",
                action="error",
            )

        status = details.get("status", "").lower()
        if status in ("invoiced", "partially_invoiced"):
            return ChatResponse(
                reply=f"Estimate **#{details.get('estimate_number')}** has already been converted to an invoice.",
                action="clarification_needed"
            )

        # Build draft
        raw_items = details.get("line_items") or []
        line_items = []
        for item in raw_items:
            line_items.append(ManualInvoiceLineItem(
                item_name=item.get("name") or "Service",
                task_description=item.get("description") or "",
                amount=float(item.get("rate") or 0.0),
            ))

        draft_id = str(uuid.uuid4())
        draft = ManualInvoiceDraft(
            draft_id=draft_id,
            client_name=details.get("customer_name") or "Unknown",
            client_email=details.get("email"),
            currency=details.get("currency_code") or "INR",
            zoho_contact_id=details.get("customer_id"),
            is_new_contact=False,
            line_items=line_items,
            estimate_id=estimate_id,
        )
        _pending_manual_invoice_drafts[draft_id] = draft

        return ChatResponse(
            reply=f"Found accepted estimate **#{details.get('estimate_number')}** for **{draft.client_name}**. Here is the draft invoice for your final approval:",
            action="manual_invoice_pending",
            manual_invoice_draft=draft,
        )

    # If multiple estimates match
    ambiguous = []
    for e in estimates:
        base = _ZOHO_APP_BASE.get(settings.ZOHO_REGION, "https://invoice.zoho.com")
        est_url = f"{base}/app#/estimates/{e.get('estimate_id', '')}"
        ambiguous.append(AmbiguousEstimate(
            estimate_id=e["estimate_id"],
            estimate_number=e.get("estimate_number") or "",
            customer_name=e.get("customer_name") or "",
            status=e.get("status") or "",
            amount=float(e.get("total") or 0.0),
            currency=e.get("currency_code") or "INR",
            date=e.get("date"),
            estimate_url=est_url,
        ))

    _pending_estimate_disambiguations[session_key] = {
        "estimates": estimates,
        "mode": "convert",
    }

    return ChatResponse(
        reply=_estimate_disambiguation_reply(estimates, "convert"),
        action="clarification_needed",
        ambiguous_estimates=_ambiguous_estimate_cards(estimates),
        disambiguation_mode="convert",
    )


async def process_chat(
    message: str,
    db: AsyncSession,
    status_cb=None,          # optional async callable(str) for SSE status updates
    session_key: str | int = "web",
) -> ChatResponse:
    global _manual_invoice_conversation
    normalized_message = " ".join((message or "").lower().split())

    async def emit(text: str) -> None:
        """Send a status update to the client if streaming is active."""
        if status_cb:
            await status_cb(text)

    # ── 1. Parse intent ─────────────────────────────────────────────────────
    await emit("🧠 Reading your request…")
    intent = await parse_intent(message)
    action: Optional[str] = intent.get("action")
    person_name: Optional[str] = intent.get("person_name")
    date_filter: Optional[str] = intent.get("date_filter")
    keywords: list[str] = intent.get("keywords") or []
    logger.info(f"Intent parsed: {intent}")

    conversation_response = await _handle_manual_invoice_conversation(message, db)
    if conversation_response:
        return conversation_response

    # ── Check if user is replying to a payment customer prompt ────────────────
    if session_key in _pending_payment_customer_prompts:
        prompt_data = _pending_payment_customer_prompts.pop(session_key, None)
        if normalized_message in ("cancel", "stop", "exit", "abort"):
            return ChatResponse(reply="Payment recording cancelled. ✗", action="clarification_needed")
        
        person_name = message.strip()
        return await _handle_record_payment(
            person_name=person_name,
            amount=prompt_data["amount"],
            payment_mode=prompt_data["payment_mode"],
            payment_date=prompt_data["payment_date"],
            db=db,
            session_key=session_key,
            emit=emit
        )

    # ── Check if user is replying to an estimate ambiguity ───────────────────
    if session_key in _pending_estimate_disambiguations and action not in (
        "accept_estimate", "reject_estimate", "convert_estimate", "create_estimate"
    ):
        disambig = _pending_estimate_disambiguations[session_key]
        if normalized_message in ("cancel", "stop", "exit", "abort"):
            _pending_estimate_disambiguations.pop(session_key, None)
            return ChatResponse(reply="Estimate action cancelled. ✗", action="clarification_needed")

        selected_idx = None
        m_idx = re.match(r"^(\d+)$", message.strip())
        if m_idx:
            idx = int(m_idx.group(1)) - 1
            if 0 <= idx < len(disambig["estimates"]):
                selected_idx = idx

        if selected_idx is None:
            clean_msg = message.strip().lower()
            for idx, est in enumerate(disambig["estimates"]):
                if clean_msg in (est.get("estimate_id") or "").lower() or clean_msg in (est.get("estimate_number") or "").lower():
                    selected_idx = idx
                    break

        if selected_idx is not None:
            est = disambig["estimates"][selected_idx]
            mode = disambig.get("mode", "convert")  # accept | reject | convert
            _pending_estimate_disambiguations.pop(session_key, None)

            # ── Accept mode: just mark the estimate accepted ─────────────────
            if mode == "accept":
                try:
                    updated = await update_estimate_status(est["estimate_id"], "accepted", db)
                    _mark_estimate_status(est["estimate_id"], "accepted")
                    number   = updated.get("estimate_number") or est.get("estimate_number")
                    customer = updated.get("customer_name")   or est.get("customer_name")
                    return ChatResponse(
                        reply=(
                            f"Estimate **#{number}** for **{customer}** is now **accepted** ✅\n\n"
                            "Say **\"convert to invoice\"** when you're ready to bill."
                        ),
                        action="estimate_accepted",
                    )
                except Exception as e:
                    logger.error(f"Failed to accept estimate {est.get('estimate_id')}: {e}")
                    return ChatResponse(reply=f"Could not accept estimate: {e}", action="error")

            # ── Reject mode: mark the estimate declined ──────────────────────
            if mode == "reject":
                status_now = (est.get("status") or "").lower()
                if status_now in ("invoiced", "partially_invoiced"):
                    return ChatResponse(
                        reply=f"Estimate **#{est.get('estimate_number')}** has already been converted to an invoice and can't be declined.",
                        action="clarification_needed",
                    )
                try:
                    updated = await update_estimate_status(est["estimate_id"], "declined", db)
                    _mark_estimate_status(est["estimate_id"], "declined")
                    number   = updated.get("estimate_number") or est.get("estimate_number")
                    customer = updated.get("customer_name")   or est.get("customer_name")
                    return ChatResponse(
                        reply=f"Estimate **#{number}** for **{customer}** has been **declined**. ✗",
                        action="estimate_declined",
                    )
                except Exception as e:
                    logger.error(f"Failed to decline estimate {est.get('estimate_id')}: {e}")
                    return ChatResponse(reply=f"Could not decline estimate: {e}", action="error")

            # ── Convert mode (default): build invoice draft ──────────────────
            estimate_id = est["estimate_id"]
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        f"{settings.zoho_api_base}/estimates/{estimate_id}",
                        headers=await _headers(db),
                        params={"organization_id": await get_org_id(db)}
                    )
                    resp.raise_for_status()
                    details = resp.json().get("estimate", {})
            except Exception as e:
                logger.error(f"Failed to fetch estimate details for {estimate_id}: {e}")
                return ChatResponse(
                    reply=f"Could not fetch estimate details: {e}",
                    action="error",
                )

            status = details.get("status", "").lower()
            if status in ("invoiced", "partially_invoiced"):
                return ChatResponse(
                    reply=f"Estimate **#{details.get('estimate_number')}** has already been converted to an invoice.",
                    action="clarification_needed"
                )

            raw_items = details.get("line_items") or []
            line_items = []
            for item in raw_items:
                line_items.append(ManualInvoiceLineItem(
                    item_name=item.get("name") or "Service",
                    task_description=item.get("description") or "",
                    amount=float(item.get("rate") or 0.0),
                ))

            draft_id = str(uuid.uuid4())
            draft = ManualInvoiceDraft(
                draft_id=draft_id,
                client_name=details.get("customer_name") or "Unknown",
                client_email=details.get("email"),
                currency=details.get("currency_code") or "INR",
                zoho_contact_id=details.get("customer_id"),
                is_new_contact=False,
                line_items=line_items,
                estimate_id=estimate_id,
            )
            _pending_manual_invoice_drafts[draft_id] = draft

            return ChatResponse(
                reply=f"Here is the draft invoice from estimate **#{details.get('estimate_number')}** for **{draft.client_name}**. Approve below when you're ready:",
                action="manual_invoice_pending",
                manual_invoice_draft=draft,
            )
        else:
            mode = disambig.get("mode", "convert")
            verb = {"accept": "accept", "reject": "decline", "convert": "convert"}.get(mode, "convert")
            reply_lines = [f"I didn't recognize that selection. Please choose a number from 1 to {len(disambig['estimates'])} or type the estimate number:"]
            for idx, est in enumerate(disambig["estimates"], 1):
                currency = est.get("currency_code") or "INR"
                total_fmt = f"₹{float(est.get('total') or 0.0):,.2f}" if currency == "INR" else f"{currency} {float(est.get('total') or 0.0):,.2f}"
                reply_lines.append(f"{idx}. Estimate **#{est.get('estimate_number')}** for **{est.get('customer_name')}** — {total_fmt}")
            return ChatResponse(
                reply="\n".join(reply_lines),
                action="clarification_needed",
            )

    # ── Check if user is replying to a payment ambiguity ────────────────────
    if session_key in _pending_payment_disambiguations:
        disambig = _pending_payment_disambiguations[session_key]
        if normalized_message in ("cancel", "stop", "exit", "abort"):
            _pending_payment_disambiguations.pop(session_key, None)
            return ChatResponse(reply="Payment recording cancelled. ✗", action="clarification_needed")

        selected_idx = None
        m_idx = re.match(r"^(\d+)$", message.strip())
        if m_idx:
            idx = int(m_idx.group(1)) - 1
            if 0 <= idx < len(disambig["invoices"]):
                selected_idx = idx

        if selected_idx is None:
            clean_msg = message.strip()
            for idx, inv in enumerate(disambig["invoices"]):
                if clean_msg == str(inv.invoice_id):
                    selected_idx = idx
                    break

        if selected_idx is not None:
            inv = disambig["invoices"][selected_idx]
            _pending_payment_disambiguations.pop(session_key, None)

            actual_amount = float(inv.balance) if disambig["amount"] is None else disambig["amount"]
            is_overpayment = actual_amount > float(inv.balance)
            overpayment_warn = ""
            if is_overpayment:
                overpayment_warn = f"\n\n⚠️ **Warning**: The payment amount (₹{actual_amount:,.2f}) exceeds the outstanding balance (₹{float(inv.balance):,.2f}) of this invoice. Approving this will create an overpayment credit in Zoho."

            draft_id = str(uuid.uuid4())[:8]
            draft = PaymentRecordDraft(
                draft_id=draft_id,
                invoice_id=inv.invoice_id,
                customer_id=disambig["customer_id"],
                customer_name=disambig["customer_name"],
                invoice_number=inv.invoice_id,
                amount=actual_amount,
                payment_mode=disambig["payment_mode"],
                payment_date=disambig["payment_date"],
                currency=inv.currency_code or "INR",
            )
            _pending_payment_drafts[draft_id] = draft

            currency_sym = "₹" if draft.currency == "INR" else f"{draft.currency} "
            reply = (
                f"Got it! Selected **Invoice #{inv.invoice_id}**.\n\n"
                f"I've prepared a draft to record a manual payment for **{draft.customer_name}**:\n\n"
                f"• Invoice ID: **{draft.invoice_id}**\n"
                f"• Amount: **{currency_sym}{draft.amount:,.2f}**\n"
                f"• Method: **{draft.payment_mode.upper()}**\n"
                f"• Date: **{draft.payment_date}**"
                f"{overpayment_warn}\n\n"
                "Confirm below if this looks correct. ✓"
            )
            return ChatResponse(
                reply=reply,
                action="payment_record_pending",
                payment_record_draft=draft,
            )
        else:
            reply_lines = [f"I didn't recognize that selection. Please choose a number from 1 to {len(disambig['invoices'])} or type the invoice ID:"]
            for idx, inv in enumerate(disambig["invoices"], 1):
                currency = inv.currency_code or "INR"
                balance_fmt = f"₹{float(inv.balance):,.2f}" if currency == "INR" else f"{currency} {float(inv.balance):,.2f}"
                due_fmt = inv.due_date.isoformat() if inv.due_date else "no due date"
                reply_lines.append(f"{idx}. Invoice **#{inv.invoice_id}** — {balance_fmt} (due: {due_fmt})")
            return ChatResponse(
                reply="\n".join(reply_lines),
                action="clarification_needed",
            )

    # ── Check if a recurring invoice conversation is in progress ────────────
    if session_key in _pending_recurring_conv:
        return await _handle_recurring_create(message, db, emit, session_key)

    # ── Check if user is replying with numbers to stop recurring invoices ───
    if session_key in _pending_recurring_list:
        numbers = [t for t in re.findall(r"\b(\d+)\b", message) if int(t) >= 1]
        if numbers:
            return await _handle_recurring_stop(message, db, emit, session_key)

    # ── Handle greetings and unclear intents naturally ───────────────────────
    if action == "greeting":
        return ChatResponse(
            reply=(
                "Hey 👋 I am Invoice Agent! Happy to help. You can ask me things like:\n"
                "• Jacks invoice from yesterday's design work\n"
                "• Check emails from last Sunday\n"
                "• Who hasn't paid me? or Payment summary\n"
                "• Add payment for a client\n"
                "• Make a new invoice for clients\n"
                "• Make a new recuring invoice for clients and also stop them\n"
                "What would you like to do?"
            ),
            action="clarification_needed",
        )
    if action == "unknown":
        return ChatResponse(
            reply=(
                "Hmm, I'm not quite sure what you need. You could try something like:\n"
                "• Invoice Jacks for the design work from yesterday\n"
                "• Look at emails from last Sunday\n"
                "• Make an invoice for Rahul for the website project for 9000"
            ),
            action="clarification_needed",
        )

    # ── Handle create_invoice: ALWAYS use guided step-by-step flow ────────────
    # The old email-scan path for create_invoice is removed.
    # Email scanning only happens via explicit "scan_emails" action.
    if action == "create_invoice":
        parsed = await extract_manual_invoice_request(message)

        client_name = parsed.get("client_name") or person_name
        client_email = parsed.get("client_email")
        currency = parsed.get("currency") or "INR"
        
        # Pull first item details if present in parsed payload
        item_name = None
        item_desc = None
        item_amount = None
        
        parsed_items = parsed.get("items") or []
        if parsed_items:
            first_item = parsed_items[0]
            item_name = first_item.get("item_name")
            item_desc = first_item.get("task_description") or item_name
            item_amount = first_item.get("amount")

        _manual_invoice_conversation = ManualInvoiceConversation(
            step="awaiting_customer",
            client_name=client_name,
            client_email=client_email,
            currency=currency,
            pending_item_name=item_name,
            pending_item_description=item_desc,
            pending_item_amount=item_amount,
            item_count=1
        )
        return await _advance_manual_invoice_conversation(db)

    if action == "create_estimate":
        parsed = await extract_manual_invoice_request(message, is_estimate=True)

        client_name = parsed.get("client_name") or person_name
        client_email = parsed.get("client_email")
        currency = parsed.get("currency") or "INR"
        
        # Pull first item details if present in parsed payload
        item_name = None
        item_desc = None
        item_amount = None
        
        parsed_items = parsed.get("items") or []
        if parsed_items:
            first_item = parsed_items[0]
            item_name = first_item.get("item_name")
            item_desc = first_item.get("task_description") or item_name
            item_amount = first_item.get("amount")

        _manual_invoice_conversation = ManualInvoiceConversation(
            step="awaiting_customer",
            client_name=client_name,
            client_email=client_email,
            currency=currency,
            pending_item_name=item_name,
            pending_item_description=item_desc,
            pending_item_amount=item_amount,
            item_count=1,
            is_estimate=True,
        )
        return await _advance_manual_invoice_conversation(db)

    if action in ("accept_estimate", "reject_estimate", "convert_estimate", "create_estimate"):
        # Clear any stale disambiguation so a fresh command starts clean
        _pending_estimate_disambiguations.pop(session_key, None)

    if action == "accept_estimate":
        estimate_ref: Optional[str] = intent.get("estimate_ref") or None
        return await _handle_accept_estimate(person_name, estimate_ref, db, session_key)

    if action == "reject_estimate":
        estimate_ref_rej: Optional[str] = intent.get("estimate_ref") or None
        return await _handle_reject_estimate(person_name, estimate_ref_rej, db, session_key)

    if action == "convert_estimate":
        return await _handle_convert_estimate(person_name, db, session_key)

    # ── Handle "send invoice(s)" intent ─────────────────────────────────────
    if action == "send_invoices":
        recent = list(_recent_invoices)
        if not recent:
            return ChatResponse(
                reply="There are no invoices from this session to send yet. "
                      "Create one first and then ask me to send it!",
                action="clarification_needed",
            )

        unsent = [inv for inv in recent if not inv.email_sent]
        if not unsent:
            return ChatResponse(
                reply="All invoices from this session have already been sent. 📧",
                action="clarification_needed",
            )

        send_all = "all" in message.lower() or any(
            kw.lower() == "all" for kw in keywords
        )

        if person_name:
            targets = [
                inv for inv in unsent
                if person_name.lower() in (inv.client_name or "").lower()
            ]
            if not targets:
                already_sent = [
                    inv for inv in recent
                    if person_name.lower() in (inv.client_name or "").lower()
                    and inv.email_sent
                ]
                if already_sent:
                    return ChatResponse(
                        reply=f"The invoice for **{already_sent[-1].client_name}** was already sent. 📧",
                        action="clarification_needed",
                    )
                return ChatResponse(
                    reply=f"I couldn't find a recently created invoice for **{person_name}**. "
                          f"The invoices I have from this session are for: "
                          f"{', '.join('**' + i.client_name + '**' for i in recent)}.",
                    action="clarification_needed",
                )
        elif send_all:
            targets = unsent
        else:
            # "send the invoice" / "send this invoice" → most recent unsent only
            targets = [unsent[-1]]

        # Send each target invoice via Zoho email API
        sent, failed = [], []
        for inv in targets:
            ok, reason = await send_invoice_email(inv.zoho_invoice_id, db, to_email=inv.client_email)
            if ok:
                sent.append(inv.model_copy(update={"email_sent": True}))
                _mark_invoice_sent(inv.zoho_invoice_id)
            else:
                failed.append((inv, reason))

        reply_parts = []
        if sent:
            names = ", ".join(f"**{i.client_name}**" for i in sent)
            reply_parts.append(
                f"Done! Sent {'the invoice' if len(sent) == 1 else str(len(sent)) + ' invoices'} "
                f"to {names} via email. 📧 They should receive it shortly."
            )
        if failed:
            for inv, reason in failed:
                hint = f" ({reason})" if reason else ""
                reply_parts.append(
                    f"⚠️ Couldn't send the invoice for **{inv.client_name}**{hint}. "
                    "Check that Zoho is connected and try again."
                )

        return ChatResponse(
            reply="\n".join(reply_parts) or "Nothing was sent.",
            action="invoice_sent" if sent else "error",
            invoices_created=sent or None,
        )
    # ── Payment status queries ───────────────────────────────────────────────
    if action in ("check_overdue", "check_pending", "check_specific_payment", "payment_summary"):
        return await _handle_payment_query(action, person_name, db, emit=emit)

    # ── Payment reminders ────────────────────────────────────────────────────
    if action == "remind_overdue":
        days_overdue_min = int(intent.get("days_overdue_min") if intent.get("days_overdue_min") is not None else 0)
        return await _handle_remind_overdue(person_name, days_overdue_min, db, emit=emit)

    # ── Record payment ───────────────────────────────────────────────────────
    if action == "record_payment":
        amount = intent.get("amount")
        payment_mode = intent.get("payment_mode") or "banktransfer"
        payment_date = intent.get("payment_date")
        if not payment_date:
            from datetime import date as _date
            payment_date = _date.today().isoformat()
        return await _handle_record_payment(
            person_name, amount, payment_mode, payment_date, db, session_key=session_key, emit=emit
        )

    # ── Recurring invoice intents ────────────────────────────────────────────
    if action == "list_recurring":
        return await _handle_recurring_list(db, emit, session_key)

    if action == "stop_recurring":
        return await _handle_recurring_stop(message, db, emit, session_key)

    if action == "create_recurring":
        return await _handle_recurring_create(message, db, emit, session_key)

    # ── 2. Resolve contact name ─────────────────────────────────────────────
    person_email: Optional[str] = None
    resolved_contact_id: Optional[str] = None
    unknown_person: Optional[str] = None  # name not found in Zoho

    if person_name:
        await emit(f"📇 Looking up **{person_name}** in your contacts…")
        try:
            contacts = await search_contact_by_name(person_name, db)
        except RuntimeError as e:
            return ChatResponse(reply=str(e), action="error")

        if len(contacts) == 0:
            # Don't bail out — search Gmail by name keyword and let the user
            # create the contact when they approve the draft.
            logger.info(f"Contact '{person_name}' not in Zoho — will search Gmail and queue as new-contact draft.")
            unknown_person = person_name
            if person_name not in keywords:
                keywords = [person_name] + keywords
        elif len(contacts) > 1:
            names = "\n".join(
                f"• {c['contact_name']} ({c.get('email') or 'no email'})"
                for c in contacts
            )
            return ChatResponse(
                reply=f"I found {len(contacts)} contacts named **{person_name}** — which one did you mean?\n\n{names}",
                action="clarification_needed",
                ambiguous_contacts=[
                    AmbiguousContact(
                        name=c["contact_name"],
                        email=c.get("email"),
                        zoho_contact_id=c["contact_id"],
                    )
                    for c in contacts
                ],
            )
        else:
            person_email = contacts[0].get("email")
            resolved_contact_id = contacts[0]["contact_id"]

    # ── No criteria at all ──────────────────────────────────────────────────
    if not person_email and not date_filter and not keywords:
        return ChatResponse(
            reply=(
                "I need a bit more to go on! Try something like:\n"
                "• Invoice Piyusha for yesterday's work\n"
                "• Check emails from last Sunday\n"
                "• Make an invoice for Rahul for the website project"
            ),
            action="clarification_needed",
        )

    # ── 3. Search Gmail ─────────────────────────────────────────────────────
    await emit("📧 Searching Gmail…")
    try:
        emails = await search_gmail(
            db,
            person_email=person_email,
            date_filter=date_filter,
            keywords=keywords,
            person_name=unknown_person,
        )
    except (ValueError, RuntimeError) as e:
        return ChatResponse(reply=str(e), action="clarification_needed" if isinstance(e, ValueError) else "error")

    if not emails:
        # Build a natural "what was searched" description
        search_desc_parts = []
        if person_name:
            search_desc_parts.append(f"from or to **{person_name}**")
        if date_filter:
            search_desc_parts.append(f"from **{_DATE_LABELS.get(date_filter, date_filter)}**")
        search_desc = " ".join(search_desc_parts) or "matching your request"
        return ChatResponse(
            reply=(
                f"I couldn't find any new emails {search_desc}. "
                "They might've already been processed, or there just weren't any matching messages."
            ),
            action="emails_scanned",
        )

    # ── 4–6. Extract → Decide → Create ─────────────────────────────────────
    created_invoices: list[CreatedInvoice] = []
    pending_drafts: list[DraftInvoice] = []
    skipped = 0

    n_emails = len(emails)
    if n_emails:
        await emit(f"Found {n_emails} email{'s' if n_emails > 1 else ''} — analyzing with AI…")

    for i, email in enumerate(emails):
        email_text = (email.get("body") or email.get("subject") or "").strip()
        if not email_text:
            skipped += 1
            continue

        await emit(f"📄 Analyzing email {i + 1}/{n_emails}…")

        # Small delay between LLM calls to stay within Groq free-tier TPM limits
        if i > 0:
            await asyncio.sleep(1.5)

        try:
            raw = await extract_invoice_data(email_text)
            data = InvoiceData(**raw)

            # Skip emails that are NOT freelance invoice confirmations (subscriptions,
            # receipts, bank alerts, etc.) — the LLM explicitly sets is_confirmation=False.
            if not data.is_confirmation:
                skipped += 1
                logger.info(f"Email {email['id']} skipped — not a freelance invoice (is_confirmation=False).")
                continue

            # Also skip when confidence is low AND there are no meaningful signals.
            if (
                data.confidence == "low"
                and not data.amount
                and not data.task_description
                and not data.client_name
            ):
                skipped += 1
                logger.info(f"Email {email['id']} skipped — low confidence, no invoice signals.")
                continue

            # ── Contact resolution ─────────────────────────────────────────────
            contact_id = resolved_contact_id
            is_new = False
            sent_by_self = email.get("sent_by_self", False)
            from_header = email.get("from", "")
            to_header = email.get("to", "")

            if not contact_id:
                if unknown_person:
                    # Step 2 already confirmed this person is NOT in Zoho —
                    # don't do a second lookup; trust the first result.
                    is_new = True
                elif data.client_name:
                    # Date-only / keyword scan: try to match the extracted name
                    try:
                        matches = await search_contact_by_name(data.client_name, db)
                        if len(matches) == 1:
                            contact_id = matches[0]["contact_id"]
                        elif not matches:
                            is_new = True  # name not found → new contact on approval
                    except Exception:
                        pass
                else:
                    is_new = True  # no name at all → treat as new contact

            # If client_name not extracted from email, fall back to the search name
            if not data.client_name and unknown_person:
                updated_missing = [f for f in data.missing_fields if f != "client_name"]
                data = data.model_copy(update={"client_name": unknown_person, "missing_fields": updated_missing})

            # ── Email fallback from headers ────────────────────────────────────
            # If the LLM didn't extract client_email (e.g. body says
            # "send to this email" without the actual address), pull it from
            # the correct header:
            #   - Email FROM a client → client address is in From:
            #   - Email sent BY the freelancer → client address is in To:
            # First: validate whatever the LLM gave us — discard non-email
            # strings like "this email", "N/A", "unknown", etc.
            if data.client_email and not _EMAIL_RE.match(data.client_email.strip()):
                logger.info(
                    f"Discarding invalid LLM-extracted email {data.client_email!r} — "
                    "will try header fallback."
                )
                data = data.model_copy(update={"client_email": None})

            if not data.client_email:
                from_addrs  = _EMAIL_RE.findall(from_header)
                to_addrs    = _EMAIL_RE.findall(to_header)

                if sent_by_self:
                    # Freelancer sent this → client is in To:
                    candidate = to_addrs[0] if to_addrs else None
                    logger.info(f"Email sent by freelancer — using To: for client email")
                else:
                    # Client sent this → client is in From:
                    candidate = from_addrs[0] if from_addrs else (to_addrs[0] if to_addrs else None)

                if candidate:
                    updated_missing = [f for f in data.missing_fields if f != "client_email"]
                    data = data.model_copy(update={"client_email": candidate, "missing_fields": updated_missing})
                    logger.info(f"Client email inferred from headers: {candidate}")

            # If the model missed the client name, use header display names.
            if not data.client_name:
                header_name = _header_display_name(to_header if sent_by_self else from_header)
                if header_name:
                    updated_missing = [f for f in data.missing_fields if f != "client_name"]
                    data = data.model_copy(update={"client_name": header_name, "missing_fields": updated_missing})
                    logger.info(f"Client name inferred from headers: {header_name}")

            # If we have a client email but still no resolved contact, try Zoho lookup by email.
            if not contact_id and data.client_email:
                try:
                    email_matches = await search_contact_by_email(data.client_email, db)
                    if len(email_matches) == 1:
                        contact_id = email_matches[0]["contact_id"]
                        is_new = False
                        if not data.client_name and email_matches[0].get("contact_name"):
                            updated_missing = [f for f in data.missing_fields if f != "client_name"]
                            data = data.model_copy(update={
                                "client_name": email_matches[0]["contact_name"],
                                "missing_fields": updated_missing,
                            })
                        logger.info(
                            f"Resolved contact by email {data.client_email} -> {contact_id}"
                        )
                    elif not email_matches and not data.client_name:
                        is_new = True
                except Exception:
                    logger.exception("Email-based Zoho contact lookup failed")

            # Lightweight deterministic fallback for common self-sent invoice phrasing.
            if not data.item_name or not data.task_description:
                data = _apply_invoice_text_fallbacks(data, email_text)

            # ── Clean up missing_fields — remove any field that now has a value ──
            # The LLM sometimes lists a field as missing even though it extracted a
            # value for it (e.g. item_name listed as missing but item_name is set).
            _field_map = {
                "client_name":    data.client_name,
                "client_email":   data.client_email,
                "item_name":      data.item_name,
                "task_description": data.task_description,
                "amount":         data.amount,
            }
            clean_missing = [f for f in data.missing_fields if not _field_map.get(f)]
            if clean_missing != data.missing_fields:
                data = data.model_copy(update={"missing_fields": clean_missing})

            if _should_auto_create(data, contact_id):
                invoice = await create_invoice(
                    contact_id=contact_id,
                    task_description=data.task_description,
                    amount=data.amount,
                    currency=data.currency or "USD",
                    item_name=data.item_name,
                    db=db,
                )
                await mark_email_processed(
                    email["id"], invoice.get("invoice_id", ""), db
                )
                created_invoices.append(CreatedInvoice(
                    zoho_invoice_id=invoice.get("invoice_id", ""),
                    invoice_number=invoice.get("invoice_number", ""),
                    client_name=data.client_name or "",
                    amount=data.amount,
                    currency=data.currency or "USD",
                    invoice_url=invoice.get("invoice_url"),
                ))
                logger.info(f"Auto-created invoice for {data.client_name}.")
            else:
                draft_id = str(uuid.uuid4())
                draft = DraftInvoice(
                    draft_id=draft_id,
                    data=data,
                    gmail_message_id=email["id"],
                    email_subject=email.get("subject"),
                    zoho_contact_id=contact_id,
                    is_new_contact=is_new,
                )
                _pending_drafts[draft_id] = draft
                pending_drafts.append(draft)
                logger.info(
                    f"Draft queued for {data.client_name} "
                    f"(new_contact={is_new}, confidence={data.confidence}, missing={data.missing_fields})"
                )

        except Exception as e:
            err_str = str(e)
            logger.error(f"Failed to process email {email['id']}: {err_str}")
            if "quota" in err_str.lower() or "rate" in err_str.lower() or "429" in err_str:
                # Surface rate limit errors immediately — no point continuing
                return ChatResponse(
                    reply=f"⚠️ Groq rate limit hit. Please wait a moment and try again.\n\nError: {err_str[:200]}",
                    action="error",
                )
            skipped += 1



    # ── Build reply ─────────────────────────────────────────────────────────
    # Reconcile unresolved drafts using any contact we *did* resolve in this scan.
    resolved_by_email: dict[str, tuple[str, str]] = {}
    for draft in pending_drafts:
        draft_email = (draft.data.client_email or "").strip().lower()
        draft_name = (draft.data.client_name or "").strip()
        if draft.zoho_contact_id and draft_email:
            resolved_by_email[draft_email] = (
                draft.zoho_contact_id,
                draft_name or draft_email,
            )

    for draft in pending_drafts:
        draft_email = (draft.data.client_email or "").strip().lower()
        if draft.zoho_contact_id or not draft_email:
            continue
        match = resolved_by_email.get(draft_email)
        if not match:
            continue

        draft.zoho_contact_id = match[0]
        draft.is_new_contact = False
        if not draft.data.client_name:
            updated_missing = [f for f in draft.data.missing_fields if f != "client_name"]
            draft.data = draft.data.model_copy(update={
                "client_name": match[1],
                "missing_fields": updated_missing,
            })
        logger.info(
            f"Reconciled draft {draft.draft_id} to existing contact {match[0]} via email {draft_email}"
        )

    new_contact_drafts = [d for d in pending_drafts if d.is_new_contact]
    regular_drafts     = [d for d in pending_drafts if not d.is_new_contact]

    # Group regular drafts by zoho_contact_id to form batches
    batch_draft = None
    contact_groups = {}
    for d in regular_drafts:
        if d.zoho_contact_id:
            contact_groups.setdefault(d.zoho_contact_id, []).append(d)

    final_regular_drafts = []
    for cid, drafts in contact_groups.items():
        if len(drafts) >= 2 and not batch_draft:
            batch_id = str(uuid.uuid4())
            items = [
                BatchDraftItem(
                    item_id=d.draft_id,
                    data=d.data,
                    gmail_message_id=d.gmail_message_id,
                    email_subject=d.email_subject,
                ) for d in drafts
            ]
            batch_draft = BatchDraft(
                batch_id=batch_id,
                client_name=drafts[0].data.client_name or "Unknown",
                client_email=drafts[0].data.client_email,
                zoho_contact_id=cid,
                items=items,
            )
            _pending_batches[batch_id] = batch_draft
        else:
            final_regular_drafts.extend(drafts)

    # Re-evaluate pending_drafts after batching
    regular_drafts = final_regular_drafts
    pending_drafts = new_contact_drafts + regular_drafts

    # Build context label for the reply
    ctx_parts = []
    if person_name:
        ctx_parts.append(f"from **{person_name}**")
    if date_filter:
        ctx_parts.append(f"from **{_DATE_LABELS.get(date_filter, date_filter)}**")
    ctx = " ".join(ctx_parts)

    parts = []
    if created_invoices:
        n = len(created_invoices)
        parts.append(f"Done! Created {n} invoice{'s' if n > 1 else ''} automatically. ✅")
    if new_contact_drafts:
        names = ", ".join(f"**{d.data.client_name or 'Unknown'}**" for d in new_contact_drafts)
        parts.append(
            f"Found {'an email' if len(new_contact_drafts) == 1 else 'emails'} for {names}, "
            "but they're not in your Zoho contacts yet. "
            "Take a look at the draft below and hit **Create Contact & Invoice** when you're ready."
        )
    if batch_draft:
        n = len(batch_draft.items)
        parts.append(
            f"I found {n} invoice-worthy emails for **{batch_draft.client_name}**. "
            "I've grouped them together below so you can combine them into a single invoice or send them separately."
        )
    if regular_drafts:
        n = len(regular_drafts)
        parts.append(
            f"I found {n} relevant email{'s' if n > 1 else ''}{' ' + ctx if ctx else ''} "
            f"and put {'them' if n > 1 else 'it'} together as a draft for you. "
            "Have a look and approve when you're happy with the details."
        )
    if skipped and not parts:
        parts.append(
            f"I went through the emails{' ' + ctx if ctx else ''} but none of them looked invoice-related. "
            f"({skipped} skipped)"
        )
    elif skipped:
        parts.append(f"({skipped} email{'s' if skipped > 1 else ''} skipped — didn't look invoice-related.)")
    if not parts:
        parts.append("Hmm, nothing to process from those emails.")

    final_action = (
        "invoice_created" if created_invoices and not pending_drafts and not batch_draft
        else "batch_pending" if batch_draft
        else "draft_pending" if pending_drafts
        else "emails_scanned"
    )

    return ChatResponse(
        reply="\n".join(parts),
        action=final_action,
        invoices_created=created_invoices or None,
        drafts=pending_drafts or None,
        batch_draft=batch_draft,
    )


async def approve_draft(draft_id: str, overrides: dict, db: AsyncSession) -> ChatResponse:
    """Create a Zoho invoice from a user-approved draft."""
    draft = _pending_drafts.get(draft_id)
    if not draft:
        return ChatResponse(
            reply="This draft has expired or was already processed. Please run the search again.",
            action="error",
        )

    send_email: bool = bool(overrides.pop("send_email", False))

    data = draft.data.model_copy()
    if overrides.get("item_name"):
        data.item_name = overrides["item_name"]
    if overrides.get("task_description"):
        data.task_description = overrides["task_description"]
    if overrides.get("amount"):
        data.amount = float(overrides["amount"])
    if overrides.get("currency"):
        data.currency = overrides["currency"]
    if overrides.get("client_name"):
        data.client_name = overrides["client_name"]
    if overrides.get("client_email"):
        data.client_email = overrides["client_email"]

    if not draft.zoho_contact_id:
        # No existing contact — create one now (user approved this via the draft card)
        if not data.client_name:
            return ChatResponse(
                reply="Cannot create invoice: no client name available. Please add the contact manually in Zoho.",
                action="error",
            )
        logger.info(f"Creating new Zoho contact for: {data.client_name}")
        try:
            new_contact_id = await create_contact(data.client_name, data.client_email, db)
            draft.zoho_contact_id = new_contact_id
        except Exception as e:
            return ChatResponse(
                reply=f"Failed to create Zoho contact for {data.client_name}: {e}",
                action="error",
            )

    if not data.task_description or data.amount is None:
        return ChatResponse(
            reply="Missing task description or amount — please fill them in before approving.",
            action="error",
        )

    try:
        invoice = await create_invoice(
            contact_id=draft.zoho_contact_id,
            task_description=data.task_description,
            amount=data.amount,
            currency=data.currency or "USD",
            item_name=data.item_name,
            db=db,
        )
        await mark_email_processed(
            draft.gmail_message_id, invoice.get("invoice_id", ""), db
        )
        _pending_drafts.pop(draft_id, None)

        # ── Silently update Zoho contact email if we have it ──────────────────
        if data.client_email and draft.zoho_contact_id:
            asyncio.create_task(
                update_contact_email(draft.zoho_contact_id, data.client_email, db)
            )

        # ── Optionally send the invoice email ───────────────────────────────
        email_sent = False
        if send_email:
            email_sent, send_err = await send_invoice_email(
                invoice.get("invoice_id", ""), db, to_email=data.client_email
            )

        # ── Track in recent invoices so "send it" works later ───────────────
        created = CreatedInvoice(
            zoho_invoice_id=invoice.get("invoice_id", ""),
            invoice_number=invoice.get("invoice_number", ""),
            client_name=data.client_name or "",
            client_email=data.client_email,
            amount=data.amount,
            currency=data.currency or "USD",
            invoice_url=invoice.get("invoice_url"),
            email_sent=email_sent,
        )
        _recent_invoices.append(created)

        # ── Build reply ──────────────────────────────────────────────────────
        if send_email and email_sent:
            reply = (
                f"Invoice **#{invoice.get('invoice_number', '')}** created and sent to "
                f"**{data.client_name}** — {data.currency} {data.amount:,.2f} 📧"
            )
        elif send_email and not email_sent:
            reply = (
                f"Invoice **#{invoice.get('invoice_number', '')}** created for **{data.client_name}** "
                f"({data.currency} {data.amount:,.2f}), but the email couldn't be sent. "
                "You can send it manually from Zoho or try again."
            )
        else:
            reply = (
                f"Invoice **#{invoice.get('invoice_number', '')}** created for "
                f"**{data.client_name}** — {data.currency} {data.amount:,.2f} ✅\n"
                f"Want me to send it to them? Just say *\"send the invoice\"*."
            )

        # Force cache refresh to keep stats/dashboard in sync
        try:
            await ensure_fresh_cache(db, force=True)
        except Exception as cache_err:
            logger.error(f"Failed to force refresh cache after invoice approval: {cache_err}")

        return ChatResponse(
            reply=reply,
            action="invoice_created",
            invoices_created=[created],
        )
    except Exception as e:
        logger.error(f"Failed to create invoice from draft {draft_id}: {e}")
        return ChatResponse(reply=f"Failed to create invoice: {e}", action="error")


async def approve_batch(
    batch_draft_id: str,
    mode: str,
    selected_item_ids: list[str],
    send_email: bool,
    db: AsyncSession,
) -> ChatResponse:
    batch = _pending_batches.get(batch_draft_id)
    if not batch:
        return ChatResponse(
            reply="This batch draft has expired or was already processed.",
            action="error"
        )
    
    items_to_process = [item for item in batch.items if item.item_id in selected_item_ids]
    if not items_to_process:
        return ChatResponse(reply="No items selected.", action="error")
        
    created_invoices_list = []
    try:
        def _clean(val: Optional[str]) -> str:
            if not val: return ""
            val = val.strip()
            if val.lower() in ("null", "none"): return ""
            return val

        if mode == "combined":
            line_items = []
            total_amount = 0.0
            for item in items_to_process:
                item_amount = item.data.amount or 0.0

                # Skip line items with zero/missing amount AND no useful description —
                # these are phantom "Service ₹0" rows the LLM failed to extract.
                item_desc_raw = _clean(item.data.task_description)
                item_name_raw = _clean(item.data.item_name)
                if item_amount == 0.0 and not item_desc_raw and not item_name_raw:
                    logger.info(f"Skipping zero-amount line item (no description) for email {item.gmail_message_id}")
                    continue

                # Derive a safe item name
                raw_name = item_name_raw
                if not raw_name:
                    desc_text = item_desc_raw or "Service"
                    first_sentence = desc_text.split(".")[0].split("!")[0].split("?")[0].strip()
                    first_words    = " ".join(desc_text.split()[:6])
                    raw_title      = first_sentence if len(first_sentence) <= len(first_words) else first_words
                    raw_name       = raw_title
                
                safe_name = raw_name[:60].rstrip() + ("…" if len(raw_name) > 60 else "")
                
                desc = item_desc_raw
                if not desc:
                    desc = safe_name

                line_items.append({
                    "name": safe_name,
                    "description": desc,
                    "rate": item_amount,
                    "quantity": 1,
                })
                total_amount += item_amount

            if not line_items:
                return ChatResponse(
                    reply="All selected items have zero amount and no description — nothing to invoice.",
                    action="error"
                )
        
            invoice = await create_invoice(
                contact_id=batch.zoho_contact_id,
                task_description="",
                amount=0.0,
                currency=items_to_process[0].data.currency or "USD",
                db=db,
                line_items=line_items,
            )
            for item in items_to_process:
                await mark_email_processed(item.gmail_message_id, invoice.get("invoice_id", ""), db)
                
            email_sent = False
            if send_email:
                email_sent, _ = await send_invoice_email(invoice.get("invoice_id", ""), db, to_email=batch.client_email)
                
            created = CreatedInvoice(
                zoho_invoice_id=invoice.get("invoice_id", ""),
                invoice_number=invoice.get("invoice_number", ""),
                client_name=batch.client_name,
                client_email=batch.client_email,
                amount=total_amount,
                currency=items_to_process[0].data.currency or "USD",
                invoice_url=invoice.get("invoice_url"),
                email_sent=email_sent,
            )
            _recent_invoices.append(created)
            created_invoices_list.append(created)
            
        else: # separate or draft
            if mode == "draft":
                send_email = False
                
            for item in items_to_process:
                item_amount = item.data.amount or 0.0
                desc = _clean(item.data.task_description)
                name = _clean(item.data.item_name)

                # Skip if no amount AND no description/name — nothing to invoice.
                if item_amount == 0.0 and not desc and not name:
                    logger.info(f"Skipping zero-amount, no-description item for email {item.gmail_message_id}")
                    continue

                if not desc:
                    desc = name or "Service"
                    
                invoice = await create_invoice(
                    contact_id=batch.zoho_contact_id,
                    task_description=desc,
                    amount=item_amount,
                    currency=item.data.currency or "USD",
                    item_name=name or desc[:60] or "Service",
                    db=db,
                )
                await mark_email_processed(item.gmail_message_id, invoice.get("invoice_id", ""), db)
                
                email_sent = False
                if send_email:
                    email_sent, _ = await send_invoice_email(invoice.get("invoice_id", ""), db, to_email=batch.client_email)
                    
                created = CreatedInvoice(
                    zoho_invoice_id=invoice.get("invoice_id", ""),
                    invoice_number=invoice.get("invoice_number", ""),
                    client_name=batch.client_name,
                    client_email=batch.client_email,
                    amount=item.data.amount or 0.0,
                    currency=item.data.currency or "USD",
                    invoice_url=invoice.get("invoice_url"),
                    email_sent=email_sent,
                )
                _recent_invoices.append(created)
                created_invoices_list.append(created)
                
        _pending_batches.pop(batch_draft_id, None)
        
        # Build reply
        if mode == "combined":
            if send_email:
                reply = f"Combined invoice for **{batch.client_name}** created and sent! 📧"
            else:
                reply = f"Combined invoice for **{batch.client_name}** saved as draft. 💾"
        else:
            if send_email:
                reply = f"Created and sent {len(created_invoices_list)} individual invoices for **{batch.client_name}**. 📧"
            else:
                reply = f"Saved {len(created_invoices_list)} individual invoices for **{batch.client_name}** as drafts. 💾"
                
        # Force cache refresh to keep stats/dashboard in sync
        try:
            await ensure_fresh_cache(db, force=True)
        except Exception as cache_err:
            logger.error(f"Failed to force refresh cache after batch invoice approval: {cache_err}")

        return ChatResponse(
            reply=reply,
            action="invoice_created",
            invoices_created=created_invoices_list,
        )
        
    except Exception as e:
        logger.error(f"Failed to process batch {batch_draft_id}: {e}")
        return ChatResponse(reply=f"Failed to process invoices: {e}", action="error")


async def approve_manual_invoice(
    draft_id: str,
    send_email: bool,
    db: AsyncSession,
    client_name: str | None = None,
    client_email: str | None = None,
    line_items: list | None = None,
) -> ChatResponse:
    draft = _pending_manual_invoice_drafts.get(draft_id)
    if not draft:
        return ChatResponse(
            reply="This manual invoice draft has expired or was already processed.",
            action="error",
        )

    # Apply any edits the user made in the confirmation card
    if client_name:
        draft.client_name = client_name.strip()
    if client_email is not None:
        draft.client_email = client_email.strip() or None
    if line_items:
        draft.line_items = [
            ManualInvoiceLineItem(
                item_name=li.get("item_name", "Service") if isinstance(li, dict) else li.item_name,
                task_description=li.get("task_description", "") if isinstance(li, dict) else li.task_description,
                amount=float(li.get("amount", 0)) if isinstance(li, dict) else float(li.amount),
            )
            for li in line_items
        ]

    contact_id = draft.zoho_contact_id
    try:
        if not contact_id:
            if not draft.client_name:
                return ChatResponse(reply="Missing client name for this invoice.", action="error")
            contact_id = await create_contact(draft.client_name, draft.client_email, db)
            draft.zoho_contact_id = contact_id
        elif draft.client_email:
            asyncio.create_task(update_contact_email(contact_id, draft.client_email, db))

        total_amount = sum(item.amount for item in draft.line_items)
        line_items_payload = [
            {
                "name": item.item_name,
                "description": item.task_description,
                "rate": item.amount,
                "quantity": 1,
            }
            for item in draft.line_items
        ]

        if draft.estimate_id:
            invoice = await convert_estimate_to_invoice(
                estimate_id=draft.estimate_id,
                db=db,
                line_items=line_items_payload,
            )
        else:
            invoice = await create_invoice(
                contact_id=contact_id,
                task_description="",
                amount=0.0,
                currency=draft.currency or "INR",
                db=db,
                line_items=line_items_payload,
            )

        email_sent = False
        if send_email:
            email_sent, _ = await send_invoice_email(
                invoice.get("invoice_id", ""),
                db,
                to_email=draft.client_email,
            )

        created = CreatedInvoice(
            zoho_invoice_id=invoice.get("invoice_id", ""),
            invoice_number=invoice.get("invoice_number", ""),
            client_name=draft.client_name,
            client_email=draft.client_email,
            amount=total_amount,
            currency=draft.currency or "INR",
            invoice_url=invoice.get("invoice_url"),
            email_sent=email_sent,
        )
        _recent_invoices.append(created)
        _pending_manual_invoice_drafts.pop(draft_id, None)

        if send_email and email_sent:
            reply = f"Invoice **#{created.invoice_number}** created and sent to **{draft.client_name}**. 📧"
        elif send_email and not email_sent:
            reply = (
                f"Invoice **#{created.invoice_number}** created for **{draft.client_name}**, "
                "but I couldn't send the email automatically."
            )
        else:
            reply = (
                f"Invoice **#{created.invoice_number}** created for **{draft.client_name}** — "
                f"{created.currency} {created.amount:,.2f} ✅"
            )

        # Force cache refresh to keep stats/dashboard in sync
        try:
            await ensure_fresh_cache(db, force=True)
        except Exception as cache_err:
            logger.error(f"Failed to force refresh cache after manual invoice approval: {cache_err}")

        return ChatResponse(
            reply=reply,
            action="invoice_created",
            invoices_created=[created],
        )
    except Exception as e:
        logger.error(f"Failed to create manual invoice {draft_id}: {e}")
        return ChatResponse(reply=f"Failed to create manual invoice: {e}", action="error")



async def multi_estimate_action(
    estimate_ids: list[str],
    mode: str,  # "accept" | "reject" | "convert"
    db: AsyncSession,
) -> ChatResponse:
    """
    Apply accept / reject / convert to multiple estimates at once.
    Returns a summary ChatResponse with counts.
    """
    succeeded: list[str] = []
    failed: list[str] = []
    convert_drafts: list[ManualInvoiceDraft] = []

    for eid in estimate_ids:
        try:
            if mode in ("accept", "reject"):
                zoho_status = "accepted" if mode == "accept" else "declined"
                updated = await update_estimate_status(eid, zoho_status, db)
                _mark_estimate_status(eid, zoho_status)
                succeeded.append(updated.get("estimate_number") or eid)

            elif mode == "convert":
                details = await get_estimate(eid, db)
                status = (details.get("status") or "").lower()
                if status in ("invoiced", "partially_invoiced"):
                    failed.append(f"#{details.get('estimate_number')} (already invoiced)")
                    continue
                draft = await _build_invoice_draft_from_estimate(details)
                convert_drafts.append(draft)
                succeeded.append(details.get("estimate_number") or eid)
        except Exception as e:
            logger.error(f"multi_estimate_action: {mode} {eid} failed: {e}")
            failed.append(eid)

    if mode == "convert" and convert_drafts:
        # For convert: return the first draft for approval
        # (converting multiple at once just queues first; user can repeat)
        if len(convert_drafts) == 1:
            d = convert_drafts[0]
            reply = f"Here is the draft invoice from the selected estimate for **{d.client_name}**. Approve below to create it:"
            return ChatResponse(reply=reply, action="manual_invoice_pending", manual_invoice_draft=d)
        # Multiple converts — return each draft card
        # For now return first one with a note
        d = convert_drafts[0]
        reply = (
            f"I've queued **{len(convert_drafts)}** estimate conversions. "
            f"Showing the first one for **{d.client_name}** — approve it, then I'll show the next."
        )
        return ChatResponse(reply=reply, action="manual_invoice_pending", manual_invoice_draft=d)

    if mode == "accept":
        verb = "accepted"
        emoji = "✅"
    else:
        verb = "declined"
        emoji = "✗"

    parts = []
    if succeeded:
        nums = ", ".join(f"**#{n}**" for n in succeeded)
        parts.append(f"{emoji} {len(succeeded)} estimate(s) {verb}: {nums}")
    if failed:
        parts.append(f"⚠️ {len(failed)} could not be processed: {', '.join(failed)}")

    reply = "\n".join(parts) if parts else "No estimates were processed."
    action = f"estimate_{verb}" if succeeded else "error"
    return ChatResponse(reply=reply, action=action)


async def approve_estimate_draft(

    draft_id: str,
    send_email: bool,
    db: AsyncSession,
    client_name: str | None = None,
    client_email: str | None = None,
    line_items: list | None = None,
) -> ChatResponse:
    draft = _pending_estimate_drafts.get(draft_id)
    if not draft:
        return ChatResponse(
            reply="This estimate draft has expired or was already processed.",
            action="error",
        )

    # Apply any edits the user made in the confirmation card
    if client_name:
        draft.client_name = client_name.strip()
    if client_email is not None:
        draft.client_email = client_email.strip() or None
    if line_items:
        draft.line_items = [
            EstimateLineItem(
                item_name=li.get("item_name", "Service") if isinstance(li, dict) else li.item_name,
                description=li.get("description", "") if isinstance(li, dict) else li.description,
                amount=float(li.get("amount", 0)) if isinstance(li, dict) else float(li.amount),
            )
            for li in line_items
        ]

    contact_id = draft.zoho_contact_id
    try:
        if not contact_id:
            if not draft.client_name:
                return ChatResponse(reply="Missing client name for this estimate.", action="error")
            contact_id = await create_contact(draft.client_name, draft.client_email, db)
            draft.zoho_contact_id = contact_id
        elif draft.client_email:
            asyncio.create_task(update_contact_email(contact_id, draft.client_email, db))

        total_amount = sum(item.amount for item in draft.line_items)
        line_items_payload = [
            {
                "name": item.item_name,
                "description": item.description,
                "rate": item.amount,
                "quantity": 1,
            }
            for item in draft.line_items
        ]

        estimate = await create_estimate(
            customer_id=contact_id,
            line_items=line_items_payload,
            currency=draft.currency or "INR",
            db=db,
        )

        email_sent = False
        if send_email:
            email_sent, _ = await send_estimate_email(
                estimate.get("estimate_id", ""),
                db,
                to_email=draft.client_email,
            )

        created = CreatedEstimate(
            zoho_estimate_id=estimate.get("estimate_id", ""),
            estimate_number=estimate.get("estimate_number", ""),
            client_name=draft.client_name,
            client_email=draft.client_email,
            amount=total_amount,
            currency=draft.currency or "INR",
            estimate_url=estimate.get("estimate_url"),
            email_sent=email_sent,
        )
        _pending_estimate_drafts.pop(draft_id, None)

        if send_email and email_sent:
            reply = f"Estimate **#{created.estimate_number}** created and sent to **{draft.client_name}**. 📧"
        elif send_email and not email_sent:
            reply = (
                f"Estimate **#{created.estimate_number}** created for **{draft.client_name}**, "
                "but I couldn't send the email automatically."
            )
        else:
            reply = (
                f"Estimate **#{created.estimate_number}** created for **{draft.client_name}** — "
                f"{created.currency} {created.amount:,.2f} ✅"
            )

        # Force cache refresh to keep stats/dashboard in sync
        try:
            await ensure_fresh_cache(db, force=True)
        except Exception as cache_err:
            logger.error(f"Failed to force refresh cache after manual estimate approval: {cache_err}")

        return ChatResponse(
            reply=reply,
            action="estimate_created",
            estimates_created=[created],
        )

    except Exception as e:
        logger.error(f"Failed to process estimate {draft_id}: {e}")
        return ChatResponse(reply=f"Failed to process estimate: {e}", action="error")


async def approve_payment_record(draft_id: str, overrides: dict, db: AsyncSession) -> ChatResponse:
    """Record payment in Zoho from approved draft."""
    draft = _pending_payment_drafts.get(draft_id)
    if not draft:
        return ChatResponse(
            reply="This payment draft has expired or was already recorded. Please try again.",
            action="error",
        )

    amount = float(overrides.get("amount") or draft.amount)
    payment_date = overrides.get("payment_date") or draft.payment_date
    payment_mode = overrides.get("payment_mode") or draft.payment_mode

    try:
        # Step 1: Write to Zoho customer payments
        payment_info = await record_payment(
            customer_id=draft.customer_id,
            invoice_id=draft.invoice_id,
            amount=amount,
            payment_date=payment_date,
            payment_mode=payment_mode,
            db=db,
        )

        # Step 2: Pop from memory store ONLY after API call succeeds
        _pending_payment_drafts.pop(draft_id, None)

        # Step 3: Update local DB cache (InvoiceCache)
        res = await db.execute(
            select(InvoiceCache).where(InvoiceCache.invoice_id == draft.invoice_id)
        )
        row = res.scalar()
        if row:
            new_balance = float(row.balance) - amount
            row.balance = max(0.0, round(new_balance, 2))
            if row.balance == 0:
                row.status = "paid"
            else:
                row.status = "partially_paid"
            await db.commit()

        currency_sym = "₹" if draft.currency == "INR" else f"{draft.currency} "
        reply = (
            f"✅ Offline payment of **{currency_sym}{amount:,.2f}** recorded successfully in Zoho "
            f"against invoice **#{draft.invoice_id}** (Method: {payment_mode.upper()}, Date: {payment_date})."
        )

        # Force cache refresh to keep stats/dashboard in sync
        try:
            await ensure_fresh_cache(db, force=True)
        except Exception as cache_err:
            logger.error(f"Failed to force refresh cache after payment recording: {cache_err}")

        return ChatResponse(
            reply=reply,
            action="payment_recorded",
        )

    except Exception as e:
        logger.error(f"Failed to record manual payment: {e}")
        return ChatResponse(
            reply=f"⚠️ Failed to record payment in Zoho: {e}",
            action="error",
        )

