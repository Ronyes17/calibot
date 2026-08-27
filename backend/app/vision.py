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

CLASSIFY_PROMPT = (
    "מה מופיע בתמונה? ענה במילה אחת בלבד:\n"
    "אוכל — אם זו ארוחה, מנה, חטיף או משקה (צולם, לא תפריט)\n"
    "לוז — אם זה צילום מסך/מסמך עם לוח זמנים, משמרות, תור או הודעה\n"
    "אחר — כל דבר אחר"
)

FOOD_PROMPT = (
    "זו תמונה של אוכל. זהה את המנה והערך ערכים תזונתיים למנה שנראית "
    "בתמונה (גודל מנה ישראלי טיפוסי, לפי מה שרואים בפועל).\n"
    "החזר JSON בלבד, בלי הסברים ובלי גרשי קוד, בפורמט:\n"
    '{"is_food": true, "description": "תיאור קצר בעברית", '
    '"kcal": מספר_שלם, "protein_g": מספר_שלם}\n'
    'אם זו בעצם לא תמונת אוכל: {"is_food": false}'
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


async def _vertex_generate(image: bytes, prompt: str) -> Optional[str]:
    """קריאה אחת ל-Vertex עם תמונה ופרומפט. None בכשל."""
    token = await asyncio.to_thread(_fresh_token_sync)
    if token is None:
        return None
    url = (f"https://{REGION}-aiplatform.googleapis.com/v1/projects/"
           f"{_project_id}/locations/{REGION}/publishers/google/models/"
           f"{MODEL}:generateContent")
    body = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": prompt},
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
                url, headers={"Authorization": f"Bearer {token}"}, json=body)
            resp.raise_for_status()
            data = resp.json()
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts).strip()
    except Exception as exc:
        logger.error("קריאת Vertex נכשלה: %s", exc)
        return None


FOOD_HINTS = ("אכל", "ארוח", "קלוריות", "אוכל", "צהריים", "בוקר של", "ערב של")
SCHED_HINTS = ("משמר", "תור", "פגיש", "זימון", "לוז", "יומן")


async def analyze_photo(message: dict):
    """
    נקודת הכניסה מ-routes לתמונות.
    מחזיר (kind, payload, err):
      kind=None            אין תמונה בהודעה
      kind="meal"          payload=dict של הארוחה (description/kcal/protein_g)
      kind="schedule"      payload=טקסט לסוכן (הזרימה הישנה של משמרות/תורים)
      err                  הודעת שגיאה להצגה למשתמש (כש-kind=None)
    """
    photos = message.get("photo")
    if not photos:
        return None, None, None

    image = await _download_photo(photos[-1]["file_id"])
    if image is None:
        return None, None, "לא הצלחתי להוריד את התמונה. תנסה שוב?"

    if _load_credentials() is None:
        return None, None, "עיבוד תמונות לא מוגדר (חסר חשבון שירות של Vertex)."

    # ניתוב: קודם לפי הכיתוב (חוסך קריאה), אחרת שאלת סיווג קצרה
    caption = (message.get("caption") or "").strip()
    kind = None
    if caption:
        if any(w in caption for w in FOOD_HINTS):
            kind = "אוכל"
        elif any(w in caption for w in SCHED_HINTS):
            kind = "לוז"
    if kind is None:
        answer = await _vertex_generate(image, CLASSIFY_PROMPT) or ""
        kind = "אוכל" if "אוכל" in answer else "לוז" if "לוז" in answer else "אחר"

    if kind == "אוכל":
        raw = await _vertex_generate(image, FOOD_PROMPT)
        if raw:
            try:
                clean = raw.replace("```json", "").replace("```", "").strip()
                meal = json.loads(clean)
                if meal.get("is_food") and meal.get("description"):
                    return "meal", {
                        "description": str(meal["description"])[:200],
                        "kcal": int(meal.get("kcal") or 0) or None,
                        "protein_g": int(meal.get("protein_g") or 0) or None,
                    }, None
            except (ValueError, TypeError) as exc:
                logger.warning("JSON של ארוחה לא נקרא: %s | %r", exc, raw[:120])
        return None, None, "זה נראה כמו אוכל אבל לא הצלחתי לזהות מה יש שם. תכתוב לי מה אכלת?"

    # לוז / אחר — הזרימה המקורית: חילוץ טקסט והעברה לסוכן
    text = await _vertex_generate(image, EXTRACT_PROMPT)
    if text is None:
        return None, None, "לא הצלחתי לקרוא את התמונה. תוכל לכתוב את הפרטים?"
    if not text or "אין אירוע" in text:
        return None, None, "לא מצאתי בתמונה אירוע או אוכל. מה רצית שאעשה איתה?"
    prefix = f"{caption}\n" if caption else ""
    return "schedule", (f"{prefix}מהתמונה שצירפתי:\n{text}\n\nתציע מה לקבוע לפי זה."), None


async def photo_to_text(message: dict):
    """תאימות לאחור — לא בשימוש אחרי עדכון routes."""
    kind, payload, err = await analyze_photo(message)
    if kind == "schedule":
        return payload, None
    return None, err
