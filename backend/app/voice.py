"""
הודעות קוליות.

הזרימה: טלגרם שולח voice -> מורידים את הקובץ -> תמלול -> הטקסט נכנס
לאותו pipeline בדיוק כמו הודעת טקסט. אין מסלול נפרד.

למה Gemini לתמלול ולא Whisper מקומי:
    ה-droplet שלך כבר מריץ את בוט המסחר, ויש לו היסטוריה של OOM.
    להריץ שם מודל תמלול מקומי זה להזמין נפילות בשני הבוטים במקביל.
    Gemini מקבל audio/ogg ישירות, אז גם אין צורך ב-ffmpeg ובהמרה.

טלגרם שולח OGG/Opus. Gemini בולע את זה כמו שהוא.
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
GEMINI_MODEL = os.getenv("GEMINI_TRANSCRIBE_MODEL", "gemini-2.5-flash")
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

# הודעה ארוכה מזה כמעט תמיד שיחה שנשלחה בטעות, לא פנייה לסוכן
MAX_DURATION_SECONDS = 300
MAX_FILE_BYTES = 20 * 1024 * 1024

DOWNLOAD_TIMEOUT = 30.0
TRANSCRIBE_TIMEOUT = 60.0

TRANSCRIBE_PROMPT = (
    "תמלל את ההקלטה הזו במדויק. ההקלטה בעברית, ועשויה לכלול "
    "מונחים באנגלית, שמות, שעות ותאריכים. "
    "החזר אך ורק את התמלול עצמו — בלי הקדמה, בלי הסבר, בלי מרכאות. "
    "אם לא נשמע דיבור כלל, החזר מחרוזת ריקה."
)


async def download_voice(file_id: str) -> Optional[tuple[bytes, str]]:
    """מוריד קובץ קולי מטלגרם. מחזיר (bytes, mime) או None."""
    try:
        async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT) as client:
            meta = await client.get(
                f"{TELEGRAM_BASE}/bot{TELEGRAM_API_TOKEN}/getFile",
                params={"file_id": file_id},
            )
            meta.raise_for_status()
            info = meta.json()

            if not info.get("ok"):
                logger.warning("getFile החזיר שגיאה: %s", info)
                return None

            result = info["result"]
            size = result.get("file_size", 0)
            if size > MAX_FILE_BYTES:
                logger.info("קובץ קולי גדול מדי: %s בייטים", size)
                return None

            path = result["file_path"]
            audio = await client.get(
                f"{TELEGRAM_BASE}/file/bot{TELEGRAM_API_TOKEN}/{path}"
            )
            audio.raise_for_status()

        mime = "audio/ogg" if path.endswith(".oga") or path.endswith(".ogg") \
            else "audio/mpeg"
        return audio.content, mime

    except Exception as exc:
        logger.error("הורדת קובץ קולי נכשלה: %s", exc)
        return None


async def transcribe(audio: bytes, mime: str = "audio/ogg") -> Optional[str]:
    """מחזיר טקסט מתומלל, או None אם התמלול נכשל."""
    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY חסר — אין תמלול")
        return None

    body = {
        "contents": [{
            "parts": [
                {"text": TRANSCRIBE_PROMPT},
                {"inline_data": {
                    "mime_type": mime,
                    "data": base64.b64encode(audio).decode("ascii"),
                }},
            ]
        }],
        "generationConfig": {"temperature": 0.0},
    }

    try:
        async with httpx.AsyncClient(timeout=TRANSCRIBE_TIMEOUT) as client:
            resp = await client.post(
                f"{GEMINI_BASE}/models/{GEMINI_MODEL}:generateContent",
                headers={"x-goog-api-key": GEMINI_API_KEY},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
        return text or None

    except Exception as exc:
        logger.error("תמלול נכשל: %s", exc)
        return None


def extract_voice(message: dict) -> Optional[dict]:
    """
    שולף voice או audio מהודעת טלגרם.
    מחזיר {'file_id', 'duration'} או None אם זו לא הודעה קולית.
    """
    media = message.get("voice") or message.get("audio")
    if not media:
        return None
    return {
        "file_id": media["file_id"],
        "duration": media.get("duration", 0),
    }


async def voice_to_text(message: dict) -> tuple[Optional[str], Optional[str]]:
    """
    נקודת הכניסה מ-routes.
    מחזיר (טקסט, שגיאה_להצגה). בדיוק אחד מהם יהיה None.
    """
    media = extract_voice(message)
    if not media:
        return None, None

    if media["duration"] > MAX_DURATION_SECONDS:
        return None, "ההקלטה ארוכה מדי. תוכל לשלוח משהו קצר יותר?"

    downloaded = await download_voice(media["file_id"])
    if downloaded is None:
        return None, "לא הצלחתי להוריד את ההקלטה. תנסה שוב?"

    audio, mime = downloaded
    text = await transcribe(audio, mime)

    if not text:
        return None, "לא הצלחתי לתמלל את ההקלטה. תוכל לכתוב במקום?"

    logger.info("תומלל (%s שניות): %s", media["duration"], text[:80])
    return text, None
