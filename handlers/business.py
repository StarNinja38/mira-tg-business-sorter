import re
import html
import base64
import asyncio
import aiosqlite
from anthropic import AsyncAnthropic
from aiogram import Bot, Router
from aiogram.types import Message, MessageOriginChannel
from loguru import logger
from typing import Optional

from config import Settings
from core import classifier, forwarder, responder, whitelist
from core.actions import process_action_items, _check_update_existing, update_todoist_task
from core.util import profile_link, contact_label, has_username
from core.vault import enrich_profile, get_profile_context
from core.usage import record as _rec
from core import llm
from core.llm import text_of as _llm_text   # v4.61: читать ответ по типу блока (Claude 5 может начать с thinking)
from db import (get_message_count, get_mode, increment_message_count, log_message,
               get_open_tasks_for_user, mark_sync_done, get_recent_context,
               vault_add, vault_get_context, can_notify, log_notification,
               upsert_contact_profile, is_over_budget)
async def upsert_contact_profile_count(db, user_id, username, display_name):
    """Helper to increment message count in profile."""
    await upsert_contact_profile(db, user_id, username, display_name)


def _bg_done(task) -> None:
    """done-callback для fire-and-forget create_task: падение не глотается молча."""
    try:
        exc = task.exception()
    except Exception:
        return
    if exc:
        logger.error(f"Background task failed: {exc!r}")


async def _analyze_photo(client: AsyncAnthropic, model: str, bot: Bot, message: Message, username: str) -> Optional[str]:
    """Download photo and analyze with Claude vision."""
    try:
        # C17: Anthropic принимает ≤5 МБ на изображение (base64 +33%) — берём самый большой размер ≤3 МБ
        sizes = [p for p in message.photo if not p.file_size or p.file_size <= 3_000_000] or message.photo[:1]
        photo = sizes[-1]
        file_info = await bot.get_file(photo.file_id)
        file_bytes = await bot.download_file(file_info.file_path)
        raw = file_bytes.read()
        if len(raw) > 3_500_000:
            logger.warning(f"Photo from @{username} too large for vision ({len(raw)} B) — skipped")
            return None
        img_b64 = base64.b64encode(raw).decode()
        # v4.85: через llm.create. Прямой вызов шёл с мышлением Haiku 5.5, оно съедало лимит 400,
        # и описание обрывалось раньше дат: у брони картинга 01.11 от текста осталось 149 символов.
        response = await llm.create(
            client, model=model, max_tokens=1000,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                {"type": "text", "text": f"Изображение от @{username}. Опиши содержимое и укажи есть ли action items (договорённости, задачи, важная информация). Обязательно перепиши даты, время, суммы, имена и адреса. Ответь по-русски кратко."},
            ]}]
        )
        await _rec(model, response.usage, "vision")
        return _llm_text(response)
    except Exception as exc:
        logger.error(f"Photo analysis failed: {exc}")
        return None

_HAIKU_IN = 1.00 / 1_000_000    # v4.36 (B6): Haiku 4.5
_HAIKU_OUT = 5.00 / 1_000_000

def _calc_cost(inp: Optional[int], out: Optional[int]) -> float:
    return (inp or 0) * _HAIKU_IN + (out or 0) * _HAIKU_OUT

_DATE_RE = re.compile(
    r"завтра|послезавтра|сегодня| today|tomorrow"
    r"|пятниц|суббот|воскрес|понедел|вторник|сред[уа]|четверг|выходны"
    r"|на следующей недел|на этой недел|след(ующ\w*|\.)?\s*недел|эт\w*\s*недел"
    r"|\d{1,2}[-–]?\s*(го|е|ое|ого|числа)"           # "8-го", "8 числа", "15е"
    r"|\d{1,2}[./]\d{1,2}"                              # "15.06", "8/7"
    r"|в \d{1,2}([:.]\d{2})?\b|к \d{1,2}|после \d{1,2}"  # время "в 17", "в 17:00"
    r"|утром|днём|днем|вечером|ночью|обед"
    r"|встрет|созвон|договорил|давай в|во сколько|когда",
    re.IGNORECASE,
)

_URGENT_RE = re.compile(
    r"\bсроч|\basap\b|\bгорит\b|урген|немедленн|как можно скорее|очень важн|"
    r"\burgent\b|прямо сейчас|дедлайн сегодня|до конца дня",
    re.IGNORECASE,
)

_LINK_RE = re.compile(r"https?://[^\s]+|t\.me/[^\s]+|www\.[^\s]+", re.IGNORECASE)

# Признаки выполнения задачи (для авто-закрытия)
# Задача вида «ответить …»: обычный ответ владельца сам по себе может быть её выполнением
_REPLY_TASK_RE = re.compile(r"\s*(❓\s*)?(ответить|написать|сообщить|рассказать|скинуть|прислать|отправить)", re.I)
_DONE_RE = re.compile(
    r"приш(ёл|ел|ла)|на месте|уже (здесь|тут|на месте)|встретил|"
    r"скинул|отправил|выслал|держи|вот( файл|,| держи)|сделал|готово|"
    r"закрыл|оплатил|ответил|выполнил|done|отправлен|прислал",
    re.IGNORECASE,
)

# Признаки просьбы о правках (для авто-переоткрытия закрытой задачи)
_REOPEN_RE = re.compile(
    r"переделай|переделать|исправ|правк|не то\b|не так|ещё раз|еще раз|"
    r"доработа|не подходит|поправ|verni|верни|замени|по-другому|по другому|"
    r"не нравится|плохо получил|ошибк",
    re.IGNORECASE,
)

# Сигнал ОБЯЗАТЕЛЬСТВА владельца в ИСХОДЯЩЕМ («приеду/привезу/отдам/сделаю/буду …»).
# Нужен, чтобы settle-разбор армился и на ТВОИ собственные обещания, а не только на входящие
# от контакта (баг: «готов отдать плиту, через 2 часа буду» — задача не создавалась, т.к.
# ветка исходящих не ставила contact в очередь разбора). Создание задач по-прежнему делает
# settle (полный контекст + дедуп + направление), здесь — только триггер.
_OWNER_COMMIT_RE = re.compile(
    r"привез|приед|подъед|заед|зайд|\bбуду\b|подвез|занес|верн[уё]|отда[мв]|"
    r"пришл[юё]|скин[уё]|вышл[юе]|сдела[ю]|почин|исправлю|подготовлю|"
    r"договорил|готов\b|готова\b|успею|"
    r"завтра|послезавтра|в понедельник|во вторник|в сред|в четверг|в пятниц|в суббот|в воскрес|"
    r"через \d|через пару|через недел|в \d{1,2}([:.]\d{2})?\b|после \d{1,2}",
    re.IGNORECASE,
)

router = Router(name="business")

# v4.27 «анти-удалялка»: кэш последних сообщений (chat_id, msg_id) → {text, direction, username},
# чтобы при правке показать «было→стало», а при удалении — САМ удалённый текст (Telegram его
# в апдейте не присылает). In-memory, кап 800; после рестарта старые правки/удаления покажем
# без исходника (честное ограничение).
from collections import OrderedDict
_MSG_CACHE: "OrderedDict[tuple, dict]" = OrderedDict()


def _cache_msg(chat_id: int, msg_id: int, text: str, direction: str, username: str) -> None:
    _MSG_CACHE[(chat_id, msg_id)] = {
        "text": (text or "").strip() or "[медиа/без текста]",
        "direction": direction, "username": username}
    while len(_MSG_CACHE) > 800:
        _MSG_CACHE.popitem(last=False)


@router.business_connection()
async def handle_business_connection(business_connection, db, settings) -> None:
    """Аккаунт подключил/отключил бота → запоминаем его как владельца (мультиаккаунт).
    Чтобы исходящие этого аккаунта не принимались за входящие от контакта."""
    from core import owners
    bc = business_connection
    try:
        uid = bc.user.id if bc.user else None
        if uid:
            await owners.upsert(db, uid, bc.id, bool(getattr(bc, "is_enabled", True)))
            # метка из имени/username аккаунта (если ещё не задана вручную)
            if bc.user and not owners.label(uid, settings.owner_user_id):
                lbl = bc.user.username and f"@{bc.user.username}" or bc.user.first_name or f"акк {str(uid)[-4:]}"
                await owners.set_label(db, uid, lbl)
    except Exception as exc:
        logger.error(f"business_connection handle failed: {exc}")


async def _offer_contact_memory_photo(bot, db, settings, user_id: int, username: str, sent_file_id: str) -> None:
    """Фото в памяти: аватар авто (getUserProfilePhotos), иначе спросить владельца про присланное.
    Если фото уже есть — не спрашиваем. Вопрос троттлим (не чаще раза в 24ч). Best-effort."""
    try:
        from db import get_contact_profile, mark_photo_asked
        from core import photos as _photos
        prof = await get_contact_profile(db, user_id) or {}
        if prof.get("photo_file_id"):
            return  # уже есть — не трогаем
        first_time = not prof.get("photo_asked_at")
        if first_time:
            try:
                pp = await bot.get_user_profile_photos(user_id, limit=1)
                if pp and pp.total_count and pp.photos and pp.photos[0]:
                    ph = pp.photos[0][-1]
                    await _photos.attach_photo(bot, db, settings, user_id, username, ph.file_id,
                                               unique_id=ph.file_unique_id, src="avatar")
                    await bot.send_message(settings.owner_user_id,
                                           f"📷 Поставил аватар @{username} в памяти (из профиля Telegram).")
                    return
            except Exception as exc:
                logger.debug(f"avatar fetch skip @{username}: {exc}")
        pa = prof.get("photo_asked_at")
        if pa:
            try:
                from datetime import datetime, timezone, timedelta
                if (datetime.now(timezone.utc) - datetime.fromisoformat(str(pa))) < timedelta(hours=24):
                    return
            except Exception:
                pass
        token = _photos.stash(user_id, username, sent_file_id)
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Это он/она", callback_data=f"pho:set:{token}", style="success"),
            InlineKeyboardButton(text="❌ Нет", callback_data=f"pho:no:{token}", style="danger"),
        ]])
        await bot.send_photo(settings.owner_user_id, sent_file_id,
                             caption=f"📷 Это фото <b>@{username}</b>? Добавить в памяти?",
                             reply_markup=kb, parse_mode="HTML")
        await mark_photo_asked(db, user_id)
    except Exception as exc:
        logger.debug(f"offer contact_memory photo skip @{username}: {exc}")


def _jit_stems(s: str) -> set:
    """Слова ≥4 симв. → стемы по 4 символа (грубая русская морфология: «монтажом»→«монт»)."""
    return {w[:4] for w in re.findall(r"[а-яёa-z0-9]{4,}", (s or "").lower())}


def _jit_topic_overlap(text: str, titles: list, commitments: str):
    """v4.21: тема сообщения совпала с открытой задачей/долгом → вернуть, с чем."""
    stems = _jit_stems(text)
    if not stems:
        return None
    for t in titles:
        if stems & _jit_stems(t):
            return t
    if stems & _jit_stems(commitments):
        return "обещание"
    return None


async def _jit_context_card(bot, db, settings: Settings, user_id: int, username: str,
                            text: str, category: str, msg_count: int) -> None:
    """v4.21 JIT-шпаргалка: память сама приходит в момент контакта (аудит: память копятся,
    /about не открывается). Гейты анти-шума: не SPAM, msg_count≥5, кулдаун 12ч на контакт,
    и (пауза ≥3 дней ИЛИ в сообщении всплыла тема открытой задачи/долга). Без LLM — сборка из БД."""
    try:
        if category == "SPAM" or msg_count < 5:
            return
        from db import can_notify, log_notification, get_contact_profile, get_open_tasks_for_user
        if not await can_notify(db, user_id, "jit", cooldown_minutes=12 * 60):
            return
        # пауза: предыдущее сообщение диалога (текущее уже записано → OFFSET 1)
        async with db.execute(
                "SELECT created_at FROM conversations WHERE user_id=? ORDER BY id DESC LIMIT 1 OFFSET 1",
                (user_id,)) as cur:
            r = await cur.fetchone()
        pause_days = 999.0
        if r:
            from datetime import datetime, timezone
            try:
                t0 = datetime.fromisoformat(str(r[0]).replace(" ", "T"))
                if t0.tzinfo is None:
                    t0 = t0.replace(tzinfo=timezone.utc)
                pause_days = (datetime.now(timezone.utc) - t0).total_seconds() / 86400
            except Exception:
                pass
        prof = await get_contact_profile(db, user_id) or {}
        open_tasks = await get_open_tasks_for_user(db, user_id)
        commitments = (prof.get("commitments") or "").strip()
        overlap = _jit_topic_overlap(text, [t["title"] for t in open_tasks], commitments)
        if pause_days < 3 and not overlap:
            return
        key = ""
        try:
            import json as _json
            key = ((_json.loads(prof.get("facts_json") or "{}").get("sections") or {}).get("key") or "")
        except Exception:
            pass
        # v4.48.1: обрезка по границе ЗАПИСИ/СЛОВА (скрин владельца: «…в обычном с», «(после с») + лимиты шире
        from core.vault import _clip, _clip_section
        lines = []
        if key:
            lines.append(f"• {_clip_section(key, 400)}")
        if open_tasks:
            lines.append("📋 Открыто: " + "; ".join(_clip(t["title"], 90) for t in open_tasks[:3]))
        if commitments:
            lines.append(f"🤝 Ты обещал: {_clip_section(commitments, 300)}")
        if 3 <= pause_days < 900:
            lines.append(f"🕒 Пауза в диалоге была {int(pause_days)} дн.")
        if overlap and overlap != "обещание":
            lines.append(f"🔗 Похоже, речь о задаче: {_clip(overlap, 120)}")
        if not lines:
            return
        who = prof.get("name") or f"@{username}"
        card = (f"📎 <b>Контекст: {html.escape(str(who))}</b> (@{username})\n"
                + html.escape("\n".join(lines)))
        from core.topics import send_owner, snooze_row
        from aiogram.types import InlineKeyboardMarkup
        await send_owner(bot, settings.owner_user_id, "contacts", card, parse_mode="HTML",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[snooze_row()]))
        await log_notification(db, user_id, "jit")
        logger.info(f"JIT-контекст по @{username} (pause={pause_days:.1f}d, overlap={bool(overlap)})")
    except Exception as exc:
        logger.debug(f"jit skip @{username}: {exc}")


async def _handle_checklist_done(bot, db, settings: Settings, ctd, reply_to=None) -> None:
    """v4.20: владелец отметил пункты чек-листа дайджеста → закрыть задачи (Todoist+календарь+БД).
    v4.70: снятая галочка возвращает задачу. Маппинг пункт→sync_id лежит в settings_kv
    chk:{message_id} (пишет _send_checklist_digest)."""
    try:
        import json as _json
        from db import (get_setting, set_setting, get_sync_by_id, mark_sync_done_by_id,
                        reopen_task_sync_by_id)
        src = getattr(ctd, "checklist_message", None) or reply_to
        mid = src.message_id if src else None
        raw = await get_setting(db, f"chk:{mid}") if mid else None
        done, undone = ctd.marked_as_done_task_ids or [], ctd.marked_as_not_done_task_ids or []
        if not raw:
            # v4.70: раньше молча выходили — 31.10 пять галочек пропали без следа в логе
            logger.warning(f"checklist: нет маппинга chk:{mid} (checklist_message={bool(ctd.checklist_message)}, "
                           f"reply={bool(reply_to)}, done={done}, undone={undone})")
            return
        mapping = _json.loads(raw)
        from core.sync import complete_todoist_task
        from core.actions import _delete_task_calendar_event, _reopen_todoist
        closed, reopened = [], []
        for tid in done:
            sync_id = mapping.get(str(tid))
            if not sync_id:
                continue
            row = await get_sync_by_id(db, sync_id)
            if not row or row.get("synced"):
                continue  # нет/уже закрыта — идемпотентно
            if settings.todoist_api_token and row.get("todoist_id"):
                await complete_todoist_task(settings.todoist_api_token, row["todoist_id"])
            await _delete_task_calendar_event(db, {"id": sync_id})
            await mark_sync_done_by_id(db, sync_id)
            await set_setting(db, f"ownerclosed:{sync_id}", "checklist")   # судья переоткрытия не вернёт
            closed.append(row.get("title") or "?")
        for tid in undone:
            sync_id = mapping.get(str(tid))
            if not sync_id:
                continue
            row = await get_sync_by_id(db, sync_id)
            if not row or not row.get("synced"):
                continue
            if settings.todoist_api_token and row.get("todoist_id"):
                await _reopen_todoist(settings.todoist_api_token, row["todoist_id"])
            await reopen_task_sync_by_id(db, sync_id)
            await set_setting(db, f"ownerclosed:{sync_id}", "")
            reopened.append(row.get("title") or "?")
        if closed or reopened:
            from core.topics import send_owner
            lines = []
            if closed:
                lines.append("✅ Закрыла по чек-листу: " + "; ".join(c[:60] for c in closed))
            if reopened:
                lines.append("↩️ Вернула по чек-листу: " + "; ".join(c[:60] for c in reopened))
            await send_owner(bot, settings.owner_user_id, "tasks", "\n".join(lines))
            logger.info(f"Checklist: закрыто {len(closed)}, возвращено {len(reopened)}")
    except Exception as exc:
        logger.warning(f"checklist done handling failed: {exc}")


@router.business_message()
async def handle_business_message(
    message: Message,
    bot: Bot,
    db: aiosqlite.Connection,
    settings: Settings,
    anthropic_client: AsyncAnthropic,
) -> None:
    from_user = message.from_user
    if not from_user:
        return

    bc_id = message.business_connection_id
    chat_id = message.chat.id

    # Запоминаем бизнес-подключение (одно на аккаунт) — нужно для smart scheduling:
    # отправить черновик контакту вне контекста входящего сообщения.
    if bc_id:
        try:
            from db import set_setting
            await set_setting(db, "bc_id", bc_id)
        except Exception:
            pass

    # Dedup: Telegram may redeliver an update — skip if already processed
    from db import is_update_processed
    if await is_update_processed(db, f"bm:{bc_id}:{chat_id}:{message.message_id}"):   # v4.36 (B3): + аккаунт
        logger.debug(f"Duplicate update skipped: {chat_id}:{message.message_id}")
        return

    # v4.20: галочка на чек-листе дайджеста (в Избранном) → закрыть задачу
    _ctd = getattr(message, "checklist_tasks_done", None)
    if _ctd:
        await _handle_checklist_done(bot, db, settings, _ctd, message.reply_to_message)
        return

    # v4.27: кэш для «анти-удалялки» (правки/удаления) — обе стороны диалога
    from core import owners as _own
    _cache_msg(chat_id, message.message_id, message.text or message.caption or "",
               "out" if _own.is_owner(settings.owner_user_id, from_user.id) else "in",
               message.chat.username or from_user.username or f"id{chat_id}")
    # C4: заметки владельца в СВОЁМ «Избранном» — не диалог с контактом (иначе settle делал задачи «себе от себя»)
    if _own.is_owner(settings.owner_user_id, from_user.id) and chat_id == from_user.id:
        return

    # outgoing: owner replied — save to vault + check for date agreements
    from core import owners
    if owners.is_owner(settings.owner_user_id, from_user.id):
        partner_id = chat_id
        partner_username = message.chat.username or f"id{partner_id}"
        text_out = message.text or message.caption or ""

        # Save owner's outgoing message to vault — ALWAYS (text or media placeholder)
        out_media = (
            "photo" if message.photo else "voice" if (message.voice or message.audio)
            else "video_note" if message.video_note else "video" if message.video
            else "document" if message.document else "sticker" if message.sticker else None
        )
        await vault_add(db, partner_id, partner_username, "out", text_out or None, out_media)

        # Обучение черновиков (path B): если по этому контакту взведён режим «✍️ Отвечу сам»,
        # запоминаем РЕАЛЬНЫЙ ответ владельца как пример стиля.
        if text_out:
            from core import drafts
            teach = drafts.pop_teach_chat(partner_id)
            if teach:
                from db import save_draft_example
                await save_draft_example(db, partner_id, teach.get("incoming") or "", text_out)
                try:
                    from core.topics import send_owner
                    await send_owner(
                        bot, settings.owner_user_id, "drafts",
                        f"✅ Запомнил, как ты ответил @{partner_username} — учту твой стиль в черновиках.")
                except Exception:
                    pass
                logger.info(f"Draft style learned from real reply to @{partner_username}")

        if not await whitelist.check(db, partner_id):
            await whitelist.add(db, partner_id, partner_username, reason="auto_outgoing")
            logger.info(f"Auto-whitelisted {partner_id} (@{partner_username}) — owner replied")

        # Auto-close: owner did the thing (sent file / "скинул" / "я пришёл" / "готово").
        # Стикер НЕ считаем сигналом выполнения (это не «сделал»), хотя в память он пишется выше.
        close_media = out_media and out_media != "sticker"
        if close_media or (text_out and _DONE_RE.search(text_out)):
            from core.actions import auto_close_tasks
            from db import is_over_budget
            ob = await is_over_budget(db, settings.anthropic_monthly_budget)
            ctx = await vault_get_context(db, partner_id, limit=8)
            ctx = f"{ctx}\n\nТы (владелец): {text_out or '[отправил файл]'}"
            await auto_close_tasks(
                anthropic_client, settings.anthropic_model_smart, db,
                settings.todoist_api_token, settings.notion_token,
                partner_id, partner_username, ctx,
                bot=bot, owner_id=settings.owner_user_id, over_budget=ob,
            )

        # Владелец ответил/занялся → открытые задачи «Новая» → «В работе» (оживляем среднюю колонку доски)
        try:
            from core.actions import mark_tasks_in_progress
            moved = await mark_tasks_in_progress(db, settings.notion_token, partner_id)
            if moved:
                logger.info(f"{moved} task(s) → «В работе» (owner engaged @{partner_username})")
        except Exception as _e:
            logger.debug(f"mark_in_progress skipped: {_e}")

        # Если в ответе владельца есть дата/время → ТОЛЬКО обновляем дату существующей задачи.
        # СОЗДАНИЕ задач здесь НЕ делаем — это работа settle-цикла (полный контекст + все защиты:
        # направление, смолток, дедуп-судья). Иначе одна реплика владельца с датой-словом
        # («Который сегодняшний», «Или у тебя завтра?») ложно фабриковалась как «Договорились» → лишняя задача.
        if text_out and _DATE_RE.search(text_out):
            open_tasks = await get_open_tasks_for_user(db, partner_id)
            if open_tasks:
                recent_ctx = await vault_get_context(db, partner_id, limit=10)
                ctx = f"История переписки:\n{recent_ctx}\n\nТы написал: {text_out}" if recent_ctx else text_out
                for task in open_tasks:
                    upd = await _check_update_existing(
                        anthropic_client, settings.anthropic_model_smart,
                        "owner", ctx, task["title"],
                    )
                    if upd.get("should_update") and upd.get("updated_due_date"):
                        due = upd["updated_due_date"]
                        logger.info(f"Owner reply: updating task '{task['title']}' → {due}")
                        if settings.todoist_api_token:
                            await update_todoist_task(settings.todoist_api_token, task["todoist_id"], due)
                        from core.actions import update_task_calendar, _normalize_due
                        await update_task_calendar(db, task, task["title"], _normalize_due(due))
                        break

        # Армим settle-разбор на ОБЯЗАТЕЛЬСТВО владельца в исходящем (приеду/привезу/отдам/…).
        # Без этого твоё собственное обещание не попадало в очередь извлечения, если контакт
        # не прислал новое сообщение. Создание/дедуп — на стороне settle (полный контекст).
        _reply_due = bool(text_out) and any(_REPLY_TASK_RE.match(t["title"]) for t in await get_open_tasks_for_user(db, partner_id))
        if _reply_due or (text_out and len(text_out.strip()) >= 12 and not text_out.rstrip().endswith("?")
                          and _OWNER_COMMIT_RE.search(text_out)):  # вопрос («ты завтра будешь?») — не обязательство
            try:
                from db import mark_pending_analysis, get_recent_category
                _cat = await get_recent_category(db, partner_id, hours=6) or "UNCLEAR"
                await mark_pending_analysis(db, partner_id, partner_username, _cat)
                logger.info(f"Settle armed by owner {'reply' if _reply_due else 'commitment'} → @{partner_username}: {text_out[:50]!r}")
            except Exception as _e:
                logger.debug(f"arm-on-owner-commit skip: {_e}")
        return

    user_id = from_user.id
    username = from_user.username or f"id{user_id}"
    display_name = from_user.full_name or username
    text = message.text or message.caption
    if text:   # v4.106: номер карты не уходит ни в модель, ни в базу
        from core.redact import mask_cards
        text = mask_cards(text, message.entities or message.caption_entities)

    # Save any incoming attachment to vault (Telegram/Вложения) — фон, не блокирует
    if (message.photo or message.document or message.voice or message.audio or
            message.video or message.video_note or message.animation or message.sticker):
        asyncio.create_task(forwarder.save_attachment(bot, message, username)).add_done_callback(_bg_done)

    # Фото в памяти контакта (аватар авто / спросить про присланное) — фоном, best-effort
    if message.photo:
        asyncio.create_task(_offer_contact_memory_photo(bot, db, settings, user_id, username, message.photo[-1].file_id)).add_done_callback(_bg_done)

    # Handle voice/audio/video-notes (кружочки) — transcribe locally via faster-whisper
    if message.voice or message.audio or message.video_note:
        from core.transcribe import transcribe_voice
        voice_file = message.voice or message.audio or message.video_note
        label = "Кружок" if message.video_note else "Голосовое"
        # Skip very long audio (>5 min) — would block CPU too long
        duration = getattr(voice_file, "duration", 0) or 0
        mt = "video_note" if message.video_note else "voice"
        if duration > 300:
            logger.warning(f"{label} from @{username} too long ({duration}s) — skipping transcription")
            await vault_add(db, user_id, username, "in", f"[{label}, {duration//60} мин, не распознано]", mt)
            await forwarder.forward_to_saved(
                bot=bot, message=message, owner_user_id=settings.owner_user_id,
                category="UNCLEAR", summary=f"🎙 Длинное {label.lower()} ({duration//60} мин) — нужна ручная проверка",
                username=username,
            )
            return
        # Dedup identical audio (resent same file) — reuse prior transcript, skip Whisper
        from db import get_processed_media, mark_processed_media
        v_fuid = getattr(voice_file, "file_unique_id", None)
        prior_audio = await get_processed_media(db, v_fuid)
        if prior_audio is not None and prior_audio.get("description"):
            transcribed = prior_audio["description"]
            logger.info(f"Duplicate {label.lower()} from @{username} — reusing transcript, skip Whisper")
        else:
            transcribed = await transcribe_voice(bot, voice_file.file_id)
            if transcribed:
                await mark_processed_media(db, v_fuid, user_id, mt, transcribed)
        if transcribed:
            text = f"[{label}] {transcribed}" + (f"\n{message.caption.strip()}" if message.caption else "")   # C18: подпись не теряем
            logger.info(f"{label} from @{username}: {transcribed[:80]}")
        else:
            await vault_add(db, user_id, username, "in", f"[{label} — не распознано]", mt)
            await forwarder.forward_to_saved(
                bot=bot, message=message, owner_user_id=settings.owner_user_id,
                category="UNCLEAR", summary=f"🎙 {label} — не удалось распознать",
                username=username,
            )
            return

    # Handle documents — extract text from PDF/DOCX/TXT
    if message.document and not text:
        from core.documents import extract_document_text
        doc_result = await extract_document_text(bot, message.document)
        if doc_result:
            fname, doc_text = doc_result
            text = f"[Документ: {fname}]\n{doc_text}"
            logger.info(f"Document from @{username}: {fname}")
            # Save the actual file to Saved Messages so it's always accessible
            await forwarder.save_file_to_owner(
                bot, message, settings.owner_user_id,
                f"📎 Файл от @{username}: {fname}",
            )
        else:
            await vault_add(db, user_id, username, "in", f"[Документ: {message.document.file_name or 'файл'} — не извлечено]", "document")
            await forwarder.forward_to_saved(
                bot=bot, message=message, owner_user_id=settings.owner_user_id,
                category="UNCLEAR", summary=f"📎 Документ: {message.document.file_name or 'файл'} — нужна ручная проверка",
                username=username,
            )
            return

    # Handle photos — analyze with Claude vision (with daily cap + identical-photo dedup)
    if message.photo and not text:
        from db import count_photos_today, get_processed_media, mark_processed_media
        photo = message.photo[-1]
        fuid = getattr(photo, "file_unique_id", None)
        prior = await get_processed_media(db, fuid)
        if prior is not None:
            # Идентичное фото уже разбирали (тот же file_unique_id) — не тратим Vision и не дублируем в Saved
            logger.info(f"Duplicate photo from @{username} — reusing prior analysis, skipping Vision/forward")
            if prior.get("description"):
                text = f"[Фото] {prior['description']}"
            else:
                await vault_add(db, user_id, username, "in", None, "photo")
                return
        elif await count_photos_today(db) >= settings.daily_photo_limit:
            logger.warning(f"Daily photo limit ({settings.daily_photo_limit}) reached — skipping vision")
            await forwarder.forward_to_saved(
                bot=bot, message=message, owner_user_id=settings.owner_user_id,
                category="UNCLEAR", summary="📸 Фото (лимит анализа на сегодня исчерпан)",
                username=username,
            )
            await vault_add(db, user_id, username, "in", None, "photo")
            return
        else:
            logger.info(f"Photo from @{username} — analyzing with Vision")
            photo_desc = await _analyze_photo(anthropic_client, settings.anthropic_model, bot, message, username)
            if photo_desc:
                await mark_processed_media(db, fuid, user_id, "photo", photo_desc)
                text = f"[Фото] {photo_desc}"
                logger.info(f"Photo from @{username}: {photo_desc[:80]}")
                await forwarder.save_file_to_owner(
                    bot, message, settings.owner_user_id, f"📸 Фото от @{username}",
                )
            else:
                await vault_add(db, user_id, username, "in", None, "photo")
                await forwarder.forward_to_saved(
                    bot=bot, message=message, owner_user_id=settings.owner_user_id,
                    category="UNCLEAR", summary="📸 Фото — не удалось распознать, посмотри вручную",
                    username=username,
                )
                return

    # Catch-all for other media without text (video-notes/кружочки, video, stickers, etc.)
    if not text:
        media_kind = (
            "🎥 Видеосообщение (кружок)" if message.video_note else
            "🎬 Видео" if message.video else
            "🖼 Стикер" if message.sticker else
            "🎞 GIF" if message.animation else
            "📍 Геолокация" if message.location else
            "👤 Контакт" if message.contact else
            None
        )
        if media_kind:
            mt = ("video_note" if message.video_note else "video" if message.video
                  else "sticker" if message.sticker else "media")
            logger.info(f"{media_kind} from @{username} — forwarded to Saved")
            await vault_add(db, user_id, username, "in", None, mt)
            # Don't forward stickers/gifs from friends (noise); forward video/video_note always
            wl = await whitelist.check(db, user_id)
            noisy = message.sticker or message.animation
            if not (wl and noisy):
                await forwarder.forward_to_saved(
                    bot=bot, message=message, owner_user_id=settings.owner_user_id,
                    category="UNCLEAR", summary=f"{media_kind} от @{username} — посмотри вручную",
                    username=username,
                )
        else:
            # Unknown content type — still save for full context
            await vault_add(db, user_id, username, "in", "[сообщение без текста]", "other")
        return

    is_whitelisted = await whitelist.check(db, user_id)
    msg_count = await get_message_count(db, user_id)
    is_frequent = msg_count > 5

    # Правила по контакту (v2.11): mute — без пушей владельцу; never — без извлечения задач
    from db import get_contact_rule
    _rule = await get_contact_rule(db, user_id)
    muted = (_rule == "mute")
    no_tasks = (_rule == "never")

    # Мультиаккаунт: на какой аккаунт пришло → метка источника в уведомлениях + запомнить у контакта
    from core import owners
    _src_acct = owners.account_for_bc(bc_id)
    acct_tag = owners.tag(_src_acct, settings.owner_user_id)
    if _src_acct:
        try:
            from db import set_contact_src_account
            await set_contact_src_account(db, user_id, _src_acct)
        except Exception:
            pass

    # Classify all messages regardless of whitelist/frequency
    is_forwarded_from_channel = isinstance(message.forward_origin, MessageOriginChannel)
    has_photo = bool(message.photo)

    # Budget circuit-breaker — compute once, gate all LLM calls this message
    from db import is_over_budget, get_recent_corrections
    over_budget = await is_over_budget(db, settings.anthropic_monthly_budget)
    if over_budget:
        from core.alerts import alert_owner
        await alert_owner(
            bot, db, settings.owner_user_id, "budget_exceeded",
            f"🪙 Месячный бюджет API (${settings.anthropic_monthly_budget:.2f}) исчерпан. "
            f"LLM отключён до конца месяца — работают только правила. "
            f"Поднять: ANTHROPIC_MONTHLY_BUDGET в .env.",
            cooldown_minutes=1440,
        )
    corrections = await get_recent_corrections(db, limit=15)
    # Conversation context so short fragments ("Не знаю") are understood in chat flow
    convo_ctx = await vault_get_context(db, user_id, limit=8)
    from db import get_recent_category, get_dominant_category
    thread_cat = await get_recent_category(db, user_id, hours=3)
    established = await get_dominant_category(db, user_id)  # P1.3: устоявшаяся роль → наследование

    result = await classifier.classify(
        client=anthropic_client,
        model=settings.anthropic_model,
        text=text,
        username=username,
        msg_number=msg_count + 1,
        has_username=bool(from_user.username),
        has_photo=has_photo,
        is_forwarded_from_channel=is_forwarded_from_channel,
        over_budget=over_budget,
        corrections=corrections,
        context=convo_ctx,
        thread_category=thread_cat,
        trusted_sender=is_whitelisted or is_frequent,  # форвард от друга не глушим в SPAM
        established_category=established,
    )

    # v4.46: роль, выставленную владельцем кнопкой (rolelock), классификатор не переспоривает.
    # Кейс 09.09: @recruiter_n (роль «рекрутер») прислала приглашение на собеседование со ссылкой → SPAM 0.95,
    # задача не получила время. Роль → категория (BUSINESS/FRIEND); SPAM/UNCLEAR от модели → заменяем.
    try:
        from db import get_setting as _gs
        _locked_role = await _gs(db, f"rolelock:{user_id}")
        if _locked_role:
            from core.actions import _category_from_relationship
            _role_cat = _category_from_relationship(_locked_role)
            if _role_cat in ("BUSINESS", "FRIEND") and result.category in ("SPAM", "UNCLEAR"):
                logger.info(f"Role override @{username}: {result.category} → {_role_cat} (owner-set role {_locked_role!r})")
                result.category = _role_cat
                result.rule_matched = "role_lock"
    except Exception as exc:
        logger.debug(f"role override skip: {exc}")

    # Alert on silent Anthropic degradation
    if result.rule_matched == "llm_error":
        from core.alerts import alert_owner
        await alert_owner(
            bot, db, settings.owner_user_id, "apifail:anthropic",
            "⚠️ Anthropic API не отвечает — классификация валится в UNCLEAR. Проверь ключ/лимит.",
            cooldown_minutes=60,
        )

    mode = await get_mode(db, default=settings.mode)
    action = "logged"

    if result.category == "SPAM":
        # Don't reply to whitelisted/frequent users even in active mode
        if mode == "active" and not is_whitelisted and not is_frequent:
            await responder.send_spam_reply(bot, chat_id, bc_id)
            await responder.mark_as_read(bot, bc_id, chat_id, message.message_id)
            action = "replied"
        else:
            action = "observed"
        logger.info(f"SPAM from @{username} [{result.rule_matched}] conf={result.confidence:.2f}")

    elif result.category == "BUSINESS":
        await forwarder.forward_to_saved(
            bot=bot, message=message, owner_user_id=settings.owner_user_id,
            category="BUSINESS", summary=result.summary, username=username,
        )
        # Умный черновик ответа → владельцу на одобрение (отправка контакту только по кнопке).
        # Работает в любом режиме (никогда не авто-шлёт). Заменяет тупой шаблон, если включён.
        drafted = False
        from db import get_setting
        _dr = await get_setting(db, "draft_replies")
        _drafts_on = (_dr == "on") if _dr is not None else settings.enable_draft_replies
        if _drafts_on and text and bc_id and not over_budget and not muted and not is_forwarded_from_channel:
            if await can_notify(db, user_id, "draft_reply", cooldown_minutes=120):
                try:
                    from core import drafts
                    from db import get_contact_profile
                    prof = await get_contact_profile(db, user_id)
                    await drafts.offer_draft(
                        bot, anthropic_client, settings.anthropic_model_smart,
                        settings.owner_user_id, bc_id, chat_id, username, prof, text,
                        db=db, user_id=user_id, acct_tag=acct_tag)
                    await log_notification(db, user_id, "draft_reply")
                    drafted = True
                except Exception as exc:
                    logger.debug(f"draft offer skip: {exc}")

        # Auto-reply template in active mode — debounced (max 1 per user / 24h). Только если не сделали умный черновик.
        from db import get_setting
        reply_tpl = await get_setting(db, "business_reply")
        if reply_tpl is None:
            reply_tpl = settings.business_reply_template
        if mode == "active" and reply_tpl and not drafted and not is_forwarded_from_channel \
                and not is_whitelisted and not muted:   # C6: шаблон не друзьям и не заглушённым
            if await can_notify(db, user_id, "business_reply", cooldown_minutes=1440):
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=reply_tpl,
                        business_connection_id=bc_id,
                        parse_mode=None,
                    )
                    await log_notification(db, user_id, "business_reply")
                except Exception as exc:
                    logger.warning(f"BUSINESS auto-reply failed: {exc}")
        action = "forwarded"
        logger.info(f"BUSINESS from @{username}: {result.summary}")
        # (v4.20: автопробив Perplexity удалён по аудиту — 0 использований за 3 недели)

    elif result.category == "UNCLEAR":
        # Don't flood Saved with UNCLEAR fragments from an active chat / known friend
        if is_frequent or is_whitelisted:
            action = "observed"
            logger.info(f"UNCLEAR (ongoing chat) from @{username} — not forwarded")
        else:
            await forwarder.forward_to_saved(
                bot=bot, message=message, owner_user_id=settings.owner_user_id,
                category="UNCLEAR", summary=result.summary, username=username,
            )
            action = "forwarded"
            logger.info(f"UNCLEAR from @{username}: {result.summary}")

    else:  # FRIEND
        action = "logged"
        logger.debug(f"FRIEND from @{username}")

    await log_message(
        db, user_id=user_id, username=username, message_text=text,
        category=result.category, rule_matched=result.rule_matched,
        action_taken=action, llm_latency_ms=result.llm_latency_ms,
        llm_tokens_input=result.llm_tokens_input, llm_tokens_output=result.llm_tokens_output,
    )
    await increment_message_count(db, user_id)

    # Save incoming message to vault (with category for follow-up tracking)
    media_type = "photo" if message.photo else "voice" if message.voice else None
    await vault_add(db, user_id, username, "in", text, media_type, category=result.category)

    # v4.21 JIT-шпаргалка: память сама приходит в момент контакта (все гейты внутри)
    asyncio.create_task(_jit_context_card(
        bot, db, settings, user_id, username, text or "", result.category, msg_count
    )).add_done_callback(_bg_done)

    # v4.26 переговоры о встрече: «когда встретимся?» → слоты-кнопки владельцу (гейты внутри)
    if (getattr(settings, "meeting_negotiate", True) and result.category != "SPAM"
            and msg_count >= 3):
        from core.meeting import MEETING_ASK_RE, offer_meeting
        if MEETING_ASK_RE.search(text or ""):
            asyncio.create_task(offer_meeting(
                bot, db, settings, user_id, username, text or "", bc_id=bc_id)).add_done_callback(_bg_done)

    # Contact asks for changes → re-open a recently closed task
    if text and _REOPEN_RE.search(text):
        from core.actions import auto_reopen_tasks
        ctx_re = await vault_get_context(db, user_id, limit=8)
        await auto_reopen_tasks(
            anthropic_client, settings.anthropic_model_smart, db,
            settings.todoist_api_token, settings.notion_token,
            user_id, username, ctx_re, bot=bot, owner_id=settings.owner_user_id,
            over_budget=over_budget,
        )

    # Save any links from the message
    if text:
        links = _LINK_RE.findall(text)
        if links:
            from db import save_links
            await save_links(db, user_id, username, links, text, result.category)
            logger.info(f"Saved {len(links)} link(s) from @{username}")
            # 🔗 Ссылки — лёгкая карточка в тему (раньше ссылки просто молча копились)
            try:
                from core import richmsg
                from core.topics import send_owner, thread_id
                cat_icon = {"BUSINESS": "💼", "UNCLEAR": "❓", "FRIEND": "🙂"}.get(result.category, "🔗")
                items = "".join(
                    f'<li><a href="{html.escape(u, quote=True)}">{html.escape(u[:70])}</a></li>'
                    for u in links[:5])
                doc = (f"<h3>🔗 Ссылк{'а' if len(links) == 1 else 'и'} от @{html.escape(username)}</h3>"
                       f"<p>{cat_icon} {result.category}</p><ul>{items}</ul>")
                if not await richmsg.send_rich(bot.token, settings.owner_user_id, doc, thread_id("links")):
                    await send_owner(
                        bot, settings.owner_user_id, "links",
                        f"🔗 <b>От @{username}</b> ({result.category}):\n" +
                        "\n".join(html.escape(u) for u in links[:5]),
                        disable_web_page_preview=True)
            except Exception as exc:
                logger.debug(f"links push skip: {exc}")

    # 👤 Контакты — первое сообщение нового человека (CRM-сигнал) с быстрой триажей
    if msg_count == 0 and result.category != "SPAM" and not muted:
        try:
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            from core.topics import send_owner
            cat_label = {"BUSINESS": "💼 бизнес", "UNCLEAR": "❓ проверить", "FRIEND": "🙂 личное"}.get(result.category, "📋")
            tg_link = profile_link(username, user_id)
            # v4.37: «бот говорит — ты киваешь»: догадка роли (Haiku, + bio профиля) → [✓ Верно][✏️ Поправить][🚫 Спам]
            from core import roles as _roles
            bio = ""
            try:
                _ch = await bot.get_chat(user_id)
                bio = (getattr(_ch, "bio", None) or "")[:200]
            except Exception:
                pass
            guess = await _roles.guess_role(anthropic_client, settings.anthropic_model, db, text or "",
                                            result.category, display_name, bio)
            if guess:
                await _roles.remember_guess(db, user_id, guess)
                guess_line = (f"\n🤔 Думаю: {_roles.cat_label(guess['category'])} · роль «{html.escape(guess['role'])}»"
                              + (f"\n<i>в профиле: {html.escape(bio)}</i>" if bio else ""))
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✓ Верно", callback_data=f"rolok:{user_id}", style="success"),
                     InlineKeyboardButton(text="✏️ Поправить", callback_data=f"rolfix:{user_id}", style="primary"),
                     InlineKeyboardButton(text="🚫 Спам", callback_data=f"sp:{user_id}", style="danger")],
                    [InlineKeyboardButton(text="📋 Память", callback_data=f"info:{user_id}")],
                ])
            else:   # догадки нет (сбой LLM) — старая триажа
                guess_line = f"\nКатегория: {cat_label}"
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⭐ Друг", callback_data=f"fr:{user_id}", style="success"),
                     InlineKeyboardButton(text="🚫 Спам", callback_data=f"sp:{user_id}", style="danger")],
                    [InlineKeyboardButton(text="📋 Память", callback_data=f"info:{user_id}", style="primary"),
                     InlineKeyboardButton(text="🔕 Ок", callback_data="ig", style="primary")],
                ])
            await send_owner(
                bot, settings.owner_user_id, "contacts",
                (
                    f"👤 <b>Новый контакт: <a href=\"{tg_link}\">{html.escape(display_name)}</a> (@{username})</b>{acct_tag}"
                    f"{guess_line}\n\n"
                    f"<i>{html.escape((text or '')[:200])}</i>"
                ),
                reply_markup=kb,
            )
        except Exception as exc:
            logger.debug(f"new contact push skip: {exc}")

    # ⚡ Urgency detector — instant push, bypasses digest
    if text and _URGENT_RE.search(text) and result.category != "SPAM" and not muted:
        if await can_notify(db, user_id, "urgent", cooldown_minutes=(0 if _rule == "vip" else 60)):
            tg_link = profile_link(username, user_id)
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            from core.topics import send_owner
            # Текст+кнопки (не rich): «➕ В задачу» превращает срочное в задачу одним тапом;
            # колбэк правит карточку через edit_text, поэтому сообщение текстовое.
            rows = [[
                InlineKeyboardButton(text="➕ В задачу", callback_data=f"mk:{user_id}", style="primary"),
                InlineKeyboardButton(text="✅ Ок", callback_data="ig", style="success"),
            ]]
            if not has_username(username):   # v4.82: ответ через Миру
                rows.append([InlineKeyboardButton(text="✍️ Ответить", callback_data=f"rep:{user_id}", style="primary")])
            kb = InlineKeyboardMarkup(inline_keyboard=rows)
            await send_owner(
                bot, settings.owner_user_id, "urgent",
                (
                    f"🔴 <b>СРОЧНО от {contact_label(username, user_id, display_name)}</b>{acct_tag}\n\n"
                    f"<i>{html.escape(text[:300])}</i>\n\n"
                    f"<a href=\"{tg_link}\">Открыть чат</a>"
                ),
                reply_markup=kb,
            )
            await log_notification(db, user_id, "urgent")

    # For UNCLEAR messages longer than 30 chars — notify with ❗ (with cooldown)
    if result.category == "UNCLEAR" and len(text or "") > 30 and not muted:
        if await can_notify(db, user_id, "unclear", cooldown_minutes=30):
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            tg_link = profile_link(username, user_id)
            rows = [[
                InlineKeyboardButton(text="➕ Создать задачу", callback_data=f"mk:{user_id}", style="primary"),
                InlineKeyboardButton(text="🔕 Игнорировать", callback_data="ig", style="danger"),
            ]]
            if not has_username(username):   # v4.82: ответ через Миру
                rows.append([InlineKeyboardButton(text="✍️ Ответить", callback_data=f"rep:{user_id}", style="primary")])
            kb = InlineKeyboardMarkup(inline_keyboard=rows)
            from core.topics import send_owner
            await send_owner(
                bot, settings.owner_user_id, "tasks",
                (
                    f"❗ <b>{contact_label(username, user_id, display_name)}</b> — возможно нужна задача?{acct_tag}\n\n"
                    f"<i>{html.escape((text or '')[:300])}</i>\n\n"
                    f"<a href=\"{tg_link}\">Открыть чат</a>"
                ),
                reply_markup=kb,
            )
            await log_notification(db, user_id, "unclear")

    # Increment the message counter FIRST (sync), THEN schedule enrich — so enrich reads
    # the authoritative count and the 1/3/10th cadence is stable (no read-before-write race).
    await upsert_contact_profile_count(db, user_id, username, display_name)
    recent_ctx = await vault_get_context(db, user_id, limit=10)
    _t_enrich = asyncio.create_task(enrich_profile(
        anthropic_client, settings.anthropic_model_smart,
        user_id, username, text or "", result.category,
        recent_ctx, db,
    ))
    _t_enrich.add_done_callback(_bg_done)

    # RAG (optional): index message for semantic recall later
    if settings.enable_rag and text:
        from core.embeddings import index_message
        asyncio.create_task(index_message(db, user_id, text)).add_done_callback(_bg_done)

    # Mark for deferred task analysis — задача соберётся когда разговор затихнет (10 мин),
    # чтобы один диалог про одно = одна задача (а не на каждое сообщение)
    if result.category != "SPAM" and not no_tasks:
        from db import mark_pending_analysis
        await mark_pending_analysis(db, user_id, username, result.category)

    # Контакт в Notion ведёт ТОЛЬКО vault.enrich_profile (по notion_contact_id, без дублей).
    # Прямой upsert_contact убран — он создавал вторую запись (источник дублей).


@router.edited_business_message()
async def handle_edited_business_message(
    message: Message,
    bot: Bot,
    db: aiosqlite.Connection,
    settings: Settings,
    anthropic_client: AsyncAnthropic,
) -> None:
    """v4.27: правка входящего → владельцу «было → стало» (кэш; фича по запросу владельца)."""
    from_user = message.from_user
    from core import owners
    if not from_user or owners.is_owner(settings.owner_user_id, from_user.id):
        return  # свои правки не интересны
    new_text = (message.text or message.caption or "").strip() or "[медиа/без текста]"
    username = from_user.username or f"id{from_user.id}"
    key = (message.chat.id, message.message_id)
    old = (_MSG_CACHE.get(key) or {}).get("text")
    _cache_msg(message.chat.id, message.message_id, new_text, "in", username)
    if old == new_text:
        return  # правка без изменения текста (напр. только медиа) — не шумим
    tg_link = profile_link(username, from_user.id)
    if old:
        body = (f"Было: «{html.escape(old[:250])}»\n"
                f"Стало: «{html.escape(new_text[:250])}»")
    else:
        body = f"Теперь: «{html.escape(new_text[:250])}»\n<i>(исходный текст не застала)</i>"
    from core.topics import send_owner
    await send_owner(bot, settings.owner_user_id, "contacts",
                     f"✏️ <b>{contact_label(username, from_user.id, from_user.full_name)} изменил сообщение</b>\n{body}\n"
                     f"<a href=\"{tg_link}\">Открыть чат</a>", parse_mode="HTML")
    try:  # vault = источник правды: правка фиксируется
        await vault_add(db, from_user.id, username, "in", f"[правка] {new_text}", None)
    except Exception:
        pass
    logger.info(f"Edit notified: @{username}")


@router.deleted_business_messages()
async def handle_deleted_business_messages(event, bot: Bot, db: aiosqlite.Connection,
                                           settings: Settings) -> None:
    """v4.27: контакт удалил сообщение → владельцу УДАЛЁННЫЙ ТЕКСТ (из кэша).
    Свои удаления (direction=out) и неизвестные id — молчим (не шумим на себя)."""
    try:
        chat = getattr(event, "chat", None)
        ids = list(getattr(event, "message_ids", []) or [])
        if not chat or not ids:
            return
        deleted_in, uname = [], getattr(chat, "username", None)
        for mid in ids:
            c = _MSG_CACHE.pop((chat.id, mid), None)
            if c and c.get("direction") == "in":
                deleted_in.append(c["text"])
                uname = uname or c.get("username")
        if not deleted_in:
            logger.info(f"Deleted msgs in chat {chat.id}: {len(ids)} (не входящие/не в кэше — молчу)")
            return
        uname = uname or f"id{chat.id}"
        body = "\n".join(f"«{html.escape(t[:200])}»" for t in deleted_in[:5])
        from core.topics import send_owner
        await send_owner(bot, settings.owner_user_id, "contacts",
                         f"🗑 <b>@{html.escape(str(uname))} удалил сообщение</b>"
                         f"{'' if len(deleted_in) == 1 else f' ({len(deleted_in)})'}:\n{body}",
                         parse_mode="HTML")
        try:  # vault: удаление тоже история
            for t in deleted_in[:5]:
                await vault_add(db, chat.id, uname, "in", f"[удалил] {t}", None)
        except Exception:
            pass
        logger.info(f"Deletion notified: @{uname} ({len(deleted_in)} msg)")
    except Exception as exc:
        logger.debug(f"deleted handler error: {exc}")
