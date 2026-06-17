import uuid
import asyncio
import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from backend.schemas import (
    ChatResponse, DraftInvoice, CreatedInvoice,
    AmbiguousContact, InvoiceData, ApproveRequest,
)
from backend.services.groq_service import parse_intent
from backend.services.gemini_service import extract_invoice_data
from backend.services.gmail_service import search_gmail
from backend.services.zoho_service import (
    search_contact_by_name, create_contact, create_invoice, mark_email_processed,
)

logger = logging.getLogger(__name__)

# In-memory draft store (single-user prototype — cleared on server restart)
_pending_drafts: dict[str, DraftInvoice] = {}


def get_pending_draft(draft_id: str) -> Optional[DraftInvoice]:
    return _pending_drafts.get(draft_id)


def _should_auto_create(data: InvoiceData, contact_id: Optional[str]) -> bool:
    return (
        data.confidence == "high"
        and data.amount is not None
        and data.task_description is not None
        and contact_id is not None # Require an existing contact for auto-creation
    )


async def process_chat(message: str, db: AsyncSession) -> ChatResponse:
    # ── 1. Parse intent ─────────────────────────────────────────────────────
    intent = await parse_intent(message)
    person_name: Optional[str] = intent.get("person_name")
    date_filter: Optional[str] = intent.get("date_filter")
    logger.info(f"Intent parsed: {intent}")

    # ── 2. Resolve contact name ─────────────────────────────────────────────
    person_email: Optional[str] = None
    resolved_contact_id: Optional[str] = None

    if person_name:
        try:
            contacts = await search_contact_by_name(person_name, db)
        except RuntimeError as e:
            return ChatResponse(reply=str(e), action="error")

        if len(contacts) == 0:
            return ChatResponse(
                reply=(
                    f"I couldn't find **{person_name}** in your Zoho contacts. "
                    "Check the spelling or add them as a contact in Zoho first."
                ),
                action="clarification_needed",
            )
        elif len(contacts) > 1:
            names = "\n".join(
                f"• {c['contact_name']} ({c.get('email') or 'no email'})"
                for c in contacts
            )
            return ChatResponse(
                reply=f"I found {len(contacts)} contacts named **{person_name}**. Which one?\n\n{names}",
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
    if not person_email and not date_filter:
        return ChatResponse(
            reply=(
                "Could you be more specific? Try something like:\n"
                "• \"Make an invoice for James\"\n"
                "• \"Look at emails from today\"\n"
                "• \"Invoice Sarah for the website work this week\""
            ),
            action="clarification_needed",
        )

    # ── 3. Search Gmail ─────────────────────────────────────────────────────
    try:
        emails = await search_gmail(
            db, person_email=person_email, date_filter=date_filter
        )
    except (ValueError, RuntimeError) as e:
        return ChatResponse(reply=str(e), action="clarification_needed" if isinstance(e, ValueError) else "error")

    if not emails:
        return ChatResponse(
            reply="No new matching emails found. They may have already been processed, or nothing matched the search.",
            action="emails_scanned",
        )

    # ── 4–6. Extract → Decide → Create ─────────────────────────────────────
    created_invoices: list[CreatedInvoice] = []
    pending_drafts: list[DraftInvoice] = []
    skipped = 0

    for i, email in enumerate(emails):
        email_text = (email.get("body") or email.get("subject") or "").strip()
        if not email_text:
            skipped += 1
            continue

        # Small delay between Gemini calls to stay within free-tier RPM limits
        if i > 0:
            await asyncio.sleep(1.5)

        try:
            raw = await extract_invoice_data(email_text)
            data = InvoiceData(**raw)

            if not data.is_confirmation:
                skipped += 1
                logger.info(f"Email {email['id']} skipped — not a confirmation.")
                continue

            # For scan_emails path: try to match contact from extracted email
            contact_id = resolved_contact_id
            if not contact_id and data.client_name:
                try:
                    matches = await search_contact_by_name(data.client_name, db)
                    if len(matches) == 1:
                        contact_id = matches[0]["contact_id"]
                except Exception:
                    pass

            if _should_auto_create(data, contact_id):
                invoice = await create_invoice(
                    contact_id=contact_id,
                    task_description=data.task_description,
                    amount=data.amount,
                    currency=data.currency or "USD",
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
                )
                _pending_drafts[draft_id] = draft
                pending_drafts.append(draft)
                logger.info(
                    f"Draft queued for {data.client_name} "
                    f"(confidence={data.confidence}, missing={data.missing_fields})"
                )

        except Exception as e:
            err_str = str(e)
            logger.error(f"Failed to process email {email['id']}: {err_str}")
            if "quota" in err_str.lower() or "rate" in err_str.lower() or "429" in err_str:
                # Surface rate limit errors immediately — no point continuing
                return ChatResponse(
                    reply=f"⚠️ Gemini API rate limit hit. Please wait a minute and try again, or reduce the number of emails being scanned.\n\nError: {err_str[:200]}",
                    action="error",
                )
            skipped += 1

    # ── Build reply ─────────────────────────────────────────────────────────
    parts = []
    if created_invoices:
        parts.append(f"✅ Created **{len(created_invoices)}** invoice(s) automatically.")
    if pending_drafts:
        parts.append(f"📋 **{len(pending_drafts)}** draft(s) need your review below.")
    if skipped:
        parts.append(f"⏭ Skipped **{skipped}** email(s) (not invoice-related or failed).")
    if not parts:
        parts.append("Nothing to process.")

    final_action = (
        "invoice_created" if created_invoices and not pending_drafts
        else "draft_pending" if pending_drafts
        else "emails_scanned"
    )

    return ChatResponse(
        reply="\n".join(parts),
        action=final_action,
        invoices_created=created_invoices or None,
        drafts=pending_drafts or None,
    )


async def approve_draft(draft_id: str, overrides: dict, db: AsyncSession) -> ChatResponse:
    """Create a Zoho invoice from a user-approved draft."""
    draft = _pending_drafts.get(draft_id)
    if not draft:
        return ChatResponse(
            reply="This draft has expired or was already processed. Please run the search again.",
            action="error",
        )

    data = draft.data.model_copy()
    if overrides.get("task_description"):
        data.task_description = overrides["task_description"]
    if overrides.get("amount"):
        data.amount = float(overrides["amount"])
    if overrides.get("currency"):
        data.currency = overrides["currency"]
    if overrides.get("client_name"):
        data.client_name = overrides["client_name"]

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
            db=db,
        )
        await mark_email_processed(
            draft.gmail_message_id, invoice.get("invoice_id", ""), db
        )
        _pending_drafts.pop(draft_id, None)

        return ChatResponse(
            reply=(
                f"✅ Invoice **#{invoice.get('invoice_number', '')}** created for "
                f"**{data.client_name}** — {data.currency} {data.amount:,.2f}"
            ),
            action="invoice_created",
            invoices_created=[CreatedInvoice(
                zoho_invoice_id=invoice.get("invoice_id", ""),
                invoice_number=invoice.get("invoice_number", ""),
                client_name=data.client_name or "",
                amount=data.amount,
                currency=data.currency or "USD",
                invoice_url=invoice.get("invoice_url"),
            )],
        )
    except Exception as e:
        logger.error(f"Failed to create invoice from draft {draft_id}: {e}")
        return ChatResponse(reply=f"Failed to create invoice: {e}", action="error")
