import html as _html   # v4.33: экранирование plain-веток команд (глобальный parse_mode=HTML)
import re
import time
from collections import Counter
from pathlib import Path

import aiosqlite
import psutil
from anthropic import AsyncAnthropic
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from loguru import logger

from config import Settings
from core import whitelist
from db import get_stats, get_mode, set_mode, get_recent_messages

router = Router(name="commands")

# Haiku 4.5 pricing: $0.80/MTok input, $4.00/MTok output
_HAIKU_INPUT_COST_PER_M = 0.80
_HAIKU_OUTPUT_COST_PER_M = 4.00


async def _is_owner(message: Message, settings: Settings) -> bool:
    # v4.12: админ-команды (расход/режим/статистика/whitelist…) — ТОЛЬКО основному аккаунту.
    # Вторичные business-аккаунты (мультиаккаунт, @starninjaMAN и т.п.) регистрируются лишь
    # чтобы их ИСХОДЯЩИЕ не принимались за входящие от контакта — доступа к админке у них НЕТ
    # (иначе /start с них сливал расходы/режим). Классификация владельца — по-прежнему owners.is_owner.
    return bool(message.from_user and message.from_user.id == settings.owner_user_id)


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("help"))
async def cmd_help(message: Message, settings: Settings) -> None:
    """Меню: все команды по категориям."""
    if not await _is_owner(message, settings):
        return
    txt = (
        "🤖 <b>Команды бота</b>\n\n"
        "📊 <b>Статус и расходы</b>\n"
        "/start · /health · /credits · /stats · /insights · /last\n\n"
        "👤 <b>Контакты</b>\n"
        "/about @user — память · /summary @user — LLM-сводка\n"
        "/find слово — поиск по переписке\n"
        "/setname @user Имя · /addfact @user текст · /rule @user vip|task|never|mute|clear\n"
        "/setphoto @user (фото) · /correct · /friend · /unfriend · /whitelist\n"
        "/day — мой день · /habits — привычки · /market — рынок · /call test — карточка звонка · /roles — роли контактов · /promises — твои открытые обещания · /links — сохранённые ссылки\n\n"
        "🪞 <b>О тебе</b>\n"
        "/me — твоя память (8 секций + хронология) · /me build — пересобрать\n\n"
        "/subs — подписки (суммы, даты списания, итог в месяц; ✖ — убрать) · /subs spent [дней] — сколько списалось\n"
        "/routines — регулярные сообщения от Миры · /routines del N — убрать\n\n"
        "📡 <b>Проактивность</b>\n"
        "/radar — радар связей · /schedule @user — найти время\n\n"
        "⚙️ <b>Настройки</b>\n"
        "/mode observe|active · /drafts on|off · /setreply · /topics · /owner · /help"
    )
    rich_ok = False
    try:
        from core import richmsg
        rich_ok = await richmsg.try_rich(message, "<h3>🤖 Команды бота</h3>" + txt.replace("\n", "<br/>"))
    except Exception:
        pass
    if not rich_ok:
        await message.answer(txt)


@router.message(Command("start"))
async def cmd_start(
    message: Message,
    db: aiosqlite.Connection,
    settings: Settings,
) -> None:
    if not await _is_owner(message, settings):
        # v4.13: обычный человек нажал /start → нейтральное приветствие (без утечки админки),
        # чтобы было понятно, что это за бот и что можно писать. Дальше его сообщения релеятся.
        try:
            await message.answer(
                "👋 Привет! Я Mira — личный ассистент владельца этого аккаунта.\n\n"
                "Напиши мне — я передам ему, и он ответит тебе прямо здесь. "
                "Можно текстом, голосовым, фото — как удобно.")
        except Exception:
            pass
        return

    current_mode = await get_mode(db, default=settings.mode)
    stats = await get_stats(db, days=1)
    counts = stats["counts"]
    total = sum(counts.values())
    emoji = "👀" if current_mode == "observe" else "⚡️"

    text = (
        f"👋 <b>TG Business Sorter</b> запущен\n\n"
        f"Режим: {emoji} <b>{current_mode}</b>\n"
        f"За сутки обработано: {total} сообщений\n\n"
        f"<b>Команды:</b>\n"
        f"/stats — статистика за 7 дней\n"
        f"/mode observe|active — переключить режим\n"
        f"/friend @username — добавить в whitelist\n"
        f"/unfriend @username — удалить из whitelist\n"
        f"/whitelist — список разрешённых\n"
        f"/health — статус бота\n"
        f"/last N — последние N сообщений"
    )
    rich_ok = False
    try:
        from core import richmsg
        doc = (
            f"<h3>👋 TG Business Sorter</h3>"
            f"<p>Режим: {emoji} <b>{current_mode}</b> · за сутки: <b>{total}</b> сообщений</p>"
            + richmsg.table(["Команда", "Что делает"], [
                ["/stats", "статистика за 7 дней"], ["/credits", "расход и кредиты"],
                ["/health", "статус бота"], ["/mode", "observe | active"],
                ["/friend @u", "добавить в whitelist"], ["/whitelist", "список разрешённых"],
                ["/last N", "последние сообщения"], ["/topics", "топики уведомлений"]])
        )
        rich_ok = await richmsg.try_rich(message, doc)
    except Exception as exc:
        logger.debug(f"rich start skip: {exc}")
    if not rich_ok:
        await message.answer(text)


# ── /friend ───────────────────────────────────────────────────────────────────

@router.message(Command("friend"))
async def cmd_friend(
    message: Message,
    db: aiosqlite.Connection,
    settings: Settings,
) -> None:
    if not await _is_owner(message, settings):
        return

    user_id: int | None = None
    username: str | None = None

    if message.reply_to_message and message.reply_to_message.forward_from:
        fwd = message.reply_to_message.forward_from
        user_id = fwd.id
        username = fwd.username

    elif message.text:
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1:
            arg = parts[1].strip().lstrip("@")
            if arg.isdigit():
                user_id = int(arg)
            else:
                # v4.33: whitelist ищется ТОЛЬКО по user_id — запись с user_id=0 никогда не
                # матчилась и затирала предыдущую такую же. Резолвим @username через контакты.
                from db import get_contact_by_username
                prof = await get_contact_by_username(db, arg)
                if not prof or not prof.get("user_id"):
                    await message.answer(
                        f"⚠️ @{arg} не нашла среди контактов — ответь /friend реплаем на его сообщение")
                    return
                user_id, username = int(prof["user_id"]), prof.get("username") or arg

    if user_id:
        await whitelist.add(db, user_id=user_id, username=username, reason="manual")
        display = f"@{username}" if username else str(user_id)
        await message.answer(f"✅ {display} добавлен в whitelist")
        logger.info(f"Owner whitelisted {user_id} (@{username})")
    else:
        await message.answer(
            "Использование: /friend @username  или  /friend в ответ на пересланное сообщение"
        )


# ── /unfriend ─────────────────────────────────────────────────────────────────

@router.message(Command("unfriend"))
async def cmd_unfriend(
    message: Message,
    db: aiosqlite.Connection,
    settings: Settings,
) -> None:
    if not await _is_owner(message, settings):
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /unfriend @username или /unfriend 123456")
        return

    arg = parts[1].strip().lstrip("@")
    if arg.isdigit():
        removed = await whitelist.remove(db, int(arg))
        display = arg
    else:
        all_wl = await whitelist.list_all(db)
        match = next((e for e in all_wl if e["username"] == arg), None)
        if match:
            removed = await whitelist.remove(db, match["user_id"])
            display = f"@{arg}"
        else:
            await message.answer(f"@{arg} не найден в whitelist")
            return

    if removed:
        await message.answer(f"🗑 {display} удалён из whitelist")
    else:
        await message.answer(f"{display} не был в whitelist")


# ── /whitelist ────────────────────────────────────────────────────────────────

@router.message(Command("whitelist"))
async def cmd_whitelist(
    message: Message,
    db: aiosqlite.Connection,
    settings: Settings,
) -> None:
    if not await _is_owner(message, settings):
        return

    entries = await whitelist.list_all(db)
    if not entries:
        await message.answer("Whitelist пуст")
        return

    lines = [f"👥 Whitelist ({len(entries)} чел.):\n"]
    for e in entries:
        uname = f"@{e['username']}" if e["username"] else f"id{e['user_id']}"
        lines.append(f"• {uname} — {e['reason']} ({e['added_at'][:10]})")

    rich_ok = False
    try:
        import html as _html
        from core import richmsg
        rows = [[("@" + e["username"]) if e["username"] else f"id{e['user_id']}",
                 _html.escape(str(e["reason"] or "")), (e["added_at"] or "")[:10]]
                for e in entries]
        doc = f"<h3>👥 Whitelist ({len(entries)})</h3>" + richmsg.table(["Кто", "Причина", "Добавлен"], rows)
        rich_ok = await richmsg.try_rich(message, doc)
    except Exception as exc:
        logger.debug(f"rich whitelist skip: {exc}")
    if not rich_ok:
        await message.answer("\n".join(lines))


@router.message(Command("roles", "role"))
async def cmd_roles(
    message: Message,
    db: aiosqlite.Connection,
    settings: Settings,
) -> None:
    """👥 Роли: бот сам создаёт роли и объединяет в них людей — список групп и кто в них."""
    if not await _is_owner(message, settings):
        return
    import html
    from db import get_contacts_by_role
    groups = await get_contacts_by_role(db)
    if not groups:
        await message.answer("Ролей пока нет — заполнятся по мере общения."); return
    items = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    total = sum(len(v) for v in groups.values())
    lines = [f"👥 <b>Роли</b> — {len(items)} групп, {total} контактов:\n"]
    for role, names in items:
        shown = ", ".join(names[:12]) + ("…" if len(names) > 12 else "")
        lines.append(f"<b>{html.escape(role)}</b> ({len(names)}): {html.escape(shown)}")
    rich_ok = False
    try:
        from core import richmsg
        rows = [[html.escape(r), str(len(n)), html.escape(", ".join(n[:10]))] for r, n in items]
        doc = f"<h3>👥 Роли ({len(items)} групп · {total})</h3>" + richmsg.table(["Роль", "N", "Кто"], rows)
        rich_ok = await richmsg.try_rich(message, doc)
    except Exception as exc:
        logger.debug(f"rich roles skip: {exc}")
    if not rich_ok:
        await message.answer("\n".join(lines), parse_mode="HTML")


# ── /stats ────────────────────────────────────────────────────────────────────

@router.message(Command("stats"))
async def cmd_stats(
    message: Message,
    db: aiosqlite.Connection,
    settings: Settings,
) -> None:
    if not await _is_owner(message, settings):
        return

    stats = await get_stats(db, days=7)
    counts = stats["counts"]
    spam_texts = stats["spam_texts"]
    total_input = stats["total_input_tokens"]
    total_output = stats["total_output_tokens"]
    avg_latency = stats["avg_llm_latency_ms"]

    total = sum(counts.values())
    spam = counts.get("SPAM", 0)
    business = counts.get("BUSINESS", 0)
    unclear = counts.get("UNCLEAR", 0)
    friend = counts.get("FRIEND", 0)

    words: list[str] = []
    for t in spam_texts:
        words.extend(re.findall(r"\b[а-яёa-z]{4,}\b", t.lower()))
    top_words = Counter(words).most_common(5)
    kw_line = (
        ", ".join(f"{w}({n})" for w, n in top_words)
        if top_words else "нет данных"
    )

    input_cost = (total_input / 1_000_000) * _HAIKU_INPUT_COST_PER_M
    output_cost = (total_output / 1_000_000) * _HAIKU_OUTPUT_COST_PER_M
    total_cost = input_cost + output_cost

    text = (
        f"📊 <b>Статистика за 7 дней</b> (всего {total}):\n\n"
        f"🚫 SPAM:     {spam}\n"
        f"💼 BUSINESS: {business}\n"
        f"❓ UNCLEAR:  {unclear}\n"
        f"👥 FRIEND:   {friend}\n\n"
        f"🤖 LLM (avg latency): {avg_latency} ms\n"
        f"📥 Токены input:  {total_input:,}\n"
        f"📤 Токены output: {total_output:,}\n"
        f"💰 Классификатор (Haiku): ~${total_cost:.4f}\n"
        f"<i>полный расход по всем моделям → /credits</i>\n\n"
        f"🔑 Топ слова в спаме: {kw_line}"
    )
    # Rich-версия (Bot API 10.1): категории и LLM таблицами. Сбой → обычный текст.
    rich_ok = False
    try:
        import html as _html
        from core import richmsg
        doc = (
            f"<h3>📊 Статистика за 7 дней</h3>"
            f"<p>Всего: <b>{total}</b> сообщений</p>"
            + richmsg.table(["Категория", "Сообщений"], [
                ["💼 BUSINESS", str(business)], ["🚫 SPAM", str(spam)],
                ["❓ UNCLEAR", str(unclear)], ["👥 FRIEND", str(friend)]])
            + richmsg.table(["Метрика", "Значение"], [
                ["avg latency", f"{avg_latency} ms"],
                ["📥 input", f"{total_input:,}"], ["📤 output", f"{total_output:,}"],
                ["💰 Haiku ≈", f"${total_cost:.4f}"]], caption="🤖 Классификатор")
            + f"<p>🔑 Топ слова в спаме: {_html.escape(kw_line)}</p>"
            f"<footer>полный расход по всем моделям → /credits</footer>"
        )
        rich_ok = await richmsg.send_rich(
            settings.bot_token, message.chat.id, doc, message.message_thread_id)
    except Exception as exc:
        logger.debug(f"rich stats skip: {exc}")
    if not rich_ok:
        await message.answer(text)


# ── /mode ─────────────────────────────────────────────────────────────────────

@router.message(Command("mode"))
async def cmd_mode(
    message: Message,
    db: aiosqlite.Connection,
    settings: Settings,
) -> None:
    if not await _is_owner(message, settings):
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        current = await get_mode(db, default=settings.mode)
        await message.answer(
            f"Текущий режим: <b>{current}</b>\n\n"
            "/mode observe — только логировать, ничего не отправлять\n"
            "/mode active  — авто-ответы на спам включены",
        )
        return

    new_mode = parts[1].strip().lower()
    if new_mode not in ("observe", "active"):
        await message.answer("Доступные режимы: observe | active")
        return

    await set_mode(db, new_mode)
    emoji = "👀" if new_mode == "observe" else "⚡️"
    await message.answer(f"{emoji} Режим переключён на <b>{new_mode}</b>")
    logger.info(f"Mode changed to {new_mode} by owner")


@router.message(Command("drafts"))
async def cmd_drafts(message: Message, db: aiosqlite.Connection, settings: Settings) -> None:
    """Вкл/выкл умные черновики ответов на BUSINESS (приходят владельцу на одобрение)."""
    if not await _is_owner(message, settings):
        return
    from db import get_setting, set_setting
    parts = (message.text or "").split(maxsplit=1)
    cur = await get_setting(db, "draft_replies")
    enabled = (cur == "on") if cur is not None else settings.enable_draft_replies
    if len(parts) < 2:
        await message.answer(
            f"Умные черновики: <b>{'вкл' if enabled else 'выкл'}</b>\n\n"
            "/drafts on — бот предлагает черновик ответа на деловые сообщения\n"
            "/drafts off — не предлагать")
        return
    val = parts[1].strip().lower()
    if val not in ("on", "off"):
        await message.answer("Использование: /drafts on | off"); return
    await set_setting(db, "draft_replies", val)
    await message.answer(f"💬 Умные черновики: <b>{'вкл' if val == 'on' else 'выкл'}</b>")
    logger.info(f"Draft replies toggled to {val} by owner")


# ── /health ───────────────────────────────────────────────────────────────────

@router.message(Command("health"))
async def cmd_health(
    message: Message,
    db: aiosqlite.Connection,
    settings: Settings,
    anthropic_client: AsyncAnthropic,
    start_time: float,
) -> None:
    if not await _is_owner(message, settings):
        return

    proc = psutil.Process()
    mem_mb = proc.memory_info().rss / 1024 / 1024

    db_path = Path(settings.db_path)
    db_size_kb = db_path.stat().st_size / 1024 if db_path.exists() else 0

    uptime_s = time.monotonic() - start_time
    h, rem = divmod(int(uptime_s), 3600)
    m, s = divmod(rem, 60)
    uptime_str = f"{h}h {m}m {s}s"

    # Активность за час — чтобы видеть, что бот реально обрабатывает (FRIEND логируется на DEBUG,
    # снаружи кажется «тихо»; эта строка снимает ложную тревогу «бот ничего не делает»).
    async with db.execute("SELECT COUNT(*) FROM conversations WHERE created_at > datetime('now','-60 minutes')") as _c:
        msgs_hour = (await _c.fetchone())[0]
    async with db.execute("SELECT COUNT(*) FROM message_log WHERE created_at > datetime('now','-60 minutes')") as _c:
        classified_hour = (await _c.fetchone())[0]

    api_ok = False
    api_latency_ms = 0
    try:
        t0 = time.monotonic()
        await anthropic_client.messages.create(
            model=settings.anthropic_model,
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
        api_latency_ms = int((time.monotonic() - t0) * 1000)
        api_ok = True
    except Exception as exc:
        logger.warning(f"Anthropic ping failed: {exc}")

    current_mode = await get_mode(db, default=settings.mode)
    api_status = f"✅ {api_latency_ms}ms" if api_ok else "❌ недоступен"

    from db import get_spending
    spending = await get_spending(db)
    today_usd = spending["today_usd"]
    month_usd = spending["month_usd"]
    remaining = max(0.0, settings.anthropic_monthly_budget - month_usd)

    from db import get_credits_estimate
    credits = await get_credits_estimate(db)
    credits_line = ""
    if credits:
        credits_line = (
            f"\n🪙 <b>Кредиты Anthropic</b> (оценка)\n"
            f"≈ ${credits['remaining']:.2f} осталось\n"
            f"(база ${credits['base']:.2f} на {credits['anchor_date']}, "
            f"потрачено ${credits['spent_since']:.4f})\n"
            f"<i>⚠️ общий ключ с @snmodbot — учтён только расход этого бота, реальный остаток ниже</i>\n"
        )

    text = (
        f"🏥 <b>Health</b>\n\n"
        f"RAM:     {mem_mb:.1f} MB\n"
        f"БД:      {db_size_kb:.1f} KB\n"
        f"Uptime:  {uptime_str}\n"
        f"Режим:   {current_mode}\n"
        f"API:     {api_status}\n"
        f"📨 За час:  {msgs_hour} сообщ. ({classified_hour} классиф.)\n\n"
        f"💰 <b>Расходы Anthropic</b>\n"
        f"Сегодня:  ${today_usd:.4f}\n"
        f"Месяц:    ${month_usd:.4f}\n"
        f"Лимит/мес: ${remaining:.2f} из ${settings.anthropic_monthly_budget:.2f} (стоп-кран)\n"
        f"{credits_line}"
        f"🔗 <a href=\"https://console.anthropic.com/settings/billing\">Точный баланс →</a> "
        f"(обновить: /credits СУММА)"
    )
    # Rich-версия (Bot API 10.1). Сбой → обычный текст.
    rich_ok = False
    try:
        from core import richmsg
        cred_html = ""
        if credits:
            cred_html = (f"<p>🪙 Кредиты: ≈ <b>${credits['remaining']:.2f}</b> осталось "
                         f"(база ${credits['base']:.2f} на {credits['anchor_date']}, "
                         f"потрачено ${credits['spent_since']:.4f})<br/>"
                         f"<i>⚠️ общий ключ с @snmodbot — учтён только расход этого бота</i></p>")
        doc = (
            f"<h3>🏥 Health</h3>"
            + richmsg.table(["Параметр", "Значение"], [
                ["RAM", f"{mem_mb:.1f} MB"], ["БД", f"{db_size_kb:.1f} KB"],
                ["Uptime", uptime_str], ["Режим", current_mode],
                ["API", "✅ " + str(api_latency_ms) + "ms" if api_ok else "❌ недоступен"],
                ["📨 За час", f"{msgs_hour} сообщ. ({classified_hour} классиф.)"]])
            + richmsg.table(["Период", "$"], [
                ["Сегодня", f"${today_usd:.4f}"], ["Месяц", f"${month_usd:.4f}"],
                ["Лимит/мес (стоп-кран)", f"${remaining:.2f} из ${settings.anthropic_monthly_budget:.2f}"]],
                caption="💰 Расходы Anthropic")
            + cred_html
            + "<footer><a href=\"https://console.anthropic.com/settings/billing\">Точный баланс</a> · "
              "обновить: /credits СУММА</footer>"
        )
        rich_ok = await richmsg.send_rich(
            settings.bot_token, message.chat.id, doc, message.message_thread_id)
    except Exception as exc:
        logger.debug(f"rich health skip: {exc}")
    if not rich_ok:
        await message.answer(text)


# ── /about ────────────────────────────────────────────────────────────────────

@router.message(Command("about"))
async def cmd_about(
    message: Message,
    db: aiosqlite.Connection,
    settings: Settings,
) -> None:
    if not await _is_owner(message, settings):
        return
    from db import get_contact_by_username, vault_get_context, get_open_tasks_for_user
    import json as _json

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /about @username")
        return

    uname = parts[1].strip().lstrip("@")
    profile = await get_contact_by_username(db, uname)
    if not profile:
        await message.answer(f"Нет данных о @{uname}")
        return

    if profile.get("photo_file_id"):
        try:
            await message.answer_photo(profile["photo_file_id"])
        except Exception:
            pass

    topics = _json.loads(profile.get("topics") or "[]")
    ctx = await vault_get_context(db, profile["user_id"], limit=8)
    open_tasks = await get_open_tasks_for_user(db, profile["user_id"])

    from core.vault import _coarse_notion_type
    _coarse = profile.get("contact_type") or _coarse_notion_type(profile.get("relationship") or "")
    _e = _html.escape   # v4.33: plain-ветка при parse_mode=HTML — любой «<» в данных ронял ответ
    text = f"👤 <b>@{_e(str(profile.get('username')))}</b>\n\n"
    if profile.get("name"):
        text += f"Имя: {_e(str(profile['name']))}\n"
    text += f"Тип: {_e(str(_coarse))}\n"
    text += f"Роль: {_e(str(profile.get('relationship') or '—'))}\n"
    text += f"Сообщений: {profile.get('message_count', 0)}\n"
    if profile.get("intent"):
        text += f"Зачем пишет: {_e(str(profile['intent']))}\n"
    if topics:
        text += f"Темы: {_e(', '.join(map(str, topics)))}\n"
    if profile.get("summary"):
        text += f"\n🧠 {_e(str(profile['summary']))}\n"
    if open_tasks:
        text += f"\n📋 Открытых задач: {len(open_tasks)}\n"
        for t in open_tasks[:5]:
            text += f"  • {_e(str(t['title']))}\n"
    if ctx:
        text += f"\n💬 Последние сообщения:\n{_e(ctx[:800])}"

    rich_ok = False
    try:
        # v4.51: память целиком — 17 разделов сворачиваемыми блоками, страницами (раньше разделов не было вовсе)
        from core import contact_memory as _dos
        from core import richmsg
        _uid = profile["user_id"]
        rich_ok = await richmsg.try_rich(message, _dos.render_overview(
            dict(profile, contact_type=_coarse, user_id=_uid), open_tasks,
            people=await _dos.persons_linked(db, _uid), rhythm_line=_dos.rhythm_text(await _dos.rhythm(db, _uid))))
    except Exception as exc:
        logger.debug(f"rich about skip: {exc}")
    if not rich_ok:
        await message.answer(text)


# ── /find ─────────────────────────────────────────────────────────────────────

@router.message(Command("find"))
async def cmd_find(
    message: Message,
    db: aiosqlite.Connection,
    settings: Settings,
) -> None:
    if not await _is_owner(message, settings):
        return
    from db import search_conversations

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /find <слово или фраза>")
        return

    query = parts[1].strip()
    results = await search_conversations(db, query, limit=20)
    if not results:
        await message.answer(f"Ничего не найдено по «{query}»")
        return

    lines = [f"🔍 <b>Найдено по «{_html.escape(query)}»</b> ({len(results)}):\n"]
    for r in results:
        who = "Ты" if r["direction"] == "out" else f"@{r['username'] or r['user_id']}"
        ts = (r["created_at"] or "")[:16]
        snippet = _html.escape((r["text"] or "").replace("\n", " ")[:120])   # v4.33: «<3» ломал /find
        lines.append(f"• <code>{ts}</code> {who}: {snippet}")
    rich_ok = False
    try:
        import html as _html
        from core import richmsg
        rows = [[(r["created_at"] or "")[:16],
                 "Ты" if r["direction"] == "out" else f"@{r['username'] or r['user_id']}",
                 _html.escape((r["text"] or "").replace("\n", " ")[:120])]
                for r in results]
        doc = (f"<h3>🔍 Найдено по «{_html.escape(query)}» ({len(results)})</h3>"
               + richmsg.table(["Когда", "Кто", "Фрагмент"], rows))
        rich_ok = await richmsg.try_rich(message, doc)
    except Exception as exc:
        logger.debug(f"rich find skip: {exc}")
    if not rich_ok:
        await message.answer("\n".join(lines)[:4000])


# ── /credits ──────────────────────────────────────────────────────────────────

SUMMARY_SYSTEM = """Ты — память владельца. По данным о контакте сделай краткую живую сводку ОТНОШЕНИЙ
(не пересказ полей, а суть). 4–6 строк, по-русски: кто это и статус, что владелец обещал/что висит,
тон общения, как часто пишет, что важно помнить при следующем контакте. Без воды и заголовков."""


@router.message(Command("owner"))
async def cmd_owner(
    message: Message,
    db: aiosqlite.Connection,
    settings: Settings,
) -> None:
    """Мультиаккаунт: /owner — список; /owner add <id> — добавить второй аккаунт; /owner del <id>."""
    if not await _is_owner(message, settings):
        return
    from core import owners
    parts = (message.text or "").split()
    if len(parts) >= 3 and parts[1].lower() in ("add", "del"):
        try:
            uid = int(parts[2].lstrip("@"))
        except ValueError:
            await message.answer("id должен быть числом. Узнать id аккаунта: напиши боту @userinfobot с того аккаунта."); return
        enabled = parts[1].lower() == "add"
        await owners.upsert(db, uid, owners.bc_for(uid), enabled)
        await message.answer(f"{'✅ Добавлен' if enabled else '🗑 Удалён'} аккаунт-владелец: <code>{uid}</code>\n"
                             f"Текущие вторичные: {owners.secondary_ids() or '—'}")
        return
    sec = owners.secondary_ids()
    await message.answer(
        f"👤 <b>Аккаунты-владельцы</b>\n"
        f"Основной (уведомления сюда): <code>{settings.owner_user_id}</code>\n"
        f"Вторичные: {', '.join(f'<code>{i}</code>' for i in sec) if sec else '— нет'}\n\n"
        f"Добавить: <code>/owner add ID</code>\n"
        f"<i>ID второго аккаунта узнать: с него написать @userinfobot</i>")
@router.message(Command("schedule"))
async def cmd_schedule(
    message: Message,
    db: aiosqlite.Connection,
    settings: Settings,
) -> None:
    """📅 Подобрать время встречи с контактом: /schedule @user [минуты] [today|tomorrow]."""
    if not await _is_owner(message, settings):
        return
    if not settings.google_calendar_id:
        await message.answer("Google Calendar не настроен."); return
    from db import get_contact_by_username
    from core import scheduler
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /schedule @user [минуты] [today|tomorrow]\nНапр.: /schedule @ivan 60 tomorrow"); return
    uname = parts[1].lstrip("@")
    mins = 60
    period = "tomorrow"
    for p in parts[2:]:
        if p.isdigit():
            mins = max(15, min(int(p), 480))
        elif p.lower() in ("today", "tomorrow", "сегодня", "завтра"):
            period = "today" if p.lower() in ("today", "сегодня") else "tomorrow"
    prof = await get_contact_by_username(db, uname)
    if not prof:
        await message.answer(f"Нет контакта @{uname} в базе"); return
    slots = await scheduler.compute_slots(settings, period, mins)
    if not slots:
        await message.answer(f"Свободных окон ≥{mins} мин {('завтра' if period=='tomorrow' else 'сегодня')} нет 😬"); return
    token = scheduler.stash(prof["user_id"], uname, slots, mins)
    rows = []
    for i, (s, e) in enumerate(slots[:6]):
        rows.append([InlineKeyboardButton(
            text=f"{s.strftime('%a %H:%M')}", callback_data=f"sch:{token}:{i}", style="primary")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await message.answer(
        f"📅 <b>Слоты для встречи с @{uname}</b> ({mins} мин, {'завтра' if period=='tomorrow' else 'сегодня'}):\n"
        f"<i>Выбери — создам событие и подготовлю черновик ему.</i>",
        reply_markup=kb)


@router.message(Command("insights"))
async def cmd_insights(
    message: Message,
    db: aiosqlite.Connection,
    settings: Settings,
) -> None:
    """📈 Аналитика за 30 дней: категории, топ-контакты, задачи, расход."""
    if not await _is_owner(message, settings):
        return
    stats = await get_stats(db, days=30)
    counts = stats["counts"]
    total = sum(counts.values()) or 1
    # топ контактов по объёму входящих
    async with db.execute(
        "SELECT COALESCE(username,'?'), COUNT(*) n FROM message_log "
        "WHERE created_at > datetime('now','-30 days') GROUP BY user_id ORDER BY n DESC LIMIT 5") as c:
        top = await c.fetchall()
    async with db.execute(
        "SELECT COUNT(*) FROM task_sync WHERE created_at > datetime('now','-30 days')") as c:
        tasks_made = (await c.fetchone())[0]
    from db import get_spending
    sp = await get_spending(db)
    import html as _html
    from core import richmsg
    cat_rows = [[k, str(counts.get(k, 0)), f"{counts.get(k,0)*100//total}%"]
                for k in ("BUSINESS", "FRIEND", "UNCLEAR", "SPAM")]
    top_rows = [[f"@{_html.escape(u)}", str(n)] for u, n in top]
    doc = (
        f"<h3>📈 Аналитика · 30 дней</h3>"
        f"<p>Всего сообщений: <b>{sum(counts.values())}</b> · задач создано: <b>{tasks_made}</b></p>"
        + richmsg.table(["Категория", "Кол-во", "Доля"], cat_rows, caption="По категориям")
        + richmsg.table(["Контакт", "Сообщений"], top_rows, caption="🔝 Топ по объёму")
        + f"<footer>💰 Месяц ${sp['month_usd']:.2f} · сегодня ${sp['today_usd']:.4f}</footer>"
    )
    if not await richmsg.send_rich(settings.bot_token, message.chat.id, doc, message.message_thread_id):
        lines = [f"📈 <b>Аналитика 30 дней</b>", f"Сообщений: {sum(counts.values())}, задач: {tasks_made}"]
        for k in ("BUSINESS", "FRIEND", "UNCLEAR", "SPAM"):
            lines.append(f"  {k}: {counts.get(k,0)}")
        lines.append("Топ: " + ", ".join(f"@{u}({n})" for u, n in top))
        await message.answer("\n".join(lines))


@router.message(Command("summary"))
async def cmd_summary(
    message: Message,
    db: aiosqlite.Connection,
    settings: Settings,
    anthropic_client: AsyncAnthropic,
) -> None:
    """🧠 Умная память отношений: /summary @username."""
    if not await _is_owner(message, settings):
        return
    import json as _json
    from db import get_contact_by_username, vault_get_context, get_open_tasks_for_user
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /summary @username"); return
    uname = parts[1].strip().lstrip("@")
    prof = await get_contact_by_username(db, uname)
    if not prof:
        await message.answer(f"Нет данных о @{uname}"); return
    uid = prof["user_id"]
    ctx = await vault_get_context(db, uid, limit=12)
    tasks = await get_open_tasks_for_user(db, uid)
    facts = (
        f"Контакт: @{prof.get('username')} ({prof.get('name') or '—'}), роль: {prof.get('relationship') or '—'}, "
        f"сообщений: {prof.get('message_count', 0)}, последний контакт: {prof.get('last_contact') or '?'}\n"
        f"Зачем пишет: {prof.get('intent') or '—'}\n"
        f"Обещания владельца: {prof.get('commitments') or '—'}\n"
        f"Открытые задачи: {', '.join(t['title'] for t in tasks) if tasks else 'нет'}\n"
        f"Последние сообщения:\n{ctx[:1200]}"
    )
    import html as _html
    from core import richmsg
    try:
        summary = await richmsg.stream_to_draft(
            message.bot, anthropic_client, message.chat.id, getattr(message, "message_thread_id", None),
            model=settings.anthropic_model_smart, system=SUMMARY_SYSTEM, content=facts,
            max_tokens=350, label="🤖 собираю сводку…", kind="summary")
    except Exception as exc:
        logger.error(f"summary failed: {exc}")
        await message.answer("Не смог собрать сводку, попробуй позже."); return
    doc = (f"<h3>🧠 @{_html.escape(uname)}</h3>"
           f"<p>{_html.escape(summary)}</p>"
           f"<footer>{prof.get('relationship') or '—'} · {prof.get('message_count', 0)} сообщ. · "
           f"открытых задач: {len(tasks)}</footer>")
    if not await richmsg.send_rich(settings.bot_token, message.chat.id, doc, message.message_thread_id):
        await message.answer(f"🧠 <b>@{_html.escape(str(uname))}</b>\n\n{_html.escape(str(summary))}")


@router.message(Command("rule"))
async def cmd_rule(
    message: Message,
    db: aiosqlite.Connection,
    settings: Settings,
) -> None:
    """Правило по контакту: /rule @user vip|task|never|mute|clear."""
    if not await _is_owner(message, settings):
        return
    from db import get_contact_by_username, set_contact_rule, get_contact_rule
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer(
            "Использование: <code>/rule @user РЕЖИМ</code>\n"
            "Режимы:\n"
            "• <b>mute</b> — не слать уведомления по контакту\n"
            "• <b>never</b> — не создавать задачи из его сообщений\n"
            "• <b>vip</b> — срочное от него всегда пингует (без кулдауна)\n"
            "• <b>task</b> — внимательнее к задачам от него\n"
            "• <b>clear</b> — снять правило")
        return
    uname = parts[1].lstrip("@")
    mode = parts[2].lower()
    valid = {"vip", "task", "never", "mute", "clear"}
    if mode not in valid:
        await message.answer(f"Режим неверный. Доступно: {', '.join(sorted(valid))}"); return
    prof = await get_contact_by_username(db, uname)
    if not prof:
        await message.answer(f"Нет контакта @{uname} в базе"); return
    await set_contact_rule(db, prof["user_id"], None if mode == "clear" else mode)
    if mode == "clear":
        await message.answer(f"✅ Правило для @{uname} снято")
    else:
        await message.answer(f"✅ @{uname} → <b>{mode}</b>")
    logger.info(f"Contact rule set: @{uname} → {mode}")


@router.message(Command("setname"))
async def cmd_setname(
    message: Message,
    db: aiosqlite.Connection,
    settings: Settings,
) -> None:
    """Исправить имя контакта (правит память бота + vault): /setname @user Имя."""
    if not await _is_owner(message, settings):
        return
    import html as _html
    from db import get_contact_by_username, get_contact_profile, upsert_contact_profile
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Использование: <code>/setname @user Имя</code>"); return
    uname = parts[1].lstrip("@")
    # только первая строка — чтобы многострочный ввод / второй командой не попал в имя
    new_name = (parts[2].strip().splitlines()[0] if parts[2].strip() else "").strip()
    if not new_name:
        await message.answer("Использование: <code>/setname @user Имя</code>"); return
    prof = await get_contact_by_username(db, uname)
    if not prof:
        await message.answer(f"Нет контакта @{uname} в базе"); return
    await upsert_contact_profile(
        db, prof["user_id"], prof.get("username") or uname, increment=False,
        name=new_name, last_contact=prof.get("last_contact"))
    updated = await get_contact_profile(db, prof["user_id"])
    await message.answer(f"✅ Имя @{uname} → <b>{_html.escape(new_name)}</b> (память бота + vault)")
    logger.info(f"Contact name set: @{uname} → {new_name}")


@router.message(Command("addfact"))
async def cmd_addfact(
    message: Message,
    db: aiosqlite.Connection,
    settings: Settings,
) -> None:
    """Добавить факт в памяти контакта (память бота + vault): /addfact @user текст."""
    if not await _is_owner(message, settings):
        return
    from db import get_contact_by_username, get_contact_profile, upsert_contact_profile
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Использование: <code>/addfact @user текст факта</code>"); return
    uname = parts[1].lstrip("@")
    fact = (parts[2].strip().splitlines()[0] if parts[2].strip() else "").strip()
    if not fact:
        await message.answer("Использование: <code>/addfact @user текст факта</code>"); return
    prof = await get_contact_by_username(db, uname)
    if not prof:
        await message.answer(f"Нет контакта @{uname} в базе"); return
    existing = (prof.get("facts") or "").strip()
    merged = (f"{existing}; {fact}" if existing else fact)[:1500]
    await upsert_contact_profile(
        db, prof["user_id"], prof.get("username") or uname, increment=False,
        facts=merged, last_contact=prof.get("last_contact"))
    updated = await get_contact_profile(db, prof["user_id"])
    await message.answer(f"✅ Факт добавлен к @{uname} (память бота + vault)")
    logger.info(f"Contact fact added: @{uname}: {fact[:60]}")


@router.message(Command("me"))
async def cmd_me(
    message: Message,
    db: aiosqlite.Connection,
    settings: Settings,
    anthropic_client: AsyncAnthropic,
) -> None:
    """🪞 Память о самом владельце (по твоим исходящим). /me build — пересобрать."""
    if not await _is_owner(message, settings):
        return
    import html as _html
    from db import get_contact_profile
    from core import richmsg
    from core.vault import enrich_owner_profile

    force = "build" in (message.text or "").lower()
    prof = await get_contact_profile(db, settings.owner_user_id)
    if force or not prof or not prof.get("summary"):
        async with richmsg.thinking(message.bot, message.chat.id,
                                    getattr(message, "message_thread_id", None), "🤖 собираю память о тебе…"):
            await enrich_owner_profile(anthropic_client, settings.anthropic_model_smart, db)
        prof = await get_contact_profile(db, settings.owner_user_id)
    if not prof or not prof.get("summary"):
        await message.answer("Пока недостаточно твоих исходящих сообщений для памяти."); return

    if prof.get("photo_file_id"):
        try:
            await message.answer_photo(prof["photo_file_id"])
        except Exception:
            pass

    name = prof.get("name") or settings.owner_name or "Владелец"
    doc = (f"<h3>🪞 Память: {_html.escape(str(name))}</h3>"
           f"<blockquote>{_html.escape(str(prof.get('summary') or ''))}</blockquote>")
    _rendered = False
    if prof.get("facts_json"):
        try:
            import json as _json
            _d = _json.loads(prof["facts_json"])
            _secs = _d.get("sections") or {}
            from core.contact_memory import UI_LABELS as _LBL   # v4.47: 12 секций, единый источник
            for _k, _lab in _LBL:
                _v = (_secs.get(_k) or "").strip().rstrip(";").strip()
                if not _v:
                    continue
                _items = "".join(f"<li>{_html.escape(it.strip())}</li>" for it in _v.split(";") if it.strip())
                doc += f"<details><summary>{_lab}</summary><ul>{_items}</ul></details>"
                _rendered = True
            if _d.get("style"):
                doc += f"<details><summary>✍️ Стиль письма</summary><p>{_html.escape(str(_d['style']).strip())}</p></details>"
                _rendered = True
            _trows = "".join(
                f"<li><b>{_html.escape(str(t.get('date') or ''))}</b> — {_html.escape((t.get('event') or '').strip())}</li>"
                for t in (_d.get("timeline") or []) if (t.get("event") or "").strip())
            if _trows:
                doc += f"<details><summary>🕐 Хронология</summary><ul>{_trows}</ul></details>"
                _rendered = True
        except Exception:
            _rendered = False
    if not _rendered and prof.get("facts"):
        doc += f"<details><summary>📌 Факты</summary><p>{_html.escape(str(prof['facts']))}</p></details>"
    doc += "<footer>обновляется ежедневно из твоих исходящих · /me build — пересобрать</footer>"
    if not await richmsg.send_rich(settings.bot_token, message.chat.id, doc, message.message_thread_id):
        txt = f"🪞 <b>{_html.escape(str(name))}</b>\n\n{_html.escape(str(prof.get('summary') or ''))}"
        if prof.get("facts"):
            txt += f"\n\n📌 {_html.escape(str(prof['facts']))[:3000]}"
        await message.answer(txt)


@router.message(lambda m: ((m.caption or "") or (m.text or "")).startswith("/setphoto"))
async def cmd_setphoto(
    message: Message,
    bot: Bot,
    db: aiosqlite.Connection,
    settings: Settings,
) -> None:
    """Поставить фото в памяти контакта: пришли ФОТО с подписью /setphoto @user
    (или ответь /setphoto @user на сообщение с фото)."""
    if not await _is_owner(message, settings):
        return
    cap = (message.caption or message.text or "")
    parts = cap.split()
    if len(parts) < 2:
        await message.answer("Использование: пришли <b>фото с подписью</b> <code>/setphoto @user</code> "
                             "(или ответь <code>/setphoto @user</code> на фото)."); return
    uname = parts[1].lstrip("@")
    photo = None
    if message.photo:
        photo = message.photo[-1]
    elif message.reply_to_message and message.reply_to_message.photo:
        photo = message.reply_to_message.photo[-1]
    if not photo:
        await message.answer("Не вижу фото. Пришли фото с подписью /setphoto @user, "
                             "или ответь этой командой на сообщение с фото."); return
    from db import get_contact_by_username
    prof = await get_contact_by_username(db, uname)
    if not prof:
        await message.answer(f"Нет контакта @{uname} в базе"); return
    try:
        from core import photos as _photos
        await _photos.attach_photo(bot, db, settings, prof["user_id"], prof.get("username") or uname, photo.file_id, src="manual")
        await message.answer(f"✅ Фото @{uname} установлено в памяти (Telegram + vault)")
        logger.info(f"ContactMemory photo set via /setphoto for @{uname}")
    except Exception as exc:
        logger.error(f"/setphoto failed for @{uname}: {exc}")
        await message.answer("Фото не встало, попробуй ещё раз.")


@router.message(Command("radar"))
async def cmd_radar(
    message: Message,
    db: aiosqlite.Connection,
    settings: Settings,
) -> None:
    """📡 Радар отношений: остывшие важные контакты, висящие обещания, без ответа."""
    if not await _is_owner(message, settings):
        return
    from core.digest import build_radar
    from core import richmsg
    doc, text = await build_radar(db, settings)
    if not await richmsg.send_rich(settings.bot_token, message.chat.id, doc, message.message_thread_id):
        await message.answer(text, disable_web_page_preview=True)


@router.message(Command("topics"))
async def cmd_topics(
    message: Message,
    db: aiosqlite.Connection,
    settings: Settings,
) -> None:
    """Включить/проверить топики в личке (после включения Topics в BotFather)."""
    if not await _is_owner(message, settings):
        return
    from core.topics import ensure_topics, TOPICS
    ok = await ensure_topics(message.bot, db, settings.owner_user_id)
    if ok:
        await message.answer(
            "📂 Топики активны: " + " · ".join(TOPICS.values()) +
            "\nУведомления разнесены по темам.")
    else:
        await message.answer(
            "📂 Топики в личке пока недоступны.\n"
            "Включи: @BotFather → /mybots → @Sort38_bot → Bot Settings → "
            "Topics → Enable, затем снова /topics.")


@router.message(Command("credits"))
async def cmd_credits(
    message: Message,
    db: aiosqlite.Connection,
    settings: Settings,
) -> None:
    if not await _is_owner(message, settings):
        return
    from db import (get_setting, set_setting, get_credits_estimate,
                    get_spending, get_spend_breakdown_month)
    from core.util import today_local_iso

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        raw = parts[1].strip().replace(",", ".").lstrip("$")
        try:
            amount = float(raw)
        except ValueError:
            await message.answer("Использование: /credits 4.92  (текущий баланс из console.anthropic.com)")
            return
        await set_setting(db, "credits_balance", str(amount))
        await set_setting(db, "credits_date", today_local_iso())
        await message.answer(
            f"🪙 Баланс кредитов зафиксирован: <b>${amount:.2f}</b> на {today_local_iso()}.\n"
            f"Бот будет показывать оценку остатка в /health, вычитая траты с этой даты."
        )
        return

    # Разбивка расхода по моделям за месяц (Haiku — классификация, Sonnet — извлечение/память/судья/черновики/ask)
    spending = await get_spending(db)
    breakdown = await get_spend_breakdown_month(db)
    fam_label = {"haiku": "Haiku 4.5", "haiku-5": "Haiku 5.5", "sonnet": "Sonnet 4.5", "sonnet-5": "Sonnet 5.5", "opus": "Opus", "opus-5-5": "Opus 5.5", "opus-5": "Opus 5"}
    lines = []
    for fam, d in sorted(breakdown.items(), key=lambda kv: -kv[1]["cost"]):
        lines.append(f"  {fam_label.get(fam, fam):7}${d['cost']:.4f}  "
                     f"<i>(in {d['in']:,} / out {d['out']:,})</i>")
    bd = ("\n".join(lines)) if lines else "  <i>пока пусто</i>"

    est = await get_credits_estimate(db)
    head = "🪙 <b>Кредиты Anthropic</b>\n"
    if est:
        head += (f"≈ ${est['remaining']:.2f} осталось "
                 f"<i>(база ${est['base']:.2f} на {est['anchor_date']}, "
                 f"потрачено ${est['spent_since']:.4f})</i>\n")
    else:
        head += "<i>баланс не задан — посмотри на console.anthropic.com и введи /credits 4.92</i>\n"
    head += "<i>⚠️ общий ключ с @snmodbot — учтён только расход этого бота, реальный остаток ниже (сверяйся с console)</i>\n"

    # Rich-версия (Bot API 10.1): разбивка таблицей. Сбой → обычный текст.
    rich_ok = False
    if breakdown:
        try:
            from core import richmsg
            rows = [[fam_label.get(fam, fam), f"${d['cost']:.4f}", f"{d['in']:,}", f"{d['out']:,}"]
                    for fam, d in sorted(breakdown.items(), key=lambda kv: -kv[1]["cost"])]
            doc = (
                f"<h3>🪙 Кредиты Anthropic</h3>"
                + (f"<p>≈ <b>${est['remaining']:.2f}</b> осталось "
                   f"(база ${est['base']:.2f} на {est['anchor_date']}, потрачено ${est['spent_since']:.4f})</p>"
                   if est else "<p>баланс не задан — console.anthropic.com → /credits 4.92</p>")
                + "<p><i>⚠️ общий ключ с @snmodbot — учтён только расход этого бота, реальный остаток ниже</i></p>"
                + richmsg.table(["Модель", "$", "in", "out"], rows, caption="📊 Расход за месяц")
                + f"<footer>Σ месяц ${spending['month_usd']:.4f} · сегодня ${spending['today_usd']:.4f} · "
                  f"обновить: /credits СУММА</footer>"
            )
            rich_ok = await richmsg.send_rich(
                settings.bot_token, message.chat.id, doc, message.message_thread_id)
        except Exception as exc:
            from loguru import logger
            logger.debug(f"rich credits skip: {exc}")
    if not rich_ok:
        await message.answer(
            f"{head}\n"
            f"📊 <b>Расход за месяц по моделям:</b>\n{bd}\n\n"
            f"Σ месяц ${spending['month_usd']:.4f} · сегодня ${spending['today_usd']:.4f}\n\n"
            f"Обновить баланс: <code>/credits СУММА</code>"
        )
# ── /links ────────────────────────────────────────────────────────────────────

@router.message(Command("links"))
async def cmd_links(
    message: Message,
    db: aiosqlite.Connection,
    settings: Settings,
) -> None:
    if not await _is_owner(message, settings):
        return
    from db import get_recent_links

    parts = (message.text or "").split(maxsplit=1)
    username = None
    limit = 20
    if len(parts) > 1:
        arg = parts[1].strip()
        if arg.isdigit():
            limit = min(int(arg), 50)
        else:
            username = arg  # /links @username

    rows = await get_recent_links(db, limit=limit, username=username)
    if not rows:
        await message.answer("Сохранённых ссылок нет" + (f" от @{username}" if username else ""))
        return

    cat_icons = {"BUSINESS": "💼", "UNCLEAR": "❓", "FRIEND": "👤", "SPAM": "🚫"}
    title = f"🔗 <b>Ссылки от @{username}</b>" if username else "🔗 <b>Последние ссылки</b>"
    lines = [f"{title} ({len(rows)}):\n"]
    for r in rows:
        icon = cat_icons.get(r["category"], "•")
        who = f"@{r['username']}" if r["username"] else "?"
        ts = (r["created_at"] or "")[:10]
        lines.append(f"{icon} {r['url']}\n   <i>{who} · {ts}</i>")
    rich_ok = False
    try:
        import html as _html
        from core import richmsg
        rows_t = []
        for r in rows:
            icon = cat_icons.get(r["category"], "•")
            who = f"@{r['username']}" if r["username"] else "?"
            url = r["url"] or ""
            label = _html.escape(url[:60] + ("…" if len(url) > 60 else ""))
            rows_t.append([icon, f'<a href="{_html.escape(url, quote=True)}">{label}</a>',
                           _html.escape(who), (r["created_at"] or "")[:10]])
        head_r = f"🔗 Ссылки от @{_html.escape(username)}" if username else "🔗 Последние ссылки"
        doc = (f"<h3>{head_r} ({len(rows)})</h3>"
               + richmsg.table(["", "Ссылка", "Кто", "Когда"], rows_t))
        rich_ok = await richmsg.try_rich(message, doc)
    except Exception as exc:
        logger.debug(f"rich links skip: {exc}")
    if not rich_ok:
        await message.answer("\n".join(lines)[:4000], disable_web_page_preview=True)


# ── /setreply ─────────────────────────────────────────────────────────────────

@router.message(Command("setreply"))
async def cmd_setreply(
    message: Message,
    db: aiosqlite.Connection,
    settings: Settings,
) -> None:
    if not await _is_owner(message, settings):
        return
    from db import get_setting, set_setting

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        current = await get_setting(db, "business_reply") or settings.business_reply_template
        await message.answer(
            f"📝 <b>Текущий авто-ответ на BUSINESS:</b>\n\n"
            f"<i>{current}</i>\n\n"
            f"Изменить: <code>/setreply твой текст</code>\n"
            f"Выключить: <code>/setreply off</code>\n\n"
            f"⚠️ Отправляется только в режиме <b>active</b>, 1 раз на человека в сутки"
        )
        return

    new_text = parts[1].strip()
    if new_text.lower() in ("off", "выкл", "отключить"):
        await set_setting(db, "business_reply", "")
        await message.answer("🔕 Авто-ответ на BUSINESS отключён")
    else:
        await set_setting(db, "business_reply", new_text)
        await message.answer(f"✅ Авто-ответ обновлён:\n\n<i>{new_text}</i>")
    logger.info("Owner updated business reply template")


# ── /correct ──────────────────────────────────────────────────────────────────

@router.message(Command("correct"))
async def cmd_correct(
    message: Message,
    db: aiosqlite.Connection,
    settings: Settings,
) -> None:
    if not await _is_owner(message, settings):
        return

    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(
            "Использование: /correct @username КАТЕГОРИЯ\n"
            "Категории: BUSINESS | FRIEND | SPAM | UNCLEAR"
        )
        return

    raw_user = parts[1].lstrip("@")
    new_cat = parts[2].strip().upper()
    valid = {"BUSINESS", "FRIEND", "SPAM", "UNCLEAR"}
    if new_cat not in valid:
        await message.answer(f"Неверная категория. Доступно: {', '.join(valid)}")
        return

    # Find last logged message for this user (id + text) — robust without UPDATE..LIMIT
    async with db.execute(
        "SELECT id, user_id, message_text FROM message_log WHERE username = ? ORDER BY created_at DESC LIMIT 1",
        (raw_user,),
    ) as cur:
        row = await cur.fetchone()

    if row:
        log_id, corr_user_id, corr_text = row[0], row[1] or 0, row[2] or ""
        await db.execute("UPDATE message_log SET category = ? WHERE id = ?", (new_cat, log_id))
        await db.commit()
        # Save as few-shot example for the classifier to learn from
        from db import save_correction
        await save_correction(db, corr_user_id, raw_user, corr_text, new_cat)
    else:
        await message.answer(f"⚠️ Нет сообщений от @{raw_user} в логе")
        return

    if new_cat == "FRIEND":
        from core import whitelist
        if corr_user_id:   # v4.33: раньше писали user_id=0 → whitelist не работал
            await whitelist.add(db, user_id=corr_user_id, username=raw_user, reason="manual")
            await message.answer(f"✅ @{raw_user} → {new_cat} (whitelist + бот запомнил пример)")
        else:
            await message.answer(f"✅ @{raw_user} → {new_cat} (пример запомнен; в whitelist не добавила — нет user_id)")
    else:
        await message.answer(f"✅ Исправлено: @{raw_user} → {new_cat} (бот запомнил пример)")
    logger.info(f"Owner corrected @{raw_user} → {new_cat}")


# ── /last ─────────────────────────────────────────────────────────────────────

@router.message(Command("last"))
async def cmd_last(
    message: Message,
    db: aiosqlite.Connection,
    settings: Settings,
) -> None:
    if not await _is_owner(message, settings):
        return

    parts = (message.text or "").split(maxsplit=1)
    limit = 10
    if len(parts) > 1 and parts[1].strip().isdigit():
        limit = min(int(parts[1].strip()), 50)

    rows = await get_recent_messages(db, limit=limit)
    if not rows:
        await message.answer("Нет сообщений в логе")
        return

    cat_icons = {"SPAM": "🚫", "BUSINESS": "💼", "UNCLEAR": "❓", "FRIEND": "👥"}
    lines = [f"📋 <b>Последние {len(rows)} сообщений:</b>\n"]
    for r in rows:
        uname = f"@{r['username']}" if r["username"] else f"id{r['user_id']}"
        cat = r["category"] or "?"
        icon = cat_icons.get(cat, "•")
        rule = r["rule_matched"] or "?"
        ts = r["created_at"][:16] if r["created_at"] else "?"
        lines.append(f"{icon} {uname} [{rule}] {ts}")

    rich_ok = False
    try:
        import html as _html
        from core import richmsg
        rows_t = [[cat_icons.get(r["category"] or "?", "•") + " "
                   + (f"@{r['username']}" if r["username"] else f"id{r['user_id']}"),
                   _html.escape(r["rule_matched"] or "?"),
                   (r["created_at"] or "?")[:16]] for r in rows]
        doc = (f"<h3>📋 Последние {len(rows)} сообщений</h3>"
               + richmsg.table(["Кто", "Правило", "Когда"], rows_t))
        rich_ok = await richmsg.try_rich(message, doc)
    except Exception as exc:
        logger.debug(f"rich last skip: {exc}")
    if not rich_ok:
        await message.answer("\n".join(lines))


def _app_kb(settings: Settings, query: str):
    """v4.112: кнопка «📱 Открыть в приложении» на нужный экран мини-приложения (или None без WEBAPP_URL)."""
    base = (getattr(settings, "webapp_url", "") or "").strip()
    if not base:
        return None
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
    url = base.split("#")[0].rstrip("/") + "/?" + query
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📱 Открыть в приложении", web_app=WebAppInfo(url=url))]])


@router.message(Command("day", "день"))
async def cmd_day(message: Message, db: aiosqlite.Connection, settings: Settings) -> None:
    """v4.106: виджет «Мой день» (Bot API 10.4) — задачи на сегодня, закрыть или перенести нажатием."""
    if not await _is_owner(message, settings):
        return
    from core import daywidget
    ok, err = await daywidget.send(db, settings, message.chat.id, message.message_thread_id)
    if not ok:
        from core import widgets as _W
        if _W.refused(err):   # v4.110: Telegram с 13.11 не пускает виджеты ботов — то же текстом
            await message.answer(await daywidget.text(db), parse_mode=None, reply_markup=_app_kb(settings, "tab=den"))
        else:
            await message.answer(f"Виджет не отправился: {err[:200]}", parse_mode=None)


@router.message(Command("habits", "привычки"))
async def cmd_habits(message: Message, db: aiosqlite.Connection, settings: Settings) -> None:
    """v4.107: виджет «Привычки» — неделя кружками, нажатие отмечает день."""
    if not await _is_owner(message, settings):
        return
    from core.widgets import habits as _wh
    ok, err = await _wh.send(db, settings, message.chat.id, message.message_thread_id)
    if not ok:
        from core import widgets as _W
        if _W.refused(err):   # v4.110: Telegram с 13.11 не пускает виджеты ботов — то же текстом
            await message.answer(await _wh.text(db), parse_mode=None)
        else:
            await message.answer(f"Виджет не отправился: {err[:200]}", parse_mode=None)


@router.message(Command("market", "рынок"))
async def cmd_market(message: Message, db: aiosqlite.Connection, settings: Settings) -> None:
    """v4.107: виджет «Рынок» — крипта, индексы, золото, нефть, доллар с графиками за 1Д/7Д/1М."""
    if not await _is_owner(message, settings):
        return
    from core.widgets import market as _wm
    ok, err = await _wm.send(db, settings, message.chat.id, message.message_thread_id)
    if not ok:
        from core import widgets as _W
        if _W.refused(err):   # v4.110: Telegram с 13.11 не пускает виджеты ботов — то же текстом
            await message.answer(await _wm.text(), parse_mode=None, reply_markup=_app_kb(settings, "open=market"))
        else:
            await message.answer(f"Виджет не отправился: {err[:200]}", parse_mode=None)




@router.message(Command("обещания", "promises", "promised"))
async def cmd_promises(message: Message, db: aiosqlite.Connection, settings: Settings) -> None:
    """🤝 Открытые обещания владельца = открытые задачи-обещания. Каждое с кнопками ✅/📅 —
    закрываешь прямо в боте (как и в Todoist: метка «обещание»). Источник живой, не виснет."""
    if not await _is_owner(message, settings):
        return
    from db import get_open_commitments
    items = await get_open_commitments(db, limit=20)
    if not items:
        await message.answer("🤝 Открытых обещаний нет — чисто!")
        return
    await message.answer(f"🤝 <b>Твои обещания ({len(items)})</b> — закрой ✅ или перенеси 📅:", parse_mode="HTML")
    for c in items:
        who = c.get("name") or (f"@{c['username']}" if c.get("username") else "")
        sid = c.get("id")
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Выполнено", callback_data=f"td:{sid}"),
            InlineKeyboardButton(text="📅 Перенести", callback_data=f"snm:{sid}"),
        ]]) if sid else None
        txt = f"🤝 {_html.escape(str(c['commitments']))}" + (f"\n👤 {_html.escape(str(who))}" if who else "")
        await message.answer(txt, reply_markup=kb)


@router.message(Command("mirastats"))
async def cmd_mirastats(message: Message, db: aiosqlite.Connection, settings: Settings) -> None:
    """v4.44 Ф5: телеметрия Мира-чата за 7 дней — ответов, инструменты, откаты на старый роутер."""
    if not await _is_owner(message, settings):
        return
    from core import mira as _mira
    st = await _mira.stats(db, days=7)
    tools = "\n".join(f"  • {k}: {v}" for k, v in st["tools"].items()) or "  —"
    await message.reply(f"🗣 Мира-чат за 7 дней (v2={'on' if getattr(settings, 'mira_v2', False) else 'off'})\n"
                        f"Ответов: {st['replies']}\nОткатов на старый роутер: {st['fallbacks']}\nИнструменты:\n{tools}",
                        parse_mode=None)


@router.message(Command("routines"))
async def cmd_routines(message: Message, db: aiosqlite.Connection, settings: Settings) -> None:
    """v4.72: /routines — что Мира пишет сама; /routines del N — убрать. Добавляются фразой в чате."""
    if not await _is_owner(message, settings):
        return
    from core import routines as _routines
    parts = (message.text or "").split()
    if len(parts) == 3 and parts[1].lower() == "del" and parts[2].isdigit():
        await message.reply(await _routines.remove(db, parts[2]), parse_mode=None)
        return
    await message.reply("⏰ " + await _routines.list_text(db), parse_mode=None)


@router.message(Command("subs"))
async def cmd_subs(message: Message, db: aiosqlite.Connection, settings: Settings) -> None:
    """v4.59: /subs — подписки (rich-таблица с кнопками ✖); /subs all — включая отменённые; /subs spent [дней]."""
    if not await _is_owner(message, settings):
        return
    from core import subscriptions as _subs
    from core import richmsg as _rm
    parts = (message.text or "").split()
    sub = parts[1].lower() if len(parts) > 1 else ""
    if sub == "spent":
        await message.reply("💳 " + await _subs.spent(db, int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 30), parse_mode=None)
        return
    await _subs.roll_forward(db)
    subs = await _subs.list_all(db, active_only=(sub != "all"))
    if not await _rm.send_rich(settings.bot_token, message.chat.id, _subs.render_rich(subs), message.message_thread_id):
        await message.reply(_subs.plain_list(subs), parse_mode=None)


@router.message(Command("youtube"))
async def cmd_youtube(message: Message, db: aiosqlite.Connection, settings: Settings) -> None:
    """v4.95: вход Миры в YouTube её отдельным каналом (Google OAuth, адрес localhost присылают обратно)."""
    from core import owners
    if not owners.is_owner(settings.owner_user_id, message.from_user.id):
        return
    from core import social as _social, youtube_chat
    if not youtube_chat.ready(settings):
        await message.reply("YouTube не настроен: нужны GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET и ключ YouTube в .env", parse_mode=None); return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        if not await _social.youtube_exchange_code(db, settings, parts[1], youtube_chat.KEY):
            await message.reply("⚠️ Код не принят, напиши /youtube ещё раз", parse_mode=None); return
        try:
            u = await youtube_chat.save_login(db, settings)
        except youtube_chat.YouTubeError as exc:
            await message.reply(f"⚠️ {exc}", parse_mode=None); return
        await message.reply(f"✅ Вошла в YouTube как {u['title']}. Чат начну читать, когда начнётся эфир.\n"
                            "Проверь, что этот канал — модератор: YouTube Studio → Настройки → Сообщество → Модераторы", parse_mode=None)
        return
    await message.reply("1) Открой ссылку, войдя в Google аккаунтом Миры (не своим), и разреши доступ. "
                        "Если Google скажет, что приложение не проверено: «Дополнительно» → «Перейти».\n"
                        + _social.youtube_auth_url(settings, youtube_chat.SCOPES) +
                        "\n2) Браузер уйдёт на http://localhost/?code=… и покажет ошибку, это нормально. Скопируй весь адрес и пришли:\n/youtube <адрес>",
                        parse_mode=None)


@router.message(Command("twitch"))
async def cmd_twitch(message: Message, db: aiosqlite.Connection, settings: Settings) -> None:
    """v4.93: вход Миры в Twitch её отдельным аккаунтом (код на twitch.tv/activate)."""
    from core import owners
    if not owners.is_owner(settings.owner_user_id, message.from_user.id):
        return
    from core import social as _social, twitch_chat
    if not _social.tw_enabled(settings):
        await message.reply("Twitch не настроен: нужны TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET / TWITCH_LOGIN в .env", parse_mode=None); return
    d = await _social.twitch_device_start(settings, twitch_chat.SCOPES)
    if not d.get("device_code"):
        await message.reply(f"Twitch не выдал код: {str(d)[:200]}", parse_mode=None); return
    await message.reply(f"Войди на Twitch аккаунтом Миры (не своим), открой {d.get('verification_uri')} и введи код {d.get('user_code')}.\n"
                        "Жду до 10 минут.", parse_mode=None)
    import asyncio as _aio
    step = max(5, int(d.get("interval", 5)))
    for _ in range(600 // step):
        await _aio.sleep(step)
        if await _social.twitch_device_poll(db, settings, d["device_code"], twitch_chat.KEY):
            u = await twitch_chat.save_login(db, settings)
            warn = ("\n⚠️ Это твой основной аккаунт: я буду писать в чат от твоего имени. Лучше войти отдельным."
                    if u["login"].lower() == (settings.twitch_login or "").lower() else "")
            await message.reply(f"✅ Вошла в Twitch как {u['display_name']}. Чат начну слушать в течение пары минут.\n"
                                f"Проверь, что у него есть модератор: в чате канала /mod {u['login']}{warn}", parse_mode=None)
            return
    await message.reply("⌛ Не дождалась подтверждения, напиши /twitch ещё раз", parse_mode=None)


@router.message(Command("social"))
async def cmd_social(message: Message, db: aiosqlite.Connection, settings: Settings) -> None:
    """v4.53: /social — сводка за 7 дн.; /social collect — собрать сейчас; /social auth twitch — авторизация (фолловеры)."""
    if not await _is_owner(message, settings):
        return
    from core import social as _social
    parts = (message.text or "").split()
    sub = parts[1].lower() if len(parts) > 1 else ""
    if sub == "collect":
        res = await _social.collect_all(db, settings, message.bot)
        await message.reply("📊 Собрала: " + (_html.escape(str(res)) if res else "ничего — ключи не заданы (YOUTUBE_API_KEY / TWITCH_CLIENT_ID…)"), parse_mode=None)
        return
    if sub == "auth" and len(parts) > 2 and parts[2].lower() == "youtube":
        if not _social.yta_enabled(settings):
            await message.reply("YouTube Analytics не настроен: нужны GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET в .env", parse_mode=None); return
        if len(parts) > 3:   # /social auth youtube <код или URL localhost/?code=…>
            ok = await _social.youtube_exchange_code(db, settings, " ".join(parts[3:]))
            await message.reply("✅ YouTube Analytics подключён — аналитика начнёт собираться с 06:00" if ok else "⚠️ Код не принят — попробуй ещё раз", parse_mode=None); return
        await message.reply("1) Открой ссылку и разреши доступ (аккаунт канала):\n" + _social.youtube_auth_url(settings) +
                            "\n2) Браузер уйдёт на http://localhost/?code=… и покажет ошибку — это нормально. Скопируй ВЕСЬ адрес из строки браузера и пришли:\n/social auth youtube <адрес>", parse_mode=None); return
    if sub == "auth" and len(parts) > 2 and parts[2].lower() == "twitch":
        if not _social.tw_enabled(settings):
            await message.reply("Twitch не настроен: нужны TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET / TWITCH_LOGIN в .env", parse_mode=None); return
        d = await _social.twitch_device_start(settings)
        if not d.get("device_code"):
            await message.reply(f"Twitch device-flow не стартовал: {str(d)[:200]}", parse_mode=None); return
        await message.reply(f"Открой {d.get('verification_uri')} и введи код: {d.get('user_code')}\nЖду до 10 минут…", parse_mode=None)
        import asyncio as _aio
        for _ in range(int(600 / max(5, int(d.get("interval", 5))))):
            await _aio.sleep(max(5, int(d.get("interval", 5))))
            if await _social.twitch_device_poll(db, settings, d["device_code"]):
                await message.reply("✅ Twitch авторизован — фолловеры будут в статистике", parse_mode=None); return
        await message.reply("⌛ Не дождалась подтверждения — повтори /social auth twitch", parse_mode=None); return
    days = int(sub) if sub.isdigit() else 7
    await message.reply("📊 " + await _social.summary(db, "all", days), parse_mode=None)
