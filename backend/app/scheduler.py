"""
השכבה הפרואקטיבית — שלושת הג'ובים הקבועים.

  07:30  בריף בוקר      כל היום מראש, כולל אזהרות על מעברים צפופים
  21:00  סיכום ערב      מה מחר, ושאלה מה הספקת היום
  11:00  משימות תקועות  רק כשיש מה להגיד

שתי החלטות שחשובות בפרודקשן:
  coalesce=True — אם השרת היה למטה משבע עד תשע, לא יורים שלוש הרצות
  שהצטברו, אלא אחת.
  misfire_grace_time — בריף בוקר שמגיע ב-14:00 הוא רעש, לא עזרה. אחרי
  שעה הוא כבר לא רלוונטי ומדלגים עליו. סיכום הערב סלחני יותר.

הג'ובים לא יודעים כלום על גוגל או על טלגרם. הכל מוזרק דרך Deps,
ולכן אפשר לבדוק את כל השכבה בלי רשת.
"""

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app import backup
from app import gmail_reader
from app import policy
from app import weather
from app import db
from app import weather
from app import travel

logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Jerusalem")

# שעות הג'ובים נקראות מהסביבה כדי שאפשר יהיה לשנות בלי לגעת בקוד.
# שינוי דורש רק עריכת .env והפעלה מחדש של השירות.
MORNING_HOUR = int(os.getenv("MORNING_HOUR", 7))
MORNING_MINUTE = int(os.getenv("MORNING_MINUTE", 30))
EVENING_HOUR = int(os.getenv("EVENING_HOUR", 21))
EVENING_MINUTE = int(os.getenv("EVENING_MINUTE", 0))
NUDGE_HOUR = int(os.getenv("NUDGE_HOUR", 11))
NUDGE_MINUTE = int(os.getenv("NUDGE_MINUTE", 0))
BACKUP_HOUR = int(os.getenv("BACKUP_HOUR", 3))
BACKUP_MINUTE = int(os.getenv("BACKUP_MINUTE", 30))


@dataclass
class Deps:
    """כל מה שהג'ובים צריכים מבחוץ."""
    chat_id: int
    send: Callable[[int, str], Awaitable[None]]
    # מקבל (from_dt, to_dt) ומחזיר [{'summary','start','end','location'}]
    fetch_events: Callable[[datetime, datetime], Awaitable[list[dict]]]


_deps: Optional[Deps] = None
_scheduler: Optional[AsyncIOScheduler] = None


def _fmt_time(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%H:%M")


def _day_bounds(day: datetime) -> tuple[datetime, datetime]:
    start = day.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


async def _tight_transitions(events: list[dict]) -> list[str]:
    """
    עובר על אירועי היום לפי הסדר ומחפש מעברים שאין בהם מספיק זמן.
    מדלג בשקט כשאין מיקום או כשהחישוב נכשל.
    """
    warnings = []
    ordered = sorted(events, key=lambda e: e["start"])

    for prev, nxt in zip(ordered, ordered[1:]):
        dest = (nxt.get("location") or "").strip()
        if not dest:
            continue
        origin = (prev.get("location") or "").strip() or travel.HOME_ADDRESS

        est = await travel.travel_minutes(origin, dest)
        if not est.known:
            continue

        gap = int((nxt["start"] - prev["end"]).total_seconds() // 60)
        if gap < est.total_needed:
            warnings.append(
                f"⚠️ מ\"{prev['summary']}\" ל\"{nxt['summary']}\" — "
                f"{gap} דקות בלבד, וצריך {est.total_needed}"
            )
    return warnings


# ------------------------------------------------------- בריף בוקר

async def morning_brief() -> None:
    if _deps is None:
        return
    now = datetime.now(TZ)
    start, end = _day_bounds(now)
    events = await _deps.fetch_events(start, end)
    tasks = db.list_open_tasks(_deps.chat_id)

    heb = policy.hebrew_date(now)
    header = f"בוקר טוב. {now.strftime('%d/%m')}"
    if heb:
        header += f" · {heb}"
    lines = [header]

    forecast = await weather.today_line()
    if forecast:
        lines.append(forecast)
    lines.append("")

    if events:
        lines.append("📅 היום:")
        ordered = sorted(events, key=lambda e: e["start"])
        prev_location = travel.HOME_ADDRESS   # היום מתחיל מהבית

        for ev in ordered:
            loc = f" — {ev['location']}" if ev.get("location") else ""

            # שורת נסיעה לפני כל אירוע פיזי עם מיקום:
            # מאיפה יוצאים (הבית או האירוע הפיזי הקודם), מתי, וכמה נסיעה.
            if travel.classify(ev) != "virtual" and (ev.get("location") or "").strip():
                dest = ev["location"].strip()
                est = await travel.travel_minutes(prev_location, dest)
                if est.known and est.minutes > 0:
                    leave = ev["start"] - timedelta(minutes=est.total_needed)
                    lines.append(
                        f"  🚗 {_fmt_time(leave)} יציאה ({est.minutes} דק' נסיעה)"
                    )
                prev_location = dest

            lines.append(f"  {_fmt_time(ev['start'])}  {ev['summary']}{loc}")
    else:
        lines.append("📅 היומן ריק היום.")

    # מאמן הכושר: שקט כשאתה בסדר, מציק בעדינות כשמצטבר פער
    try:
        from app import executors as _ex
        stats = await _ex.workout_stats(days=7)
        gap = stats.get("days_since_last")
        if gap is not None and gap >= 4:
            lines.append(f"\n💪 עברו {gap} ימים מהאימון האחרון — לשבץ אחד השבוע?")
        elif stats.get("count", 0) == 0:
            lines.append("\n💪 לא נרשמו אימונים השבוע — מתחילים היום?")
    except Exception as exc:
        logger.warning("סטטיסטיקת אימונים נכשלה: %s", exc)

    emails = await gmail_reader.unread_recent(limit=5)
    if emails:
        lines.append(f"\n📧 מחכים בתיבה ({len(emails)}):")
        for m in emails:
            lines.append(f"  • {m['from']} — {m['subject']}")

    warnings = await _tight_transitions(events)
    if warnings:
        lines.append("")
        lines.extend(warnings)

    prep = db.tasks_for_date(_deps.chat_id, now.strftime("%Y-%m-%d"))
    if prep:
        lines.append("\n🎒 לפני האירועים היום:")
        for t in prep:
            target = f" ({t['linked_event']})" if t.get("linked_event") else ""
            lines.append(f"  • {t['title']}{target}")

    prep_ids = {t["id"] for t in prep}
    tasks = [t for t in tasks if t["id"] not in prep_ids]

    if tasks:
        today_str = now.strftime("%Y-%m-%d")
        due_today = [t for t in tasks if t.get("soft_due") == today_str]
        rest = [t for t in tasks if t.get("soft_due") != today_str]

        # דדליינים מתקרבים: 1-3 ימים קדימה, עם ספירה לאחור.
        # מספיק רחוק כדי להספיק להתכונן, מספיק קרוב כדי לא להפוך לרעש.
        def _days_until(t):
            try:
                due = datetime.strptime(t["soft_due"], "%Y-%m-%d").date()
                return (due - now.date()).days
            except (TypeError, ValueError):
                return None

        approaching = sorted(
            [(d, t) for t in rest if (d := _days_until(t)) and 1 <= d <= 3],
            key=lambda pair: pair[0],
        )
        if approaching:
            lines.append("\n⏳ מתקרב:")
            for days, t in approaching:
                when = "מחר" if days == 1 else f"בעוד {days} ימים"
                lines.append(f"  • {t['title']} — {when}")
            rest = [t for t in rest if all(t is not p[1] for p in approaching)]

        if due_today:
            lines.append("\n📌 להיום:")
            for t in due_today:
                lines.append(f"  • {t['title']}")

        if rest:
            lines.append(f"\n📝 משימות פתוחות ({len(rest)}):")
            for t in rest[:5]:
                lines.append(f"  • {t['title']}")
            if len(rest) > 5:
                lines.append(f"  ...ועוד {len(rest) - 5}")

    await _deps.send(_deps.chat_id, "\n".join(lines))


# ------------------------------------------------------- סיכום ערב

async def evening_review() -> None:
    if _deps is None:
        return
    tomorrow = datetime.now(TZ) + timedelta(days=1)
    start, end = _day_bounds(tomorrow)
    events = await _deps.fetch_events(start, end)

    lines = ["ערב טוב. מה שמחכה מחר:\n"]

    if events:
        for ev in sorted(events, key=lambda e: e["start"]):
            loc = f" — {ev['location']}" if ev.get("location") else ""
            lines.append(f"  {_fmt_time(ev['start'])}  {ev['summary']}{loc}")
    else:
        lines.append("  היומן פנוי.")

    # מאמן הכושר: שקט כשאתה בסדר, מציק בעדינות כשמצטבר פער
    try:
        from app import executors as _ex
        stats = await _ex.workout_stats(days=7)
        gap = stats.get("days_since_last")
        if gap is not None and gap >= 4:
            lines.append(f"\n💪 עברו {gap} ימים מהאימון האחרון — לשבץ אחד השבוע?")
        elif stats.get("count", 0) == 0:
            lines.append("\n💪 לא נרשמו אימונים השבוע — מתחילים היום?")
    except Exception as exc:
        logger.warning("סטטיסטיקת אימונים נכשלה: %s", exc)

    emails = await gmail_reader.unread_recent(limit=5)
    if emails:
        lines.append(f"\n📧 מחכים בתיבה ({len(emails)}):")
        for m in emails:
            lines.append(f"  • {m['from']} — {m['subject']}")

    warnings = await _tight_transitions(events)
    if warnings:
        lines.append("")
        lines.extend(warnings)

    # התשובה שלך נקלטת דרך הכלי log_activity של המודל —
    # אין כאן מכונת מצבים נפרדת שצריך לתחזק.
    lines.append("\nומה הספקת היום?")

    await _deps.send(_deps.chat_id, "\n".join(lines))


# --------------------------------------------------- משימות תקועות

async def stuck_task_nudge() -> None:
    if _deps is None:
        return
    stuck = db.get_stuck_tasks(_deps.chat_id)
    if not stuck:
        return  # שקט זו תכונה, לא באג

    lines = [f"משימות שלא זזו כבר {db.STUCK_AFTER_DAYS} ימים:\n"]
    for t in stuck[:5]:
        lines.append(f"  • {t['title']}")
    lines.append("\nעדיין רלוונטי, או שנוריד מהרשימה?")

    await _deps.send(_deps.chat_id, "\n".join(lines))
    db.mark_nudged([t["id"] for t in stuck[:5]])


# ---------------------------------------------------------- אתחול

async def run_scheduled_prompt(chat_id: int, prompt: str) -> None:
    """
    הרצת הוראה מתוזמנת: ההוראה נכנסת לסוכן כאילו רון כתב אותה עכשיו,
    והתשובה נשלחת אליו. כך "תשלחי לי סיכום הוצאות" יכול להשתמש בכל
    הכלים — יומן, משימות, חיפוש — בלי קוד ייעודי לכל תרחיש.
    """
    from app import agent, policy
    if policy.in_quiet_hours():
        logger.info("הוראה מתוזמנת דולגה — שעות שקט")
        return
    try:
        reply = await agent.handle_message(chat_id, prompt)
        if reply and _deps:
            await _deps.send(chat_id, reply)
    except Exception as exc:
        logger.error("הוראה מתוזמנת נכשלה: %s", exc)


def register_prompt_job(row: dict) -> None:
    """רישום ג'וב חי עבור הוראה — בעלייה ובהוספה תוך כדי ריצה."""
    if _scheduler is None:
        return
    day = row["day_of_week"]
    trigger = {"hour": int(row["hour"]), "minute": int(row["minute"])}
    if day and day != "*":
        trigger["day_of_week"] = day
    _scheduler.add_job(
        run_scheduled_prompt, "cron", id=f"prompt_{row['id']}",
        args=[row["chat_id"], row["prompt"]],
        replace_existing=True, **trigger,
    )


def remove_prompt_job(schedule_id: int) -> None:
    if _scheduler is None:
        return
    try:
        _scheduler.remove_job(f"prompt_{schedule_id}")
    except Exception:
        pass


def start_scheduler(deps: Deps) -> AsyncIOScheduler:
    """נקרא מתוך lifespan ב-main.py, ליד הפעלת הבוט."""
    global _deps, _scheduler
    _deps = deps

    scheduler = AsyncIOScheduler(timezone=TZ)

    scheduler.add_job(
        morning_brief, CronTrigger(hour=MORNING_HOUR, minute=MORNING_MINUTE),
        id="morning_brief", coalesce=True, misfire_grace_time=3600,
        replace_existing=True,
    )
    scheduler.add_job(
        evening_review, CronTrigger(hour=EVENING_HOUR, minute=EVENING_MINUTE),
        id="evening_review", coalesce=True, misfire_grace_time=7200,
        replace_existing=True,
    )
    scheduler.add_job(
        stuck_task_nudge, CronTrigger(hour=NUDGE_HOUR, minute=NUDGE_MINUTE),
        id="stuck_tasks", coalesce=True, misfire_grace_time=10800,
        replace_existing=True,
    )

    scheduler.add_job(
        backup.daily_backup, CronTrigger(hour=BACKUP_HOUR, minute=BACKUP_MINUTE),
        id="daily_backup", coalesce=True, misfire_grace_time=21600,
        replace_existing=True,
    )

    scheduler.start()
    _scheduler = scheduler

    # הוראות מתוזמנות ששמורות במסד — נרשמות מחדש בכל עלייה
    for row in db.list_scheduled_prompts(deps.chat_id):
        register_prompt_job(row)
    logger.info("מתזמן הופעל — %s ג'ובים", len(scheduler.get_jobs()))
    return scheduler


def stop_scheduler() -> None:
    if _scheduler:
        _scheduler.shutdown(wait=False)
