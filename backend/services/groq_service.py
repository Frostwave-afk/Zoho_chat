import json
import logging
import asyncio

from groq import Groq
from backend.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_client = Groq(api_key=settings.GROQ_API_KEY)

_SYSTEM_PROMPT = """You are an intent parser for an invoicing assistant.
Parse the user's message and return ONLY a valid JSON object with this exact structure:
{
  "action": "create_invoice" | "scan_emails",
  "person_name": "<name or null>",
  "date_filter": "today" | "yesterday" | "this_week" | null
}

Rules:
- action is "create_invoice" when user mentions making/creating an invoice for someone
- action is "scan_emails" when user wants to look at / review emails
- person_name: the person's name if mentioned, otherwise null
- date_filter: extract time period if mentioned (today/yesterday/this_week), otherwise null
- Return ONLY the JSON object, no extra text or markdown."""


async def parse_intent(message: str) -> dict:
    """Call Groq to parse the user's natural-language message into structured intent."""
    try:
        response = await asyncio.to_thread(
            _client.chat.completions.create,
            model="llama-3.3-70b-versatile",
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
        # Safe fallback — treat as a scan of today's emails
        return {"action": "scan_emails", "person_name": None, "date_filter": "today"}
