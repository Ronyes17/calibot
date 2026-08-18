"""
תמונה -> אירוע.

צילום של זימון תור, כרטיס, הזמנה — Gemini Vision מחלץ ממנו טקסט
מובנה, והטקסט נכנס לאותו pipeline כמו הודעה כתובה. אותו דפוס בדיוק
כמו voice.py: להמיר את המדיה לטקסט מוקדם ככל האפשר, ומשם הכל אחיד.
"""

import base64
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_API_TOKEN = None  # מוזרק מ-config באתחול
TELEGRAM_BASE = "https://api.telegram.org"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_VISION_MODEL", "gemini-2.5-flash")
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

MAX_FILE_BYTES = 10 * 1024 * 1024

EXTRACT_PROMPT = (
    "זו תמונה שעשויה להכיל פרטי אירוע: זימון תור, הזמנה, כרטיס, "
    "צילום מסך של התכתבות וכדומה.\n"
    "חלץ ממנה את פרטי האירוע בעברית, בשורות קצרות: מה האירוע, "
    "תאריך, שעה, מיקום, וכל פרט רלוונטי אחר (רופא, אסמכתא).\n"
    "אם יש כמה אירועים — פרט את כולם.\n"
    "אם אין בתמונה שום פרט של אירוע, החזר בדיוק: אין אירוע\n"
    "החזר רק את הפרטים, בלי הקדמות."
)


async def _download_photo(file_id: str) -> Optional[bytes]:
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            meta = await client.get(
                f"{TELEGRAM_BASE}/bot{TELEGRAM_API_TOKEN}/getFile",
                params={"file_id": file_id},
            )
            info = meta.json()
            if not info.get("ok"):
                return None
            if info["result"].get("file_size", 0) > MAX_FILE_BYTES:
                return None
            path = info["result"]["file_path"]
            resp = await client.get(
                f"{TELEGRAM_BASE}/file/bot{TELEGRAM_API_TOKEN}/{path}"
            )
            resp.raise_for_status()
            return resp.content
    except Exception as exc:
        logger.error("הורדת תמונה נכשלה: %s", exc)
        return None


async def photo_to_text(message: dict) -> tuple[Optional[str], Optional[str]]:
    """
    נקודת הכניסה מ-routes. מחזיר (טקסט, שגיאה_להצגה).
    (None, None) כשאין תמונה בהודעה בכלל.
    """
    photos = message.get("photo")
    if not photos:
        return None, None

    # טלגרם שולח כמה רזולוציות; האחרונה היא הגדולה ביותר
    file_id = photos[-1]["file_id"]

    image = await _download_photo(file_id)
    if image is None:
        return None, "לא הצלחתי להוריד את התמונה. תנסה שוב?"

    if not GEMINI_API_KEY:
        return None, "עיבוד תמונות לא מוגדר."

    body = {
        "contents": [{
            "parts": [
                {"text": EXTRACT_PROMPT},
                {"inline_data": {
                    "mime_type": "image/jpeg",
                    "data": base64.b64encode(image).decode("ascii"),
                }},
            ]
        }],
        "generationConfig": {"temperature": 0.0},
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{GEMINI_BASE}/models/{GEMINI_MODEL}:generateContent",
                headers={"x-goog-api-key": GEMINI_API_KEY},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
    except Exception as exc:
        logger.error("חילוץ מתמונה נכשל: %s", exc)
        return None, "לא הצלחתי לקרוא את התמונה. תוכל לכתוב את הפרטים?"

    if not text or "אין אירוע" in text:
        return None, "לא מצאתי פרטי אירוע בתמונה."

    caption = (message.get("caption") or "").strip()
    prefix = f"{caption}\n" if caption else ""
    return (f"{prefix}מהתמונה שצירפתי:\n{text}\n\nתציע מה לקבוע לפי זה.", None)
