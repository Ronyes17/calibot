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
from app import tts
from app.services.telegram import send_telegram_message, send_telegram_voice

logger = logging.getLogger(__name__)
router = APIRouter()
TZ = ZoneInfo("Asia/Jerusalem")

calendar_service = GoogleCalendarService()


@router.get("/dashboard")
async def dashboard_view(key: str = "", view: str = "today"):
    """דאשבורד קריאה בלבד. מוגן בקישור סודי: /dashboard?key=<DASHBOARD_TOKEN>."""
    from fastapi.responses import HTMLResponse
    from app import dashboard as dash
    from app import executors as _ex
    from app.config import OWNER_CHAT_ID

    if not dash.token_ok(key):
        return HTMLResponse("<h3>404</h3>", status_code=404)
    if view not in ("today", "week"):
        view = "today"
    html_out = await dash.render(int(OWNER_CHAT_ID), _ex.fetch_events,
                                 view=view, key=key)
    return HTMLResponse(html_out)


_LEGAL_STYLE = ("<meta name='viewport' content='width=device-width, initial-scale=1'>"
                "<style>body{font-family:system-ui,sans-serif;max-width:640px;"
                "margin:40px auto;padding:0 16px;line-height:1.6}</style>")


@router.get("/privacy")
async def privacy_page():
    """מדיניות פרטיות — נדרשת לפרסום אפליקציית OAuth בגוגל."""
    from fastapi.responses import HTMLResponse
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Nikol - Privacy Policy</title>{_LEGAL_STYLE}</head><body>
<h1>Nikol - Privacy Policy</h1>
<p>Nikol is a private personal assistant operated by Ron Yeshayahu for his own use only.
It is not offered to the public.</p>
<p><b>Data accessed.</b> With the owner's explicit consent, Nikol accesses the owner's
Google Calendar, Gmail and Google Drive/Sheets to read and manage events, read and send
email, and read or update files the owner asks about.</p>
<p><b>Use.</b> Data is used only to provide assistant features to the owner. It is not
sold or shared with third parties for advertising. Content may be processed by AI service
providers (such as Google Gemini) solely to generate responses.</p>
<p><b>Storage.</b> Data is stored on the owner's private server and deleted on request.
Access can be revoked at any time at https://myaccount.google.com/permissions.</p>
<p>Use of Google API data complies with the Google API Services User Data Policy,
including the Limited Use requirements.</p>
<p>Contact: ronnoy17@gmail.com</p></body></html>""")


@router.get("/terms")
async def terms_page():
    from fastapi.responses import HTMLResponse
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Nikol - Terms of Service</title>{_LEGAL_STYLE}</head><body>
<h1>Nikol - Terms of Service</h1>
<p>Nikol is a private personal assistant used solely by its owner, Ron Yeshayahu.
No service is offered to other users. The software is provided as-is, without warranty.</p>
<p>Contact: ronnoy17@gmail.com</p></body></html>""")


@router.get("/dashboard/manifest.webmanifest")
async def dashboard_manifest(key: str = ""):
    """מאפשר 'הוספה למסך הבית' — הדאשבורד נפתח כאפליקציה במסך מלא."""
    from fastapi.responses import JSONResponse
    from app import dashboard as dash
    if not dash.token_ok(key):
        return JSONResponse({}, status_code=404)
    return JSONResponse({
        "name": "ניקול",
        "short_name": "ניקול",
        "start_url": f"/dashboard?key={key}",
        "scope": "/dashboard",
        "display": "standalone",
        "dir": "rtl",
        "lang": "he",
        "background_color": "#0E1026",
        "theme_color": "#0E1026",
        "icons": [{"src": "/dashboard/icon.png", "sizes": "512x512",
                   "type": "image/png", "purpose": "any maskable"}],
    }, media_type="application/manifest+json")


@router.get("/dashboard/icon.png")
async def dashboard_icon():
    from pathlib import Path
    from fastapi.responses import FileResponse
    return FileResponse(Path(__file__).resolve().parent.parent / "nikol-icon.png",
                        media_type="image/png")


@router.post("/dashboard/chat")
async def dashboard_chat(payload: dict):
    """צ'אט מהפאנל בדאשבורד — אותה ניקול, אותה היסטוריה, בלי טלגרם באמצע."""
    from fastapi.responses import JSONResponse
    from app import dashboard as dash
    from app.config import OWNER_CHAT_ID

    if not dash.token_ok(payload.get("key", "")):
        return JSONResponse({"reply": ""}, status_code=404)
    text = (payload.get("text") or "").strip()[:2000]
    if not text:
        return {"reply": "לא קיבלתי טקסט."}
    try:
        reply = await agent.handle_message(int(OWNER_CHAT_ID), text)
        # reply ריק = ניקול שלחה הצעת אישור עם כפתורים לטלגרם
        return {"reply": reply or "הכנתי הצעה שדורשת אישור — שלחתי אותה לטלגרם שלך 📲"}
    except Exception as exc:
        logger.error("צ'אט הדאשבורד נכשל: %s", exc)
        return {"reply": "משהו השתבש אצלי. נסה שוב?"}


@router.get("/oauth2callback")
async def oauth_callback(request: Request):
    """
    חזרה מגוגל אחרי אישור ההרשאות. בלי הנתיב הזה הדפדפן מקבל
    Not Found וה-token.pickle לעולם לא נוצר.
    """
    logger.info("התקבלה חזרה מ-OAuth")
    return await calendar_service.handle_oauth_callback(request)


from collections import deque
_seen_updates: deque = deque(maxlen=500)


@router.post("/webhook")
async def telegram_webhook(update: TelegramUpdate):
    # טלגרם שולח שוב עדכון שלא נענה מהר. בלי זה הודעה קולית איטית
    # טופלה פעמיים (שני תמלולים, שתי תשובות).
    uid = getattr(update, "update_id", None)
    if uid is not None:
        if uid in _seen_updates:
            return {"status": "ok"}
        _seen_updates.append(uid)
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

    # --- 3א. הקלטה ארוכה / קובץ אודיו: תמלול וסיכום ברקע ---
    from app import features
    if features.is_recording(update.message, voice.MAX_DURATION_SECONDS):
        media = features.media_of(update.message)
        features.start_recording_job(
            chat_id, file_id=media["file_id"], size=media.get("file_size") or 0,
            title=(update.message.get("caption") or media.get("file_name")
                   or media.get("title") or ""))
        await send_telegram_message(chat_id, "🎙 קיבלתי. מעבדת את ההקלטה ברקע…")
        return {"status": "ok"}

    # --- 3. הודעה קולית ---
    text, err = await voice.voice_to_text(update.message)
    if err:
        await send_telegram_message(chat_id, err)
        return {"status": "ok"}

    # --- 3ב. תמונה: אוכל -> יומן תזונה; זימון/משמרות -> הסוכן ---
    if text is None:
        kind, payload, err = await vision.analyze_photo(update.message)
        if err:
            await send_telegram_message(chat_id, err)
            return {"status": "ok"}
        if kind == "meal":
            from app import nutrition
            reply = nutrition.log(chat_id, payload["description"],
                                  payload.get("kcal"), payload.get("protein_g"))
            await send_telegram_message(chat_id, f"📸 {reply}")
            return {"status": "ok"}
        if kind == "receipt":
            from app import features
            reply = features.add_expense(
                chat_id, payload["amount"], payload.get("category") or "אחר",
                payload.get("merchant") or "", payload.get("currency") or "ILS",
                payload.get("date") or "")
            await send_telegram_message(
                chat_id, f"🧾 {reply}\n(טעות? כתוב 'תמחקי את ההוצאה האחרונה')")
            return {"status": "ok"}
        if kind == "schedule":
            text = payload

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
    came_as_voice = bool(update.message.get("voice"))
    VOICE_WORDS = ("תקליטי", "בקול", "הקליטי", "תעני בקול")
    want_voice = came_as_voice or any(w in user_message for w in VOICE_WORDS)
    try:
        reply = await agent.handle_message(chat_id, user_message)
        if reply:                      # None = נשלחה הודעת אישור עם כפתורים
            sent_as_voice = False
            if want_voice:
                audio, why = await tts.synthesize(reply)
                if audio:
                    await send_telegram_voice(chat_id, audio, caption=reply[:1024])
                    sent_as_voice = True
                else:
                    logger.info("TTS לא סינתז (%s) — נופל לטקסט", why)
            if not sent_as_voice:
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

    if cmd in ("/reauth", "/התחברות"):
        # אימות מחדש מול גוגל — נדרש אחרי הוספת הרשאות (Drive/Sheets)
        import os as _os
        for f in ("token.pickle",):
            try:
                _os.remove(f)
            except FileNotFoundError:
                pass
        calendar_service.credentials = None
        calendar_service.service = None
        return ("כדי לאשר מחדש את ההרשאות של גוגל (יומן, מייל, Drive, Sheets), "
                f"פתח את הקישור:\n{calendar_service.get_auth_url()}")

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
            "/undo — ביטול האירוע האחרון\n"
            "/reauth — אישור מחדש של הרשאות גוגל"
        )

    return None


MAX_DOC_BYTES = 5 * 1024 * 1024


async def _ingest_document(doc: dict) -> tuple[str, str]:
    """הורדת מסמך מטלגרם, חילוץ טקסט, ושמירה בבסיס הידע."""
    import httpx
    from app.config import TELEGRAM_API_TOKEN as TOKEN

    name = doc.get("file_name", "מסמך")
    if doc.get("file_size", 0) > MAX_DOC_BYTES:
        return "", f"{name} גדול מדי (מעל 5MB)."

    lowered = name.lower()
    if not lowered.endswith((".pdf", ".txt", ".md")):
        return "", "אני יודעת לשמור PDF או קובצי טקסט (txt/md)."

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            meta = await client.get(
                f"https://api.telegram.org/bot{TOKEN}/getFile",
                params={"file_id": doc["file_id"]},
            )
            path = meta.json()["result"]["file_path"]
            resp = await client.get(
                f"https://api.telegram.org/file/bot{TOKEN}/{path}"
            )
            resp.raise_for_status()
            raw = resp.content
    except Exception as exc:
        logger.error("הורדת מסמך נכשלה: %s", exc)
        return "", "לא הצלחתי להוריד את הקובץ."

    if lowered.endswith(".pdf"):
        try:
            import io
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(raw))
            content = "\n".join((p.extract_text() or "") for p in reader.pages)
        except Exception as exc:
            logger.error("חילוץ PDF נכשל: %s", exc)
            return "", "לא הצלחתי לקרוא את ה-PDF."
    else:
        content = raw.decode("utf-8", errors="replace")

    content = content.strip()[:200_000]
    if not content:
        return "", "הקובץ ריק או שלא הצלחתי לחלץ ממנו טקסט."

    db.add_document(name, content)
    return f'נשמר בבסיס הידע: {name}. תוכל לשאול אותי עליו מתי שתרצה.', ""
