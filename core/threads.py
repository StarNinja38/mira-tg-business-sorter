"""
v4.76 Треды Миры: отдельные темы в чате с ботом («💼 Работа», «🎓 Учёба»).

Раздельный только разговор за сутки. Память, задачи, календарь и ночная сводка общие для всех тредов,
поэтому факт из «Работы» Мира помнит и в «Учёбе». Служебные темы бота тредами не считаются:
там всё идёт как раньше, в общий разговор.
"""
from loguru import logger

MAX_THREADS = 20


async def _ensure(db) -> None:
    await db.execute("""CREATE TABLE IF NOT EXISTS mira_threads (
        thread_id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        focus TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    await db.commit()


async def rows(db) -> list:
    await _ensure(db)
    async with db.execute("SELECT thread_id, name, focus FROM mira_threads ORDER BY created_at") as cur:
        return [tuple(r) for r in await cur.fetchall()]


async def key_of(db, message_thread_id) -> int:
    """Номер разговора: тред Миры или 0 для общего чата и служебных тем."""
    if not message_thread_id:
        return 0
    await _ensure(db)
    async with db.execute("SELECT 1 FROM mira_threads WHERE thread_id=?", (message_thread_id,)) as cur:
        return message_thread_id if await cur.fetchone() else 0


async def get(db, thread: int):
    await _ensure(db)
    async with db.execute("SELECT name, focus FROM mira_threads WHERE thread_id=?", (thread,)) as cur:
        return await cur.fetchone()


async def _alive(bot, owner_id: int, thread: int, name: str) -> bool:
    """Тред могли удалить руками в Telegram: тогда запись устарела."""
    try:
        await bot.send_message(owner_id, f"Этот тред уже есть, пиши сюда 👋", message_thread_id=thread, parse_mode=None)
        return True
    except Exception as exc:
        logger.info(f"thread {name!r} is gone: {type(exc).__name__}: {exc}")
        return False


async def create(bot, db, owner_id: int, name: str, focus: str = "") -> str:
    """Создать тред; вернуть текст для модели. ValueError = что поправить."""
    name, focus = (name or "").strip()[:60], (focus or "").strip()[:200]
    if not name:
        raise ValueError("не хватает названия")
    known = await rows(db)
    same = next((r for r in known if r[1].lower() == name.lower()), None)
    if same:
        if await _alive(bot, owner_id, same[0], name):
            return f"тред {name} уже есть"
        await db.execute("DELETE FROM mira_threads WHERE thread_id=?", (same[0],))
        await db.commit()
    elif len(known) >= MAX_THREADS:
        raise ValueError(f"уже {MAX_THREADS} тредов, сначала удали лишние в Telegram")
    topic = await bot.create_forum_topic(chat_id=owner_id, name=name)
    await db.execute("INSERT INTO mira_threads (thread_id, name, focus) VALUES (?,?,?)", (topic.message_thread_id, name, focus))
    await db.commit()
    hello = focus or "Здесь говорим только об этом"
    await bot.send_message(owner_id, f"{hello} 🙌\nВсё остальное я тоже помню", message_thread_id=topic.message_thread_id, parse_mode=None)
    logger.info(f"Mira thread created: {name!r} ({topic.message_thread_id})")
    return f"создала тред {name}"


async def list_text(db) -> str:
    known = await rows(db)
    return "\n".join(f"{r[1]}" + (f": {r[2]}" if r[2] else "") for r in known) or "тредов пока нет"
