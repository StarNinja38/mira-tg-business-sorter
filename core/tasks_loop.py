"""
Deferred task analysis — wait until a conversation settles (no new messages
for N minutes), then analyze the whole thread and produce ONE consolidated task.
Solves: one dialog about one topic = one task (not a task per message).
Also handles one-off messages without a reply (fires on inactivity, не требует ответа).
"""
import asyncio
from loguru import logger

SETTLE_MINUTES = 10
CHECK_INTERVAL = 180  # 3 min


async def run_task_settle_loop(bot, db, settings, anthropic_client) -> None:
    logger.info(f"Task settle loop started (settle={SETTLE_MINUTES}m, check every {CHECK_INTERVAL//60}m)")
    from db import (get_settled_pending, clear_pending_analysis, vault_get_context,
                    is_over_budget)
    from core.actions import (process_action_items, auto_close_tasks, auto_reopen_tasks,
                              time_settle_tasks, build_settle_context)
    from core.vault import get_profile_context

    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        # Гибрид-закрытие timed-задач по времени (детерминированно, независимо от затихания диалогов)
        try:
            await time_settle_tasks(bot, db, settings)
        except Exception as exc:
            logger.error(f"time_settle_tasks error: {exc}")
        try:
            settled = await get_settled_pending(db, settle_minutes=SETTLE_MINUTES)
            if not settled:
                continue
            over_budget = await is_over_budget(db, settings.anthropic_monthly_budget)
            # Бюджет исчерпан → НЕ снимаем dirty (иначе задачи из этого окна терялись бы навсегда,
            # как было до 23.06 при лимите 98%). Откладываем весь проход — разберётся после сброса.
            if over_budget:
                logger.warning(f"Settle: over budget — defer {len(settled)} pending thread(s) "
                               f"(dirty preserved, retry after budget resets)")
                continue
            for item in settled:
                uid = item["user_id"]
                uname = item["username"] or f"id{uid}"
                category = item["category"] or "UNCLEAR"
                try:
                    # Whole settled thread → one consolidated extraction
                    thread = await vault_get_context(db, uid, limit=25)
                    # Для ИЗВЛЕЧЕНИЯ новых задач берём только свежее окно (48ч), чтобы не цеплять
                    # старый, уже закрытый контекст (баг: задачи из переписки 3-дневной давности).
                    recent = await vault_get_context(db, uid, limit=25, since_hours=48) or thread
                    # First: reopen tasks if changes were requested ("переделай/правки")
                    await auto_reopen_tasks(
                        anthropic_client, settings.anthropic_model_smart, db,
                        settings.todoist_api_token, settings.notion_token,
                        uid, uname, recent, bot=bot, owner_id=settings.owner_user_id,
                        over_budget=over_budget,
                    )
                    # Then: auto-close any fulfilled tasks (встреча состоялась, файл отправлен)
                    await auto_close_tasks(
                        anthropic_client, settings.anthropic_model_smart, db,
                        settings.todoist_api_token, settings.notion_token,
                        uid, uname, recent, bot=bot, owner_id=settings.owner_user_id,
                        over_budget=over_budget,
                    )
                    # v4.33: для ИЗВЛЕЧЕНИЯ профиль — короткая справка (факты ≤600), а не вся память:
                    # репро @tutor_lena 06.09 — полный профиль (факты 2800 симв. после v4.32) ПЕРЕД
                    # перепиской давал 0 задач, любой из 4 усечённых вариантов — 2 задачи.
                    profile = await get_profile_context(db, uid, facts_limit=600, query=thread[-600:])   # v4.47: релевантные записи, не обрезка
                    # RAG (optional): pull semantically relevant past messages by meaning
                    rag_ctx = ""
                    if getattr(settings, "enable_rag", False):
                        try:
                            from core.embeddings import retrieve_relevant
                            rel = await retrieve_relevant(db, uid, thread[-500:], k=5)
                            if rel:
                                rag_ctx = "Релевантное из прошлой истории:\n" + "\n".join(f"- {r}" for r in rel)
                        except Exception:
                            pass
                    # v4.33: переписка — ПЕРВОЙ (это источник задач), профиль и RAG — после, как
                    # справка. Порядок «память → разговор» глушил извлечение (см. выше).
                    context_text = build_settle_context(recent, profile, rag_ctx)
                    created = await process_action_items(
                        client=anthropic_client, model=settings.anthropic_model_smart,
                        username=uname, user_id=uid, category=category,
                        text=context_text, todoist_token=settings.todoist_api_token,
                        notion_token=settings.notion_token,
                        notion_database_id=settings.notion_database_id,
                        db=db, bot=bot, owner_id=settings.owner_user_id,
                        request_cost_usd=0.0, monthly_budget=settings.anthropic_monthly_budget,
                        over_budget=over_budget, thread_text=recent,
                    )
                    if created:
                        logger.info(f"Settle: created/updated {created} task for @{uname}")
                    # Clear ONLY on success И только если за время разбора не пришло новое
                    # сообщение (last_activity == снимок на входе) — иначе новое сообщение не
                    # затирается и разберётся следующим проходом (без тихой потери задачи).
                    await clear_pending_analysis(db, uid, item.get("last_activity"))
                except Exception as item_exc:
                    logger.error(f"Settle item failed for @{uname} (will retry): {item_exc}")
        except Exception as exc:
            logger.error(f"Task settle loop error: {exc}")
