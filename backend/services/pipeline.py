import uuid
import asyncio
import logging
from collections import deque
from email.utils import parseaddr
from typing import Optional
import re

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from backend.schemas import (
    ChatResponse, DraftInvoice, CreatedInvoice, PaymentInvoice,
    AmbiguousContact, InvoiceData, ApproveRequest, BatchDraft, BatchDraftItem,
    ManualInvoiceConversation, ManualInvoiceDraft, ManualInvoiceLineItem,
)
from backend.services.groq_service import parse_intent, extract_manual_invoice_request
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
)
from backend.db.models import OAuthToken, ProcessedEmail, ContactCache, InvoiceCache

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_DONE_WORK_RE = re.compile(
    r"work on\s+(?:the\s+)?(?P<project>.+?)\s+is done",
    re.IGNORECASE | re.DOTALL,
)
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
_manual_invoice_conversation: Optional[ManualInvoiceConversation] = None

# Tracks the last N invoices created this session so the user can say
# "send the invoice just created" or "send all invoices created today"
_recent_invoices: deque[CreatedInvoice] = deque(maxlen=20)


def get_pending_draft(draft_id: str) -> Optional[DraftInvoice]:
    return _pending_drafts.get(draft_id)

def get_pending_batch(batch_id: str) -> Optional[BatchDraft]:
    return _pending_batches.get(batch_id)


def get_recent_invoices() -> list[CreatedInvoice]:
    return list(_recent_invoices)


def get_pending_manual_invoice(draft_id: str) -> Optional[ManualInvoiceDraft]:
    return _pending_manual_invoice_drafts.get(draft_id)


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


async def _handle_manual_invoice_conversation(
    message: str,
    db: AsyncSession,
) -> Optional[ChatResponse]:
    global _manual_invoice_conversation
    state = _manual_invoice_conversation
    if not state:
        return None

    text = (message or "").strip()

    if state.step == "awaiting_customer":
        parsed = await extract_manual_invoice_request(text)
        if not _message_supports_manual_invoice_payload(text, parsed):
            parsed = {
                "client_name": None,
                "client_email": None,
                "currency": None,
                "send_email": None,
                "items": [],
            }
        proposed_name = parsed.get("client_name") or text
        proposed_email = parsed.get("client_email")
        name, email, contact_id, is_new, clarification = await _resolve_manual_customer(
            proposed_name,
            proposed_email,
            db,
        )
        state.client_name = name
        state.client_email = email
        state.zoho_contact_id = contact_id
        state.is_new_contact = is_new
        if clarification:
            if is_new and not email:
                state.step = "awaiting_customer_email"
            return ChatResponse(reply=clarification, action="clarification_needed")

        if parsed.get("currency"):
            state.currency = parsed["currency"]
        parsed_items = [
            ManualInvoiceLineItem(**item)
            for item in (parsed.get("items") or [])
            if item.get("item_name") and item.get("task_description") and item.get("amount") is not None
        ]
        if parsed_items:
            state.line_items = parsed_items
            state.item_count = len(parsed_items)
            state.send_email = parsed.get("send_email")
            if state.send_email is None:
                state.step = "awaiting_send_choice"
                return ChatResponse(
                    reply="Should I send the invoice email after creating it? Reply with `yes` or `no`.",
                    action="clarification_needed",
                )
            draft = _build_manual_invoice_draft_from_state(state)
            _manual_invoice_conversation = None
            return ChatResponse(
                reply=_manual_invoice_summary(draft),
                action="manual_invoice_pending",
                manual_invoice_draft=draft,
            )

        state.step = "awaiting_item_count"
        return ChatResponse(
            reply=f"Got it — this invoice is for **{state.client_name}**. How many line items should I add?",
            action="clarification_needed",
        )

    if state.step == "awaiting_customer_email":
        email = text if _EMAIL_RE.fullmatch(text) else None
        if not email:
            return ChatResponse(
                reply="Please send a valid customer email address so I can create the new Zoho contact.",
                action="clarification_needed",
            )
        state.client_email = email
        state.is_new_contact = True
        state.step = "awaiting_item_count"
        return ChatResponse(
            reply=f"Perfect — I'll create **{state.client_name}** as a new customer. How many line items should I add?",
            action="clarification_needed",
        )

    if state.step == "awaiting_item_count":
        parsed = await extract_manual_invoice_request(text)
        if not _message_supports_manual_invoice_payload(text, parsed):
            parsed = {
                "client_name": None,
                "client_email": None,
                "currency": None,
                "send_email": None,
                "items": [],
            }
        parsed_items = [
            ManualInvoiceLineItem(**item)
            for item in (parsed.get("items") or [])
            if item.get("item_name") and item.get("task_description") and item.get("amount") is not None
        ]
        if parsed_items:
            state.line_items = parsed_items
            state.item_count = len(parsed_items)
            if parsed.get("currency"):
                state.currency = parsed["currency"]
            state.send_email = parsed.get("send_email")
            if state.send_email is None:
                state.step = "awaiting_send_choice"
                return ChatResponse(
                    reply="Nice. Should I send the invoice email after creating it? Reply with `yes` or `no`.",
                    action="clarification_needed",
                )
            draft = _build_manual_invoice_draft_from_state(state)
            _manual_invoice_conversation = None
            return ChatResponse(
                reply=_manual_invoice_summary(draft),
                action="manual_invoice_pending",
                manual_invoice_draft=draft,
            )

        count_match = re.search(r"\d+", text)
        if not count_match:
            return ChatResponse(
                reply="Tell me the number of line items, like `1`, `2`, or `3`.",
                action="clarification_needed",
            )
        state.item_count = max(1, int(count_match.group()))
        state.step = "awaiting_item_name"
        return ChatResponse(reply="What's the name of item 1?", action="clarification_needed")

    if state.step == "awaiting_item_name":
        state.pending_item_name = text
        state.step = "awaiting_item_description"
        return ChatResponse(
            reply=f"Got it. What's the description for **{state.pending_item_name}**?",
            action="clarification_needed",
        )

    if state.step == "awaiting_item_description":
        state.pending_item_description = text
        state.step = "awaiting_item_amount"
        return ChatResponse(
            reply=f"And what's the price for **{state.pending_item_name}**?",
            action="clarification_needed",
        )

    if state.step == "awaiting_item_amount":
        amount, currency = _parse_amount_and_currency(text)
        if amount is None:
            return ChatResponse(
                reply="Send the amount as a number, for example `5000`, `₹5000`, or `1200 USD`.",
                action="clarification_needed",
            )
        if currency:
            state.currency = currency
        state.line_items.append(
            ManualInvoiceLineItem(
                item_name=state.pending_item_name or "Item",
                task_description=state.pending_item_description or (state.pending_item_name or "Item"),
                amount=amount,
            )
        )
        state.pending_item_name = None
        state.pending_item_description = None

        if len(state.line_items) < state.item_count:
            next_idx = len(state.line_items) + 1
            state.step = "awaiting_item_name"
            return ChatResponse(reply=f"What's the name of item {next_idx}?", action="clarification_needed")

        state.step = "awaiting_send_choice"
        return ChatResponse(
            reply="All set. Should I send the invoice email after creating it? Reply with `yes` or `no`.",
            action="clarification_needed",
        )

    if state.step == "awaiting_send_choice":
        send_email = _parse_yes_no(text)
        if send_email is None:
            return ChatResponse(reply="Please reply with `yes` or `no`.", action="clarification_needed")
        state.send_email = send_email
        draft = _build_manual_invoice_draft_from_state(state)
        _manual_invoice_conversation = None
        return ChatResponse(
            reply=_manual_invoice_summary(draft),
            action="manual_invoice_pending",
            manual_invoice_draft=draft,
        )

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
    _recent_invoices.clear()
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

    if action == "check_overdue":
        rows = await get_overdue(db)
        reply = format_payment_response(
            rows,
            title="**Overdue invoices** (oldest first):",
            empty_message="Good news — you have no overdue invoices right now. ✅",
        )
        return ChatResponse(reply=reply, action="payment_status", payment_invoices=_payment_invoices(rows))

    if action == "check_pending":
        overdue_rows = await get_overdue(db)
        pending_rows = await get_pending(db)
        rows = overdue_rows + pending_rows
        reply = format_payment_response(
            rows,
            title="**Unpaid invoices:**",
            empty_message="No unpaid invoices — everything has been paid. ✅",
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


async def process_chat(
    message: str,
    db: AsyncSession,
    status_cb=None,          # optional async callable(str) for SSE status updates
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

    # ── Handle greetings and unclear intents naturally ───────────────────────
    if action == "greeting":
        return ChatResponse(
            reply=(
                "Hey! 👋 Happy to help. You can ask me things like:\n"
                "• *\"Invoice Piyusha for yesterday's design work\"*\n"
                "• *\"Check emails from last Sunday\"*\n"
                "• *\"Who hasn't paid me?\"* or *\"Payment summary\"*\n\n"
                "What would you like to do?"
            ),
            action="clarification_needed",
        )
    if action == "unknown":
        return ChatResponse(
            reply=(
                "Hmm, I'm not quite sure what you need. You could try something like:\n"
                "• *\"Invoice Piyusha for the design work from yesterday\"*\n"
                "• *\"Look at emails from last Sunday\"*\n"
                "• *\"Make an invoice for Rahul for the website project\"*"
            ),
            action="clarification_needed",
        )

    if action == "create_invoice" and (
        "new invoice" in normalized_message
        or "manual invoice" in normalized_message
        or "from scratch" in normalized_message
        or "without email" in normalized_message
        or "dont read email" in normalized_message
        or "don't read email" in normalized_message
    ):
        parsed = await extract_manual_invoice_request(message)
        if parsed.get("is_manual_invoice_request") and _message_supports_manual_invoice_payload(message, parsed):
            items = [
                ManualInvoiceLineItem(**item)
                for item in (parsed.get("items") or [])
                if item.get("item_name") and item.get("task_description") and item.get("amount") is not None
            ]
            client_name = parsed.get("client_name")
            client_email = parsed.get("client_email")
            currency = parsed.get("currency") or "USD"

            if client_name:
                name, email, contact_id, is_new, clarification = await _resolve_manual_customer(
                    client_name,
                    client_email,
                    db,
                )
                if clarification and (not items or not email):
                    _manual_invoice_conversation = ManualInvoiceConversation(
                        step="awaiting_customer_email" if is_new and not email and name else "awaiting_customer",
                        client_name=name,
                        client_email=email,
                        currency=currency,
                        zoho_contact_id=contact_id,
                        is_new_contact=is_new,
                    )
                    return ChatResponse(reply=clarification, action="clarification_needed")

                if items:
                    draft = ManualInvoiceDraft(
                        draft_id=str(uuid.uuid4()),
                        client_name=name or client_name,
                        client_email=email,
                        currency=currency,
                        zoho_contact_id=contact_id,
                        is_new_contact=is_new,
                        line_items=items,
                    )
                    _pending_manual_invoice_drafts[draft.draft_id] = draft
                    return ChatResponse(
                        reply=_manual_invoice_summary(draft),
                        action="manual_invoice_pending",
                        manual_invoice_draft=draft,
                    )

                _manual_invoice_conversation = ManualInvoiceConversation(
                    step="awaiting_item_count",
                    client_name=name or client_name,
                    client_email=email,
                    currency=currency,
                    zoho_contact_id=contact_id,
                    is_new_contact=is_new,
                    send_email=parsed.get("send_email"),
                )
                return ChatResponse(
                    reply=f"Okay — creating a new invoice for **{name or client_name}**. How many line items should I add?",
                    action="clarification_needed",
                )

        _manual_invoice_conversation = ManualInvoiceConversation(step="awaiting_customer")
        return ChatResponse(
            reply=(
                "Sure — let's create a new invoice.\n\n"
                "Who is it for? If it's an existing customer, send the name.\n"
                "If it's a new customer, send the name and email."
            ),
            action="clarification_needed",
        )

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
                "• *\"Invoice Piyusha for yesterday's work\"*\n"
                "• *\"Check emails from last Sunday\"*\n"
                "• *\"Make an invoice for Rahul for the website project\"*"
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
) -> ChatResponse:
    draft = _pending_manual_invoice_drafts.get(draft_id)
    if not draft:
        return ChatResponse(
            reply="This manual invoice draft has expired or was already processed.",
            action="error",
        )

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
        line_items = [
            {
                "name": item.item_name,
                "description": item.task_description,
                "rate": item.amount,
                "quantity": 1,
            }
            for item in draft.line_items
        ]

        invoice = await create_invoice(
            contact_id=contact_id,
            task_description="",
            amount=0.0,
            currency=draft.currency or "USD",
            db=db,
            line_items=line_items,
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
            currency=draft.currency or "USD",
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

        return ChatResponse(
            reply=reply,
            action="invoice_created",
            invoices_created=[created],
        )
    except Exception as e:
        logger.error(f"Failed to create manual invoice {draft_id}: {e}")
        return ChatResponse(reply=f"Failed to create manual invoice: {e}", action="error")
