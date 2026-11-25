"""
v4.72 Подсказка недели: по понедельникам в утренней сводке одна возможность Миры.
Команды, которыми владелец пользовался за 30 дней, пропускаем (см. command_log), ноль LLM.
Проверка идеи: растёт ли число запросов к Мире; если нет, блок надо убрать.
"""
import json

TIPS = [
    ("routines", "«Каждое воскресенье в 20 спрашивай, как прошла неделя»: Мира напишет сама. Список: /routines", "routines"),
    ("slots", "«Когда я свободен на час в среду?»: Мира сверит календарь, пары и задачи.", ""),
    ("promises", "«Что я обещал Ване?»: Мира найдёт это в памяти и переписке.", ""),
    ("draft", "«Ответь Кристине, что буду в 7»: Мира подготовит черновик, отправишь кнопкой.", ""),
    ("forward", "Перешли Мире чужое сообщение: она объяснит, что человек имел в виду, и предложит ответ.", ""),
    ("about", "Перед разговором открой память человека: /about @ник", "about"),
    ("find", "Поиск по всей переписке: /find слово", "find"),
    ("subs", "«Сколько уходит на подписки?»: Мира покажет итог в месяц. Список: /subs", "subs"),
    ("social", "«Как прошла неделя на YouTube?»: Мира покажет статистику. Подробно: /social", "social"),
    ("birthday", "«У Вани др 12 мая»: Мира напомнит и подготовит поздравление.", ""),
    ("rule", "Если человек создаёт лишние задачи: /rule @ник never", "rule"),
]


def pick_tip(shown: list, used: set):
    """Следующая подсказка; круг заканчивается → новый. Чистая функция, (ключ, текст) или None."""
    fresh = [t for t in TIPS if not (t[2] and t[2] in used)]
    if not fresh:
        return None
    for key, text, _ in fresh:
        if key not in shown:
            return key, text
    return fresh[0][0], fresh[0][1]


async def weekly_tip(db, today) -> str:
    """Текст подсказки на сегодня или ''. Повторный вызов в тот же день вернёт ту же (откат дайджеста)."""
    from db import get_setting, set_setting
    if today.weekday() != 0:
        return ""
    state = json.loads(await get_setting(db, "tips_state") or "{}")
    if state.get("day") == today.isoformat():
        return state.get("text", "")
    async with db.execute("SELECT DISTINCT command FROM command_log WHERE created_at >= datetime('now', '-30 day')") as cur:
        used = {r[0] for r in await cur.fetchall()}
    tip = pick_tip(state.get("shown", []), used)
    if not tip:
        return ""
    shown = [k for k in state.get("shown", []) if k != tip[0]] + [tip[0]]
    if len(shown) >= len(TIPS):
        shown = [tip[0]]
    await set_setting(db, "tips_state", json.dumps({"day": today.isoformat(), "text": tip[1], "shown": shown}, ensure_ascii=False))
    return tip[1]
