"""
תמונה -> אירוע, דרך Vertex AI.

הגלגול השני של המודול: הגרסה הראשונה דיברה עם AI Studio
(generativelanguage.googleapis.com), שחוסם גיאוגרפית IP של
דאטהסנטרים. Vertex AI הוא המסלול שגוגל מייעדת לשרתים — אותם
מודלי Gemini, אימות ב-Service Account במקום מפתח API, ובחירת
region מפורשת. אין תלות במיקום ה-IP.

עיקרון הצינור לא השתנה: מדיה -> טקסט מוקדם ככל האפשר, ומשם
הכל זורם כמו הודעה כתובה.

env:
    VERTEX_SA_FILE   נתיב ל-JSON של חשבון השירות (nikol-vertex)
    VERTEX_REGION    ברירת מחדל europe-west1
    VERTEX_MODEL     ברירת מחדל gemini-2.5-flash
"""

import asyncio
import base64
import json
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_API_TOKEN = None  # מוזרק מ-main באתחול
TELEGRAM_BASE = "https://api.telegram.org"

SA_FILE = os.getenv("VERTEX_SA_FILE", "")
REGION = os.getenv("VERTEX_REGION", "europe-west1")
MODEL = os.getenv("VERTEX_MODEL", "gemini-2.5-flash")

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

_credentials = None
_project_id: Optional[str] = None


def _load_credentials():
    """טעינת חשבון השירות פעם אחת; רענון טוקן לפי הצורך."""
    global _credentials, _project_id
    if _credentials is None:
        if not SA_FILE or not os.path.exists(SA_FILE):
            return None
        from google.oauth2.service_account import Credentials
        _credentials = Credentials.from_service_account_file(
            SA_FILE, scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        with open(SA_FILE, encoding="utf-8") as f:
            _project_id = json.load(f).get("project_id")
    return _credentials


def _fresh_token_sync() -> Optional[str]:
    creds = _load_credentials()
    if creds is None:
        return None
    if not creds.valid:
        from google.auth.transport.requests import Request as GoogleAuthRequest
        creds.refresh(GoogleAuthRequest())
    return creds.token


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

    file_id = photos[-1]["file_id"]   # הרזולוציה הגדולה ביותר

    image = await _download_photo(file_id)
    if image is None:
        return None, "לא הצלחתי להוריד את התמונה. תנסה שוב?"

    token = await asyncio.to_thread(_fresh_token_sync)
    if token is None:
        return None, "עיבוד תמונות לא מוגדר (חסר חשבון שירות של Vertex)."

    url = (f"https://{REGION}-aiplatform.googleapis.com/v1/projects/"
           f"{_project_id}/locations/{REGION}/publishers/google/models/"
           f"{MODEL}:generateContent")

    body = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": EXTRACT_PROMPT},
                {"inline_data": {
                    "mime_type": "image/jpeg",
                    "data": base64.b64encode(image).decode("ascii"),
                }},
            ],
        }],
        "generationConfig": {"temperature": 0.0},
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
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
