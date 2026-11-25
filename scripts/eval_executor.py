#!/usr/bin/env python3
"""
Eval проверки «кто делает» (v4.74) на настоящих переписках прод-БД, только чтение. Текста переписок в репо нет.
BAD — дела собеседника, заведённые на владельца; GOOD — настоящие дела; широкий корпус показывает, что отсеялось бы.
База 01.11, Sonnet: 13/15 (промах #363, ложный other на встрече #423), широкий корпус 2 из 39.
Запуск: cd /opt/tg-business-sorter && ./venv/bin/python scripts/eval_executor.py   Стоит денег: около 170 вызовов.
"""
import asyncio, collections, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import aiosqlite
from anthropic import AsyncAnthropic
from config import settings
from core import llm
from core.actions import EXECUTOR_SYSTEM as SYSTEM
from core.util import loads_loose
from db import vault_get_context
BAD = [396, 513, 516, 363, 344, 520]
GOOD = [423, 517, 511, 488, 472, 462, 452, 398, 388]
RUNS = 5
async def ask(client, model, thread, title):
    r = await llm.create(client, model=model, max_tokens=1500, system=SYSTEM, messages=[{"role": "user", "content": f"Переписка:\n{thread}\n\nЗадача: {title}"}], output_config={"effort": "low"})
    try:
        return loads_loose(llm.text_of(r)).get("who", "?")
    except Exception:
        return "?"
async def case(db, client, model, tid, runs):
    async with db.execute("SELECT user_id, created_at, task_title FROM task_sync WHERE id=?", (tid,)) as cur:
        uid, created, title = await cur.fetchone()
    th = await vault_get_context(db, uid, limit=25, since_hours=48, until=created) or await vault_get_context(db, uid, limit=25, until=created)
    res = await asyncio.gather(*[ask(client, model, th, title) for _ in range(runs)], return_exceptions=True)
    return title, collections.Counter(x if isinstance(x, str) else "err" for x in res)
async def main():
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    db = await aiosqlite.connect(f"file:{settings.db_path}?mode=ro", uri=True); db.row_factory = aiosqlite.Row
    try:
        for model in (settings.anthropic_model_smart,):
            print("==", model, flush=True)
            ok = 0
            for tid in BAD:
                t, c = await case(db, client, model, tid, RUNS); g = c["other"] > RUNS / 2; ok += g
                print("✅" if g else "❌", "ИНВЕРСИЯ", tid, t[:50], dict(c), flush=True)
            for tid in GOOD:
                t, c = await case(db, client, model, tid, RUNS); g = c["other"] < RUNS / 2; ok += g
                print("✅" if g else "❌", "НАСТОЯЩАЯ", tid, t[:50], dict(c), flush=True)
            print("ИТОГ", ok, "из", len(BAD) + len(GOOD), flush=True)
            async with db.execute("SELECT id FROM task_sync WHERE COALESCE(topic,'')!='обещание' AND id>=430 AND id<520 ORDER BY id DESC LIMIT 45") as cur:
                ids = [r[0] for r in await cur.fetchall() if r[0] not in BAD + GOOD]
            drop = 0
            for tid in ids:
                t, c = await case(db, client, model, tid, 3)
                if c["other"] >= 2:
                    drop += 1; print("  ОТСЕВ", tid, t[:70], dict(c), flush=True)
            print("широкий корпус: отсеяно", drop, "из", len(ids), flush=True)
    finally:
        await db.close()
asyncio.run(main())
