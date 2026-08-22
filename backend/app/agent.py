"""
ליבת הסוכן — קריאה אחת למודל עם כלים, במקום שלוש קריאות ושרשרת if/else.

עיקרון: כלי קריאה (query_events, list_tasks) מתבצעים מיד ומוחזרים
למודל להמשך ניסוח. כלי כתיבה (create_event, delete_event, add_task)
לא מתבצעים כאן בכלל — הם נכנסים ל-confirm.propose ומחכים לאישור.

זה מה שהופך את "מסכם ומחכה לאישור" מכוונה לארכיטקטורה: אין מסלול
שבו המודל כותב ליומן בלי שאישרת.
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from litellm import acompletion

from app import confirm
from app import db
from app import prompts
from app import travel

logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Jerusalem")
# הספק והמודל נקבעים מהסביבה. ברירת המחדל: Groq, כי Gemini חסום
# גיאוגרפית מ-IP של דאטהסנטרים (נכון לאוגוסט 2026).
# החלפת מודל = שינוי LLM_MODEL ב-.env והפעלה מחדש, בלי לגעת בקוד.
MODEL = os.getenv("LLM_MODEL", "groq/openai/gpt-oss-120b")
MAX_ROUNDS = 5

WEEKDAYS = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]

# כלים שמחזירים מידע ומתבצעים מיד
READ_TOOLS = {"query_events", "list_tasks", "search_events", "meeting_stats"}

# כלים שמשנים משהו ולכן עוברים דרך אישור
WRITE_TOOLS = {"create_event", "update_event", "delete_event",
               "add_task", "complete_task", "log_activity"}

# מטופל בנפרד: הוא לא פעולה חדשה אלא הצעה מחדש של פעולה קיימת
RESTORE_TOOL = "restore_last"

TOOLS = [
    {"type": "function", "function": {
        "name": "create_event",
        "description": "קובע אירוע חדש ביומן. דורש אישור מהמשתמש.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "כותרת האירוע"},
            "start": {"type": "string", "description": "התחלה ISO עם אזור זמן"},
            "end": {"type": "string", "description": "סיום ISO עם אזור זמן"},
            "location": {"type": "string", "description": "כתובת. ריק לאירוע וירטואלי"},
            "virtual": {"type": "boolean",
                        "description": "true לפגישה טלפונית/זום — אין נסיעה"},
            "category": {"type": "string",
                         "enum": ["עבודה", "נסיעה", "אימון", "לימודים",
                                  "אישי", "פרוייקט", "טלפוני"],
                         "description": "קטגוריית האירוע — קובעת את הצבע ביומן"},
            "rrule": {"type": "string",
                      "description": ("חזרתיות בפורמט RRULE, למשל "
                                      "RRULE:FREQ=WEEKLY;BYDAY=TU לכל יום שלישי. "
                                      "ריק לאירוע חד-פעמי")},
            "recurrence": {"type": "string", "description": (
                "לאירוע חוזר. אחד מ: DAILY, WEEKLY, MONTHLY, YEARLY. "
                "אפשר להוסיף מספר חזרות אחרי פסיק, למשל 'WEEKLY,10'. "
                "השאר ריק לאירוע חד-פעמי.")},
        }, "required": ["title", "start", "end"]},
    }},
    {"type": "function", "function": {
        "name": "query_events",
        "description": "מחזיר אירועים מהיומן בטווח תאריכים.",
        "parameters": {"type": "object", "properties": {
            "start": {"type": "string", "description": "תחילת טווח ISO"},
            "end": {"type": "string", "description": "סוף טווח ISO"},
        }, "required": ["start", "end"]},
    }},
    {"type": "function", "function": {
        "name": "update_event",
        "description": "מעדכן אירוע קיים. דורש אישור.",
        "parameters": {"type": "object", "properties": {
            "event_id": {"type": "string"},
            "title": {"type": "string"},
            "start": {"type": "string"},
            "end": {"type": "string"},
            "location": {"type": "string"},
        }, "required": ["event_id"]},
    }},
    {"type": "function", "function": {
        "name": "delete_event",
        "description": "מוחק אירוע. דורש אישור.",
        "parameters": {"type": "object", "properties": {
            "event_id": {"type": "string"},
            "title": {"type": "string", "description": "לתצוגה באישור"},
        }, "required": ["event_id"]},
    }},
    {"type": "function", "function": {
        "name": "add_task",
        "description": "מוסיף משימה בלי זמן קבוע. דורש אישור.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"},
            "notes": {"type": "string"},
            "soft_due": {"type": "string", "description": "יעד רך YYYY-MM-DD"},
            "linked_event": {"type": "string", "description": (
                "כותרת האירוע שהמשימה מכינה אליו, אם זו משימת הכנה")},
            "linked_date": {"type": "string", "description": (
                "YYYY-MM-DD של אותו אירוע. המשימה תעלה בבריף של אותו בוקר")},
        }, "required": ["title"]},
    }},
    {"type": "function", "function": {
        "name": "list_tasks",
        "description": "מחזיר את המשימות הפתוחות.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "complete_task",
        "description": "מסמן משימה כבוצעה. דורש אישור.",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "integer"},
            "title": {"type": "string", "description": "לתצוגה באישור"},
        }, "required": ["task_id"]},
    }},
    {"type": "function", "function": {
        "name": "search_events",
        "description": ("מחפש אירועים בעבר או בעתיד לפי טקסט חופשי. "
                        "לשאלות כמו 'מתי נפגשתי עם X לאחרונה'."),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "מילות חיפוש"},
            "days_back": {"type": "integer",
                          "description": "כמה ימים אחורה לחפש. ברירת מחדל 180"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "meeting_stats",
        "description": ("מסכם כמה זמן בילית בפגישות בתקופה. "
                        "לשאלות כמו 'כמה זמן ביליתי בפגישות החודש'."),
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer", "description": "כמה ימים אחורה. ברירת מחדל 30"},
        }},
    }},
    {"type": "function", "function": {
        "name": "search_events",
        "description": ("חיפוש ביומן אחורה בזמן. למשל 'מתי נפגשתי עם דנה "
                        "לאחרונה'. מחזיר אירועים שתואמים לטקסט."),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "מה לחפש (שם, מילה)"},
            "months_back": {"type": "integer", "description": "כמה חודשים אחורה (ברירת מחדל 6)"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "meeting_stats",
        "description": ("סטטיסטיקה על זמן בפגישות בטווח תאריכים — "
                        "כמה פגישות וכמה שעות."),
        "parameters": {"type": "object", "properties": {
            "start": {"type": "string", "description": "תחילת טווח ISO"},
            "end": {"type": "string", "description": "סוף טווח ISO"},
        }, "required": ["start", "end"]},
    }},
    {"type": "function", "function": {
        "name": "restore_last",
        "description": ("מחזיר את האירוע האחרון שבוטל או נמחק, עם כל הפרטים "
                        "המקוריים. השתמש כשהמשתמש מבקש להחזיר/לשחזר אירוע."),
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "log_activity",
        "description": "מתעד מה המשתמש עשה בפועל. לא דורש אישור.",
        "parameters": {"type": "object", "properties": {
            "content": {"type": "string"},
        }, "required": ["content"]},
    }},
]

# מוזרק מבחוץ כדי שהמודול יישאר נבדק בלי גוגל
_read_executors: dict = {}


def set_read_executor(name: str, fn) -> None:
    _read_executors[name] = fn


def _system_prompt() -> str:
    now = datetime.now(TZ)
    return prompts.SYSTEM_PROMPT.format(
        now=now.strftime("%Y-%m-%d %H:%M"),
        weekday=WEEKDAYS[now.weekday()],
    )


def _summarize(name: str, args: dict) -> str:
    """הטקסט שמוצג לך בהודעת האישור."""
    if name == "create_event":
        start = datetime.fromisoformat(args["start"])
        when = start.strftime("%d/%m בשעה %H:%M")
        where = ""
        if args.get("virtual"):
            where = " (טלפוני)"
        if args.get("rrule"):
            where += " (חוזר)"
        if args.get("category") and not args.get("virtual"):
            where += f" · {args['category']}"
        elif args.get("location"):
            where = f" — {args['location']}"
        repeat = ""
        if args.get("recurrence"):
            names = {"DAILY": "כל יום", "WEEKLY": "כל שבוע",
                     "MONTHLY": "כל חודש", "YEARLY": "כל שנה"}
            freq = str(args["recurrence"]).split(",")[0].upper()
            repeat = f" ({names.get(freq, 'חוזר')})"
        return f"לקבוע \"{args['title']}\" ב-{when}{where}{repeat}?"

    if name == "update_event":
        return f"לעדכן את \"{args.get('title', 'האירוע')}\"?"
    if name == "delete_event":
        return f"למחוק את \"{args.get('title', 'האירוע')}\"?"
    if name == "add_task":
        due = f" (עד {args['soft_due']})" if args.get("soft_due") else ""
        return f"להוסיף משימה: \"{args['title']}\"{due}?"
    if name == "complete_task":
        return f"לסמן כבוצע: \"{args.get('title', 'המשימה')}\"?"
    if name == "log_activity":
        return f"לתעד: \"{args['content']}\"?"
    return "לבצע את הפעולה?"


async def _conflicts_for(args: dict) -> list:
    """בדיקת התנגשויות לפני שמציגים הצעה לאירוע חדש."""
    fetch = _read_executors.get("query_events")
    if fetch is None:
        return []
    try:
        start = datetime.fromisoformat(args["start"])
        end = datetime.fromisoformat(args["end"])
        day_start = start.astimezone(TZ).replace(hour=0, minute=0, second=0,
                                                 microsecond=0)
        events = await fetch(day_start, day_start + timedelta(days=1))
        return await travel.check_conflicts(
            start, end, args.get("location", ""), events,
            new_virtual=bool(args.get("virtual")),
        )
    except Exception as exc:
        logger.warning("בדיקת התנגשויות נכשלה: %s", exc)
        return []


async def _departure_line(args: dict) -> str:
    """
    שורת "צא ב-" שנוספת להצעה. עונה על המקרה של הפגישה הראשונה ביום,
    שבו אין אירוע קודם ולכן בדיקת ההתנגשויות שותקת — אבל עדיין צריך
    לצאת מהבית בזמן.
    """
    fetch = _read_executors.get("query_events")
    if fetch is None:
        return ""
    try:
        start = datetime.fromisoformat(args["start"])
        day_start = start.astimezone(TZ).replace(hour=0, minute=0, second=0,
                                                 microsecond=0)
        events = await fetch(day_start, day_start + timedelta(days=1))
        leave_at, origin, minutes = await travel.departure_hint(
            start, args.get("location", ""), events,
            new_virtual=bool(args.get("virtual")),
        )
        if leave_at is None:
            return ""
        return (f"\n\nצא ב-{leave_at.astimezone(TZ):%H:%M} "
                f"({minutes} דק' נסיעה + {travel.BUFFER_MINUTES} חיץ)")
    except Exception as exc:
        logger.warning("חישוב שעת יציאה נכשל: %s", exc)
        return ""


async def _call_llm(messages: list) -> Optional[dict]:
    """
    קריאה למודל עם ניסיון חוזר אחד על מגבלת קצב.
    המדרגה החינמית של Groq מוגבלת ל-8000 טוקנים לדקה, ותור עם כמה
    פריטים שולח כמה קריאות ברצף — קל לפגוע בתקרה. Groq מציינת בשגיאה
    כמה לחכות; אם זה סביר, מחכים ומנסים שוב במקום להיכשל בפני המשתמש.
    """
    for attempt in (1, 2):
        try:
            return await acompletion(
                model=MODEL, messages=messages, tools=TOOLS,
                tool_choice="auto", max_tokens=800,
            )
        except Exception as exc:
            if "RateLimit" in type(exc).__name__ and attempt == 1:
                match = re.search(r"in ([\d.]+)s", str(exc))
                wait = min(float(match.group(1)) if match else 20.0, 25.0) + 1
                logger.warning("מגבלת קצב — ממתין %.0f שניות ומנסה שוב", wait)
                await asyncio.sleep(wait)
                continue
            logger.error("קריאה למודל נכשלה: %s", exc)
            return None
    return None


async def handle_message(chat_id: int, text: str) -> Optional[str]:
    """
    מחזיר טקסט לשליחה, או None כשכבר נשלחה הודעת אישור עם כפתורים.
    """
    db.add_message(chat_id, "user", text)

    history = db.get_history(chat_id, limit=6)
    messages = [{"role": "system", "content": _system_prompt()}]
    messages += [{"role": m["role"], "content": m["content"]} for m in history]

    proposed_any = False

    for _ in range(MAX_ROUNDS):
        response = await _call_llm(messages)
        if response is None:
            return ("יש עומס רגעי על המודל. חכה חצי דקה ותנסה שוב — "
                    "שום דבר לא אבד.")

        choice = response["choices"][0]["message"]
        tool_calls = choice.get("tool_calls") or []

        if not tool_calls:
            reply = (choice.get("content") or "").strip()
            if reply:
                db.add_message(chat_id, "assistant", reply)
                return reply
            # אין טקסט: אם כבר נשלחו הצעות — סיימנו, הכפתורים אצל המשתמש
            return None if proposed_any else "לא הבנתי. תוכל לנסח אחרת?"

        messages.append(choice)

        for call in tool_calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}

            # --- שחזור: שולפים את הפעולה הקודמת ומציעים אותה שוב ---
            if name == RESTORE_TOOL:
                previous = db.get_last_action(chat_id, ("create_event",))
                if previous is None:
                    return "אין אירוע קודם שאני יכול להחזיר."
                args = previous["payload"]
                conflicts = await _conflicts_for(args)
                await confirm.propose(
                    chat_id=chat_id, action_type="create_event", payload=args,
                    summary="להחזיר: " + _summarize("create_event", args),
                    conflicts=conflicts,
                )
                return None

            # --- כלי כתיבה: מציעים, ו*ממשיכים* —
            # חיוני כשהמודל שולח פריטים בזה אחר זה ולא במקביל.
            # בלי ההמשך, "תקבע X וגם Y" היה נעצר אחרי X.
            if name in WRITE_TOOLS:
                conflicts = []
                summary = _summarize(name, args)
                if name == "create_event":
                    conflicts = await _conflicts_for(args)
                    summary += await _departure_line(args)
                await confirm.propose(
                    chat_id=chat_id, action_type=name, payload=args,
                    summary=summary, conflicts=conflicts,
                )
                proposed_any = True
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", name),
                    "name": name,
                    "content": ("נשלחה למשתמש הודעת אישור עם כפתורים. "
                                "אל תשאל אותו שוב על הפריט הזה. אם נשארו "
                                "פריטים נוספים בבקשה — טפל בהם עכשיו. "
                                "אם לא — סיים בלי טקסט."),
                })
                continue

            # --- כלי קריאה: מבצעים ומחזירים למודל ---
            executor = _read_executors.get(name)
            if executor is None:
                result = "הכלי אינו זמין."
            else:
                try:
                    result = await executor(**args) if args else await executor()
                except Exception as exc:
                    logger.error("כלי %s נכשל: %s", name, exc)
                    result = "הפעולה נכשלה."

            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", name),
                "name": name,
                "content": json.dumps(result, ensure_ascii=False, default=str),
            })

    if proposed_any:
        return None
    return "הסתבכתי עם הבקשה הזו. תוכל לפרק אותה לשני חלקים?"
