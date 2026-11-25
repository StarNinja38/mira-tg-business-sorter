"""
v4.94 Журнал модерации для экрана «Модерация» в приложении: кто что сделал в Telegram и Twitch.

platform: telegram | twitch. actor: mira (сама) | owner (кнопкой, командой или из приложения).
action: card (карточка на проверку), delete, ban, timeout, unban, keep, mode, say, reply, alert.
Запись best-effort: сбой журнала не ломает модерацию. Храним 30 дней.
"""
import time

from loguru import logger

KEEP_DAYS = 30
_ready = False


async def _table(db) -> None:
    global _ready
    if not _ready:
        await db.execute("CREATE TABLE IF NOT EXISTS mod_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, platform TEXT, "
                         "action TEXT, actor TEXT, target TEXT, text TEXT)")
        _ready = True


async def add(db, platform: str, action: str, actor: str, target: str = "", text: str = "") -> None:
    try:
        await _table(db)
        now = time.time()
        await db.execute("INSERT INTO mod_log (ts, platform, action, actor, target, text) VALUES (?,?,?,?,?,?)",
                         (now, platform, action, actor, (target or "")[:80], (text or "")[:300]))
        await db.execute("DELETE FROM mod_log WHERE ts < ?", (now - KEEP_DAYS * 86400,))
        await db.commit()
    except Exception as exc:
        logger.debug(f"modlog skip: {exc}")


async def recent(db, limit: int = 30) -> list:
    await _table(db)
    async with db.execute("SELECT ts, platform, action, actor, target, text FROM mod_log ORDER BY id DESC LIMIT ?", (limit,)) as cur:
        return [{"ts": r[0], "platform": r[1], "action": r[2], "actor": r[3], "target": r[4], "text": r[5]} for r in await cur.fetchall()]


async def today(db, since: float) -> dict:
    """Счётчики с полуночи: {action: число}."""
    await _table(db)
    async with db.execute("SELECT action, COUNT(*) FROM mod_log WHERE ts >= ? GROUP BY action", (since,)) as cur:
        return {r[0]: r[1] for r in await cur.fetchall()}
