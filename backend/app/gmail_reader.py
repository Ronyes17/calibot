"""
קריאת מיילים — לבריף הבוקר ולשאלות "יש לי מיילים חדשים?".

קריאה בלבד (gmail.readonly) בכוונה: ניקול מסכמת ומפנה תשומת לב,
היא לא שולחת ולא מוחקת. משתמשים באותם credentials של היומן —
אותו token.pickle, בלי OAuth נוסף (רק אימות מחדש חד-פעמי בגלל
ה-scope שנוסף).
"""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_calendar = None   # שירות היומן של CaliBOT — מחזיק את ה-credentials


def init(calendar_service) -> None:
    global _calendar
    _calendar = calendar_service


def _service():
    if _calendar is None or not getattr(_calendar, "credentials", None):
        return None
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=_calendar.credentials,
                 cache_discovery=False)


def _fetch_sync(query: str, limit: int) -> list[dict]:
    service = _service()
    if service is None:
        return []

    listing = service.users().messages().list(
        userId="me", q=query, maxResults=limit,
    ).execute()

    emails = []
    for ref in listing.get("messages", []):
        msg = service.users().messages().get(
            userId="me", id=ref["id"], format="metadata",
            metadataHeaders=["From", "Subject"],
        ).execute()
        headers = {h["name"]: h["value"]
                   for h in msg.get("payload", {}).get("headers", [])}
        sender = headers.get("From", "")
        # "דוד לוי <david@x.com>" -> "דוד לוי"
        if "<" in sender:
            sender = sender.split("<")[0].strip().strip('"')
        emails.append({
            "from": sender,
            "subject": headers.get("Subject", "(ללא נושא)"),
            "snippet": (msg.get("snippet") or "")[:120],
        })
    return emails


async def unread_recent(limit: int = 5) -> list[dict]:
    """מיילים שלא נקראו מהיממה האחרונה, מהתיבה הראשית."""
    try:
        return await asyncio.to_thread(
            _fetch_sync, "in:inbox is:unread newer_than:1d category:primary",
            limit,
        )
    except Exception as exc:
        logger.warning("קריאת מייל נכשלה: %s", exc)
        return []
