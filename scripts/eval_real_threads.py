#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Eval извлечения на НАСТОЯЩИХ переписках (v4.70). eval_extraction.py кормит голый короткий тред и
не видит вход бота: инверсии 28.09–30.10 рождались только на полном вводе (преамбула + память).
Здесь вход собирается тем же build_settle_context, на момент создания задачи. Корпус — id из
прод-БД, поэтому текста переписок в репо нет; читает БД только в режиме mode=ro.

Что не восстановить: память и RAG — текущие, а не тогдашние; список «владелец удалил» пустой,
чтобы итог не зависел от дня запуска; чужие задачи владельца в known не подмешиваются.
В корпус брать только задачи с topic≠«обещание»: те рождает reconcile_commitment_tasks из памяти,
извлечение их не создаёт. База 01.11 (5 прогонов): падают #396 5/5, #423 0/5, #462 1/5 («щас повезу» попадает под правило «в моменте») — не регресс.

Запуск на сервере:  cd /opt/tg-business-sorter && ./venv/bin/python scripts/eval_real_threads.py [runs]
Стоит денег (N×кейсов вызовов Sonnet на полном вводе) — гонять перед изменением извлечения.
"""
import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiosqlite
from anthropic import AsyncAnthropic
from config import settings
import core.actions as actions
from core.vault import get_profile_context
from db import get_contact_rule, vault_get_context

# (id задачи, ждём задачу?, регулярка по названию, пометка)
CASES = [
    # Инверсии: дело собеседника бот завёл на владельца
    (396, False, r"повер", "«Забери повер» — просьба владельца"),
    (513, False, r"кружо|в отел", "«напиши… кружок запиши»"),
    (516, False, r"показ", "«Завтра покажи»"),
    (363, False, r"звон", "«Только звони через телегу»"),
    (344, False, r"рассказ", "«Расскажешь потом»"),
    # Настоящие дела: фикс не должен их потерять
    (423, True, r"забрать|подъехать|встретить", "встреча, направление путано"),
    (517, True, r"ответ|написа|сообщ", "вопрос к владельцу"),
    (511, True, r"бат|ламб", "обещал поговорить"),
    (488, True, r"забрать", "обещал забрать в 20:50"),
    (472, True, r"такси", "заказать такси"),
    (462, True, r"отвез|повез", "обещал отвезти"),
    (452, True, r"кальян", "договорились на 18:00"),
    (398, True, r"отч[её]т|бки|выписк", "обещал скинуть"),
    (388, True, r"ответ|16400|деньг", "вопрос к владельцу"),
]


async def build_input(db, task_id):
    """Вход извлечения на момент создания задачи, как в settle-цикле."""
    async with db.execute("SELECT user_id, created_at FROM task_sync WHERE id=?", (task_id,)) as cur:
        uid, created_at = await cur.fetchone()
    thread = await vault_get_context(db, uid, limit=25, until=created_at)
    recent = await vault_get_context(db, uid, limit=25, since_hours=48, until=created_at) or thread
    profile = await get_profile_context(db, uid, facts_limit=600, query=thread[-600:])
    rag_ctx = ""
    if getattr(settings, "enable_rag", False):
        from core.embeddings import retrieve_relevant
        rel = await retrieve_relevant(db, uid, thread[-500:], k=5)
        if rel:
            rag_ctx = "Релевантное из прошлой истории:\n" + "\n".join(f"- {r}" for r in rel)
    text = actions.build_settle_context(recent, profile, rag_ctx)
    if await get_contact_rule(db, uid) == "task":
        text += actions.CONTACT_TASK_HINT
    known = []
    async with db.execute(
        "SELECT task_title, topic, closed_at FROM task_sync WHERE user_id=? AND id<? "
        "AND (closed_at IS NULL OR closed_at > datetime(?, '-7 days'))", (uid, task_id, created_at)) as cur:
        for title, topic, closed_at in await cur.fetchall():
            if closed_at and closed_at <= created_at:
                known.append(f"- {title} (недавно закрыта)")
            else:
                known.append(f"- {title}" + (f" [тема: {topic}]" if topic else "") + " (открыта)")
    async with db.execute("SELECT username, category FROM conversations WHERE user_id=? AND created_at<=? "
                          "AND direction='in' ORDER BY created_at DESC LIMIT 1", (uid, created_at)) as cur:
        row = await cur.fetchone()
    username, category = row if row else (f"id{uid}", "UNCLEAR")
    return dict(text=text, known="\n".join(known), username=username or f"id{uid}",
                category=category or "UNCLEAR", day=str(created_at)[:10])


async def run_case(client, data, pattern, runs):
    """Сколько прогонов из runs дали задачу с названием по pattern. Прогоны одного кейса идут
    разом: дата подменяется на кейс, поэтому сами кейсы — строго по очереди."""
    actions.today_local_iso = lambda: data["day"]
    results = await asyncio.gather(*[
        actions.extract_action_items(client, settings.anthropic_model_smart, data["username"], data["category"],
                                     data["text"], known_tasks=data["known"])
        for _ in range(runs)], return_exceptions=True)
    failed = sum(isinstance(r, Exception) for r in results)
    titles = [[t.get("title", "") for t in r] for r in results if not isinstance(r, Exception)]
    hits = sum(any(re.search(pattern, t, re.I) for t in ts) for ts in titles)
    return hits, failed, titles


async def run_all(db, client, runs):
    passed = 0
    report = []
    for task_id, expect, pattern, note in CASES:
        data = await build_input(db, task_id)
        hits, failed, titles = await run_case(client, data, pattern, runs)
        ok = (hits * 2 > runs) if expect else (hits * 2 < runs) and not failed
        passed += ok
        mark = "✅" if ok else "❌"
        want = "задача" if expect else "НЕ задача"
        print(f"{mark} #{task_id} {note}: ждём «{want}», совпало {hits}/{runs}"
              + (f", сбоев {failed}" if failed else ""), flush=True)
        report.append((task_id, expect, hits, titles))
    print(f"\nИТОГ: {passed}/{len(CASES)} кейсов прошли", flush=True)
    return passed == len(CASES), report


async def main():
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    db = await aiosqlite.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    db.row_factory = aiosqlite.Row
    try:
        ok, _ = await run_all(db, client, runs)
    finally:
        await db.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
