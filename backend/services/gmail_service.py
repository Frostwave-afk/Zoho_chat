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

# Map weekday names to Python weekday() integers (Mon=0 … Sun=6)
_WEEKDAY_MAP = {
    "monday": 0, "tuesday": 1, "wednesday": 2,
    "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
}


def _last_weekday(target_weekday: int) -> date:
    """Return the most recent past date that fell on target_weekday."""
    today = date.today()
    days_ago = (today.weekday() - target_weekday) % 7
    if days_ago == 0:
        days_ago = 7  # "last X" means the previous occurrence, not today
    return today - timedelta(days=days_ago)


def _this_weekday(target_weekday: int) -> date:
    """Return the date of target_weekday within the current week (Mon start)."""
    today = date.today()
    days_ago = (today.weekday() - target_weekday) % 7
    return today - timedelta(days=days_ago)


def _week_bounds(weeks_ago: int = 0) -> tuple[date, date]:
    """Return Monday-start week bounds as (start, end-exclusive)."""
    today = date.today()
    start_of_this_week = today - timedelta(days=today.weekday())
    start = start_of_this_week - timedelta(days=7 * weeks_ago)
    end = start + timedelta(days=7)
    return start, end


def _build_query(
    person_email: Optional[str],
    date_filter: Optional[str],
    keywords: Optional[list[str]] = None,
    person_name: Optional[str] = None,
) -> Optional[str]:
    """
    Build a Gmail search query string.

    Strategy:
    1. Known contact email  → search from:/to:/cc: that exact address + in:anywhere
    2. Unknown person name  → search from:name OR to:name (partial, no in:anywhere)
    3. Date filter          → add date range (supports specific weekdays like last_sunday)
    4. Topic keywords       → add keyword OR filter (only when no person specified)
    5. No date given + keyword/name search → cap at 30 days to avoid old noise
    """
    parts: list[str] = []
    today = date.today()

    # ── Known contact email ──────────────────────────────────────────────────
    if person_email:
        parts.append(f"{{from:{person_email} to:{person_email} cc:{person_email}}}")
        # Only include sent/all-mail folders when we have a specific contact email
        parts.append("in:anywhere")

    # ── Unknown person — search by name in from:/to: headers ─────────────────
    elif person_name:
        # Gmail supports partial-name matching in from:/to: fields
        safe_name = person_name.replace('"', "")
        parts.append(f'(from:"{safe_name}" OR to:"{safe_name}")')

    # ── Date filter ──────────────────────────────────────────────────────────
    if date_filter:
        yesterday = today - timedelta(days=1)

        simple_mapping = {
            "today": "newer_than:1d",
            "yesterday": (
                f"after:{yesterday.strftime('%Y/%m/%d')} "
                f"before:{today.strftime('%Y/%m/%d')}"
            ),
            "this_week": "newer_than:7d",
        }

        if date_filter in simple_mapping:
            parts.append(simple_mapping[date_filter])
        elif date_filter == "last_week":
            start, end = _week_bounds(weeks_ago=1)
            parts.append(
                f"after:{start.strftime('%Y/%m/%d')} "
                f"before:{end.strftime('%Y/%m/%d')}"
            )
        else:
            # Handle "last_<weekday>" and "this_<weekday>"
            for prefix, resolver in [("last_", _last_weekday), ("this_", _this_weekday)]:
                if date_filter.startswith(prefix):
                    day_name = date_filter[len(prefix):]
                    if day_name in _WEEKDAY_MAP:
                        target = resolver(_WEEKDAY_MAP[day_name])
                        next_day = target + timedelta(days=1)
                        parts.append(
                            f"after:{target.strftime('%Y/%m/%d')} "
                            f"before:{next_day.strftime('%Y/%m/%d')}"
                        )
                        logger.info(f"Resolved '{date_filter}' → {target.strftime('%Y/%m/%d')}")
                    break

    # ── Topic keywords — only when there's no person filter ──────────────────
    # (Avoid adding broad keywords that pull in unrelated emails)
    if not person_email and not person_name and keywords:
        kw_parts = []
        for kw in keywords[:3]:
            kw = kw.strip()
            if kw:
                kw_parts.append(f'"{kw}"' if " " in kw else kw)
        if kw_parts:
            parts.append(f"({' OR '.join(kw_parts)})")

    # ── Default date cap when no explicit date given ──────────────────────────
    # Prevents surfacing emails from months/years ago
    if not date_filter:
        if person_name or (not person_email and keywords):
            # Name/keyword search with no date → cap at last 30 days
            parts.append("newer_than:30d")
        elif not person_email:
            # Totally open search → cap at last 3 days
            parts.append("newer_than:3d")

    return " ".join(parts) if parts else None


def _extract_body(payload: dict) -> str:
    """Recursively pull plain-text body from a Gmail message payload."""
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
    # Try HTML as last resort — strip tags minimally
    if mime == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            raw = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
            # Very basic tag stripping so Gemini can still read it
            import re
            return re.sub(r"<[^>]+>", " ", raw)
    for part in payload.get("parts", []):
        text = _extract_body(part)
        if text:
            return text
    return ""


async def search_gmail(
    db: AsyncSession,
    person_email: Optional[str] = None,
    date_filter: Optional[str] = None,
    keywords: Optional[list[str]] = None,
    person_name: Optional[str] = None,
    max_results: int = 20,
) -> list[dict]:
    """
    Search Gmail and return a list of {id, subject, from, to, body} dicts,
    skipping message IDs that have already been processed.

    Raises RuntimeError if Gmail is not connected.
    Returns an empty list (never raises ValueError) when there are no results.
    """
    creds = await get_gmail_credentials(db)
    if not creds:
        raise RuntimeError("Gmail not connected — please connect your Gmail account first.")

    query = _build_query(person_email, date_filter, keywords, person_name)

    # If we have absolutely nothing to search on, do a broad recent scan
    if not query:
        logger.info("No specific query criteria — defaulting to newer_than:3d")
        query = "newer_than:3d"

    logger.info(f"Gmail query: {query!r}")

    def _sync_fetch() -> tuple[list[dict], str]:
        service = build("gmail", "v1", credentials=creds)
        # Get the authenticated user's own email so we can skip their sent emails
        profile = service.users().getProfile(userId="me").execute()
        own_email = profile.get("emailAddress", "").lower()

        result = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
        refs = result.get("messages", [])
        emails = []
        for ref in refs:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=ref["id"], format="full")
                .execute()
            )
            emails.append(msg)
        return emails, own_email

    raw, own_email = await asyncio.to_thread(_sync_fetch)
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

        headers = {
            h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        from_header = headers.get("from", "")

        # Tag whether this email was sent by the freelancer themselves.
        # The pipeline uses this to pick the correct header for the client
        # email (To: instead of From: when the freelancer is the sender).
        sent_by_self = bool(own_email and own_email in from_header.lower())
        if sent_by_self:
            logger.info(f"Email sent by self ({own_email}): {headers.get('subject', '')}")

        emails.append({
            "id": msg["id"],
            "subject": headers.get("subject", "(no subject)"),
            "from": from_header,
            "to": headers.get("to", ""),
            "body": _extract_body(msg.get("payload", {})),
            "sent_by_self": sent_by_self,
        })

    return emails
