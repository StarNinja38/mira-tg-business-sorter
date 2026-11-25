#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Eval судьи выполнения задач (v4.81). Судья на Haiku только ПРЕДЛАГАЕТ закрыть кнопкой, но раньше
проверить его было нечем: eval_extraction его не трогает, а отказ на обычном ответе (бабушка, 6/6)
нашёлся только руками. Реальные кейсы — id задачи и момент из прод-БД, поэтому текста переписок
в репо нет, БД только в режиме mode=ro. Вход собран как в settle-цикле: окно 48ч и все задачи,
открытые у контакта в тот момент. Синтетика ниже страхует то, чего в истории нет.

Запуск на сервере:  cd /opt/tg-business-sorter && ./venv/bin/python scripts/eval_done.py [runs]
Стоит копейки (N×кейсов вызовов Haiku) — гонять перед изменением DONE_*.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiosqlite
from anthropic import AsyncAnthropic
from config import settings
from core.actions import _check_tasks_done_batch
from db import vault_get_context

# (id задачи, момент, ждём «выполнена»?, пометка)
REAL = [
    # Владелец ответил по сути
    (514, "2026-10-31 16:48:30", True, "бабушка: как отдыхаете"),
    (495, "2026-10-26 12:37:00", True, "«Да есть» про деньги"),
    (476, "2026-10-24 09:43:00", True, "«Я поехал да»"),
    (446, "2026-10-19 09:14:00", True, "«Я уже щас буду выезжать»"),
    (433, "2026-10-14 15:49:00", True, "бабушка: что нового"),
    (412, "2026-10-08 19:45:00", True, "отказ тоже ответ"),
    (408, "2026-10-07 18:00:30", True, "«Вроде как нет»"),
    (367, "2026-09-30 21:00:00", True, "«на лифте поднимаюсь»"),
    (312, "2026-09-16 17:57:00", True, "«Да поеду»"),
    (265, "2026-09-11 07:10:00", True, "«наверное к линуксу»"),
    # Ответа ещё нет
    (514, "2026-10-30 09:00:00", False, "бабушка: ответа ещё нет"),
    (476, "2026-10-24 09:41:30", False, "вопрос без ответа"),
    (392, "2026-10-05 09:17:00", False, "вопрос без ответа"),
    # Написал, но не то
    (512, "2026-10-31 10:33:00", False, "встречный вопрос вместо ответа"),
    (447, "2026-10-19 10:37:00", False, "другая тема"),
    (486, "2026-10-25 13:24:00", False, "другое дело тому же человеку"),
]

# (задачи, переписка, номер проверяемой задачи, ждём «выполнена»?, пометка)
SYNTHETIC = [
    (["Ответить маме, когда буду дома"],
     "[2026-10-01] @x: ты когда домой?\n[2026-10-01] Ты: щас занят, отвечу позже", 0, False, "обещал ответить"),
    (["Ответить @x, во сколько завтра встреча"],
     "[2026-10-01] @x: во сколько завтра?\n[2026-10-01] Ты: хорошо", 0, False, "«хорошо» на вопрос"),
    (["Ответить @x про подарок и про поездку"],
     "[2026-10-01] @x: что дарим и едем ли в субботу?\n[2026-10-01] Ты: дарим сертификат", 0, False,
     "ответил на одну часть из двух"),
    (["Написать Саше адрес склада"],
     "[2026-10-01] @nika: ты где\n[2026-10-01] Ты: на работе", 0, False, "ответил не тому человеку"),
    (["Скинуть договор до пятницы"],
     "[2026-10-01] @x: скинь договор\n[2026-10-01] Ты: да скину вечером", 0, False, "обещание скинуть"),
    (["Скинуть договор до пятницы"],
     "[2026-10-01] @x: скинь договор\n[2026-10-01] Ты: [document]\n[2026-10-01] Ты: вот договор", 0, True,
     "файл отправлен"),
    (["Отвезти папу к врачу"],
     "[2026-10-01] @x: ты где\n[2026-10-01] Ты: приехал домой", 0, False, "приехал сам"),
    (["Спросить у Димы, где ключ"],
     "[2026-10-01] @dima: привет\n[2026-10-01] Ты: привет как дела", 0, False, "смолток вместо вопроса"),
]


async def real_input(db, task_id, moment):
    """Задачи и переписка на момент, как в settle-цикле."""
    async with db.execute("SELECT user_id FROM task_sync WHERE id=?", (task_id,)) as cur:
        uid = (await cur.fetchone())[0]
    async with db.execute(
        "SELECT id, task_title, topic FROM task_sync WHERE user_id=? AND (id=? OR (created_at<=? "
        "AND (closed_at IS NULL OR replace(closed_at,'T',' ')>?))) ORDER BY id", (uid, task_id, moment, moment)) as cur:
        tasks = [dict(id=r[0], title=r[1], topic=r[2]) for r in await cur.fetchall()]
    conversation = await vault_get_context(db, uid, limit=25, since_hours=48, until=moment)
    conversation = conversation or await vault_get_context(db, uid, limit=25, until=moment)
    return tasks, conversation, next(i for i, t in enumerate(tasks) if t["id"] == task_id)


async def judge(client, tasks, conversation, target, runs):
    done = await asyncio.gather(*[
        _check_tasks_done_batch(client, settings.anthropic_model, tasks, conversation) for _ in range(runs)])
    return sum(target in d for d in done)


async def main():
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    db = await aiosqlite.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    db.row_factory = aiosqlite.Row
    cases = []
    try:
        for task_id, moment, expect, note in REAL:
            tasks, conversation, target = await real_input(db, task_id, moment)
            cases.append((f"#{task_id} {note}", tasks, conversation, target, expect))
    finally:
        await db.close()
    for titles, conversation, target, expect, note in SYNTHETIC:
        cases.append((f"синт. {note}", [dict(title=t) for t in titles], conversation, target, expect))
    passed = 0
    for name, tasks, conversation, target, expect in cases:
        hits = await judge(client, tasks, conversation, target, runs)
        ok = (hits * 2 > runs) == expect
        passed += ok
        want = "выполнена" if expect else "открыта"
        print(f"{'✅' if ok else '❌'} {name}: ждём «{want}», выполнена {hits}/{runs}", flush=True)
    print(f"\nИТОГ: {passed}/{len(cases)} кейсов прошли", flush=True)
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
