import json
import logging
import asyncio

from groq import Groq
from backend.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_client = Groq(api_key=settings.GROQ_API_KEY)

_SYSTEM_PROMPT = """You are an intent parser for a smart invoicing assistant. Your job is to understand what the user wants and extract structured data from their message.

Return ONLY a valid JSON object with this exact structure:
{
  "action": "create_invoice" | "scan_emails" | "approve_draft" | "decline_draft" | "send_invoices" | "check_overdue" | "check_pending" | "check_specific_payment" | "payment_summary" | "create_recurring" | "list_recurring" | "stop_recurring" | "greeting" | "unknown",
  "person_name": "<name or null>",
  "date_filter": "today" | "yesterday" | "this_week" | "last_week" | "last_sunday" | "last_monday" | "last_tuesday" | "last_wednesday" | "last_thursday" | "last_friday" | "last_saturday" | "this_sunday" | "this_monday" | "this_tuesday" | "this_wednesday" | "this_thursday" | "this_friday" | "this_saturday" | null,
  "keywords": ["<keyword1>", "<keyword2>"]
}

Rules:
- action:
  - "create_invoice" when user mentions making/creating/drafting an invoice or billing someone
  - "scan_emails" when user wants to look at, review, check, or find emails
  - "approve_draft" when user wants to confirm, approve, send, go ahead, yes, create it, do it, looks good, proceed — for a pending draft invoice
  - "decline_draft" when user wants to cancel, dismiss, reject, no, skip, discard — a pending draft invoice
  - "send_invoices" when user wants to email/send/dispatch an already-created invoice to a client (e.g. "send the invoice", "send it to Vismay", "email the invoice just created", "send all invoices")
  - "check_overdue" only when user explicitly asks about overdue or past-due invoices (e.g. "Show overdue invoices", "Which invoices are past due?")
  - "check_pending" when user asks who hasn't paid or asks about pending/unpaid/outstanding invoices (e.g. "Who hasn't paid me?", "What's pending payment?", "Any unpaid invoices?")
  - "check_specific_payment" when user asks if a specific person paid (e.g. "Did Rahul pay?", "Has Piyusha paid?") — set person_name to that client name
  - "payment_summary" when user wants an overview of owed/received amounts (e.g. "Payment summary", "How much am I owed?")
  - "create_recurring" when user wants to set up a recurring / repeating / subscription invoice (e.g. "recurring invoice", "monthly invoice", "bill every month", "set up recurring")
  - "list_recurring" when user wants to see active recurring invoices (e.g. "show recurring", "list recurring", "active recurring invoices")
  - "stop_recurring" when user wants to stop/cancel/pause a recurring invoice (e.g. "stop recurring", "cancel recurring invoice", "pause billing")
  - "greeting" when user just says hi, hello, hey, thanks, etc. — no task intended
  - "unknown" if genuinely unclear
- person_name: the PERSON's name if explicitly mentioned (e.g. "Piyusha", "James", "Rahul"). Required for check_specific_payment. null otherwise. Do NOT infer names from context.
- date_filter: extract the time period from the message. Examples:
    - "yesterday" → "yesterday"
    - "today" → "today"
    - "this week" / "last 7 days" → "this_week"
    - "last week" → "last_week"
    - "last Sunday" / "on Sunday" → "last_sunday"
    - "last Monday" → "last_monday"
    - Day names without "last" that refer to a past day (e.g. today is Tuesday and user says "Monday") → "last_monday"
    - If no time period mentioned → null
- keywords: 1-3 meaningful topic keywords (project names, "invoice", "payment", "website", "design"). Exclude stop words and person names. Return [] if no useful keywords.
- CRITICAL: Return ONLY the JSON object. No markdown fences. No explanation. No prose. Invalid JSON will break the app."""

_MANUAL_INVOICE_PROMPT = """You extract manual invoice details from a user's message.

Return ONLY a valid JSON object with this exact structure:
{
  "is_manual_invoice_request": true,
  "currency": "<3-letter ISO code or null>",
  "send_email": true | false | null,
  "items": [
    {
      "item_name": "<short item/service name>",
      "task_description": "<full description>",
      "amount": <number>
    }
  ]
}

Rules:
- Extract only when the user is trying to create a brand-new invoice manually, not asking to read emails.
- If an amount is in rupees/INR/₹, set currency to "INR".
- If the message doesn't specify whether to email the invoice, set send_email to null.
- If item details are incomplete, return an empty items array.
- If the message is not a manual invoice request, set is_manual_invoice_request to false and all other fields to null/empty.
- Return only JSON."""


async def parse_intent(message: str) -> dict:
    """Call Groq to parse the user's natural-language message into structured intent."""
    normalized = " ".join(message.lower().replace("’", "'").split())

    # These common payment questions should not depend on model interpretation:
    # "hasn't paid" means outstanding, while "overdue" is explicitly past due.
    if (
        normalized == "payment summary"
        or "summary of payments" in normalized
        or "payment overview" in normalized
        or "how much am i owed" in normalized
        or "how much is owed" in normalized
        or "payments overview" in normalized
    ):
        return {"action": "payment_summary", "person_name": None, "date_filter": None, "keywords": ["payment"]}
    if "overdue" in normalized or "past due" in normalized:
        return {"action": "check_overdue", "person_name": None, "date_filter": None, "keywords": ["payment"]}
    if (
        "who hasn't paid" in normalized
        or "who has not paid" in normalized
        or "unpaid" in normalized
        or "outstanding" in normalized
    ):
        return {"action": "check_pending", "person_name": None, "date_filter": None, "keywords": ["payment"]}

    # ── Recurring invoice shortcuts ───────────────────────────────────────────
    _RECURRING_WORDS = ("recurring", "repeating", "subscription", "every month", "every week", "every year", "monthly invoice", "weekly invoice", "yearly invoice")
    has_recurring = any(w in normalized for w in _RECURRING_WORDS)
    if has_recurring:
        if any(w in normalized for w in ("stop", "cancel", "pause", "end", "disable")):
            return {"action": "stop_recurring", "person_name": None, "date_filter": None, "keywords": []}
        if any(w in normalized for w in ("list", "show", "view", "active", "all")):
            return {"action": "list_recurring", "person_name": None, "date_filter": None, "keywords": []}
        # Default: intent to create
        return {"action": "create_recurring", "person_name": None, "date_filter": None, "keywords": []}

    # ── Date-filter guard ────────────────────────────────────────────────────
    # If the message mentions a time window AND a scan verb, it's always a
    # Gmail scan — even if the word "invoice" appears.
    # This prevents "check invoice from today" mapping to payment_summary.
    _DATE_WORDS = (
        "today", "yesterday", "this week", "last week",
        "monday", "tuesday", "wednesday", "thursday",
        "friday", "saturday", "sunday",
    )
    _SCAN_VERBS = ("check", "get", "fetch", "scan", "find", "look", "show", "read", "review")
    has_date = any(d in normalized for d in _DATE_WORDS)
    has_scan_verb = any(v in normalized for v in _SCAN_VERBS)
    if has_date and has_scan_verb:
        # Let the LLM parse the date_filter correctly, but force action=scan_emails
        try:
            response = await asyncio.to_thread(
                _client.chat.completions.create,
                model="llama-3.1-8b-instant",
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": message},
                ],
                response_format={"type": "json_object"},
                temperature=0,
                max_tokens=150,
            )
            parsed = json.loads(response.choices[0].message.content)
            parsed["action"] = "scan_emails"   # override regardless of LLM guess
            return parsed
        except Exception:
            pass
        return {"action": "scan_emails", "person_name": None, "date_filter": "today", "keywords": []}

    try:
        response = await asyncio.to_thread(
            _client.chat.completions.create,
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": message},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=150,
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        logger.error(f"Groq intent parsing failed: {e}")
        # Safe fallback — treat as a scan of recent emails
        return {"action": "scan_emails", "person_name": None, "date_filter": "today", "keywords": []}


async def extract_manual_invoice_request(message: str) -> dict:
    """Extract manual invoice details from a free-form user message."""
    try:
        response = await asyncio.to_thread(
            _client.chat.completions.create,
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": _MANUAL_INVOICE_PROMPT},
                {"role": "user", "content": message},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=300,
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        logger.error(f"Manual invoice extraction failed: {e}")
        return {
            "is_manual_invoice_request": False,
            "client_name": None,
            "client_email": None,
            "currency": None,
            "send_email": None,
            "items": [],
        }


_RECURRING_EXTRACT_PROMPT = """You extract recurring invoice details from a user message.

Return ONLY a valid JSON object:
{
  "client_name": "<full name or null>",
  "client_email": "<real email with @ or null>",
  "item_name": "<short 2-5 word service label or null>",
  "task_description": "<description of service or null>",
  "amount": <number or null>,
  "currency": "<3-letter ISO code, default INR>",
  "frequency": "monthly" | "weekly" | "yearly" | "daily" | null,
  "start_date": "<YYYY-MM-DD or 'today' or null>",
  "end_date": "<YYYY-MM-DD or null>"
}

Rules:
- CRITICAL: Only extract information that is explicitly stated or directly implied in the user's message.
- CRITICAL: If any field (such as client_name, client_email, amount, frequency, start_date, etc.) is missing or not provided in the message, you MUST set it to null. Do NOT use placeholder values, dummy data, or examples (like 'John Doe', 'john.doe@example.com', '5000', or default dates).
- frequency: infer from words like "monthly", "every month", "weekly", "every week", "yearly", "annual", "daily".
- start_date: if user says "today" return "today"; if a specific date is given convert to YYYY-MM-DD; otherwise null.
- end_date: only if user explicitly mentions a stop/end date; otherwise null.
- amount: strip currency symbols. If INR/₹/Rs set currency INR.

Examples:
User: "setup a recurring invoice"
Output:
{
  "client_name": null,
  "client_email": null,
  "item_name": null,
  "task_description": null,
  "amount": null,
  "currency": "INR",
  "frequency": null,
  "start_date": null,
  "end_date": null
}

User: "create a monthly recurring invoice for Jash for ₹15000 starting today"
Output:
{
  "client_name": "Jash",
  "client_email": null,
  "item_name": null,
  "task_description": null,
  "amount": 15000,
  "currency": "INR",
  "frequency": "monthly",
  "start_date": "today",
  "end_date": null
}

Return ONLY the JSON object, no explanation."""


async def extract_recurring_details(message: str) -> dict:
    """Extract recurring invoice fields from a free-form user message."""
    try:
        response = await asyncio.to_thread(
            _client.chat.completions.create,
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": _RECURRING_EXTRACT_PROMPT},
                {"role": "user", "content": message},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=200,
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        logger.error(f"Recurring details extraction failed: {e}")
        return {
            "client_name": None, "client_email": None, "item_name": None,
            "task_description": None, "amount": None, "currency": "INR",
            "frequency": None, "start_date": None, "end_date": None,
        }
