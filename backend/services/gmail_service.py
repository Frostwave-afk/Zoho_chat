import base64
import logging
import asyncio
from datetime import date, timedelta
from typing import Optional

from googleapiclient.discovery import build
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from backend.db.models import ProcessedEmail
from backend.auth.gmail_auth import get_gmail_credentials

logger = logging.getLogger(__name__)


def _build_query(person_email: Optional[str], date_filter: Optional[str]) -> Optional[str]:
    parts = []
    if person_email:
        parts.append(f"from:{person_email}")
    if date_filter:
        today = date.today()
        yesterday = today - timedelta(days=1)
        mapping = {
            "today": "newer_than:1d",
            "yesterday": f"after:{yesterday.strftime('%Y/%m/%d')} before:{today.strftime('%Y/%m/%d')}",
            "this_week": "newer_than:7d",
        }
        if date_filter in mapping:
            parts.append(mapping[date_filter])
    return " ".join(parts) if parts else None


def _extract_body(payload: dict) -> str:
    """Recursively pull plain-text body from a Gmail message payload."""
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
    for part in payload.get("parts", []):
        text = _extract_body(part)
        if text:
            return text
    return ""


async def search_gmail(
    db: AsyncSession,
    person_email: Optional[str] = None,
    date_filter: Optional[str] = None,
    max_results: int = 10,
) -> list[dict]:
    """Search Gmail and return list of {id, subject, from, body} skipping already-processed IDs."""
    creds = await get_gmail_credentials(db)
    if not creds:
        raise RuntimeError("Gmail not connected — please connect your Gmail account first.")

    query = _build_query(person_email, date_filter)
    if not query:
        raise ValueError(
            "Search criteria too broad — please mention a person or a time period."
        )

    logger.info(f"Gmail query: {query!r}")

    def _sync_fetch() -> list[dict]:
        service = build("gmail", "v1", credentials=creds)
        result = service.users().messages().list(
            userId="me", q=query, maxResults=max_results
        ).execute()
        refs = result.get("messages", [])
        emails = []
        for ref in refs:
            msg = service.users().messages().get(
                userId="me", id=ref["id"], format="full"
            ).execute()
            emails.append(msg)
        return emails

    raw = await asyncio.to_thread(_sync_fetch)
    if not raw:
        return []

    # Filter out already-processed message IDs
    ids = [m["id"] for m in raw]
    result = await db.execute(
        select(ProcessedEmail.gmail_message_id).where(
            ProcessedEmail.gmail_message_id.in_(ids)
        )
    )
    processed_ids = {row[0] for row in result.fetchall()}

    emails = []
    for msg in raw:
        if msg["id"] in processed_ids:
            logger.info(f"Skipping already-processed email {msg['id']}")
            continue

        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
        emails.append({
            "id": msg["id"],
            "subject": headers.get("subject", "(no subject)"),
            "from": headers.get("from", ""),
            "body": _extract_body(msg.get("payload", {})),
        })

    return emails
