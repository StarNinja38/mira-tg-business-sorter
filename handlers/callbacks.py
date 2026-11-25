import aiohttp
import aiosqlite
from aiogram import Bot, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from loguru import logger

from config import Settings
from core import owners
from db import get_sync_by_id, mark_sync_done, mark_sync_done_by_id

router = Router(name="callbacks")

import time as _time
from core.llm import text_of as _llm_text   # v4.61: читать ответ по типу блока (Claude 5 может начать с thinking)
_CLICKS: dict = {}   # v4.36 (B1): (msg_id, data) → ts; второй тап по той же кнопке в течение 15с игнорируем


def _double_click(query, ttl: float = 15.0) -> bool:
    key = (getattr(query.message, "message_id", 0), query.data or "")
    now = _time.monotonic()
    for k in [k for k, t in _CLICKS.items() if now - t > ttl]:
        _CLICKS.pop(k, None)
    if key in _CLICKS:
        return True
    _CLICKS[key] = now
    return False


@router.callback_query(lambda c: c.data and c.data.startswith("drf:"))
async def cb_draft_reply(query: CallbackQuery, bot, settings: Settings) -> None:
    """Умные ответы: отправить черновик контакту (по кнопке) или проигнорировать."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    if _double_click(query):
        await query.answer("Уже делаю…"); return
    _, token, action = query.data.split(":")
    from core import drafts
    if action == "no":
        drafts.drop_draft(token)
        await query.answer("Не отправлено")
        try:
            await query.message.edit_text(query.message.html_text + "\n\n🗑 <b>Не отправлено</b>", reply_markup=None)
        except Exception:
            pass
        return
    if action == "teach":
        # «✍️ Отвечу сам» — взвести обучение: бот запомнит ответ владельца (в личку или в чате)
        uname = drafts.arm_teach_from_token(token, query.from_user.id)
        await query.answer("✍️ Ок, отвечу сам")
        try:
            await query.message.edit_text(
                query.message.html_text +
                f"\n\n✍️ <b>Отвечу сам.</b> Напиши свой вариант здесь, или просто ответь "
                f"@{uname or 'контакту'} в чате — запомню твой стиль для будущих черновиков.",
                reply_markup=None)
        except Exception:
            pass
        return
    ok, uname, err = await drafts.send_draft(bot, token)
    if not ok:
        if err == "expired":
            await query.answer("⚠️ Карточка устарела (черновик не пережил рестарт) — напиши сам", show_alert=True); return
        if "BUSINESS_PEER_USAGE_MISSING" in err:
            # v4.34.1: ограничение Telegram — писать через бота можно только контактам с недавней
            # активностью в диалоге. Отдаём текст кнопкой «скопировать» (copy_text, Bot API 7.11).
            item = drafts.peek_draft(token) or {}
            from aiogram.types import CopyTextButton
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Скопировать текст", copy_text=CopyTextButton(text=(item.get("text") or "")[:256]))],
                [InlineKeyboardButton(text="🗑 Убрать", callback_data=f"drf:{token}:no", style="danger")],
            ])
            try:
                await query.message.edit_text(
                    query.message.html_text + "\n\n⚠️ <b>Telegram не даёт боту писать этому контакту</b> — "
                    "он давно не писал тебе, а Business-бот может отвечать только в «живые» диалоги. "
                    "Скопируй и отправь сам:", reply_markup=kb)
            except Exception:
                pass
            await query.answer("Telegram запрещает — скопируй и отправь сам", show_alert=True); return
        await query.answer(f"Не отправилось: {err[:150]}", show_alert=True); return
    await query.answer("📤 Отправлено")
    try:
        await query.message.edit_text(query.message.html_text + f"\n\n📤 <b>Отправлено</b> @{uname}", reply_markup=None)
    except Exception:
        pass


async def _todoist_complete(token: str, task_id: str) -> bool:
    url = f"https://api.todoist.com/api/v1/tasks/{task_id}/close"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.post(url, headers={"Authorization": f"Bearer {token}"}) as r:
                return r.status in (200, 204)
    except Exception:
        return False


async def _todoist_delete(token: str, task_id: str) -> bool:
    url = f"https://api.todoist.com/api/v1/tasks/{task_id}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.delete(url, headers={"Authorization": f"Bearer {token}"}) as r:
                return r.status in (200, 204)
    except Exception:
        return False


@router.callback_query(lambda c: c.data and c.data.startswith("sub:del:"))
async def cb_sub_del(query: CallbackQuery, db: aiosqlite.Connection, settings: Settings) -> None:
    """v4.59: ✖ в карточке /subs — убрать подписку из списка (отменённые остаются в /subs all)."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно")
        return
    from core import subscriptions as _subs
    sid = int(query.data.split(":")[2])
    sub = await _subs.get(db, sid)
    if not sub:
        await query.answer("Уже нет в списке")
        return
    await _subs.cancel(db, sid)
    await query.answer(f"✖ {sub['name']} — убрала из подписок")
    try:
        await query.message.answer(f"✖ Подписка «{sub['name']}» убрана из списка. Вернуть: скажи Мире «добавь подписку {sub['name']}…»", parse_mode=None)
    except Exception:
        pass


@router.callback_query(lambda c: c.data and c.data.startswith("td:"))
async def cb_done(query: CallbackQuery, db: aiosqlite.Connection, settings: Settings) -> None:
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно")
        return
    sync_id = int(query.data.split(":")[1])
    row = await get_sync_by_id(db, sync_id)
    if not row:
        await query.answer("Задача не найдена")
        return
    had_external = bool(settings.todoist_api_token and row["todoist_id"])
    ok = False
    if had_external:
        ok |= await _todoist_complete(settings.todoist_api_token, row["todoist_id"])
    # Закрываем локально ВСЕГДА: иначе при сбое внешнего API задача/обещание висит вечно
    # (/promises и дайджест читают локальный synced). Todoist при сбое доберёт sync-петля.
    await mark_sync_done_by_id(db, sync_id)
    try:
        from db import set_setting as _ss
        await _ss(db, f"ownerclosed:{sync_id}", "button")   # v4.55: судья переоткрытия не вернёт закрытое владельцем
    except Exception:
        pass
    if had_external and not ok:
        await query.answer("✅ Закрыл локально; Todoist не ответил — закрой там вручную или дождись синка", show_alert=True)
    else:
        await query.answer("✅ Отмечено выполненным")
    try:
        await query.message.edit_text(
            query.message.html_text + "\n\n✅ <b>Выполнено</b>", reply_markup=None
        )
    except Exception:
        pass
    logger.info(f"Task {sync_id} marked done via button")


@router.callback_query(lambda c: c.data and c.data.startswith("tmr:"))
async def cb_tomorrow(query: CallbackQuery, db: aiosqlite.Connection, settings: Settings) -> None:
    """v4.23: «→ завтра» из вечернего итога — перенос задачи на завтра (Todoist+календарь+БД).
    v4.24-бонус: при ≥3 переносах Мира мягко предлагает переосмыслить задачу."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    sync_id = int(query.data.split(":")[1])
    from datetime import date, timedelta
    from handlers.owner import _apply_reschedule
    from core.util import today_local as _tl
    applied = await _apply_reschedule(db, settings, sync_id, (_tl() + timedelta(days=1)).isoformat())   # C14: локальная дата
    if not applied:
        await query.answer("Задача не найдена или уже закрыта", show_alert=True); return
    from db import bump_reschedule
    n = await bump_reschedule(db, sync_id)
    if n >= 3:
        await query.answer(f"→ Перенесла. Это уже {n}-й перенос — может, разбить задачу или снять её?",
                           show_alert=True)
    else:
        await query.answer("→ Перенесла на завтра")
    logger.info(f"Task {sync_id} → tomorrow via evening wrap (reschedules={n})")


@router.callback_query(lambda c: c.data and c.data.startswith("mtg:"))
async def cb_meeting_offer(query: CallbackQuery, db: aiosqlite.Connection, settings: Settings) -> None:
    """v4.26: владелец выбрал слот → Мира пишет контакту от его имени (business-коннект)."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    import json as _json
    import time as _time
    from db import get_setting
    _, token, idx = query.data.split(":")
    if _double_click(query):
        await query.answer("Уже делаю…"); return
    raw = await get_setting(db, f"mtgo:{token}")
    if not raw:
        await query.answer("Это уже неактуально", show_alert=True); return
    try:
        p = _json.loads(raw)
        slot = p["slots"][int(idx)]
    except Exception:
        await query.answer("Данные повреждены", show_alert=True); return
    if _time.time() - (p.get("ts") or 0) > 2 * 86400:
        await query.answer("Слоты устарели (>2 дней) — время могло занятья", show_alert=True); return
    # v4.36 (B3): bc_id того аккаунта, куда писал контакт — из payload; глобальный — фолбэк
    bc_id = p.get("bc_id") or await get_setting(db, "bc_id")
    if not bc_id:
        await query.answer("Нет business-подключения", show_alert=True); return
    from core.meeting import fmt_slot
    label = fmt_slot(slot)
    # v4.36 (B1) claim-first: снимаем ключ ДО отправки (второй клик → rowcount 0), при сбое возвращаем
    try:
        cur = await db.execute("DELETE FROM settings_kv WHERE key=?", (f"mtgo:{token}",)); await db.commit()
        if cur.rowcount != 1:
            await query.answer("Уже сделала"); return
    except Exception:
        await query.answer("Что-то сорвалось, нажми ещё раз", show_alert=True); return
    try:
        await query.message.bot.send_message(
            chat_id=p["uid"], business_connection_id=bc_id,
            text=f"Привет! Могу {label} — подойдёт?", parse_mode=None)
    except Exception as exc:
        logger.warning(f"meeting send failed @{p.get('username')}: {exc}")
        from db import set_setting as _ss
        await _ss(db, f"mtgo:{token}", raw)
        await query.answer("Не отправилось: Telegram закрыл окно ответа", show_alert=True); return
    try:
        await query.message.edit_text(
            query.message.html_text + f"\n\n📤 <b>Предложила: {label}</b>", reply_markup=None)
    except Exception:
        pass
    await query.answer(f"📤 Предложила {label}")
    logger.info(f"Meeting slot sent to @{p.get('username')}: {slot}")


@router.callback_query(lambda c: c.data and c.data.startswith("mtgno:"))
async def cb_meeting_self(query: CallbackQuery, db: aiosqlite.Connection, settings: Settings) -> None:
    """v4.26: «✍️ Отвечу сам» — снять кнопки, удалить токен."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    token = query.data.split(":")[1]
    try:
        await db.execute("DELETE FROM settings_kv WHERE key=?", (f"mtgo:{token}",)); await db.commit()
    except Exception:
        pass
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.answer("Ок, ответишь сам")


@router.callback_query(lambda c: c.data and c.data.startswith("snz:"))
async def cb_snooze(query: CallbackQuery, db: aiosqlite.Connection, settings: Settings) -> None:
    """v4.25 snooze: отложить карточку. В срок Мира пришлёт её ЗАНОВО с живыми кнопками
    (copy_message теряет клавиатуру — поэтому храним текст+markup и пере-отправляем)."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    import json as _json
    import time as _time
    from datetime import timedelta
    from core.util import now_local
    mode = query.data.split(":")[1]
    now = now_local()
    if mode == "h1":
        fire, label = int(_time.time()) + 3600, "через час"
    elif mode == "eve":
        t = now.replace(hour=21, minute=0, second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        fire, label = int(t.timestamp()), "в 21:00"
    else:  # morn
        t = (now + timedelta(days=1)).replace(hour=9, minute=5, second=0, microsecond=0)
        fire, label = int(t.timestamp()), "завтра утром"
    m = query.message
    payload = {"chat_id": m.chat.id,
               "text": (m.html_text or m.text or "…")[:3800],
               "kb": m.reply_markup.model_dump_json() if m.reply_markup else None,
               "thread": m.message_thread_id}
    from db import set_setting
    await set_setting(db, f"rmd:{fire}:{m.message_id}", _json.dumps(payload, ensure_ascii=False))
    try:    # кап 50 активных напоминаний
        await db.execute(
            "DELETE FROM settings_kv WHERE key LIKE 'rmd:%' AND rowid NOT IN "
            "(SELECT rowid FROM settings_kv WHERE key LIKE 'rmd:%' ORDER BY rowid DESC LIMIT 50)")
        await db.commit()
    except Exception:
        pass
    await query.answer(f"🔔 Напомню {label}")
    try:
        await m.edit_text(m.html_text + f"\n\n🔔 <i>отложено ({label})</i>", reply_markup=None)
    except Exception:
        pass
    logger.info(f"Card snoozed until ts={fire} ({label})")


@router.callback_query(lambda c: c.data and c.data.startswith("keepopen:"))
async def cb_keep_open(query: CallbackQuery, db: aiosqlite.Connection, settings: Settings) -> None:
    """P0: владелец отклонил предложение авто-закрытия → задача остаётся открытой.
    НЕ глушим надолго: done-статус меняется день в день, и предложение приходит по новым
    признакам в переписке. От повторов в тот же день защищает суточный guard timeclose:{id}."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно")
        return
    sync_id = query.data.split(":")[1]
    await query.answer("Ок, оставил открытой")
    try:
        await query.message.edit_text(
            query.message.html_text + "\n\n↩️ <b>Оставлено открытой</b>", reply_markup=None
        )
    except Exception:
        pass
    logger.info(f"Task {sync_id} kept open by owner (close suggestion dismissed)")


@router.callback_query(lambda c: c.data and c.data.startswith("rm:"))
async def cb_delete(query: CallbackQuery, db: aiosqlite.Connection, settings: Settings) -> None:
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно")
        return
    sync_id = int(query.data.split(":")[1])
    row = await get_sync_by_id(db, sync_id)
    if not row:
        await query.answer("Задача не найдена")
        return
    if settings.todoist_api_token and row["todoist_id"]:
        await _todoist_delete(settings.todoist_api_token, row["todoist_id"])
    # Удаляем и событие календаря (при ручном 🗑) — иначе остаётся сирота с попап-напоминаниями.
    if row.get("calendar_event_id") and settings.google_calendar_id:
        try:
            from core.calendar import delete_calendar_event
            await delete_calendar_event(settings.google_service_account_path,
                                        settings.google_calendar_id, row["calendar_event_id"])
        except Exception as exc:
            logger.debug(f"calendar delete skip: {exc}")
    await mark_sync_done_by_id(db, sync_id)
    # Обучение (Фаза 2): быстрое удаление (<30 мин после создания) = «бот ошибся, это НЕ задача».
    # Запоминаем как анти-пример для извлечения. Удаление спустя время = отмена легитимной задачи (не учим).
    try:
        ca = row.get("created_at")
        if ca:
            from datetime import datetime, timezone
            from db import save_task_feedback
            t0 = datetime.fromisoformat(str(ca).replace(" ", "T"))
            if t0.tzinfo is None:
                t0 = t0.replace(tzinfo=timezone.utc)
            age_min = (datetime.now(timezone.utc) - t0).total_seconds() / 60
            if age_min < 30 and row.get("title"):
                await save_task_feedback(db, row["title"], "rejected")
                logger.info(f"Task feedback: '{row['title']}' rejected (deleted in {age_min:.0f}m) — learned as non-task")
    except Exception as exc:
        logger.debug(f"task feedback skip: {exc}")
    await query.answer("🗑 Удалено")
    try:
        await query.message.edit_text(
            query.message.html_text + "\n\n🗑 <b>Удалено</b>", reply_markup=None
        )
    except Exception:
        pass
    logger.info(f"Task {sync_id} deleted via button")


@router.callback_query(lambda c: c.data and c.data.startswith("mk:"))
async def cb_make_task(query: CallbackQuery, db: aiosqlite.Connection, settings: Settings) -> None:
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно")
        return
    if _double_click(query):
        await query.answer("Уже делаю…"); return
    user_id = int(query.data.split(":")[1])
    from db import get_contact_profile, vault_get_context, save_task_sync
    from core.actions import create_todoist_task

    profile = await get_contact_profile(db, user_id) or {}
    uname = profile.get("username") or f"id{user_id}"
    ctx = await vault_get_context(db, user_id, limit=6)
    title = f"Ответить @{uname}"
    from core.util import profile_link
    tg_link = profile_link(profile.get("username"), user_id)
    desc = f"Из переписки:\n{ctx}\n\n{tg_link}"

    # Backstop от двойного нажатия «➕ В задачу» — не плодим дубль «Ответить @user»
    from db import task_exists_recently as _ter
    if await _ter(db, user_id, title):
        await query.answer("✅ Уже есть такая задача"); return

    todoist_id = None
    if settings.todoist_api_token:
        from core.actions import todoist_section, _category_from_relationship
        from db import get_contact_profile as _gcp
        _prof = await _gcp(db, user_id) or {}
        _cat = _category_from_relationship(_prof.get("relationship")) or "UNCLEAR"
        todoist_id = await create_todoist_task(settings.todoist_api_token, title, desc, None, settings.todoist_project_id,
                                               section_id=todoist_section(settings, category=_cat))   # v4.46
    # Регистрируем даже при частичном создании — иначе задача-сирота:
    # невидима дедупу/реконсиляции/закрытию (правило идемпотентности CLAUDE.md).
    if todoist_id:
        await save_task_sync(db, todoist_id, None, user_id=user_id, title=title)

    if todoist_id:
        await query.answer("Записала ✅")
        try:
            await query.message.edit_text(query.message.html_text + "\n\n✅ <b>Записала</b>", reply_markup=None)
        except Exception:
            pass
    else:
        await query.answer("Не записалось, нажми ещё раз", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("fu_ok:"))
async def cb_followup_done(query: CallbackQuery, db: aiosqlite.Connection, settings: Settings) -> None:
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно")
        return
    user_id = int(query.data.split(":")[1])
    from db import mark_followup_done
    await mark_followup_done(db, user_id)
    await query.answer("✅ Отмечено")
    try:
        await query.message.edit_text(query.message.html_text + "\n\n✅ <b>Отвечено</b>", reply_markup=None)
    except Exception:
        pass


def _task_kb(sync_id: int) -> InlineKeyboardMarkup:
    """Базовая клавиатура карточки задачи (для возврата из подменю переноса)."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Выполнено", callback_data=f"td:{sync_id}", style="success"),
        InlineKeyboardButton(text="📅 Перенести", callback_data=f"snm:{sync_id}", style="primary"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"rm:{sync_id}", style="danger"),
    ]])


@router.callback_query(lambda c: c.data and c.data.startswith("snm:"))
async def cb_snooze_menu(query: CallbackQuery, settings: Settings) -> None:
    """«📅 Перенести» → подменю: Завтра / +3 дня / Понедельник / Назад."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    sync_id = int(query.data.split(":")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Завтра", callback_data=f"sn:{sync_id}:1", style="primary"),
         InlineKeyboardButton(text="📅 +3 дня", callback_data=f"sn:{sync_id}:3", style="primary")],
        [InlineKeyboardButton(text="📅 Понедельник", callback_data=f"sn:{sync_id}:mon", style="primary"),
         InlineKeyboardButton(text="✏️ Своя дата/время", callback_data=f"snc:{sync_id}", style="primary")],
        [InlineKeyboardButton(text="↩ Назад", callback_data=f"snb:{sync_id}", style="danger")],
    ])
    await query.answer()
    try:
        await query.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass


@router.callback_query(lambda c: c.data and c.data.startswith("snc:"))
async def cb_snooze_custom(query: CallbackQuery, db: aiosqlite.Connection, settings: Settings) -> None:
    """«✏️ Своя дата/время» → ждём от владельца текст с датой/временем (перехват в owner_inbox)."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    sync_id = int(query.data.split(":")[1])
    from datetime import datetime, timezone
    from db import set_setting
    # запоминаем, какую задачу переносим и когда запросили (для протухания)
    await set_setting(db, "reschedule_await", f"{sync_id}:{datetime.now(timezone.utc).isoformat()}")
    await query.answer()
    try:
        await query.message.reply(
            "✏️ Напиши новую дату и время — например «завтра 14:00», «18 июня 13:00», «в пятницу 9:30».\n"
            "Отмена — напиши «отмена».")
    except Exception:
        pass


@router.callback_query(lambda c: c.data and c.data.startswith("snb:"))
async def cb_snooze_back(query: CallbackQuery, settings: Settings) -> None:
    """↩ Назад из подменю переноса → вернуть базовую клавиатуру."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    sync_id = int(query.data.split(":")[1])
    await query.answer()
    try:
        await query.message.edit_reply_markup(reply_markup=_task_kb(sync_id))
    except Exception:
        pass


@router.callback_query(lambda c: c.data and c.data.startswith("sn:"))
async def cb_snooze(query: CallbackQuery, db: aiosqlite.Connection, settings: Settings) -> None:
    """Перенести дедлайн задачи (Todoist + bot.db + календарь): sn:{id}:{1|3|mon}."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    parts = query.data.split(":")
    sync_id = int(parts[1])
    when = parts[2] if len(parts) > 2 else "1"
    row = await get_sync_by_id(db, sync_id)
    if not row:
        await query.answer("Задача не найдена"); return
    from datetime import timedelta
    from core.util import today_local
    t = today_local()
    if when == "3":
        d = t + timedelta(days=3)
    elif when == "mon":
        d = t + timedelta(days=((0 - t.weekday()) % 7) or 7)  # ближайший понедельник
    else:
        d = t + timedelta(days=1)
    due = d.isoformat()
    # Если у задачи было ВРЕМЯ (встреча 17:00) — сохраняем его при переносе на новый день,
    # иначе timed-событие в календаре стало бы all-day. due → 'YYYY-MM-DDTHH:MM'.
    old_due = row.get("due_date")
    if old_due and "T" in str(old_due):
        due = f"{d.isoformat()}T{str(old_due).split('T', 1)[1]}"
    from core.actions import update_todoist_task, update_task_calendar
    ok = False
    if settings.todoist_api_token and row["todoist_id"]:
        ok |= await update_todoist_task(settings.todoist_api_token, row["todoist_id"], due)
    if not ok:
        await query.answer("Не перенеслось, нажми ещё раз", show_alert=True); return
    try:
        from db import set_task_due
        await set_task_due(db, sync_id, due)
    except Exception:
        pass
    # Календарь: было пропущено — событие оставалось на старой дате. Теперь двигаем (или создаём).
    try:
        await update_task_calendar(db, row, row["title"], due)
    except Exception as exc:
        logger.debug(f"snooze calendar update skip: {exc}")
    from core.util import html_due
    await query.answer("📅 Перенесено")
    try:
        await query.message.edit_text(
            query.message.html_text + f"\n\n📅 <b>Перенесено на {html_due(due)}</b>", reply_markup=None)
    except Exception:
        pass
    logger.info(f"Task {sync_id} snoozed to {due} ({when}) via button")


@router.callback_query(lambda c: c.data and c.data.startswith("sch:"))
async def cb_schedule_pick(query: CallbackQuery, db: aiosqlite.Connection, settings: Settings, bot) -> None:
    """Выбран слот → создать событие в календаре + подготовить черновик контакту (📤 для отправки)."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    _, token, idx = query.data.split(":")
    from core import scheduler
    if _double_click(query):
        await query.answer("Уже делаю…"); return
    item = scheduler.get(token)
    if not item:
        await query.answer("Карточка устарела", show_alert=True); return
    try:
        s, _e = item["slots"][int(idx)]
    except (IndexError, ValueError):
        await query.answer("Слот не найден"); return
    due = s.strftime("%Y-%m-%dT%H:%M")
    summary = f"Встреча с @{item['username']}"
    eid = None
    try:
        from core.calendar import create_calendar_event
        eid = await create_calendar_event(
            settings.google_service_account_path, settings.google_calendar_id,
            settings.timezone, summary=summary, due=due,
            description="Запланировано через бота (/schedule)")
    except Exception as exc:
        logger.error(f"schedule create_event failed: {exc}")
    from core.util import human_due
    when = human_due(due)
    from db import get_setting
    bc_id = await get_setting(db, "bc_id")
    offered = False
    if bc_id:
        from core import drafts
        try:
            offered = await drafts.offer_fixed_draft(
                bot, settings.owner_user_id, bc_id, item["user_id"], item["username"],
                f"Давай {when}? 🙂", note=f"встреча, {item['mins']} мин")
        except Exception as exc:
            logger.error(f"schedule offer_draft failed: {exc}")
    scheduler.drop(token)
    await query.answer("📅 Запланировано")
    status = (f"\n\n✅ <b>Событие: {when}</b>"
              + ("\n💬 Черновик контакту готов — отправь 📤 в «Черновики»" if offered
                 else "\n<i>(нет бизнес-подключения — напиши контакту сам)</i>"))
    try:
        await query.message.edit_text(query.message.html_text + status, reply_markup=None)
    except Exception:
        pass
    logger.info(f"Scheduled meeting with @{item['username']} at {due} (event={eid}, draft={offered})")


@router.callback_query(lambda c: c.data and c.data.startswith("info:"))
async def cb_contact_memory(query: CallbackQuery, db: aiosqlite.Connection, settings: Settings) -> None:
    """«📋 Память» на карточке контакта → прислать что бот знает о человеке (rich)."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    user_id = int(query.data.split(":")[1])
    from db import get_contact_profile, vault_get_context, get_open_tasks_for_user
    import html as _html
    prof = await get_contact_profile(db, user_id) or {}
    if not prof:
        await query.answer("Пока нет данных о контакте", show_alert=True); return
    ctx = await vault_get_context(db, user_id, limit=8)
    open_tasks = await get_open_tasks_for_user(db, user_id)
    from core.vault import _coarse_notion_type
    rows = []
    if prof.get("name"):
        rows.append(["Имя", _html.escape(str(prof["name"]))])
    rows.append(["Тип", _html.escape(prof.get("contact_type") or _coarse_notion_type(prof.get("relationship") or ""))])
    rows.append(["Роль", _html.escape(str(prof.get("relationship") or "—"))])
    rows.append(["Сообщений", str(prof.get("message_count", 0))])
    if prof.get("intent"):
        rows.append(["Зачем пишет", _html.escape(str(prof["intent"]))])
    uname = prof.get("username") or f"id{user_id}"
    from core.topics import thread_id
    from core import contact_memory as _dos
    await query.answer("📋 Память ниже")
    from core import richmsg as _rm
    if not await _rm.send_rich(settings.bot_token, settings.owner_user_id,
                               _dos.render_overview(dict(prof, user_id=user_id), open_tasks,
                                                    people=await _dos.persons_linked(db, user_id),
                                                    rhythm_line=_dos.rhythm_text(await _dos.rhythm(db, user_id))),
                               thread_id("contacts")):   # v4.51: обзор + 📂; 3.0: люди вокруг + ритм
        from core.topics import send_owner
        txt = f"📋 <b>Память @{uname}</b>\n"
        for k, v in rows:
            txt += f"{k}: {v}\n"
        if prof.get("summary"):
            txt += f"\n🧠 {prof['summary'][:500]}"
        await send_owner(query.bot, settings.owner_user_id, "contacts", txt, disable_web_page_preview=True)


@router.callback_query(lambda c: c.data and c.data.startswith("fr:"))
async def cb_friend(query: CallbackQuery, db: aiosqlite.Connection, settings: Settings) -> None:
    """Новый контакт → в whitelist (друг) одной кнопкой."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    user_id = int(query.data.split(":")[1])
    from core import whitelist
    from db import get_contact_profile
    prof = await get_contact_profile(db, user_id) or {}
    uname = prof.get("username") or f"id{user_id}"
    await whitelist.add(db, user_id=user_id, username=uname, reason="button")
    await query.answer("⭐ Добавлен в друзья")
    try:
        await query.message.edit_text(query.message.html_text + "\n\n⭐ <b>В whitelist (друг)</b>", reply_markup=None)
    except Exception:
        pass
    logger.info(f"Contact {user_id} (@{uname}) whitelisted via button")


@router.callback_query(lambda c: c.data and c.data.startswith("sp:"))
async def cb_spam(query: CallbackQuery, db: aiosqlite.Connection, settings: Settings) -> None:
    """Новый контакт → пометить SPAM (бот запомнит пример для классификатора)."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    user_id = int(query.data.split(":")[1])
    async with db.execute(
        "SELECT id, username, message_text FROM message_log WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
        (user_id,),
    ) as cur:
        row = await cur.fetchone()
    msg_id = row[0] if row else None
    uname = (row[1] if row else None) or f"id{user_id}"
    last_text = (row[2] if row else "") or ""
    from db import save_correction
    await save_correction(db, user_id, uname, last_text, "SPAM")
    if msg_id is not None:  # метим ИМЕННО то сообщение, чей текст ушёл в коррекцию (не повторный ORDER BY)
        await db.execute("UPDATE message_log SET category = 'SPAM' WHERE id = ?", (msg_id,))
        await db.commit()
    await query.answer("🚫 Помечено спамом")
    try:
        await query.message.edit_text(query.message.html_text + "\n\n🚫 <b>SPAM</b> (бот запомнил)", reply_markup=None)
    except Exception:
        pass
    logger.info(f"Contact {user_id} (@{uname}) marked SPAM via button")


@router.callback_query(lambda c: c.data == "ig")
async def cb_ignore(query: CallbackQuery, settings: Settings) -> None:
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно")
        return
    await query.answer("Пропущено")
    try:
        await query.message.edit_text(
            query.message.html_text + "\n\n🔕 <b>Пропущено</b>", reply_markup=None
        )
    except Exception:
        pass


@router.callback_query(lambda c: c.data and c.data.startswith("pho:set:"))
async def cb_photo_set(query: CallbackQuery, db: aiosqlite.Connection, settings: Settings, bot) -> None:
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    from core import photos as _photos
    token = query.data.split(":", 2)[2]
    item = _photos.pop(token)
    if not item:
        await query.answer("Истекло — пришли фото заново", show_alert=True); return
    await _photos.attach_photo(bot, db, settings, item["user_id"], item["username"], item["file_id"], src="confirmed")
    try:
        await query.message.edit_caption(caption=f"✅ Фото @{item['username']} добавлено в памяти")
    except Exception:
        pass
    await query.answer("Добавила ✅")
    logger.info(f"ContactMemory photo set for @{item['username']}")


@router.callback_query(lambda c: c.data and c.data.startswith("pho:no:"))
async def cb_photo_no(query: CallbackQuery, settings: Settings) -> None:
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    from core import photos as _photos
    _photos.pop(query.data.split(":", 2)[2])
    try:
        await query.message.edit_caption(caption="❌ Не добавлено в памяти")
    except Exception:
        pass
    await query.answer("Ок")


# ── Генератор постов (/post) ────────────────────────────────────────────────
@router.callback_query(lambda c: c.data and c.data.startswith("somiss:"))
async def cb_somiss_create(query: CallbackQuery, db, settings: Settings) -> None:
    """➕ Создать задачи, предложенные «вторым мнением» (v4.4). Дедуп сделан при предложении."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    import json as _json, time as _time
    from db import get_setting
    from core.actions import create_task_from_suggestion
    token = query.data.split(":", 1)[1]
    _key = f"somiss:{token}"
    raw = await get_setting(db, _key)
    if not raw:
        await query.answer("Это уже неактуально", show_alert=True); return
    try:
        payload = _json.loads(raw)
    except Exception:
        await query.answer("Данные повреждены", show_alert=True); return
    ts = payload.get("ts") or 0
    # Атомарный claim ДО создания: удаляем ключ и проверяем rowcount — выигрывает только один
    # клик, payload уже в памяти. Гасит гонку двойного клика → дубли (bug #2). SQLite сериализует
    # запись: у второго клика rowcount=0 → выходит.
    try:
        cur = await db.execute("DELETE FROM settings_kv WHERE key=?", (_key,)); await db.commit()
        if cur.rowcount != 1:
            await query.answer("Уже сделала"); return
    except Exception:
        await query.answer("Что-то сорвалось, нажми ещё раз", show_alert=True); return
    if ts and _time.time() - ts > 3 * 86400:      # предложения живут 3 дня (ключ уже удалён выше)
        await query.answer("Прошло больше трёх дней, уже неактуально", show_alert=True); return
    bot = query.message.bot if query.message else None
    tasks = payload.get("tasks", [])
    ok = dup = fail = 0
    for task in tasks:
        try:
            r = await create_task_from_suggestion(
                db, settings, bot, settings.owner_user_id,
                payload["user_id"], payload.get("username") or str(payload["user_id"]),
                payload.get("category") or "BUSINESS", task)
            if r == "ok":
                ok += 1
            elif r == "dup":
                dup += 1
            else:
                fail += 1
        except Exception as exc:
            fail += 1
            logger.warning(f"somiss create skip: {exc}")
    # bug #1: ничего не создано И была реальная неудача (не «всё уже есть») → ВЕРНУТЬ ключ для
    # ретрая. При dup-only ключ НЕ возвращаем (задачи и так на месте).
    if ok == 0 and fail > 0:
        try:
            from db import set_setting
            await set_setting(db, _key, raw)
        except Exception:
            pass
        await query.answer("Не удалось создать (сервис недоступен) — попробуй позже", show_alert=True)
        logger.warning(f"Second-opinion: creation FAILED uid={payload.get('user_id')} — токен возвращён")
        return
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    _msg = f"✅ Создала: {ok}" + (f" · уже было: {dup}" if dup else "")
    await query.answer(_msg if (ok or dup) else "Нет задач")
    logger.info(f"Second-opinion: uid={payload.get('user_id')} ok={ok} dup={dup} fail={fail}")


@router.callback_query(lambda c: c.data and c.data.startswith("somissno:"))
async def cb_somiss_no(query: CallbackQuery, db, settings: Settings) -> None:
    """✖ Отклонить предложение второго мнения — удалить токен, убрать кнопки."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    token = query.data.split(":", 1)[1]
    try:
        await db.execute("DELETE FROM settings_kv WHERE key=?", (f"somiss:{token}",)); await db.commit()
    except Exception:
        pass
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.answer("Ок, пропускаю")


# ── v4.30: пинг молчащему + поздравление с др (черновик → отправка только по 📤) ──
async def _fixed_draft_flow(query: CallbackQuery, db, settings, uid: int,
                            username: str, draft: str, note: str) -> None:
    """v4.40 (B7) «сценарий в одном пузыре»: исходная карточка РЕДАКТИРУЕТСЯ в черновик
    (📤/🗑), а по 📤 — в «Отправлено» (cb_draft_reply). Probe 08.09: editMessageText работает и на
    rich-сообщениях, эфемерная замена в личке — нет (MESSAGE_ID_INVALID). Исключение — кнопка
    внутри большой сводки: её не трогаем, черновик рождается рядом в той же теме и эволюционирует сам."""
    import html as _h
    from core import drafts
    from core.util import profile_link
    from db import get_setting
    bc_id = await get_setting(db, "bc_id")
    token = drafts.stash_fixed(bc_id, uid, username, draft)
    if not token:
        await query.answer("⚠️ Нет business-соединения — не смогу отправить", show_alert=True)
        return
    _lbl = f"@{_h.escape(str(username))}" if not str(username).startswith("id") else f"id{uid}"
    body = (f'💬 <b>Предложить <a href="{profile_link(username, uid)}">{_lbl}</a>:</b>\n\n«{_h.escape(draft)}»'
            + (f"\n\n<i>{_h.escape(note)}</i>" if note else ""))
    msg = query.message
    src = (getattr(msg, "html_text", None) or getattr(msg, "text", None) or "")
    is_digest = len(src) > 1500 or "Сводка от Миры" in src or "Дайджест" in src[:80]
    if not is_digest:
        try:
            await msg.edit_text(body, reply_markup=drafts.fixed_kb(token), parse_mode="HTML")
            await query.answer("✍️ Черновик готов — жми 📤")
            return
        except Exception as exc:
            logger.debug(f"in-place draft edit failed → new card: {exc}")
    # кнопка внутри сводки (или edit не прошёл) → новый пузырь В ТОЙ ЖЕ теме, дальше он эволюционирует сам
    await query.message.bot.send_message(
        chat_id=msg.chat.id, text=body, reply_markup=drafts.fixed_kb(token), parse_mode="HTML",
        message_thread_id=getattr(msg, "message_thread_id", None))
    await query.answer("✍️ Черновик ниже — жми 📤")


@router.callback_query(lambda c: c.data and c.data.startswith("png:"))
async def cb_ping_draft(query: CallbackQuery, db, settings: Settings, anthropic_client) -> None:
    """🔕 «Черновик пинга»: Мира пишет мягкое напоминание молчащему контакту от лица владельца."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    from core import drafts, llm
    from core.usage import record as _rec
    from db import get_contact_profile
    if _double_click(query):
        await query.answer("Уже пишу…"); return
    uid = int(query.data.split(":", 1)[1])
    prof = await get_contact_profile(db, uid) or {}
    username = prof.get("username") or str(uid)
    async with db.execute(
            "SELECT text FROM conversations WHERE user_id=? AND direction='out' "
            "ORDER BY id DESC LIMIT 1", (uid,)) as cur:
        row = await cur.fetchone()
    question = (row[0] if row else "") or ""
    try:
        resp = await llm.create(anthropic_client,
            model=settings.anthropic_model_smart, max_tokens=120,
            system=("Ты пишешь КОРОТКИЙ мягкий пинг от лица владельца (мужчина) контакту, который "
                    "не ответил на его вопрос. 1–2 предложения, без укора и канцелярита. "
                    "ГЛАВНОЕ: пиши ТОЧНО в манере владельца по образцам его сообщений этому человеку — "
                    "то же обращение и приветствие, те же ты/вы, длина и эмодзи; не выдумывай чужой сленг "
                    "(«хай», «жива?»), если его нет в образцах. Верни ТОЛЬКО текст сообщения."),
            messages=[{"role": "user", "content":
                f"Контакт: {prof.get('name') or username} ({prof.get('relationship') or '—'}).\n"
                f"Неотвеченный вопрос владельца: «{question[:300]}»\n\n"
                f"{await drafts.owner_voice(db, uid)}"}])
        await _rec(settings.anthropic_model_smart, resp.usage, "ping")
        draft = (_llm_text(resp) or "").strip()
    except Exception as exc:
        logger.warning(f"ping draft failed: {exc}")
        await query.answer("Не получилось написать черновик 🙈", show_alert=True); return
    await _fixed_draft_flow(query, db, settings, uid, username, draft, note="пинг: не отвечает на твой вопрос")


@router.callback_query(lambda c: c.data and c.data.startswith("pngno:"))
async def cb_ping_no(query: CallbackQuery, settings: Settings) -> None:
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.answer("Ок, не трогаем")


@router.callback_query(lambda c: c.data and c.data.startswith("bdg:"))
async def cb_bday_greet(query: CallbackQuery, db, settings: Settings, anthropic_client) -> None:
    """🎂 «Поздравление»: Мира пишет поздравление; контакт в TG → drafts-flow, иначе текст для копирования."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    import json as _json
    from core import drafts, llm
    from core.usage import record as _rec
    from db import get_contact_profile, get_setting
    if _double_click(query):
        await query.answer("Уже пишу…"); return
    slug = query.data.split(":", 1)[1]
    raw = await get_setting(db, f"bday:{slug}")
    if not raw:
        await query.answer("Запись не нашлась", show_alert=True); return
    b = _json.loads(raw)
    uid = b.get("uid")
    prof = (await get_contact_profile(db, uid) or {}) if uid else {}
    try:
        from core.dates import turning_age
        from core.util import today_local
        age = turning_age(b, today_local().year)
        resp = await llm.create(anthropic_client,
            model=settings.anthropic_model_smart, max_tokens=200,
            system=("Ты пишешь поздравление с днём рождения от лица владельца (мужчина). "
                    "Тёпло, по-человечески, без пафоса и открыточных штампов, 2–3 предложения, "
                    "уместные эмодзи. Пиши в манере владельца по образцам его сообщений этому человеку "
                    "(обращение, ты/вы, эмодзи). Верни ТОЛЬКО текст поздравления."),
            messages=[{"role": "user", "content":
                f"Именинник: {b.get('who')} ({prof.get('relationship') or 'близкий человек'})."
                + (f" Исполняется {age}." if age else "")
                + (f"\nФакты о нём: {(prof.get('facts') or '')[:400]}" if prof.get("facts") else "")
                + (f"\n\n{await drafts.owner_voice(db, uid)}" if uid else "")}])
        await _rec(settings.anthropic_model_smart, resp.usage, "bday")
        draft = (_llm_text(resp) or "").strip()
    except Exception as exc:
        logger.warning(f"bday draft failed: {exc}")
        await query.answer("Не получилось написать 🙈", show_alert=True); return
    if uid:
        await _fixed_draft_flow(query, db, settings, uid,
                                prof.get("username") or str(uid), draft, note="поздравление с др 🎂")
    else:  # контакта нет в TG-переписках — отдаём текст, отправит сам (WhatsApp/лично)
        import html as _h
        await query.message.reply(f"🎂 Черновик для {_h.escape(b.get('who') or '')}:\n\n"
                                  f"«{_h.escape(draft)}»\n\n<i>скопируй и отправь, где удобно</i>",
                                  parse_mode="HTML")
        await query.answer("Готово ✍️")
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass


# ── v4.37: роль нового контакта — «бот говорит, ты киваешь» ─────────────────────
async def _role_done(query: CallbackQuery, db, uid: int, role: str) -> None:
    from core import roles as _roles
    import html as _h
    cat = await _roles.apply_role(db, uid, role)
    try:
        await query.message.edit_text(
            query.message.html_text + f"\n\n✓ <b>{_h.escape(role)}</b> · {_roles.cat_label(cat)}", reply_markup=None)
    except Exception:
        pass
    await query.answer(f"✓ {role}")


@router.callback_query(lambda c: c.data and c.data.startswith("rolok:"))
async def cb_role_ok(query: CallbackQuery, db, settings: Settings) -> None:
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    if _double_click(query):
        await query.answer("Уже…"); return
    from core import roles as _roles
    uid = int(query.data.split(":")[1])
    g = await _roles.load_guess(db, uid)
    if not g.get("role"):
        await query.answer("Догадка не сохранилась — нажми «Поправить»", show_alert=True); return
    await _role_done(query, db, uid, g["role"])


@router.callback_query(lambda c: c.data and c.data.startswith("rolfix:"))
async def cb_role_fix(query: CallbackQuery, db, settings: Settings) -> None:
    """Меню правки: 3 частые роли той же категории из CRM + Друг + своя (текстом)."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    import json as _json
    from core import roles as _roles
    from db import set_setting
    uid = int(query.data.split(":")[1])
    g = await _roles.load_guess(db, uid)
    opts = await _roles.top_roles(db, g.get("category") or "UNCLEAR", limit=3)
    if g.get("role") and g["role"] not in opts:
        opts = ([g["role"]] + opts)[:3]
    await set_setting(db, f"rolopt:{uid}", _json.dumps(opts, ensure_ascii=False))
    rows = [[InlineKeyboardButton(text=o[:28], callback_data=f"rolset:{uid}:{i}") for i, o in enumerate(opts)]] if opts else []
    rows.append([InlineKeyboardButton(text="👥 Друг", callback_data=f"rolset:{uid}:f", style="success"),
                 InlineKeyboardButton(text="✏️ Своя роль…", callback_data=f"rolown:{uid}", style="primary")])
    try:
        await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        pass
    await query.answer("Кем он тебе приходится?")


@router.callback_query(lambda c: c.data and c.data.startswith("rolset:"))
async def cb_role_set(query: CallbackQuery, db, settings: Settings) -> None:
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    if _double_click(query):
        await query.answer("Уже…"); return
    import json as _json
    from db import get_setting
    _, uid, idx = query.data.split(":")
    uid = int(uid)
    if idx == "f":
        role = "друг"
    else:
        opts = _json.loads(await get_setting(db, f"rolopt:{uid}") or "[]")
        try:
            role = opts[int(idx)]
        except Exception:
            await query.answer("Вариант устарел — нажми «Поправить» ещё раз", show_alert=True); return
    await _role_done(query, db, uid, role)


@router.callback_query(lambda c: c.data and c.data.startswith("rolown:"))
async def cb_role_own(query: CallbackQuery, db, settings: Settings) -> None:
    """Своя роль текстом: следующее сообщение владельца боту (10 мин) станет ролью."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    import json as _json, time as _time
    from db import set_setting
    uid = int(query.data.split(":")[1])
    await set_setting(db, f"rolwait:{settings.owner_user_id}", _json.dumps(
        {"uid": uid, "msg_id": query.message.message_id, "chat_id": query.message.chat.id, "ts": int(_time.time())}))
    await query.answer("Напиши роль одним сообщением (например: «сосед по даче»)", show_alert=True)


# ── v4.44 «Мира-чат 2.0»: кнопки из диалога ──────────────────────────────────
@router.callback_query(lambda c: c.data and c.data.startswith(("cbs:", "cbp:")))
async def cb_content_board(query: CallbackQuery, db: aiosqlite.Connection, settings: Settings) -> None:
    """v4.78: кнопки живой контент-доски."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    import time as _time
    from core import content_board
    from core.util import today_local
    from db import set_setting
    kind, key = query.data.split(":", 1)
    if kind == "cbs":
        await set_setting(db, content_board.ASKED, "")
    if key in ("none", "later"):
        await query.answer("Ок")
        await query.message.edit_reply_markup(reply_markup=None)
        return
    try:
        title = await content_board.move_card(settings, key, "s" if kind == "cbs" else "p", today_local())
    except Exception as exc:
        logger.warning(f"board move failed: {type(exc).__name__}: {exc}")
        await query.answer("Не вышло, попробуй ещё раз", show_alert=True); return
    if not title:
        await query.answer("Карточки уже нет на доске", show_alert=True); return
    if kind == "cbs":
        await content_board.plan_clips(db, key, title, _time.time())
    else:
        await set_setting(db, f"boardclips:{key}", "")
    await query.answer("Перенесла на доске")
    await query.message.edit_reply_markup(reply_markup=None)


@router.callback_query(lambda c: c.data and c.data.startswith("sra:"))
async def cb_stream_announce(query: CallbackQuery, db: aiosqlite.Connection, settings: Settings) -> None:
    """v4.99: анонс стрима в канал по кнопке."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    from core import stream_report
    sid = query.data.split(":", 1)[1]
    if sid != "none":
        try:
            note = await stream_report.publish(query.bot, db, settings, sid)
        except Exception as exc:
            logger.warning(f"stream announce failed: {type(exc).__name__}: {exc}")
            await query.answer("Не вышло опубликовать, попробуй ещё раз", show_alert=True); return
    await query.answer(note if sid != "none" else "Ок")
    await query.message.edit_reply_markup(reply_markup=None)


@router.callback_query(lambda c: c.data and c.data.startswith("tw:"))
async def cb_twitch(query: CallbackQuery, db: aiosqlite.Connection, settings: Settings) -> None:
    """v4.93: кнопки тревоги о спаме в чате Twitch."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    from core import twitch_chat
    action = query.data.split(":", 1)[1]
    try:
        if action == "followers":
            note = await twitch_chat.chat_mode(db, settings, "followers", True, 10)
        elif action == "slow":
            note = await twitch_chat.chat_mode(db, settings, "slow", True, 30)
        else:
            note = "Ок"
    except twitch_chat.TwitchError as exc:
        note = str(exc)[:180]
    await query.answer(note[:190])
    try:
        await query.message.edit_text(f"{query.message.text}\n\n{note}", reply_markup=None, parse_mode=None)
    except Exception:
        pass


@router.callback_query(lambda c: c.data and c.data.startswith("mtb:"))
async def cb_meeting_shift(query: CallbackQuery, bot: Bot, db: aiosqlite.Connection, settings: Settings) -> None:
    """v4.110: «+15 мин / +1 ч» на текстовой карточке встречи (запасной вид, когда виджеты запрещены)."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    from core.widgets import meeting
    _, minutes, mid = query.data.split(":", 2)
    await query.answer("Сдвигаю…")
    note, new_text = await meeting.on_button(db, settings, int(mid), int(minutes))
    try:
        if new_text:
            await query.message.edit_text(f"{new_text}\n\n{note}", reply_markup=query.message.reply_markup, parse_mode=None)
        else:
            await query.message.answer(note, parse_mode=None)
    except Exception:
        pass


@router.callback_query(lambda c: c.data and c.data.startswith("mod:"))
async def cb_moderation(query: CallbackQuery, bot: Bot, db: aiosqlite.Connection, settings: Settings) -> None:
    """v4.89: решение по подозрительному комментарию в обсуждении канала."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    from core import moderation
    _, action, token = query.data.split(":", 2)
    note = await moderation.decide(bot, db, token, action)
    await query.answer(note)
    try:
        await query.message.edit_text(f"{query.message.text}\n\n{note}", reply_markup=None, parse_mode=None)
    except Exception:
        pass


@router.callback_query(lambda c: c.data and c.data.startswith("chc:"))
async def cb_channel_comment(query: CallbackQuery, bot: Bot, db: aiosqlite.Connection, settings: Settings) -> None:
    """v4.79: опубликовать первый комментарий к посту или отказаться."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    from core import channel
    _, action, token = query.data.split(":", 2)
    try:
        note = await channel.decide(bot, db, token, action == "yes")
    except Exception as exc:
        logger.warning(f"channel comment publish failed: {type(exc).__name__}: {exc}")
        note = "Не вышло опубликовать"
    await query.answer(note)
    try:
        await query.message.edit_text(f"{query.message.text}\n\n{note}", reply_markup=None, parse_mode=None)
    except Exception:
        pass


@router.callback_query(lambda c: c.data and c.data.startswith("grp:"))
async def cb_group(query: CallbackQuery, db: aiosqlite.Connection, settings: Settings) -> None:
    """v4.77: решение владельца по группе."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    from core import group_mira
    _, action, raw = query.data.split(":", 2)
    chat_id = int(raw)
    if action == "no":
        await group_mira.forget(db, chat_id)
        try:
            await query.bot.leave_chat(chat_id)
        except Exception as exc:
            logger.info(f"group leave skip: {exc}")
        result = "Вышла из группы 🚪"
    else:
        try:
            title = (await query.bot.get_chat(chat_id)).title or str(chat_id)
        except Exception:
            await query.answer("Меня уже нет в этой группе", show_alert=True); return
        await group_mira.allow(db, chat_id, title, daily=action == "day")
        result = f"Готово, в «{title}» отвечаю только на твой тег"
    await query.answer()
    await query.message.edit_reply_markup(reply_markup=None)
    await query.message.reply(result, parse_mode=None)


@router.callback_query(lambda c: c.data and c.data.startswith("notmine:"))
async def cb_not_mine(query: CallbackQuery, db: aiosqlite.Connection, settings: Settings) -> None:
    """v4.74: проверка решила, что дело чужое, а владелец вернул его кнопкой."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    import json as _json
    from core.actions import create_task_from_suggestion
    from db import get_setting, set_setting
    raw = await get_setting(db, query.data)
    if not raw:
        await query.answer("Уже сделала"); return
    d = _json.loads(raw)
    res = await create_task_from_suggestion(db, settings, query.bot, settings.owner_user_id, d["user_id"], d["username"], d["category"], d["task"])
    if res == "fail":
        await query.answer("Не вышло, попробуй ещё раз", show_alert=True); return
    await set_setting(db, query.data, "")
    await query.answer("Поставила" if res == "ok" else "Такая задача уже есть")
    await query.message.edit_reply_markup(reply_markup=None)


@router.callback_query(lambda c: c.data and c.data.startswith(("ghissue:", "ghissue_no:")))
async def cb_github_issue(query: CallbackQuery, db: aiosqlite.Connection, settings: Settings) -> None:
    """v4.73: issue появляется только по кнопке владельца."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    action, tok = query.data.split(":", 1)
    if action == "ghissue_no":
        from db import set_setting
        await set_setting(db, f"ghissue:{tok}", "")
        result = "Отменила"
    else:
        from core.mira_tools import create_issue
        result = await create_issue(db, settings, tok)
    await query.answer()
    await query.message.edit_reply_markup(reply_markup=None)
    await query.message.reply(result, parse_mode=None)


@router.callback_query(lambda c: c.data and c.data.startswith(("webform:", "webform_no:")))
async def cb_web_form(query: CallbackQuery, db: aiosqlite.Connection, settings: Settings) -> None:
    """v4.86: форма на сайте уходит только по кнопке владельца."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    if _double_click(query):
        await query.answer(); return
    action, tok = query.data.split(":", 1)
    await query.answer("Отправляю…" if action == "webform" else None)
    await query.message.edit_reply_markup(reply_markup=None)
    if action == "webform_no":
        from db import set_setting
        await set_setting(db, f"webform:{tok}", "")
        result = "Не отправляю"
    else:
        from core.mira_tools import submit_form
        result = await submit_form(db, settings, tok)
    await query.message.reply(result, parse_mode=None)


@router.callback_query(lambda c: c.data and c.data.startswith("mundo:"))
async def cb_mira_undo(query: CallbackQuery, db: aiosqlite.Connection, settings: Settings) -> None:
    """Ф2: «↩️ Отменить» под «📝 Запомнила» — убрать записанные факты из памяти."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    import json as _json
    from db import get_setting, set_setting
    from core.vault import forget_fact
    raw = await get_setting(db, query.data)
    if not raw:
        await query.answer("Уже нечего отменять"); return
    n = 0
    try:
        for uid, fact in _json.loads(raw):
            n += int(await forget_fact(db, int(uid), fact))
    except Exception as exc:
        logger.warning(f"mira undo failed: {exc}")
    await set_setting(db, query.data, "")
    await query.answer("↩️ Убрала из памяти" if n else "Не нашла, что убирать")
    try:
        await query.message.edit_text((query.message.text or "") + ("\n\n↩️ Отменила" if n else "\n\n(уже удалено)"), reply_markup=None)
    except Exception:
        pass


@router.callback_query(lambda c: c.data and c.data.startswith("mrs:"))
async def cb_mira_reschedule(query: CallbackQuery, db: aiosqlite.Connection, settings: Settings) -> None:
    """Ф3: подтверждение переноса задачи из диалога: mrs:{sync_id}:{YYYY-MM-DD[THH:MM]}."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    if _double_click(query):
        await query.answer(); return
    try:
        _, sid, due = query.data.split(":", 2)
        from handlers.owner import _apply_reschedule
        ok = await _apply_reschedule(db, settings, int(sid), due)
    except Exception as exc:
        logger.warning(f"mira reschedule failed: {exc}")
        ok = False
    await query.answer("📅 Перенесла" if ok else "Задача не найдена или уже закрыта", show_alert=not ok)
    if ok:
        try:
            await query.message.edit_text((query.message.html_text or "") + "\n\n📅 <b>Перенесла</b>", reply_markup=None)
        except Exception:
            pass


@router.callback_query(lambda c: c.data == "mira:no")
async def cb_mira_dismiss(query: CallbackQuery, settings: Settings) -> None:
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    await query.answer("Ок, оставила как есть")
    try:
        await query.message.edit_text((query.message.html_text or "") + "\n\n✖ <b>Отменила</b>", reply_markup=None)
    except Exception:
        pass


@router.callback_query(lambda c: c.data and c.data.startswith("rep:"))
async def cb_reply_via_mira(query: CallbackQuery, db: aiosqlite.Connection, settings: Settings) -> None:
    """v4.48: «✍️ ответить» — следующий текст владельца в личке Миры уйдёт этому контакту от его имени
    (Business API). Нужен для контактов без username: ссылка на чат по id на iPhone не открывается."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    import json as _json, time as _time
    from db import set_setting, get_contact_profile
    uid = int(query.data.split(":")[1])
    prof = await get_contact_profile(db, uid) or {}
    who = prof.get("name") or (f"@{prof['username']}" if prof.get("username") and not str(prof["username"]).startswith("id") else f"id{uid}")
    await set_setting(db, f"repwait:{settings.owner_user_id}", _json.dumps(
        {"uid": uid, "who": who, "username": prof.get("username") or "", "ts": _time.time()}, ensure_ascii=False))
    await query.answer(f"Напиши ответ для {who} одним сообщением — отправлю от твоего имени (10 мин)", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("dsec:"))
async def cb_contact_memory_section(query: CallbackQuery, db: aiosqlite.Connection, settings: Settings) -> None:
    """v4.51: 📂 в обзоре памяти → раскрыть раздел (или хронологию/стиль) отдельным сообщением, страницами."""
    if not owners.is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    from db import get_contact_profile
    from core import contact_memory as _dos
    _, uid, key = query.data.split(":", 2)
    prof = await get_contact_profile(db, int(uid)) or {}
    if not prof:
        await query.answer("Память не найдена", show_alert=True); return
    pages = _dos.render_section_pages(dict(prof, user_id=int(uid)), key)
    await query.answer("📂 Раскрываю")
    chat_id = query.message.chat.id if query.message else settings.owner_user_id
    thread = getattr(query.message, "message_thread_id", None) if query.message else None
    if not await _dos.send_rich_pages(settings.bot_token, chat_id, pages, thread):
        import re as _re
        plain = _re.sub(r"<[^>]+>", "", pages[0].replace("<li>", "\n• ").replace("</h4>", "\n"))
        await query.bot.send_message(chat_id, plain[:4000], parse_mode=None, message_thread_id=thread)
