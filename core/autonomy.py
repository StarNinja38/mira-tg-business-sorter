"""
v4.79 Настройки приложения: что Мира присылает сама и что она запомнила.

Выключатель останавливает только сообщения по расписанию. Разбор переписок, задачи и памяти работают всегда,
это основа бота. По умолчанию всё включено, в settings_kv лежит только «0» у выключенного.
"""
import json
import re

from db import get_setting, set_setting

SWITCHES = [
    {"id": "digest", "name": "Утренняя сводка", "sub": "Задачи, пары и встречи в 09:00"},
    {"id": "pulse", "name": "Дневное напоминание", "sub": "В 14:00, только если есть повод"},
    {"id": "evening", "name": "Вечерний итог", "sub": "Что сделано и что на завтра"},
    {"id": "routines", "name": "Регулярные сообщения", "sub": "Напоминания и скиллы по расписанию"},
    {"id": "briefs", "name": "Справки перед встречами", "sub": "За час до встречи"},
    {"id": "board", "name": "Вопросы по контент-доске", "sub": "После стрима и про клипы"},
    {"id": "groups", "name": "Сводки групп", "sub": "Каждый день в 21:00"},
    {"id": "channel", "group": "mod", "name": "Комментарий к посту", "sub": "Черновик к новому посту в канале"},
    {"id": "moderation", "group": "mod", "name": "Модерация комментариев", "sub": "Карточка на спам, рекламу и грубость в обсуждении канала"},
    {"id": "modauto", "group": "mod", "name": "Сразу удалять явный спам", "sub": "Приглашения и заработок со ссылкой, потом отчёт тебе"},
    {"id": "twreply", "group": "mod", "name": "Ответы в чатах стримов", "sub": "Отвечаю зрителям Twitch и YouTube, когда меня упоминают"},
    {"id": "twwatch", "group": "mod", "name": "Тревога о спаме в чатах", "sub": "Пишу тебе, если в чате Twitch или YouTube спам-атака"},
    {"id": "twsummary", "group": "mod", "name": "Сводка чатов после стрима", "sub": "Twitch и YouTube: о чём говорили и вопросы без ответа"},
    {"id": "announce", "group": "mod", "name": "Анонс стрима", "sub": "Когда начался стрим, присылаю анонс для канала, публикуешь кнопкой"},
    {"id": "streamreport", "group": "mod", "name": "Итог стрима", "sub": "Зрители, фолловеры и чаты Twitch и YouTube одним сообщением"},
    {"id": "greet", "group": "mod", "name": "Приветствие новичков", "sub": "Привет и ссылки в чате канала, через 10 минут удаляю"},
    {"id": "postlinks", "group": "mod", "name": "Ссылки под постом", "sub": "Первый комментарий со всеми ссылками под новым постом"},
]
_IDS = {s["id"] for s in SWITCHES}
MEMORY_LIMIT = 40


async def enabled(db, switch: str) -> bool:
    return await get_setting(db, f"auto:{switch}") != "0"


async def state(db) -> list:
    return [{**s, "on": await enabled(db, s["id"])} for s in SWITCHES]


async def toggle(db, switch: str, on: bool) -> list:
    if switch not in _IDS:
        raise ValueError("нет такого переключателя")
    await set_setting(db, f"auto:{switch}", "1" if on else "0")
    return await state(db)


def remembered(facts_json: str | None) -> list:
    """Факты из разговоров с Мирой, новые сверху. Чистая функция."""
    if not facts_json:
        return []
    try:
        other = (json.loads(facts_json).get("sections") or {}).get("other") or ""
    except (ValueError, AttributeError):
        return []
    found = re.findall(r"\[(\d{2}\.\d{2})\]\s*([^;]+)", other)
    return [{"d": day, "t": text.strip()} for day, text in reversed(found)][:MEMORY_LIMIT]


async def memory(db, settings) -> list:
    async with db.execute("SELECT facts_json FROM contact_profiles WHERE user_id=?", (settings.owner_user_id,)) as cur:
        row = await cur.fetchone()
    return remembered(row[0] if row else None)


async def forget(db, settings, fact: str) -> list:
    from core.vault import forget_fact
    await forget_fact(db, settings.owner_user_id, fact)
    return await memory(db, settings)
