"""
גיבוי בסיס הנתונים.

שתי החלטות שחשובות יותר משנראה:

1. **לא מעתיקים את הקובץ.** cp על sqlite שרץ יכול לתפוס אותו באמצע
   טרנזקציה ולייצר גיבוי פגום — שתגלה בדיוק ביום שתצטרך אותו.
   כאן משתמשים ב-Online Backup API של sqlite, שמייצר עותק עקבי
   גם כשהבוט כותב באותו רגע.

2. **הגיבוי יוצא מהשרת.** גיבוי שיושב על אותו droplet שנפל הוא לא
   גיבוי. שולחים אותו לטלגרם — הקובץ קטן, אין צורך בהרשאות חדשות,
   ואין scope נוסף ב-OAuth של גוגל.

למה לא Google Drive: ה-OAuth הקיים מבקש scope של יומן בלבד. הוספת
Drive מחייבת אישור מחדש של החשבון. אפשר, אבל זה מחיר גבוה לפיצ'ר
שטלגרם פותר בחינם.
"""

import gzip
import logging
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

from app import db

logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Jerusalem")

BACKUP_DIR = Path(os.getenv("BACKUP_DIR", Path(__file__).parent / "backups"))
KEEP_LOCAL = int(os.getenv("BACKUP_KEEP", "14"))

TELEGRAM_API_TOKEN = None      # מוזרק מ-main
BACKUP_CHAT_ID = os.getenv("BACKUP_CHAT_ID") or os.getenv("OWNER_CHAT_ID")

# מעל זה טלגרם דוחה. גם התרעה שמשהו לא בסדר בגודל ה-DB.
MAX_UPLOAD_BYTES = 45 * 1024 * 1024


def create_snapshot() -> Optional[Path]:
    """יוצר עותק עקבי ודחוס. מחזיר את הנתיב, או None בכישלון."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    raw = BACKUP_DIR / f"assistant-{stamp}.db"
    packed = raw.with_suffix(".db.gz")

    try:
        source = sqlite3.connect(db.DB_PATH)
        target = sqlite3.connect(raw)
        with target:
            source.backup(target)       # ה-API שמבטיח עותק עקבי
        target.close()
        source.close()

        with open(raw, "rb") as f_in, gzip.open(packed, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        raw.unlink()

        logger.info("גיבוי נוצר: %s (%s בייטים)", packed.name,
                    packed.stat().st_size)
        return packed

    except Exception as exc:
        logger.error("יצירת גיבוי נכשלה: %s", exc)
        raw.unlink(missing_ok=True)
        return None


def rotate() -> int:
    """משאיר את KEEP_LOCAL האחרונים. מחזיר כמה נמחקו."""
    if not BACKUP_DIR.exists():
        return 0
    files = sorted(BACKUP_DIR.glob("assistant-*.db.gz"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for old in files[KEEP_LOCAL:]:
        try:
            old.unlink()
            removed += 1
        except OSError as exc:
            logger.warning("מחיקת גיבוי ישן נכשלה: %s", exc)
    return removed


async def upload(path: Path) -> bool:
    """שולח את הגיבוי לטלגרם — עותק מחוץ לשרת."""
    if not TELEGRAM_API_TOKEN or not BACKUP_CHAT_ID:
        logger.warning("אין טוקן או chat_id לגיבוי — נשמר מקומית בלבד")
        return False

    size = path.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        logger.error("הגיבוי גדול מדי לשליחה: %s בייטים", size)
        return False

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            with open(path, "rb") as fh:
                resp = await client.post(
                    f"https://api.telegram.org/bot{TELEGRAM_API_TOKEN}/sendDocument",
                    data={
                        "chat_id": str(BACKUP_CHAT_ID),
                        "caption": f"גיבוי {datetime.now(TZ):%d/%m/%Y}",
                        "disable_notification": "true",
                    },
                    files={"document": (path.name, fh, "application/gzip")},
                )
        ok = resp.json().get("ok", False)
        if not ok:
            logger.error("שליחת גיבוי נכשלה: %s", resp.text)
        return ok

    except Exception as exc:
        logger.error("שליחת גיבוי נכשלה: %s", exc)
        return False


async def daily_backup() -> None:
    """ג'וב מתוזמן. שקט בהצלחה, רועש בכישלון."""
    snapshot = create_snapshot()
    if snapshot is None:
        return
    await upload(snapshot)
    removed = rotate()
    if removed:
        logger.info("נמחקו %s גיבויים ישנים", removed)


def latest() -> Optional[Path]:
    """הגיבוי האחרון — לשחזור."""
    if not BACKUP_DIR.exists():
        return None
    files = sorted(BACKUP_DIR.glob("assistant-*.db.gz"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def restore(archive: Path, target: Path = None) -> bool:
    """
    שחזור. עוצרים את השירות, מריצים, מפעילים מחדש:
        python -c "import backup,pathlib; backup.restore(pathlib.Path('backups/assistant-....db.gz'))"
    """
    target = target or Path(db.DB_PATH)
    try:
        if target.exists():
            target.rename(target.with_suffix(".db.before-restore"))
        with gzip.open(archive, "rb") as f_in, open(target, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        logger.info("שוחזר מ-%s", archive.name)
        return True
    except Exception as exc:
        logger.error("שחזור נכשל: %s", exc)
        return False
