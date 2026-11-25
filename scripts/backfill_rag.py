#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бэкфилл RAG-индекса: эмбеддит всю существующую историю переписки в message_vectors,
чтобы семантическая память работала сразу (а не копилась с нуля). Локально, бесплатно.

Запуск на сервере (с кэш-окружением бота):
  cd /opt/tg-business-sorter && sudo -u tgsorter env HOME=/opt/tg-business-sorter \
    HF_HOME=/opt/tg-business-sorter/.cache/huggingface XDG_CACHE_HOME=/opt/tg-business-sorter/.cache \
    ./venv/bin/python scripts/backfill_rag.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiosqlite
from config import settings
from core.embeddings import index_message


async def main():
    conn = await aiosqlite.connect(settings.db_path)
    await conn.execute("PRAGMA busy_timeout=5000")
    # чистим прошлый индекс, чтобы не задвоить при повторном прогоне
    await conn.execute("DELETE FROM message_vectors")
    await conn.commit()

    rows = await (await conn.execute(
        "SELECT user_id, text FROM conversations "
        "WHERE text IS NOT NULL AND length(trim(text)) >= 12 ORDER BY created_at"
    )).fetchall()
    print(f"Бэкфилл RAG: {len(rows)} сообщений…")

    n = 0
    for uid, text in rows:
        try:
            await index_message(conn, uid, text)
            n += 1
            if n % 100 == 0:
                print(f"  …{n}")
        except Exception as e:
            print(f"  skip uid={uid}: {e}")
    await conn.close()
    print(f"Готово — проиндексировано {n} сообщений.")


if __name__ == "__main__":
    asyncio.run(main())
