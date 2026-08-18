"""
מחליף את הפונקציה telegram_webhook ב-backend/app/api/routes.py.

סדר הטיפול חשוב — כל שלב חוסך את הבאים אחריו:
    1. הרשאה        זר לא מגיע לשום דבר
    2. לחיצת כפתור  callback_query, לא הודעה
    3. הודעה קולית  תמלול -> ממשיך כטקסט רגיל
    4. פקודה מהירה  /today, /tasks — בלי מודל בכלל, מיידי וחינם
    5. אישור בטקסט  "כן"/"לא" על הצעה פתוחה
    6. הסוכן        רק מה שלא נתפס למעלה מגיע למודל
"""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request

from app import agent
from app import confirm
from app import db
from app import executors
from app import policy
from app import vision
from app import vision
from app import voice
from app.api.models import TelegramUpdate
from app.services.google_calendar import GoogleCalendarService
from app.services.telegram import send_telegram_message

logger = logging.getLogger(__name__)
router = APIRouter()
TZ = ZoneInfo("Asia/Jerusalem")

calendar_service = GoogleCalendarService()


@router.get("/oauth2callback")
async def oauth_callback(request: Request):
    """
    חזרה מגוגל אחרי אישור ההרשאות. בלי הנתיב הזה הדפדפן מקבל
    Not Found וה-token.pickle לעולם לא נוצר.
    """
    logger.info("התקבלה חזרה מ-OAuth")
    return await calendar_service.handle_oauth_callback(request)


@router.post("/webhook")
async def telegram_webhook(update: TelegramUpdate):
    # --- 2. לחיצה על כפתור ---
    if getattr(update, "callback_query", None):
        chat_id = update.callback_query["message"]["chat"]["id"]
        if not policy.is_allowed(chat_id):
            return {"status": "ok"}
        await confirm.handle_callback(update.callback_query)
        return {"status": "ok"}

    if not update.message:
        return {"status": "ok"}

    chat_id = update.message["chat"]["id"]

    # --- 1. הרשאה ---
    if not policy.is_allowed(chat_id):
        logger.warning("נחסמה פנייה מ-chat_id לא מורשה: %s", chat_id)
        return {"status": "ok"}

    # --- אימות גוגל ---
    if calendar_service.is_authenticated() is not True:
        await send_telegram_message(
            chat_id,
            f"צריך לחבר את חשבון גוגל: {calendar_service.get_auth_url()}",
        )
        return {"status": "ok"}

    # --- 3. הודעה קולית ---
    text, err = await voice.voice_to_text(update.message)
    if err:
        await send_telegram_message(chat_id, err)
        return {"status": "ok"}

    # --- 3ב. תמונה: זימון תור, כרטיס, צילום מסך ---
    if text is None:
        text, err = await vision.photo_to_text(update.message)
        if err:
            await send_telegram_message(chat_id, err)
            return {"status": "ok"}

    user_message = text or update.message.get("text") or update.message.get("caption")

    # --- 3ג. הודעה מועברת ---
    # ככה פגישות באמת נקבעות: מישהו כותב לך, ואתה מעביר לבוט.
    if user_message and _is_forwarded(update.message):
        sender = _forward_sender(update.message)
        prefix = f"הודעה שקיבלתי{f' מ{sender}' if sender else ''}"
        user_message = f"{prefix}:\n{user_message}\n\nמה שרלוונטי ליומן?"
    if not user_message:
        await send_telegram_message(chat_id, "לא הבנתי את ההודעה. תוכל לכתוב?")
        return {"status": "ok"}

    # --- 4. פקודות מהירות ---
    quick = await _quick_command(chat_id, user_message)
    if quick is not None:
        await send_telegram_message(chat_id, quick)
        return {"status": "ok"}

    # --- 5. אישור בטקסט ---
    if await confirm.try_text_confirmation(chat_id, user_message):
        return {"status": "ok"}

    # --- 6. הסוכן ---
    try:
        reply = await agent.handle_message(chat_id, user_message)
        if reply:                      # None = נשלחה הודעת אישור עם כפתורים
            await send_telegram_message(chat_id, reply)
    except Exception as exc:
        logger.error("הסוכן נכשל: %s", exc)
        await send_telegram_message(chat_id, "משהו השתבש. תנסה שוב?")

    return {"status": "ok"}


def _is_forwarded(message: dict) -> bool:
    return bool(message.get("forward_origin") or message.get("forward_from")
                or message.get("forward_sender_name")
                or message.get("forward_from_chat"))


def _forward_sender(message: dict) -> str:
    origin = message.get("forward_origin") or {}
    user = origin.get("sender_user") or message.get("forward_from") or {}
    if user.get("first_name"):
        return user["first_name"]
    chat = origin.get("chat") or message.get("forward_from_chat") or {}
    if chat.get("title"):
        return chat["title"]
    return origin.get("sender_user_name") or message.get("forward_sender_name", "")


async def _quick_command(chat_id: int, text: str):
    """מחזיר טקסט תשובה, או None אם זו לא פקודה."""
    cmd = text.strip().lower()

    if cmd in ("/today", "/היום"):
        now = datetime.now(TZ)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        events = await executors.fetch_events(start, start + timedelta(days=1))
        if not events:
            return "היומן ריק היום."
        return "\n".join(
            f"{e['start'].astimezone(TZ):%H:%M}  {e['summary']}"
            for e in sorted(events, key=lambda e: e["start"])
        )

    if cmd in ("/tasks", "/משימות"):
        tasks = db.list_open_tasks(chat_id)
        if not tasks:
            return "אין משימות פתוחות."
        return "\n".join(f"[{t['id']}] {t['title']}" for t in tasks)

    if cmd in ("/tomorrow", "/מחר"):
        start = (datetime.now(TZ) + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        events = await executors.fetch_events(start, start + timedelta(days=1))
        if not events:
            return "מחר פנוי."
        return "\n".join(
            f"{e['start'].astimezone(TZ):%H:%M}  {e['summary']}"
            for e in sorted(events, key=lambda e: e["start"])
        )

    if cmd in ("/week", "/שבוע"):
        now = datetime.now(TZ)
        events = await executors.fetch_events(now, now + timedelta(days=7))
        if not events:
            return "השבוע פנוי."
        return "\n".join(
            f"{e['start'].astimezone(TZ):%d/%m %H:%M}  {e['summary']}"
            for e in sorted(events, key=lambda e: e["start"])
        )

    if cmd == "/undo":
        return await executors.undo(chat_id)

    if cmd in ("/start", "/help", "/עזרה"):
        return (
            "אני מנהל לך את היומן ואת המשימות.\n\n"
            "אפשר לדבר איתי חופשי, גם בהודעה קולית.\n\n"
            "/today — מה יש היום\n"
            "/tomorrow — מה יש מחר\n"
            "/week — השבוע הקרוב\n"
            "/tasks — משימות פתוחות\n"
            "/undo — ביטול האירוע האחרון"
        )

    return None
