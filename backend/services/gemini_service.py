import json
import re
import logging
import asyncio

from groq import Groq
from backend.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_client = Groq(api_key=settings.GROQ_API_KEY)

_SYSTEM_PROMPT = """You are an invoice data extractor for a FREELANCER. Your job is to identify emails where a HUMAN CLIENT is paying the freelancer for professional services/work.

Return ONLY a valid JSON object with this exact structure:
{
  "is_confirmation": true/false,
  "client_name": "<full name or null>",
  "client_email": "<email address or null>",
  "item_name": "<short 2-5 word service/product label, e.g. 'Android App Development' or 'E-Commerce Website' or null>",
  "task_description": "<full sentence description of the work/service agreed upon, or null>",
  "amount": <number or null>,
  "currency": "<3-letter ISO code, default USD>",
  "confidence": "high" | "low",
  "missing_fields": ["field1", ...]
}

CRITICAL RULE — is_confirmation:
Set is_confirmation = TRUE if the email is about payment or invoicing for custom professional work/services between a freelancer and a client. This includes BOTH directions:
  ✅ A client asking the freelancer to send an invoice for a project
  ✅ A client confirming payment for completed work (app development, design, consulting, etc.)
  ✅ A freelancer telling a client that work is done and an invoice will follow
  ✅ A freelancer sending a bill, invoice details, or payment request to a client
  ✅ A project agreement between two people mentioning an agreed payment amount

Set is_confirmation = FALSE (and stop processing) for ALL of the following — these are NOT freelance invoices:
  ❌ Subscription renewals or subscription receipts (Coursera, Netflix, Spotify, LinkedIn, Adobe, etc.)
  ❌ E-commerce purchase receipts (Amazon, Flipkart, Epic Games, Steam, any online store)
  ❌ Bank, DEMAT, or financial institution notifications (account statements, transaction alerts, SIP, mutual funds)
  ❌ Utility bills (electricity, phone, internet, water)
  ❌ Government or tax notices
  ❌ Automated platform emails (payment gateways, app stores, etc.)
  ❌ Any email where a COMPANY is charging YOU (the freelancer), not a human client paying you

Key test: Ask "Does this email discuss payment/invoicing for custom professional work between a freelancer and their client?" If NO → is_confirmation = false.

- client_name: the HUMAN CLIENT (the person being invoiced/billed). If the email is FROM a client asking for an invoice, the client is the sender. If the email is FROM the freelancer telling a client about an invoice, the client is the RECIPIENT (the person being addressed in the email body). Look at the greeting (e.g. "Hi Jash", "Dear Piyusha") to identify the client.
- client_email: MUST be a real email address containing "@" and a domain. If the text says phrases like "this email", "my email", "the above email", "reply to this", or any non-address phrase — set client_email to null. Do NOT copy those phrases as the value.
- item_name: SHORT label for the service (2-5 words max). Examples: 'Android App Development', 'Website Design', 'Logo Design'.
- task_description: a full sentence describing the agreed work. Must be different from item_name — give more context.
- amount: numeric value only, strip currency symbols. If amount is in INR (₹ or Rs), set currency to "INR".
- confidence: "high" when the email is clearly from a human client about payment/invoicing for real work — even if amount or exact description is missing but the intent is obvious. "low" ONLY if the email is very ambiguous or borderline (e.g. could be spam, unclear if invoice-related at all).
- missing_fields: list any of [amount, task_description, item_name, client_email, client_name] that are absent.
- Return ONLY the JSON object, no markdown, no explanation."""


def _parse_json_response(text: str) -> dict:
    """Strip markdown fences if present and parse JSON."""
    text = re.sub(r"```(?:json)?\s*", "", text).strip()
    text = re.sub(r"```\s*$", "", text).strip()
    return json.loads(text)


async def extract_invoice_data(email_body: str) -> dict:
    """Extract structured invoice data from an email body using Groq (Llama 3.3 70B)."""
    try:
        response = await asyncio.to_thread(
            _client.chat.completions.create,
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                # Truncate to 4000 chars (~1000 tokens) — invoice data is always
                # near the top of an email; cutting here halves TPM consumption.
                {"role": "user", "content": email_body[:4000]},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=400,
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        logger.error(f"Groq extraction failed: {e}")
        raise
