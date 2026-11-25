"""
Todoist → bot.db синхронизация закрытий (bot.db = источник истины, Todoist = проекция).
Notion-слой полностью удалён в v4.0 (ретайр v3.22-3.24, физическая чистка — Спринт 3 плана 07/2026).
"""
import asyncio
from typing import Optional

import aiohttp
from loguru import logger


async def check_todoist_task_completed(token: str, task_id: str) -> bool:
    """True if task is done (404 = completed and archived)."""
    url = f"https://api.todoist.com/api/v1/tasks/{task_id}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.get(url, headers={"Authorization": f"Bearer {token}"}) as resp:
                if resp.status == 404:
                    return True
                if resp.status == 200:
                    data = await resp.json()
                    return bool(data.get("checked", False))
                return False
    except Exception as exc:
        logger.debug(f"Todoist check failed for {task_id}: {exc}")
        return False


async def complete_todoist_task(token: str, task_id: str) -> bool:
    """Mark a Todoist task as complete."""
    url = f"https://api.todoist.com/api/v1/tasks/{task_id}/close"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.post(url, headers={"Authorization": f"Bearer {token}"}) as resp:
                if resp.status in (200, 204):
                    logger.debug(f"Todoist task {task_id} closed")
                    return True
                text = await resp.text()
                logger.error(f"Todoist close error {resp.status}: {text}")
                return False
    except Exception as exc:
        logger.error(f"Todoist close failed: {exc}")
        return False


async def run_sync_loop(db, todoist_token: Optional[str], notion_token: Optional[str] = None,
                        bot=None, owner_id=None) -> None:
    """Every 5 min: Todoist → bot.db (закрытия). Владелец закрыл задачу в приложении Todoist →
    закрываем и в bot.db (иначе time_settle/радар/дайджест вечно пинают закрытое).
    P0.3 аудита 05.07: старая петля требовала оба токена и оба id → с ретайром Notion была
    полностью отключена, закрытия из Todoist не доезжали никогда.
    notion_token — мёртвый параметр (совместимость вызова из main.py), игнорируется."""
    if not todoist_token:
        logger.info("Sync loop disabled (no Todoist token)")
        return

    logger.info("Todoist → bot.db sync loop started (interval: 5 min)")
    while True:
        await asyncio.sleep(300)
        try:
            from db import get_pending_syncs, mark_sync_done_by_id
            pending = await get_pending_syncs(db)
            if not pending:
                continue

            logger.debug(f"Sync check: {len(pending)} pending task(s)")
            for item in pending:
                todoist_id = item["todoist_id"]
                if not todoist_id:
                    continue
                if await check_todoist_task_completed(todoist_token, todoist_id):
                    await mark_sync_done_by_id(db, item["id"])
                    logger.info(f"Todoist→bot.db: задача {todoist_id} закрыта в приложении → закрыта локально")
                    # Событие в календаре больше не нужно — задача выполнена
                    try:
                        from core.actions import _delete_task_calendar_event
                        await _delete_task_calendar_event(db, {"id": item["id"]})
                    except Exception as cal_exc:
                        logger.debug(f"calendar cleanup skip for sync {item['id']}: {cal_exc}")

        except Exception as exc:
            logger.error(f"Sync loop error: {exc}")
            if bot and owner_id:
                from core.alerts import alert_owner
                await alert_owner(bot, db, owner_id, "apifail:sync",
                                  "⚠️ Синхронизация Todoist→bot.db даёт сбой. Проверь Todoist-токен.",
                                  cooldown_minutes=120)
