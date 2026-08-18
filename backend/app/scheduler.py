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
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app import backup
from app import weather
from app import db
from app import weather
from app import travel

logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Jerusalem")

MORNING_HOUR, MORNING_MINUTE = 7, 30
EVENING_HOUR, EVENING_MINUTE = 21, 0
NUDGE_HOUR, NUDGE_MINUTE = 11, 0
BACKUP_HOUR, BACKUP_MINUTE = 3, 30


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

    lines = [f"בוקר טוב. {now.strftime('%d/%m')}"]

    forecast = await weather.today()
    if forecast:
        lines.append(forecast)
    lines.append("")

    if events:
        lines.append("📅 היום:")
        for ev in sorted(events, key=lambda e: e["start"]):
            loc = f" — {ev['location']}" if ev.get("location") else ""
            lines.append(f"  {_fmt_time(ev['start'])}  {ev['summary']}{loc}")
    else:
        lines.append("📅 היומן ריק היום.")

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
    logger.info("מתזמן הופעל — %s ג'ובים", len(scheduler.get_jobs()))
    return scheduler


def stop_scheduler() -> None:
    if _scheduler:
        _scheduler.shutdown(wait=False)
