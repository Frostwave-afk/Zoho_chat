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
  "action": "create_invoice" | "scan_emails" | "approve_draft" | "decline_draft" | "send_invoices" | "check_overdue" | "check_pending" | "check_specific_payment" | "payment_summary" | "create_recurring" | "list_recurring" | "stop_recurring" | "remind_overdue" | "greeting" | "create_estimate" | "accept_estimate" | "reject_estimate" | "convert_estimate" | "unknown",
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
  - "create_estimate" when user wants to create, draft, or make an estimate, quote, or bid (e.g. "create an estimate for Rahul", "send a quote to Rahul", "quote Rahul ₹40,000 for website")
  - "accept_estimate" when user wants to accept or approve an estimate/quote (e.g. "accept this estimate", "accept the quote", "approve estimate #QT-000004", "mark estimate as accepted")
  - "reject_estimate" when user wants to reject or decline an estimate/quote (e.g. "reject this estimate", "decline the quote", "mark estimate as declined")
  - "convert_estimate" when user wants to convert or turn an accepted estimate into an invoice (e.g. "convert Rahul's estimate", "Rahul accepted the quote, make the invoice", "turn the estimate into an invoice")
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

def get_extraction_prompt(is_estimate: bool) -> str:
    doc_type = "estimate/quote" if is_estimate else "manual invoice"
    key_type = "is_estimate_request" if is_estimate else "is_manual_invoice_request"
    return f"""You extract {doc_type} details from a user's message.

Return ONLY a valid JSON object with this exact structure:
{{
  "{key_type}": true,
  "client_name": "<extracted customer name or null>",
  "client_email": "<extracted email or null>",
  "currency": "<3-letter ISO code or null>",
  "send_email": true | false | null,
  "items": [
    {{
      "item_name": "<short item/service name or null>",
      "task_description": "<full description or null>",
      "amount": <number or null>
    }}
  ]
}}

Rules:
- Extract only when the user is trying to create a brand-new {doc_type} manually.
- client_name: extract if mentioned, e.g. "estimate for Rahul" -> "Rahul".
- client_email: extract if a valid email is found in the message.
- items: extract any details you can find. If an item name or amount is present, include the dictionary. Use null for any missing details.
- If the message is not a {doc_type} request, set {key_type} to false and all other fields to null/empty.
- Return only JSON."""


async def parse_intent(message: str) -> dict:
    """Call Groq to parse the user's natural-language message into structured intent."""
    normalized = " ".join(message.lower().replace("’", "'").split())

    normalized = " ".join(message.lower().replace("'", "'").split())

    import re as _re

    def _extract_estimate_ref(text: str):
        m = _re.search(r"(?:#)?((?:qt|est)[-\s]?\d+)", text, _re.I)
        if m:
            return m.group(1).upper().replace(" ", "-")
        m = _re.search(r"(?:estimate|quote)\s+#?(\d+)", text, _re.I)
        if m:
            return f"QT-{m.group(1).zfill(6)}"
        return None

    def _extract_person_for_estimate(text: str):
        for pattern in (
            r"(?:accept|reject|decline|approve|convert)\s+(?:the\s+)?(?:estimate|quote)\s+(?:for|from|of)\s+([a-z][a-z ']+)",
            r"(?:estimate|quote)\s+(?:for|from|of)\s+([a-z][a-z ']+)",
        ):
            m = _re.search(pattern, text)
            if m:
                person = m.group(1).strip()
                if person not in ("the", "a", "an", "all", "my", "this", "that"):
                    return person
        return None

    # ── Accept estimate shortcut ──────────────────────────────────────────────
    if (
        any(w in normalized for w in ("accept", "approve", "confirm"))
        and ("estimate" in normalized or "quote" in normalized)
        and "convert" not in normalized
        and "invoice" not in normalized
    ):
        return {
            "action": "accept_estimate",
            "person_name": _extract_person_for_estimate(normalized),
            "date_filter": None,
            "keywords": ["estimate", "accept"],
            "estimate_ref": _extract_estimate_ref(normalized),
        }

    # ── Reject estimate shortcut ──────────────────────────────────────────────
    if (
        any(w in normalized for w in ("reject", "decline", "deny"))
        and ("estimate" in normalized or "quote" in normalized)
    ):
        return {
            "action": "reject_estimate",
            "person_name": _extract_person_for_estimate(normalized),
            "date_filter": None,
            "keywords": ["estimate", "reject"],
            "estimate_ref": _extract_estimate_ref(normalized),
        }

    # ── Convert estimate shortcut ─────────────────────────────────────────────
    if "convert" in normalized and ("estimate" in normalized or "quote" in normalized):
        m_name = _re.search(r"convert\s+([a-z ']+?)(?:'s)?\s+(?:estimate|quote)", normalized)
        if not m_name:
            m_name = _re.search(r"convert\s+(?:estimate|quote)\s+(?:for|from|of\s+)?([a-z ']+)", normalized)
        person = m_name.group(1).strip() if m_name else None
        if person in ("the", "a", "an", "all", "my"):
            person = None
        return {
            "action": "convert_estimate",
            "person_name": person,
            "date_filter": None,
            "keywords": ["estimate", "convert"]
        }

    if "accepted" in normalized and ("quote" in normalized or "estimate" in normalized or "invoice" in normalized):
        import re as _re
        m_name = _re.search(r"([a-z ']+?)\s+accepted", normalized)
        person = m_name.group(1).strip() if m_name else None
        return {
            "action": "convert_estimate",
            "person_name": person,
            "date_filter": None,
            "keywords": ["estimate", "convert"]
        }

    if "turn" in normalized and "estimate" in normalized and "invoice" in normalized:
        import re as _re
        m_name = _re.search(r"turn\s+([a-z ']+?)(?:'s)?\s+estimate", normalized)
        person = m_name.group(1).strip() if m_name else None
        return {
            "action": "convert_estimate",
            "person_name": person,
            "date_filter": None,
            "keywords": ["estimate", "convert"]
        }

    # ── Create estimate shortcut ──────────────────────────────────────────────
    if ("estimate" in normalized or "quote" in normalized) and any(w in normalized for w in ("create", "make", "draft", "send", "give")):
        import re as _re
        m_name = _re.search(rf"(?:estimate|quote)\s+(?:to|for)\s+([a-z][a-z ']+)", normalized)
        person = m_name.group(1).strip() if m_name else None
        if person in ("the", "a", "an", "all", "my"):
            person = None
        return {
            "action": "create_estimate",
            "person_name": person,
            "date_filter": None,
            "keywords": ["estimate", "create"]
        }

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

    # ── Record payment short-circuit ──────────────────────────────────────────
    # Check if message is about recording/logging a payment
    is_record_payment = False
    client_name = None
    amount = None
    payment_mode = "banktransfer"
    payment_date = None

    # Detect payment mode if mentioned
    if "cash" in normalized:
        payment_mode = "cash"
    elif "check" in normalized or "cheque" in normalized:
        payment_mode = "check"
    elif "upi" in normalized:
        payment_mode = "upi"
    elif "card" in normalized:
        payment_mode = "creditcard"

    # Try matching: "mark [client]'s invoice as paid", "mark invoice from [client] as paid", etc.
    import re as _re
    m_mark = _re.search(r"mark\s+(?:invoices?\s+)?(?:from|of|for\s+)?([a-z ']+?)(?:'s)?\s+(?:invoices?\s+)?as\s+paid", normalized)
    if not m_mark:
        m_mark = _re.search(r"mark\s+(?:invoices?\s+)?(?:from|of|for\s+)?([a-z ']+?)(?:'s)?\s+(?:invoices?\s+)?paid", normalized)
    
    # Try matching: "mark invoice paid from [client]"
    m_paid_from = _re.search(r"mark\s+(?:invoices?\s+)?paid\s+(?:from|for|of\s+)?([a-z ']+)", normalized)
    
    # Try matching: "record payment from [client]"
    m_rec = _re.search(r"record\s+payment\s+(?:from|for|of\s+)?([a-z ']+)", normalized)
    
    # Try matching: "log payment for [client]"
    m_log = _re.search(r"log\s+payment\s+(?:from|for|of\s+)?([a-z ']+)", normalized)
    
    # Try matching: "[client] paid [amount]" or "[client] paid"
    m_paid = None
    if "paid" in normalized and not any(q in normalized for q in ("who", "has", "did", "check", "verify")):
        m_paid = _re.search(r"([a-z ']+?)\s+paid(?:\s+rs\.?|\s+inr|\s+usd|\s+[\u20b9$])?\s*(\d+(?:\.\d{2})?)", normalized)
        if not m_paid:
            m_paid = _re.search(r"([a-z ']+?)\s+paid", normalized)

    match_found = m_mark or m_paid_from or m_rec or m_log or m_paid
    if match_found:
        is_record_payment = True
        if m_mark:
            client_name = m_mark.group(1).strip()
        elif m_paid_from:
            client_name = m_paid_from.group(1).strip()
        elif m_rec:
            client_name = m_rec.group(1).strip()
        elif m_log:
            client_name = m_log.group(1).strip()
        elif m_paid:
            client_name = m_paid.group(1).strip()
            if len(m_paid.groups()) >= 2 and m_paid.group(2):
                try:
                    amount = float(m_paid.group(2))
                except ValueError:
                    pass

        # Cleanup client name from filler words
        if client_name:
            # remove words like "the", "a", "an", "invoice", "invoices" at the start/end
            words = client_name.split()
            if words and words[0] in ("the", "a", "an"):
                words = words[1:]
            client_name = " ".join(words).strip()
            # If the user typed "invoice from holly", let's clean up "invoice from" if it leaked
            if client_name.startswith("invoice from "):
                client_name = client_name.replace("invoice from ", "", 1).strip()
            elif client_name.startswith("invoice of "):
                client_name = client_name.replace("invoice of ", "", 1).strip()
            elif client_name.startswith("invoice for "):
                client_name = client_name.replace("invoice for ", "", 1).strip()
            
            if client_name in ("everyone", "all", "invoice", "invoices", "payment", "payments"):
                is_record_payment = False
                client_name = None

    # FALLBACK: If client name wasn't extracted, but the intent is clearly to record payment
    if not is_record_payment:
        _RECORD_PHRASES = (
            "record payment", "log payment", "add payment", "register payment",
            "mark invoice paid", "mark as paid", "mark paid", "record a payment", "log a payment", "add a payment"
        )
        if any(p in normalized for p in _RECORD_PHRASES):
            is_record_payment = True
            client_name = None

    if is_record_payment:
        from datetime import date as _date
        payment_date = _date.today().isoformat()

        if amount is None:
            amt_match = _re.search(r"(?:of|for|amount|amount\s+of|rs\.?|inr|[\u20b9$])\s*(\d+(?:\.\d{2})?)", normalized)
            if amt_match:
                try:
                    amount = float(amt_match.group(1))
                except ValueError:
                    pass

        return {
            "action": "record_payment",
            "person_name": client_name,
            "amount": amount,
            "payment_mode": payment_mode,
            "payment_date": payment_date,
            "date_filter": None,
            "keywords": ["payment", "record"],
        }


    # ── Remind overdue shortcut ─────────────────────────────────────────────────────
    # Catches: "remind everyone overdue", "send reminder to Vismay",
    #          "chase payment", "remind unpaid invoices", "remind jash", etc.
    _is_remind = (
        "send reminder" in normalized
        or "chase payment" in normalized
        or (
            "remind" in normalized
            and any(w in normalized for w in ("overdue", "unpaid", "everyone", "all"))
        )
    )
    # If message contains "remind" + a word that looks like a name (not a keyword
    # above), also treat it as remind_overdue so "remind Vismay" works.
    if not _is_remind and "remind" in normalized:
        words = normalized.split()
        remind_idx = next((i for i, w in enumerate(words) if w == "remind"), -1)
        if remind_idx >= 0 and remind_idx + 1 < len(words):
            _is_remind = True  # any "remind <something>" triggers it

    if _is_remind:
        # Extract optional days_overdue_min (e.g. "more than 14 days" → 14)
        import re as _re
        days_match = _re.search(r"(\d+)\s*day", normalized)
        days_overdue_min = int(days_match.group(1)) if days_match else 0

        # Extract optional client_name: look for words after "to" or "for",
        # but exclude generic payment keywords
        _PAYMENT_KEYWORDS = {
            "overdue", "unpaid", "everyone", "all", "invoices", "invoice",
            "reminder", "reminders", "payment", "payments", "chase",
        }
        client_name: str | None = None
        for prep in ("to", "for"):
            m = _re.search(rf"\b{prep}\s+([a-z][a-z ']+)", normalized)
            if m:
                candidate = m.group(1).strip()
                words_in_candidate = set(candidate.split())
                if not words_in_candidate.issubset(_PAYMENT_KEYWORDS):
                    client_name = candidate
                    break

        return {
            "action": "remind_overdue",
            "person_name": client_name,
            "date_filter": None,
            "keywords": ["payment", "reminder"],
            "days_overdue_min": days_overdue_min,
        }

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


async def extract_manual_invoice_request(message: str, is_estimate: bool = False) -> dict:
    """Extract manual invoice or estimate details from a free-form user message."""
    try:
        prompt = get_extraction_prompt(is_estimate)
        response = await asyncio.to_thread(
            _client.chat.completions.create,
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": message},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=300,
        )
        data = json.loads(response.choices[0].message.content)
        key_type = "is_estimate_request" if is_estimate else "is_manual_invoice_request"
        if key_type in data:
            data["is_manual_invoice_request"] = data[key_type]
        if "items" in data:
            for item in data["items"]:
                if is_estimate:
                    if "task_description" in item and "description" not in item:
                        item["description"] = item["task_description"]
                    elif "description" in item and "task_description" not in item:
                        item["task_description"] = item["description"]
                else:
                    if "description" in item and "task_description" not in item:
                        item["task_description"] = item["description"]
        return data
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
