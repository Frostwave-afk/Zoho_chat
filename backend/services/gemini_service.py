import json
import re
import logging
import asyncio

from groq import Groq
from backend.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_client = Groq(api_key=settings.GROQ_API_KEY)

_SYSTEM_PROMPT = """You are an invoice data extractor. Read the email provided and extract invoice/payment information.

Return ONLY a valid JSON object with this exact structure:
{
  "is_confirmation": true/false,
  "client_name": "<full name or null>",
  "client_email": "<email address or null>",
  "task_description": "<description of work/service or null>",
  "amount": <number or null>,
  "currency": "<3-letter ISO code, default USD>",
  "confidence": "high" | "low",
  "missing_fields": ["field1", ...]
}

Guidelines:
- is_confirmation: true ONLY if this email clearly describes a paid service, work agreement, or payment confirmation. Set false for newsletters, promotions, or unrelated emails.
- confidence: "high" only when BOTH amount and task_description are clearly and unambiguously stated.
- missing_fields: list any of [amount, task_description, client_email, client_name] that are absent or ambiguous.
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
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
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
