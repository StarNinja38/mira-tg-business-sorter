"""
Local voice transcription via faster-whisper (free, CPU, no API).
v4.58: модель живёт в отдельном процессе на время одной расшифровки (см. core/transcribe_worker.py).

v4.50: 'base' давал мусор по-русски («Какие у нас с тобой дела» → «Кир нас с тобой делал»; «закрыть задачу про Нику»
→ «кризоначу про Никол») → модель `small` (int8, ~0.5 ГБ), beam_size=5, VAD, и словарь-подсказка
(initial_prompt) с именами контактов и терминами бота — Whisper подхватывает написание имён.
"""
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from loguru import logger

# v4.58: одна расшифровка за раз, в отдельном процессе (см. core/transcribe_worker.py)
_sem = asyncio.Semaphore(1)
_TMP_PREFIX = "tgs_voice_"
_orphans_cleaned = False
_vocab: list[str] = []
_vocab_ts = 0.0
_BASE_TERMS = ["Мира", "Миша", "задача", "закрой задачу", "перенеси", "напомни", "запиши", "собеседование",
               "Альфа-Банк", "Т-Банк", "Todoist", "МИРЭА", "пары", "расписание", "память", "Терафлю"]


def _cleanup_orphans():
    """Временные файлы, оставшиеся от убитых расшифровок (finally не отрабатывает при kill)."""
    global _orphans_cleaned
    if _orphans_cleaned:
        return
    _orphans_cleaned = True
    try:
        now = time.time()
        for f in Path(tempfile.gettempdir()).glob(_TMP_PREFIX + "*"):
            if now - f.stat().st_mtime > 3600:
                f.unlink(missing_ok=True)
    except Exception:
        pass


async def _run_worker(path: str, prompt: str, timeout: float) -> Optional[dict]:
    from config import settings
    root = str(Path(__file__).resolve().parent.parent)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "core.transcribe_worker", path, cwd=root,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    cfg = json.dumps({"model": getattr(settings, "whisper_model", "small"), "prompt": prompt}).encode()
    try:
        out, err = await asyncio.wait_for(proc.communicate(cfg), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.error(f"Voice transcription timed out after {timeout:.0f}s — worker killed")
        return None
    if proc.returncode != 0:
        logger.error(f"Voice worker failed rc={proc.returncode}: {(err or b'').decode(errors='replace')[-300:]}")
        return None
    try:
        return json.loads(out.decode())
    except Exception as exc:
        logger.error(f"Voice worker bad output: {exc}")
        return None


async def _load_vocab(db) -> list[str]:
    """Имена контактов (раз в час) — Whisper пишет их правильно, а не «Никол»/«Кир»."""
    global _vocab, _vocab_ts
    if db is None or (time.time() - _vocab_ts < 3600 and _vocab):
        return _vocab
    try:
        async with db.execute("SELECT name FROM contact_profiles WHERE COALESCE(name,'') != '' "
                              "ORDER BY message_count DESC LIMIT 40") as cur:
            names = [str(r[0]).split()[0] for r in await cur.fetchall() if str(r[0]).strip()]
        _vocab = list(dict.fromkeys(names))
        _vocab_ts = time.time()
    except Exception as exc:
        logger.debug(f"whisper vocab skip: {exc}")
    return _vocab


async def transcribe_voice(bot, file_id: str, db=None) -> Optional[str]:
    """Download a voice/audio file from Telegram and transcribe to text."""
    tmp_path = None
    try:
        file_info = await bot.get_file(file_id)
        if getattr(file_info, "file_size", 0) and file_info.file_size > 25 * 1024 * 1024:
            logger.warning(f"Voice file too large ({file_info.file_size} bytes) — skip transcription")
            return None
        file_bytes = await bot.download_file(file_info.file_path)
        ext = Path(file_info.file_path).suffix or ".ogg"
        with tempfile.NamedTemporaryFile(prefix=_TMP_PREFIX, suffix=ext, delete=False) as tmp:
            tmp.write(file_bytes.read())
            tmp_path = tmp.name

        vocab = await _load_vocab(db)
        prompt = ", ".join(_BASE_TERMS + vocab)[:600]
        _cleanup_orphans()
        t_wait = time.monotonic()
        async with _sem:
            t0 = time.monotonic()
            res = await _run_worker(tmp_path, prompt, timeout=900)
        text = (res or {}).get("text")
        if t0 - t_wait > 60:
            logger.warning(f"Voice waited {t0 - t_wait:.0f}s in queue")
        text = (text or "").strip()
        if text:
            logger.info(f"Transcribed voice ({len(text)} chars, {time.monotonic() - t0:.1f}s): {text[:80]}")
        return text or None
    except Exception as exc:
        logger.error(f"Voice transcription failed: {exc}")
        return None
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
