"""
המבצעים: מה שקורה אחרי שלחצת "אשר".

העטיפה של CaliBOT התבררה כלא מתאימה בשלושה דברים:
  1. היא לא שומרת ולא מחזירה location — כלומר כל חישוב הנסיעה מת
  2. create_event מצפה ל-date + start_time נפרדים, לא ל-ISO
  3. query_events יודעת יום בודד בלבד, ולא טווח כמו "השבוע"
לכן כאן פונים ישירות לאובייקט השירות של גוגל. get_calendar_service()
עדיין מטפל בכל ה-OAuth והרענון — לא נגענו בזה.

חיבור ב-main.py:
    import executors
    executors.set_calendar_service(calendar_service)
"""

import logging
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from app import agent
from app import db
from app.confirm import register_executor

logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Jerusalem")
CAL_ID = "primary"

# פלטת הצבעים של יומן גוגל קבועה — 11 צבעים בלבד (colorId).
# אין חום אמיתי בפלטה; "נסיעה" ממופה לכתום, הקרוב ביותר.
CATEGORY_COLORS = {
    "עבודה":   "10",  # ירוק כהה (Basil)
    "נסיעה":   "6",   # כתום (Tangerine)
    "אימון":   "11",  # אדום (Tomato)
    "לימודים": "3",   # סגול (Grape)
    "אישי":    "7",   # טורקיז (Peacock)
    "פרוייקט": "9",   # כחול כהה (Blueberry)
    "טלפוני":  "8",   # אפור (Graphite)
}

_calendar = None
_owner_chat_id: Optional[int] = None

# אירועים שנוצרו לאחרונה, לטובת ביטול
_last_created: dict[int, str] = {}


def set_calendar_service(service, owner_chat_id: int = None) -> None:
    global _calendar, _owner_chat_id
    _calendar = service
    _owner_chat_id = owner_chat_id
    agent.set_read_executor("query_events", fetch_events)
    agent.set_read_executor("list_tasks", list_tasks)
    agent.set_read_executor("search_events", search_events)
    agent.set_read_executor("meeting_stats", meeting_stats)
    agent.set_read_executor("search_events", search_events)
    agent.set_read_executor("meeting_stats", meeting_stats)


def _service():
    """אובייקט ה-API של גוגל, או None אם אין אימות."""
    if _calendar is None:
        return None
    return _calendar.get_calendar_service()


def _tz_name() -> str:
    try:
        return _calendar.get_user_timezone() or "Asia/Jerusalem"
    except Exception:
        return "Asia/Jerusalem"


# ------------------------------------------------- כלי קריאה

async def fetch_events(start: datetime, end: datetime) -> list[dict]:
    """
    [{'id','summary','start','end','location','description'}] עם start/end כ-datetime.
    זו גם הפונקציה שהמתזמן מקבל דרך Deps.
    """
    service = _service()
    if service is None:
        return []

    if isinstance(start, str):
        start = datetime.fromisoformat(start)
    if isinstance(end, str):
        end = datetime.fromisoformat(end)
    if start.tzinfo is None:
        start = start.replace(tzinfo=TZ)
    if end.tzinfo is None:
        end = end.replace(tzinfo=TZ)

    try:
        result = service.events().list(
            calendarId=CAL_ID,
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=50,
        ).execute()
    except Exception as exc:
        logger.error("שליפת אירועים נכשלה: %s", exc)
        return []

    events = []
    for ev in result.get("items", []):
        try:
            events.append({
                "id": ev.get("id"),
                "summary": ev.get("summary", "ללא כותרת"),
                "start": _parse(ev["start"]),
                "end": _parse(ev.get("end", ev["start"])),
                "location": ev.get("location", "") or "",
                "description": ev.get("description", "") or "",
            })
        except Exception as exc:
            logger.warning("דילוג על אירוע שלא נפרסר: %s", exc)
    return events


def _parse(value) -> datetime:
    """גוגל מחזיר dateTime, או date לאירוע יום שלם."""
    if isinstance(value, dict):
        value = value.get("dateTime") or value.get("date")
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


async def search_events(query: str, days_back: int = 180) -> list[dict]:
    """
    חיפוש טקסט חופשי ביומן. גוגל מחפשת בכותרת, בתיאור ובמיקום.
    היומן הוא ארכיון — זו הדרך לשאול אותו שאלות.
    """
    service = _service()
    if service is None:
        return []

    now = datetime.now(TZ)
    try:
        result = service.events().list(
            calendarId=CAL_ID,
            q=query,
            timeMin=(now - timedelta(days=days_back)).isoformat(),
            timeMax=(now + timedelta(days=365)).isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=25,
        ).execute()
    except Exception as exc:
        logger.error("חיפוש אירועים נכשל: %s", exc)
        return []

    found = []
    for ev in result.get("items", []):
        try:
            found.append({
                "summary": ev.get("summary", "ללא כותרת"),
                "when": _parse(ev["start"]).strftime("%d/%m/%Y %H:%M"),
                "location": ev.get("location", "") or "",
            })
        except Exception:
            continue
    return found


async def meeting_stats(days: int = 30) -> dict:
    """כמה פגישות ובכמה שעות, בתקופה האחרונה."""
    now = datetime.now(TZ)
    events = await fetch_events(now - timedelta(days=days), now)

    total_minutes = 0
    for ev in events:
        total_minutes += int((ev["end"] - ev["start"]).total_seconds() // 60)

    return {
        "תקופה_בימים": days,
        "מספר_אירועים": len(events),
        "סך_שעות": round(total_minutes / 60, 1),
        "ממוצע_לאירוע_בדקות": round(total_minutes / len(events)) if events else 0,
    }


async def list_tasks() -> list[dict]:
    if _owner_chat_id is None:
        return []
    return [{"id": t["id"], "title": t["title"], "soft_due": t["soft_due"]}
            for t in db.list_open_tasks(_owner_chat_id)]


# ------------------------------------------------ כלי כתיבה

def _rrule(recurrence) -> Optional[str]:
    """
    'WEEKLY' או 'WEEKLY,10' -> כלל RRULE של גוגל.
    בלי מספר חזרות האירוע נמשך ללא הגבלה.
    """
    if not recurrence:
        return None
    parts = str(recurrence).split(",")
    freq = parts[0].strip().upper()
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        return None
    rule = f"RRULE:FREQ={freq}"
    if len(parts) > 1 and parts[1].strip().isdigit():
        rule += f";COUNT={parts[1].strip()}"
    return rule


@register_executor("create_event")
async def _create_event(payload: dict) -> str:
    service = _service()
    if service is None:
        return "צריך לחבר את חשבון גוגל קודם."

    tz = _tz_name()
    location = "" if payload.get("virtual") else (payload.get("location") or "")

    body = {
        "summary": payload["title"],
        "description": payload.get("description", ""),
        "start": {"dateTime": payload["start"], "timeZone": tz},
        "end": {"dateTime": payload["end"], "timeZone": tz},
    }
    if location:
        body["location"] = location

    category = (payload.get("category") or "").strip()
    if payload.get("virtual") and not category:
        category = "טלפוני"
    color = CATEGORY_COLORS.get(category)
    if color:
        body["colorId"] = color

    if payload.get("rrule"):
        rule = payload["rrule"]
        body["recurrence"] = [rule if rule.startswith("RRULE") else f"RRULE:{rule}"]
    if payload.get("virtual"):
        body["description"] = (body["description"] + "\nפגישה טלפונית").strip()

    rule = _rrule(payload.get("recurrence"))
    if rule:
        body["recurrence"] = [rule]

    try:
        created = service.events().insert(calendarId=CAL_ID, body=body).execute()
    except Exception as exc:
        logger.error("יצירת אירוע נכשלה: %s", exc)
        return "לא הצלחתי לקבוע את האירוע."

    if _owner_chat_id:
        _last_created[_owner_chat_id] = created["id"]

    when = _parse(payload["start"]).strftime("%d/%m בשעה %H:%M")
    line = f"נקבע: {payload['title']} — {when}"

    if location:
        from app.travel import waze_link
        line += f"\nניווט: {waze_link(location)}"
    line += "\n\n/undo לביטול"
    return line


@register_executor("update_event")
async def _update_event(payload: dict) -> str:
    service = _service()
    if service is None:
        return "צריך לחבר את חשבון גוגל קודם."

    tz = _tz_name()
    body = {}
    if payload.get("title"):
        body["summary"] = payload["title"]
    if payload.get("location"):
        body["location"] = payload["location"]
    if payload.get("start"):
        body["start"] = {"dateTime": payload["start"], "timeZone": tz}
    if payload.get("end"):
        body["end"] = {"dateTime": payload["end"], "timeZone": tz}

    if not body:
        return "לא היה מה לעדכן."

    try:
        service.events().patch(
            calendarId=CAL_ID, eventId=payload["event_id"], body=body
        ).execute()
        return "עודכן."
    except Exception as exc:
        logger.error("עדכון אירוע נכשל: %s", exc)
        return "העדכון נכשל."


@register_executor("delete_event")
async def _delete_event(payload: dict) -> str:
    service = _service()
    if service is None:
        return "צריך לחבר את חשבון גוגל קודם."
    try:
        service.events().delete(
            calendarId=CAL_ID, eventId=payload["event_id"]
        ).execute()
        return "נמחק."
    except Exception as exc:
        logger.error("מחיקת אירוע נכשלה: %s", exc)
        return "המחיקה נכשלה."


@register_executor("add_task")
async def _add_task(payload: dict) -> str:
    db.add_task(
        _owner_chat_id, payload["title"],
        payload.get("notes"), payload.get("soft_due"),
    )
    return f"נוספה משימה: {payload['title']}"


@register_executor("complete_task")
async def _complete_task(payload: dict) -> str:
    ok = db.complete_task(int(payload["task_id"]))
    return "סומן כבוצע." if ok else "לא מצאתי את המשימה."


@register_executor("log_activity")
async def _log_activity(payload: dict) -> str:
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    db.log_activity(_owner_chat_id, today, payload["content"])
    return "נרשם."


# ------------------------------------------------------- ביטול

async def undo(chat_id: int) -> str:
    """/undo — מוחק את האירוע האחרון שנוצר."""
    event_id = _last_created.pop(chat_id, None)
    if not event_id:
        return "אין מה לבטל."
    service = _service()
    if service is None:
        return "צריך לחבר את חשבון גוגל קודם."
    try:
        service.events().delete(calendarId=CAL_ID, eventId=event_id).execute()
        return "בוטל."
    except Exception as exc:
        logger.error("ביטול נכשל: %s", exc)
        return "הביטול נכשל."


async def search_events(query: str, months_back: int = 6) -> list[dict]:
    """חיפוש טקסטואלי ביומן אחורה — 'מתי נפגשתי עם X לאחרונה'."""
    service = _service()
    if service is None:
        return []
    from datetime import timedelta
    now = datetime.now(TZ)
    try:
        result = service.events().list(
            calendarId=CAL_ID,
            q=query,
            timeMin=(now - timedelta(days=30 * max(1, months_back))).isoformat(),
            timeMax=now.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=20,
        ).execute()
    except Exception as exc:
        logger.error("חיפוש אירועים נכשל: %s", exc)
        return []
    return [{
        "summary": ev.get("summary", ""),
        "start": str(_parse(ev["start"])),
        "location": ev.get("location", ""),
    } for ev in result.get("items", [])]


async def meeting_stats(start, end) -> dict:
    """כמה פגישות וכמה שעות בטווח."""
    events = await fetch_events(start, end)
    total_minutes = sum(
        int((e["end"] - e["start"]).total_seconds() // 60) for e in events
    )
    return {
        "count": len(events),
        "hours": round(total_minutes / 60, 1),
        "busiest": max(events, key=lambda e: e["end"] - e["start"])["summary"]
        if events else None,
    }
