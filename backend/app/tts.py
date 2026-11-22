"""
תשובות קוליות — ניקול עונה בקול כשמדברים אליה בקול.

edge-tts משתמש בקולות הנוירליים של מיקרוסופט: חינמי, בלי מפתח,
ועם קול עברי טבעי. הסינתזה מייצרת mp3; טלגרם דורש OGG/Opus כדי
שההודעה תוצג כהודעה קולית (עיגול עם גל), אז ממירים עם ffmpeg.
אם ffmpeg חסר — שולחים כקובץ שמע רגיל. אם הסינתזה נכשלת —
נשלח טקסט בלבד, כרגיל. הקול לעולם לא חוסם תשובה.
"""

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# קול עברי נשי — מתאים לניקול. אפשר להחליף ב-.env
VOICE = os.getenv("TTS_VOICE", "he-IL-HilaNeural")

# תקרה: תשובות ארוכות נשארות טקסט. אף אחד לא רוצה נאום של דקה.
MAX_CHARS = int(os.getenv("TTS_MAX_CHARS", "400"))

_FFMPEG = shutil.which("ffmpeg")

# קול טבעי יותר: Gemini TTS (AI Studio). פעיל כשמוגדר TTS_GEMINI_MODEL
# ויש GEMINI_API_KEY. כל כישלון (מכסה, מודל לא קיים) -> חזרה ל-edge-tts.
GEMINI_TTS_MODEL = os.getenv("TTS_GEMINI_MODEL", "")
GEMINI_TTS_VOICE = os.getenv("TTS_GEMINI_VOICE", "Kore")
GEMINI_TTS_STYLE = os.getenv(
    "TTS_GEMINI_STYLE",
    "דברי בעברית, בטון חם, טבעי ושיחתי, כמו עוזרת אישית שמדברת עם חבר:")


async def _gemini_tts(text: str) -> Optional[bytes]:
    """PCM 24kHz מ-Gemini -> OGG/Opus. None בכשל."""
    key = os.getenv("GEMINI_API_KEY", "")
    if not (GEMINI_TTS_MODEL and key and _FFMPEG):
        return None
    import base64
    import httpx
    body = {
        "contents": [{"parts": [{"text": f"{GEMINI_TTS_STYLE}\n{text}"}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {
                "prebuiltVoiceConfig": {"voiceName": GEMINI_TTS_VOICE}}},
        },
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{GEMINI_TTS_MODEL}:generateContent",
                headers={"x-goog-api-key": key}, json=body)
        if resp.status_code >= 400:
            logger.warning("Gemini TTS %s: %s", resp.status_code, resp.text[:300])
            return None
        part = resp.json()["candidates"][0]["content"]["parts"][0]
        pcm = base64.b64decode(part["inlineData"]["data"])
    except Exception as exc:
        logger.warning("Gemini TTS נכשל: %s", exc)
        return None
    proc = await asyncio.create_subprocess_exec(
        _FFMPEG, "-hide_banner", "-loglevel", "error",
        "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", "pipe:0",
        "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate(pcm)
    return out if proc.returncode == 0 and out else None


async def synthesize(text: str) -> tuple[Optional[bytes], str]:
    """
    (בייטים, סוג) — סוג הוא 'voice' (ogg/opus) או 'audio' (mp3).
    (None, '') בכל כישלון.
    """
    if not text or len(text) > MAX_CHARS:
        return None, ""

    natural = await _gemini_tts(text)
    if natural:
        return natural, "voice"

    try:
        import edge_tts
    except ImportError:
        logger.warning("edge-tts לא מותקן — אין תשובות קוליות")
        return None, ""

    tmp = Path(tempfile.mkdtemp())
    mp3 = tmp / "reply.mp3"
    try:
        await edge_tts.Communicate(text, VOICE).save(str(mp3))

        if _FFMPEG:
            ogg = tmp / "reply.ogg"
            proc = await asyncio.create_subprocess_exec(
                _FFMPEG, "-y", "-i", str(mp3),
                "-c:a", "libopus", "-b:a", "32k", str(ogg),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if proc.returncode == 0 and ogg.exists():
                return ogg.read_bytes(), "voice"

        return mp3.read_bytes(), "audio"

    except Exception as exc:
        logger.error("סינתזת קול נכשלה: %s", exc)
        return None, ""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
