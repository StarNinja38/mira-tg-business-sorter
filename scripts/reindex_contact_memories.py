"""
v4.47: пересобрать локальный индекс записей ВСЕХ память (contact_memory_vectors) — один раз после деплоя
и при смене модели эмбеддингов. Запуск: cd /opt/tg-business-sorter && ./venv/bin/python scripts/reindex_contact_memories.py
"""
import asyncio
import sys

sys.path.insert(0, "/opt/tg-business-sorter")
sys.path.insert(0, ".")


async def main():
    import aiosqlite
    from config import settings
    from core import contact_memory
    db = await aiosqlite.connect(settings.db_path)
    async with db.execute("SELECT user_id, username, facts_json FROM contact_profiles WHERE facts_json IS NOT NULL AND facts_json != ''") as cur:
        rows = await cur.fetchall()
    total = 0
    for uid, un, fj in rows:
        n = await contact_memory.reindex(db, uid, fj)
        total += n
        print(f"@{un or uid}: {n} записей")
    print(f"ИТОГО: {len(rows)} память, {total} записей")
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
