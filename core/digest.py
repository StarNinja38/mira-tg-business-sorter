import asyncio
from datetime import datetime, timedelta, date
from html import escape as _hesc   # v4.33: plain-тексты сводок при глобальном parse_mode=HTML
from zoneinfo import ZoneInfo
from typing import Optional

import aiohttp
from core import llm
from loguru import logger
from core.util import today_local
from core.usage import record as _rec
from core.llm import text_of as _llm_text   # v4.61: читать ответ по типу блока (Claude 5 может начать с thinking)


def _split_due_buckets(tasks: list[dict], today) -> tuple[list, list, list]:
    """Открытые задачи с due → (сегодня, завтра, просрочено) по локальной дате.
    Замена Notion-запросов: с ретайра v3.22 дайджест молча писал «Дедлайнов нет» (P0.1 аудита 05.07)."""
    from datetime import date as _date
    due_today, due_tomorrow, overdue = [], [], []
    tomorrow = today + timedelta(days=1)
    for t in tasks:
        try:
            dd = _date.fromisoformat(str(t["due_date"])[:10])
        except Exception:
            continue
        if dd == today:
            due_today.append(t)
        elif dd == tomorrow:
            due_tomorrow.append(t)
        elif dd < today:
            overdue.append(t)
    return due_today, due_tomorrow, overdue


def _day_start(d, settings) -> str:
    """Начало локальных суток d как RFC3339 с офсетом (для GCal и сравнения с UTC-метками в БД)."""
    from datetime import time as _t
    return datetime.combine(d, _t.min, ZoneInfo(getattr(settings, "timezone", "Europe/Moscow"))).isoformat()


_RU_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def _plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    return few if 2 <= n10 <= 4 and not 12 <= n100 <= 14 else many


def _digest_gist(today_count: int, waits: int, overdue: int) -> str:
    """Одна фраза о главном: с неё читается вся сводка."""
    bits = []
    if today_count:
        bits.append(f"сегодня {today_count} {_plural(today_count, 'дело', 'дела', 'дел')}")
    if waits:
        bits.append(f"{waits} {_plural(waits, 'человек ждёт', 'человека ждут', 'человек ждут')} ответа")
    if overdue:
        bits.append(f"{overdue} {_plural(overdue, 'дело просрочено', 'дела просрочены', 'дел просрочено')}")
    if not bits:
        return "Срочного нет, спокойный день 🌿"
    text = ", ".join(bits)
    return text[0].upper() + text[1:] + "."


def _rich_digest(today, wx, *, overdue, due_today, due_tomorrow, bd_today, bd_tmr, pair_lines,
                 commits, pending, cal_events, task_names, conflict_lines, rain_lines, waits, silent,
                 todoist_lines, social_lines, sub_lines, tip, market_lines=(), market_news="") -> str:
    """v4.83: сводка читается сверху вниз: обращение, главное, день, люди, остальное свёрнуто.
    Таблицы убраны: на телефоне они дробили сводку на равноправные блоки. Внутри раздела список,
    сами разделы разведены пустым абзацем: отступы rich-сообщения не настраиваются."""
    from core import richmsg
    from core.calendar import _norm_summary
    from core.dates import slug_for, turning_age
    from core.util import _RU_MONTHS, profile_link
    _e, B = _hesc, richmsg.button

    def _more(items, limit):
        rest = len(items) - limit
        return f"<li><i>и ещё {rest}</i></li>" if rest > 0 else ""

    def _task_li(t, when=""):
        btns = (" " + B("✓", f"td:{t['id']}", "success") + " " + B("→ завтра", f"tmr:{t['id']}")) if t.get("id") else ""
        who = f" · {_e(str(t['from']))}" if t.get("from") else ""
        return f"<li>{f'<b>{when}</b> ' if when else ''}{_e(t['title'])}{who}{btns}</li>"

    def _who(w):
        return f'<a href="{profile_link(w.get("username"), w.get("user_id"))}"><b>{_e(w["who"])}</b></a>'

    def _first(w):
        return _e((w.get("who") or "").split()[0][:12]) if (w.get("who") or "").strip() else ""

    def _fold(title, items, opened=False):
        return f"<details{' open' if opened else ''}><summary>{title}</summary><ul>{items}</ul></details>"

    # День одной лентой по времени: пары, встречи и задачи вместе
    timed, untimed = [], []
    for pl in pair_lines:
        timed.append((pl[:5], f"<li><b>{_e(pl[:5])}</b> 🎓 {_e(pl[6:])}</li>"))
    for e in cal_events[:6]:
        name = str(e.get("summary") or "")
        if _norm_summary(name) in task_names:
            continue  # событие, которое бот сам создал для задачи: показываем задачу с кнопками
        st = str(e.get("start") or "")
        if "T" in st:
            timed.append((st[11:16], f"<li><b>{st[11:16]}</b> 📆 {_e(name[:60])}</li>"))
        else:
            untimed.append(f"<li>📆 {_e(name[:60])}, весь день</li>")
    for t in due_today[:6]:
        dd = str(t.get("due_date") or "")
        if "T" in dd:
            timed.append((dd[11:16], _task_li(t, dd[11:16])))
        else:
            untimed.append(_task_li(t))
    timed.sort(key=lambda x: x[0])
    day_items = "".join(li for _, li in timed) + "".join(untimed) + _more(due_today, 6)

    weather = ""
    if wx:
        from core import weather as _w
        weather = _w.summary_day(wx, 0)
    head = ("<p>Доброе утро, босс ☀️</p><p>&nbsp;</p>"
            f"<p>Сегодня {_RU_WEEKDAYS[today.weekday()]}, {today.day} {_RU_MONTHS[today.month]}"
            + (f". {_e(weather)}" if weather else "") + "</p>"
            f"<p><b>{_e(_digest_gist(len(timed) + len(untimed), len(waits), len(overdue)))}</b></p>")
    parts = [head]

    bdays = []
    for b in bd_today:
        age = turning_age(b, today.year)
        bdays.append(f"<p>🎂 <b>Сегодня день рождения у {_e(b['who'])}" + (f", {age}!" if age else "!") + "</b> "
                     + B("✍️ Поздравление", f"bdg:{b.get('slug') or slug_for(b['who'], b.get('uid'))}", "primary") + "</p>")
    for b in bd_tmr:
        bdays.append(f"<p>🎁 Завтра день рождения у {_e(b['who'])}</p>")
    if bdays:
        parts.append("".join(bdays))

    day = f"<h4>Сегодня</h4><ul>{day_items}</ul>" if day_items else ""
    if conflict_lines:
        day += "<p>⚠️ Пересекаются: " + "; ".join(_e(c) for c in conflict_lines[:3]) + "</p>"
    if rain_lines:
        day += "<p>🌧 Дождь в это время: " + "; ".join(_e(r) for r in rain_lines[:3]) + "</p>"
    if day:
        parts.append(day)

    people = []
    for w in waits[:5]:
        uid = w.get("user_id")
        btn = (" " + B(f"✍️ ответить {_first(w)}".strip(), f"rep:{uid}", "primary")) if uid else ""
        people.append(f"<li>{_who(w)}, {w['age_days']} дн.: «{_e(w['text'][:160])}»{btn}</li>")
    for w in silent[:5]:
        uid = w.get("user_id")
        btn = (" " + B(f"✍️ пинг {_first(w)}".strip(), f"png:{uid}", "primary") + " " + B("🙈", f"pngno:{uid}")) if uid else ""
        people.append(f"<li>{_who(w)} молчит {w['age_days']} дн.: «{_e(w['text'][:120])}»{btn}</li>")
    for c in commits[:5]:
        who = c["name"] or ("@" + c["username"] if c["username"] else "?")
        people.append(f"<li>Ты обещал, {_e(who)}: {_e(c['commitments'][:90])}</li>")
    if people:
        parts.append(f"<h4>Люди</h4><ul>{''.join(people)}</ul>")
    if market_lines:
        parts.append(f"<h4>📈 Рынок</h4><ul>{''.join(f'<li>{_e(l)}</li>' for l in market_lines)}</ul>")

    # Остальное свёрнуто, в заголовке видно сколько. Короткий список просрочки оставляем открытым.
    folds = []
    if overdue:
        folds.append(_fold(f"🔴 Просрочено ({len(overdue)})",
                           "".join(_task_li(t) for t in overdue[:6]) + _more(overdue, 6), opened=len(overdue) <= 3))
    if due_tomorrow:
        folds.append(_fold(f"Завтра ({len(due_tomorrow)})",
                           "".join(_task_li(t) for t in due_tomorrow[:4]) + _more(due_tomorrow, 4)))
    if pending:
        items = "".join(f"<li>{_e(l)} {B('➕', f'somiss:{tok}', 'success')} {B('✖', f'somissno:{tok}')}</li>"
                        for l, tok in pending[:5])
        folds.append(_fold(f"Ждут твоего решения ({len(pending)})", items))
    if todoist_lines:
        folds.append(_fold(f"Ещё из Todoist ({len(todoist_lines)})", "".join(f"<li>{_e(l)}</li>" for l in todoist_lines)))
    if sub_lines:
        folds.append(_fold(f"💳 Скоро списание ({len(sub_lines)})", "".join(f"<li>{_e(l)}</li>" for l in sub_lines)))
    if market_news:
        items = [l.strip(" -•") for l in market_news.split("\n") if l.strip()]
        folds.append(_fold("📰 Новости рынка", "".join(f"<li>{_e(l[:400])}</li>" for l in items[:8]), opened=True))
    if social_lines:
        folds.append(_fold("Соцсети вчера", "".join(f"<li>{_e(l)}</li>" for l in social_lines)))
    if folds:
        parts.append("".join(folds))
    if tip:
        parts.append(f"<p>💡 {_e(tip)}</p>")
    return "<p>&nbsp;</p>".join(parts)   # пустой абзац между разделами


async def _send_morning_digest(bot, owner_id: int, db, settings) -> None:
    from core import autonomy
    if not await autonomy.enabled(db, "digest"):
        return
    from db import get_stats, get_open_commitments, get_open_tasks_due
    today = today_local()
    stats = await get_stats(db, days=1)
    counts = stats["counts"]
    total = sum(counts.values())

    due_today, due_tomorrow, _overdue = _split_due_buckets(await get_open_tasks_due(db), today)

    text = f"☀️ <b>Доброе утро! Сводка от Миры · {today.strftime('%d.%m.%Y')}</b>\n\n"
    text += "Доброе утро ☀️\n"
    # 🌤 v4.29 погода (по запросу владельца): строка сегодня + дождь-маркеры к делам со временем
    wx, rain_hours = {}, set()
    try:
        from core import weather
        wx = await weather.get_forecast(settings)
        if wx:
            _ws = weather.summary_day(wx, 0)
            if _ws:
                text += f"{_ws}\n"
            rain_hours = weather.rain_hours_today(wx)
    except Exception as exc:
        logger.debug(f"digest weather skip: {exc}")
    text += "\n"

    # 🎂 v4.30 дни рождения: сегодня + завтра (данные — settings_kv bday:*, вводятся через Миру)
    bd_today, bd_tmr = [], []
    try:
        from core.dates import all_bdays, bdays_on, fmt_mmdd, turning_age
        _bds = await all_bdays(db)
        if _bds:
            _tmr = today + timedelta(days=1)
            bd_today = bdays_on(_bds, f"{today.month:02d}-{today.day:02d}")
            bd_tmr = bdays_on(_bds, f"{_tmr.month:02d}-{_tmr.day:02d}")
            for b in bd_today:
                age = turning_age(b, today.year)
                text += f"🎂 <b>Сегодня день рождения у {_hesc(b['who'])}" + (f" — {age}!" if age else "!") + "</b>\n"
            for b in bd_tmr:
                text += f"🎁 Завтра др у {_hesc(b['who'])} ({fmt_mmdd(b['mmdd'])}) — подумай про поздравление\n"
            if bd_today or bd_tmr:
                text += "\n"
    except Exception as exc:
        logger.debug(f"digest bday skip: {exc}")

    # 🎓 v4.31 пары МИРЭА (data/schedule.json, чёт/нечёт от начала семестра). Ноль сети/LLM.
    pair_lines = []
    try:
        from core import schedule as _sched
        pair_lines = _sched.digest_lines(today)
        if pair_lines:
            text += f"🎓 <b>Пары сегодня ({len(pair_lines)}):</b>\n"
            for pl in pair_lines:
                text += f"  • {_hesc(pl)}\n"
            text += "\n"
    except Exception as exc:
        logger.debug(f"digest schedule skip: {exc}")

    if due_today:
        text += f"⏰ <b>Сегодня ({len(due_today)}):</b>\n"
        for t in due_today[:5]:
            text += f"  • {_hesc(t['title'])} — {_hesc(str(t['from']))}\n"
        text += "\n"

    if due_tomorrow:
        text += f"📅 <b>Завтра ({len(due_tomorrow)}):</b>\n"
        for t in due_tomorrow[:5]:
            text += f"  • {_hesc(t['title'])} — {_hesc(str(t['from']))}\n"
        text += "\n"

    if not due_today and not due_tomorrow:
        text += "✅ Дедлайнов нет\n\n"

    # 🔴 Просрочено (из bot.db)
    overdue = _overdue
    if overdue:
        text += f"🔴 <b>Просрочено ({len(overdue)}):</b>\n"
        for t in overdue[:5]:
            text += f"  • {_hesc(t['title'])} — {_hesc(str(t['from']))}\n"
        text += "\n"

    # ⚠️ Висящие обещания владельца (память-леджер)
    commits = await get_open_commitments(db)
    if commits:
        text += "⚠️ <b>Ты обещал:</b>\n"
        for c in commits[:5]:
            who = c["name"] or (f"@{c['username']}" if c["username"] else "?")
            text += f"  • {_hesc(who)}: {_hesc(c['commitments'][:90])}\n"
        text += "<i>закрыть: /обещания</i>\n\n"

    # ⏳ v4.19: непринятые предложения «второго мнения» — данные показали (3 предложения / 0
    # принято за 3 недели), что ночные карточки тонут. Повторяем их в утренней сводке.
    pend_lines, pend_tokens = [], []   # v4.34: токен нужен кнопке ➕ прямо в rich-сводке
    try:
        import json as _json
        import time as _time
        async with db.execute("SELECT key, value FROM settings_kv WHERE key LIKE 'somiss:%'") as cur:
            for key, raw in await cur.fetchall():
                try:
                    p = _json.loads(raw)
                except Exception:
                    continue
                if _time.time() - (p.get("ts") or 0) > 3 * 86400:
                    try:   # C20: протухшее предложение удаляем — иначе settings_kv копит somiss:* навсегда
                        await db.execute("DELETE FROM settings_kv WHERE key=?", (key,)); await db.commit()
                    except Exception:
                        pass
                    continue
                for t in p.get("tasks", [])[:3]:
                    pend_lines.append(f"{t.get('title', '?')} — @{p.get('username', '?')}")
                    pend_tokens.append(key.split(":", 1)[1])
    except Exception as exc:
        logger.debug(f"digest pending skip: {exc}")
    if pend_lines:
        text += f"⏳ <b>Ждут твоего решения ({len(pend_lines)}):</b>\n"
        for l in pend_lines[:5]:
            text += f"  • {_hesc(l)}\n"
        text += "<i>карточки с кнопками ➕ — в теме 📋 Задачи</i>\n\n"

    # 📆 v4.24: события Google Calendar на сегодня (ассистент открывает день ЦЕЛИКОМ,
    # а не только задачи бота) + детект пересечений по времени (задачи+события, Δ<60 мин)
    cal_events, conflict_lines = [], []
    try:
        if settings.google_calendar_id:
            from core.calendar import list_events
            cal_events = await list_events(
                settings.google_service_account_path, settings.google_calendar_id,
                _day_start(today, settings), _day_start(today + timedelta(days=1), settings))   # C2: локальные сутки, не UTC
    except Exception as exc:
        logger.debug(f"digest calendar skip: {exc}")
    timed = []
    # v4.45.2: событие календаря, которое бот САМ создал для задачи (то же название), — не второй участник
    # конфликта: «19:00 задача „Урок английского“ ↔ 19:00 событие „Урок английского“» — одно и то же дело.
    from core.calendar import _norm_summary
    task_names = {_norm_summary(t.get("title") or "") for t in due_today}
    for e in cal_events:
        st = str(e.get("start") or "")
        if "T" in st and _norm_summary(e.get("summary") or "") not in task_names:
            timed.append((st[11:16], f"событие «{e['summary'][:40]}»"))
    for t in due_today:
        dd = str(t.get("due_date") or "")
        if "T" in dd:
            timed.append((dd[11:16], f"задача «{t['title'][:40]}»"))
    # v4.31: пары — тоже занятость (пересечения «встреча во время пары» + дождь-маркеры)
    for pl in pair_lines:
        timed.append((pl[:5], f"пара «{pl[6:46]}»"))
    timed.sort()
    for (t1, a), (t2, b) in zip(timed, timed[1:]):
        try:
            m1 = int(t1[:2]) * 60 + int(t1[3:5]); m2 = int(t2[:2]) * 60 + int(t2[3:5])
            if 0 <= m2 - m1 < 60:
                conflict_lines.append(f"{t1} {a} ↔ {t2} {b}")
        except Exception:
            pass
    if cal_events:
        text += f"📆 <b>Календарь сегодня ({len(cal_events)}):</b>\n"
        for e in cal_events[:6]:
            st = str(e.get("start") or "")
            when = st[11:16] if "T" in st else "весь день"
            text += f"  • {when} {_hesc(str(e.get('summary') or '')[:60])}\n"
        text += "\n"
    if conflict_lines:
        text += "⚠️ <b>Пересекаются по времени:</b>\n"
        for c in conflict_lines[:3]:
            text += f"  • {_hesc(c)}\n"
        text += "\n"

    # 🌧 v4.29: дождь в час твоего дела со временем → предупреждение (контекстная погода)
    rain_lines = []
    if rain_hours and timed:
        _rw = [f"{t} {label}" for t, label in timed
               if t[:2].isdigit() and int(t[:2]) in rain_hours]
        rain_lines = _rw
        if _rw:
            text += "🌧 <b>Дождь в это время:</b>\n"
            for r in _rw[:3]:
                text += f"  • {_hesc(r)}\n"
            text += "<i>возьми зонт / учти дорогу</i>\n\n"

    # 💬 v4.22 awaiting-reply: кто ждёт твоего ответа (вопрос без ответа ≥24ч)
    waits = []
    try:
        from db import get_unanswered_questions
        waits = await get_unanswered_questions(db, hours=24, limit=5)
    except Exception as exc:
        logger.debug(f"digest awaiting skip: {exc}")
    if waits:
        text += f"💬 <b>Ждут твоего ответа ({len(waits)}):</b>\n"
        from core.util import profile_link as _pl
        for w in waits:
            text += (f'  • <a href="{_pl(w.get("username"), w.get("user_id"))}">{_hesc(w["who"])}</a>'
                     f" ({w['age_days']} дн.): {_hesc(w['text'][:50])}\n")
        text += "\n"

    # 📊 v4.53 соцсети вчера (только если данные собираются)
    social_lines = []
    try:
        from core import social as _social
        social_lines = await _social.digest_lines(db)
    except Exception as exc:
        logger.debug(f"digest social skip: {exc}")
    if social_lines:
        text += "📊 <b>Соцсети вчера:</b>\n" + "".join(f"  • {_hesc(l)}\n" for l in social_lines) + "\n"

    # 📈 v4.100 рынок: крипта, индексы, золото, нефть (CoinMarketCap/CoinGecko + Yahoo) и новости (Perplexity)
    market_lines, market_news = [], ""
    try:
        from anthropic import AsyncAnthropic
        from core import market as _market
        _mclient = AsyncAnthropic(api_key=settings.anthropic_api_key)
        try:
            market_lines, market_news = await _market.morning(db, settings, _mclient)
        finally:
            await _mclient.close()
    except Exception as exc:
        logger.warning(f"digest market skip: {exc}")
    if market_lines:
        text += "📈 <b>Рынок:</b>\n" + "".join(f"  • {_hesc(l)}\n" for l in market_lines) + "\n"
    if market_news:
        text += "📰 <b>Новости рынка:</b>\n" + _hesc(market_news[:1500]) + "\n\n"

    # 💳 v4.59 подписки: списания сегодня / в ближайшие remind_days
    try:
        from core import subscriptions as _subs
        _sub_lines = await _subs.digest_lines(db)
    except Exception as exc:
        _sub_lines = []
        logger.debug(f"digest subs skip: {exc}")
    if _sub_lines:
        text += "💳 <b>Подписки — скоро списание:</b>\n" + "".join(f"  • {_hesc(l)}\n" for l in _sub_lines) + "\n"

    # 📋 v4.75 дела из остальных проектов Todoist: сегодня и срочное просроченное
    todoist_lines = []
    try:
        from core import connectors as _conn, todoist_read as _td
        if await _conn.enabled(db, settings, "todoist"):
            todoist_lines = [_td.line(t, today, with_desc=False) for t in (await _td.for_day(settings, today))[:8]]
    except Exception as exc:
        logger.debug(f"digest todoist skip: {exc}")
    if todoist_lines:
        text += "📋 <b>Ещё из Todoist:</b>\n" + "".join(f"  • {_hesc(l)}\n" for l in todoist_lines) + "\n"

    # 🔕 v4.30 «тебе не отвечают»: ТВОЙ вопрос висит без ответа контакта ≥2 дн. (зеркало блока выше)
    silent = []
    try:
        from db import get_unanswered_outgoing
        silent = await get_unanswered_outgoing(db, hours=48, limit=5)
    except Exception as exc:
        logger.debug(f"digest silent skip: {exc}")
    if silent:
        text += f"🔕 <b>Молчат ({len(silent)}):</b>\n"
        from core.util import profile_link as _pl2
        for w in silent:
            text += (f'  • <a href="{_pl2(w.get("username"), w.get("user_id"))}">{_hesc(w["who"])}</a>'
                     f" ({w['age_days']} дн.): «{_hesc(w['text'][:50])}»\n")
        text += "<i>карточки с кнопкой пинга — ниже</i>\n\n"

    # 💡 v4.72 подсказка недели: одна возможность, которой владелец не пользовался
    tip = ""
    try:
        from core import tips as _tips
        tip = await _tips.weekly_tip(db, today)
    except Exception as exc:
        logger.debug(f"digest tip skip: {exc}")
    if tip:
        text += f"💡 {_hesc(tip)}\n\n"

    # v4.34 Rich-сводка 2.0 (Bot API 10.3): собирается из ТЕХ ЖЕ переменных, что plain-текст выше
    # (баг v4.22–v4.31: rich уходил 7/7 дней и не содержал погоду/др/пары/календарь/ждут/молчат).
    # Сбой → plain-текст + отдельные карточки (как раньше).
    rich_ok = False
    try:
        from core import richmsg
        from core.topics import thread_id
        doc = _rich_digest(
            today, wx, overdue=overdue, due_today=due_today, due_tomorrow=due_tomorrow,
            bd_today=bd_today, bd_tmr=bd_tmr, pair_lines=pair_lines, commits=commits,
            pending=list(zip(pend_lines, pend_tokens)), cal_events=cal_events, task_names=task_names,
            conflict_lines=conflict_lines, rain_lines=rain_lines, waits=waits, silent=silent,
            todoist_lines=todoist_lines, social_lines=social_lines, sub_lines=_sub_lines, tip=tip,
            market_lines=market_lines, market_news=market_news)
        rich_ok = await richmsg.send_rich(settings.bot_token, owner_id, doc, thread_id("digest"))
    except Exception as exc:
        logger.warning(f"rich digest skip: {type(exc).__name__}: {exc}")
    if not rich_ok:
        from core.topics import send_owner
        await send_owner(bot, owner_id, "digest", text)
    logger.info("Morning digest sent" + (" (rich)" if rich_ok else ""))
    if not rich_ok:   # в rich кнопки уже внутри; хвостовые карточки — только для plain-фолбэка
        await _send_bday_ping_cards(bot, owner_id, db, settings, bd_today, silent)
    await _send_checklist_digest(bot, owner_id, db, settings, overdue, due_today, today)
    try:   # v4.106: виджет «Мой день» под сводкой (если хоть раз удачно отправлялся через /day)
        from core import daywidget
        from core.topics import thread_id as _tid
        await daywidget.morning(db, settings, owner_id, _tid("digest"))
        from core.widgets import habits as _wh, market as _wm   # v4.107: если хоть раз открывал /habits и /market
        await _wh.morning(db, settings, owner_id, _tid("digest"))
        await _wm.morning(db, settings, owner_id, _tid("digest"))
    except Exception as exc:
        logger.info(f"day widget skip: {exc}")


async def _send_bday_ping_cards(bot, owner_id: int, db, settings, bd_today, silent) -> None:
    """v4.30 карточки-действия после сводки: 🎂 поздравление именинникам + 🔕 пинг молчащим.
    Кнопки → черновик от Миры → отправка контакту ТОЛЬКО по 📤 (drafts-flow). Best-effort."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from core.topics import send_owner
    try:
        from core.dates import slug_for, turning_age
        from core.util import today_local as _tl
        for b in bd_today[:3]:
            age = turning_age(b, _tl().year)
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text="✍️ Поздравление", callback_data=f"bdg:{b.get('slug') or slug_for(b['who'], b.get('uid'))}")]])
            await send_owner(bot, owner_id, "digest",
                             f"🎂 <b>{b['who']}</b>" + (f" — сегодня {age}!" if age else " — сегодня день рождения!"),
                             reply_markup=kb, parse_mode="HTML")
    except Exception as exc:
        logger.debug(f"bday cards skip: {exc}")
    try:
        import html as _h
        from db import can_notify, log_notification
        for w in silent[:3]:
            uid = w.get("user_id")
            if not uid or not await can_notify(db, uid, "ping_card", cooldown_minutes=3 * 1440):
                continue  # карточку с кнопкой не спамим чаще раза в 3 дня (строка в сводке остаётся)
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✍️ Черновик пинга", callback_data=f"png:{uid}"),
                InlineKeyboardButton(text="🙈 Не надо", callback_data=f"pngno:{uid}")]])
            await send_owner(bot, owner_id, "digest",
                             f"🔕 <b>{_h.escape(w['who'])}</b> молчит {w['age_days']} дн. на твой вопрос:\n"
                             f"«{_h.escape(w['text'][:160])}»",
                             reply_markup=kb, parse_mode="HTML")
            await log_notification(db, uid, "ping_card")
    except Exception as exc:
        logger.debug(f"ping cards skip: {exc}")


async def _send_checklist_digest(bot, owner_id: int, db, settings, overdue, due_today, today) -> None:
    """v4.20: план дня НАТИВНЫМ чек-листом Telegram (Bot API 9.1+). Уходит через business-акк
    владельца в его «Избранное»: галочка на пункте → бот закрывает задачу (Todoist+календарь+БД,
    см. checklist_tasks_done в handlers/business). Сбой не рушит дайджест."""
    try:
        from db import get_setting, set_setting
        bc_id = await get_setting(db, "bc_id")
        chat_id = getattr(settings, "checklist_chat_id", 0)
        tasks = (overdue + due_today)[:12]
        if not (bc_id and tasks and chat_id):
            return
        from aiogram.types import InputChecklist, InputChecklistTask
        items, mapping = [], {}
        for i, t in enumerate(tasks, start=1):
            items.append(InputChecklistTask(id=i, text=t["title"][:96]))
            mapping[str(i)] = t["id"]
        msg = await bot.send_checklist(
            business_connection_id=bc_id, chat_id=chat_id,
            checklist=InputChecklist(
                title=f"📋 План на {today.strftime('%d.%m')}", tasks=items,
                others_can_add_tasks=False, others_can_mark_tasks_as_done=True))
        import json as _json
        await set_setting(db, f"chk:{msg.message_id}", _json.dumps(mapping))
        try:    # не копим маппинги — держим последние 10 чек-листов
            await db.execute(
                "DELETE FROM settings_kv WHERE key LIKE 'chk:%' AND rowid NOT IN "
                "(SELECT rowid FROM settings_kv WHERE key LIKE 'chk:%' ORDER BY rowid DESC LIMIT 10)")
            await db.commit()
        except Exception:
            pass
        logger.info(f"Checklist digest sent ({len(items)} задач → Избранное)")
    except Exception as exc:
        logger.info(f"checklist digest skip: {exc}")
async def _send_evening_wrap(bot, owner_id: int, db, settings) -> None:
    """v4.23 вечернее закрытие дня: «закрыто X · висит Y» + кнопки [✓][→ завтра] по висящим.
    Пара к утреннему чеклисту: ассистент открывает и закрывает день. Без LLM."""
    from core import autonomy
    if not await autonomy.enabled(db, "evening"):
        return
    from db import get_open_tasks_due
    today = today_local()
    due_today, _tmrw, overdue = _split_due_buckets(await get_open_tasks_due(db), today)
    hanging = (overdue + due_today)[:5]
    async with db.execute(
            "SELECT COUNT(*) FROM task_sync WHERE synced=1 AND datetime(closed_at) >= datetime(?)",
            (_day_start(today, settings),)) as cur:   # C2: «сегодня» по локальным суткам
        closed_today = (await cur.fetchone())[0]
    if not hanging and not closed_today:
        return  # пустой день — не шумим
    text = f"🌙 Сегодня закрыл дел: {closed_today}, молодец" if closed_today else "🌙 Сегодня без закрытых дел, бывает"
    try:  # v4.29: завтра по погоде — для планирования вечера
        from core import weather
        _wx = await weather.get_forecast(settings)
        _wt = weather.summary_day(_wx, 1) if _wx else ""
        if _wt:
            text += f"\nЗавтра: {_wt}"
    except Exception:
        pass
    try:  # v4.31: завтра по парам — «к скольким вставать»
        from core import schedule as _sched
        _tomorrow = today + timedelta(days=1)
        _ps = _sched.pairs_for(_tomorrow)
        if _ps:
            _t1 = _sched.pair_time(_ps[0])[:5]
            text += (f"\n🎓 Завтра {len(_ps)} " + ("пара" if len(_ps) == 1 else "пары" if len(_ps) < 5 else "пар")
                     + f", первая в {_t1} ({_ps[0].get('subject', '')[:30]})")
    except Exception:
        pass
    kb = None
    if hanging:
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        text += f"\n⏳ Висит ({len(hanging)}):"
        rows = []
        for t in hanging:
            text += f"\n  • {_hesc(t['title'][:60])}"
            rows.append([InlineKeyboardButton(text=f"✓ {t['title'][:18]}", callback_data=f"td:{t['id']}"),
                         InlineKeyboardButton(text="→ завтра", callback_data=f"tmr:{t['id']}")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
    from core.topics import send_owner
    await send_owner(bot, owner_id, "digest", text, parse_mode="HTML", reply_markup=kb)
    logger.info(f"Evening wrap sent (closed={closed_today}, hanging={len(hanging)})")


async def _send_deadline_reminder(bot, owner_id: int, settings, db) -> None:
    # bot.db вместо Notion: с ретайра v3.22 функция была полным no-op (P0.2 аудита 05.07)
    from db import get_open_tasks_due
    today = today_local()
    tomorrow = (today + timedelta(days=1)).isoformat()
    _t, tasks, _o = _split_due_buckets(await get_open_tasks_due(db), today)
    if not tasks:
        return
    from core.util import html_due
    text = f"🌙 Напоминаю про завтра, {html_due(tomorrow)}. Дел со сроком: {len(tasks)}\n\n"
    for t in tasks[:10]:
        text += f"  • {t['title']} — {t['from']}\n"
    rich_ok = False
    try:
        import html as _html
        from core import richmsg
        from core.topics import thread_id
        rows = [[_html.escape(t["title"]), _html.escape(str(t["from"]))] for t in tasks[:10]]
        doc = (f"<h3>🌙 Напоминаю про завтра, {html_due(tomorrow)}. Дел со сроком: {len(tasks)}</h3>"
               + richmsg.table(["Задача", "От кого"], rows))
        rich_ok = await richmsg.send_rich(settings.bot_token, owner_id, doc, thread_id("digest"))
    except Exception as exc:
        logger.debug(f"rich reminder skip: {exc}")
    if not rich_ok:
        from core.topics import send_owner
        await send_owner(bot, owner_id, "digest", text)
    logger.info(f"Deadline reminder sent: {len(tasks)} task(s)" + (" (rich)" if rich_ok else ""))


async def _send_weekly_report(bot, owner_id: int, db, settings) -> None:
    from db import get_stats
    stats = await get_stats(db, days=7)
    counts = stats["counts"]
    total = sum(counts.values())
    text = (
        f"📊 <b>Как прошла неделя</b>\n\n"
        f"Всего сообщений: {total}\n"
        f"  💼 Дела: {counts.get('BUSINESS', 0)}\n"
        f"  👤 Личное: {counts.get('FRIEND', 0)}\n"
        f"  ❓ Непонятно: {counts.get('UNCLEAR', 0)}\n"
        f"  🚫 Спам: {counts.get('SPAM', 0)}"
    )
    # Rich-версия (Bot API 10.1). Сбой → обычный текст.
    rich_ok = False
    try:
        from core import richmsg
        from core.topics import thread_id
        doc = (
            f"<h3>📊 Как прошла неделя</h3>"
            f"<p>Всего сообщений: <b>{total}</b></p>"
            + richmsg.table(["", "Сообщений"], [
                ["💼 Дела", str(counts.get('BUSINESS', 0))],
                ["👤 Личное", str(counts.get('FRIEND', 0))],
                ["❓ Непонятно", str(counts.get('UNCLEAR', 0))],
                ["🚫 Спам", str(counts.get('SPAM', 0))]])
        )
        rich_ok = await richmsg.send_rich(settings.bot_token, owner_id, doc, thread_id("digest"))
    except Exception as exc:
        logger.debug(f"rich weekly skip: {exc}")
    if not rich_ok:
        from core.topics import send_owner
        await send_owner(bot, owner_id, "digest", text)
    logger.info("Weekly report sent" + (" (rich)" if rich_ok else ""))


DEDUP_SWEEP_SYSTEM = """Ты находишь дубли среди открытых задач ОДНОГО человека — задачи про одно и то же дело
(разные формулировки, уточнённое время/место). Разные дела одного человека — НЕ дубли. Отвечай строго JSON."""


import re as _re
# Сигналы договорённости/обещания во входящих — кандидаты на «возможно пропущенную» задачу.
_MISS_SIGNAL = _re.compile(
    r"встрет|созвон|договор|\bдавай\b|\bго\b|скин|обеща|подхват|забер|приход|поигра|погон|"
    r"пересеч|сходим|увидимся|зайд|помог|перезвон|наберу|спишемся",
    _re.IGNORECASE)


def _days_ago(ts) -> str:
    """'N дн назад' из last_contact (ISO или 'YYYY-MM-DD HH:MM:SS')."""
    if not ts:
        return "?"
    try:
        d = datetime.fromisoformat(str(ts).replace(" ", "T")[:19])
        if d.tzinfo is None:
            d = d.replace(tzinfo=ZoneInfo("UTC"))
        n = (datetime.now(ZoneInfo("UTC")) - d).days
        return "сегодня" if n <= 0 else ("вчера" if n == 1 else f"{n} дн")
    except Exception:
        return "?"
async def build_radar(db, settings) -> tuple:
    """📡 Радар отношений: остывшие важные контакты + висящие обещания + без ответа.
    Возвращает (rich_html, plain_text). Используется командой /radar и недельным пушем."""
    import html as _html
    from db import get_cold_contacts, get_open_commitments, get_unanswered_business
    cold = await get_cold_contacts(db, days=7, limit=10)
    commits = await get_open_commitments(db, limit=10)
    unans = await get_unanswered_business(db, days=2)

    # plain
    lines = ["📡 <b>Радар отношений</b>\n"]
    if cold:
        lines.append("🧊 <b>Остывшие важные контакты:</b>")
        for c in cold[:8]:
            who = c["name"] or (f"@{c['username']}" if c["username"] else "?")
            lines.append(f"  • {who} ({c['relationship']}) — молчит {_days_ago(c['last_contact'])}")
        lines.append("")
    if commits:
        lines.append("⚠️ <b>Висящие обещания:</b>")
        for c in commits[:6]:
            who = c["name"] or (f"@{c['username']}" if c["username"] else "?")
            lines.append(f"  • {who}: {c['commitments'][:90]}")
        lines.append("")
    if unans:
        lines.append("⏰ <b>Без ответа (>2 дней):</b>")
        for u in unans[:6]:
            lines.append(f"  • @{u['username'] or u['user_id']} — с {(u['last_in'] or '')[:10]}")
    if not (cold or commits or unans):
        lines.append("✨ Всё ровно: важные контакты тёплые, обещаний и долгов по ответам нет.")
    text = "\n".join(lines)

    # rich
    from core import richmsg
    doc = "<h3>📡 Радар отношений</h3>"
    if cold:
        rows = [[_html.escape(c["name"] or (("@" + c["username"]) if c["username"] else "?")),
                 _html.escape(c["relationship"]), _days_ago(c["last_contact"])] for c in cold[:8]]
        doc += richmsg.table(["Кто", "Роль", "Молчит"], rows, caption="🧊 Остывшие важные контакты")
    if commits:
        items = "".join(
            f"<li>{_html.escape(c['name'] or (('@'+c['username']) if c['username'] else '?'))}: "
            f"{_html.escape(c['commitments'][:90])}</li>" for c in commits[:6])
        doc += f"<details open><summary>⚠️ Висящие обещания</summary><ul>{items}</ul></details>"
    if unans:
        items = "".join(f"<li>@{_html.escape(u['username'] or str(u['user_id']))} — с {(u['last_in'] or '')[:10]}</li>"
                        for u in unans[:6])
        doc += f"<details open><summary>⏰ Без ответа (&gt;2 дней)</summary><ul>{items}</ul></details>"
    if not (cold or commits or unans):
        doc += "<p>✨ Всё ровно: важные контакты тёплые, висящих обещаний и долгов по ответам нет.</p>"
    return doc, text


async def _missed_task_sweep(bot, owner_id: int, db, settings) -> None:
    """Сеть «возможно пропустил» (v2.9): контакты с сигналами договорённости за 24ч, по которым
    НЕ создана ни одна задача, — переразобрать штатным путём (фикс-промпт + дедуп). Каждый контакт
    максимум раз в день (digest_state). Дедуп внутри process_action_items не даст плодить существующее."""
    from anthropic import AsyncAnthropic
    from db import (get_digest_state, set_digest_state, vault_get_context,
                    get_recent_category, is_over_budget)
    from core.actions import process_action_items
    if await is_over_budget(db, settings.anthropic_monthly_budget):
        return
    today_key = today_local().strftime("%Y-%m-%d")
    async with db.execute(
        "SELECT DISTINCT user_id, username FROM conversations "
        "WHERE direction='in' AND created_at > datetime('now','-24 hours')") as c:
        rows = await c.fetchall()
    client = None
    recovered = 0
    for uid, uname in rows:
        if not uid:
            continue
        async with db.execute(
            "SELECT group_concat(text, ' ') FROM conversations WHERE user_id=? AND direction='in' "
            "AND created_at > datetime('now','-24 hours')", (uid,)) as c:
            txt = (await c.fetchone())[0] or ""
        if not _MISS_SIGNAL.search(txt):
            continue  # нет сигнала договорённости — не кандидат
        async with db.execute(
            "SELECT COUNT(*) FROM task_sync WHERE user_id=? AND created_at > datetime('now','-24 hours')",
            (uid,)) as c:
            if (await c.fetchone())[0] > 0:
                continue  # задача за сутки уже есть — не пропущено
        gkey = f"missed_{uid}_{today_key}"
        if await get_digest_state(db, gkey):
            continue  # уже свипали сегодня — без повторов/циклов
        await set_digest_state(db, gkey, "1")
        if client is None:
            client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        thread = await vault_get_context(db, uid, limit=25)
        cat = await get_recent_category(db, uid, hours=48) or "FRIEND"
        try:
            created = await process_action_items(
                client=client, model=settings.anthropic_model_smart,
                username=uname or f"id{uid}", user_id=uid, category=cat, text=thread,
                todoist_token=settings.todoist_api_token, notion_token=settings.notion_token,
                notion_database_id=settings.notion_database_id, db=db, bot=bot, owner_id=owner_id,
                request_cost_usd=0.0, monthly_budget=settings.anthropic_monthly_budget, over_budget=False)
            if created:
                recovered += 1
                logger.info(f"Missed-task sweep: recovered task for @{uname or uid}")
        except Exception as exc:
            logger.debug(f"missed sweep item @{uname or uid}: {exc}")
    if client is not None:   # C21: не копить httpx-пулы от ежедневных клиентов
        try:
            await client.close()
        except Exception:
            pass
    logger.info(f"Missed-task sweep done: recovered {recovered} task(s)")


async def _dedup_sweep(bot, owner_id: int, db, settings) -> None:
    """Ночная страховочная сетка: ищет дубли среди открытых задач каждого контакта и схлопывает лишние
    (Todoist complete + Notion Готово + synced). Ловит то, что просочилось мимо дедупа при создании."""
    if not getattr(settings, "anthropic_api_key", None):
        return
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    try:
        await _dedup_sweep_body(bot, owner_id, db, settings, client)
    finally:
        await client.close()   # C21


async def _dedup_sweep_body(bot, owner_id: int, db, settings, client) -> None:
    from db import get_open_tasks_for_user, mark_sync_done_by_id
    from core.sync import complete_todoist_task
    from core.util import loads_loose
    async with db.execute(
        "SELECT user_id FROM task_sync WHERE synced=0 GROUP BY user_id HAVING COUNT(*)>1"
    ) as _cur:                                   # async with — иначе курсор висит и срывает ночной VACUUM-бэкап
        rows = await _cur.fetchall()
    closed = []
    closed_rows = []
    for row in rows:
        uid = row[0]
        tasks = await get_open_tasks_for_user(db, uid)
        if len(tasks) < 2:
            continue
        listing = "\n".join(
            f"{i}. {t['title']}" + (f" [тема: {t['topic']}]" if t.get("topic") else "")
            for i, t in enumerate(tasks)
        )
        try:
            resp = await llm.create(client, 
                model=settings.anthropic_model_smart, max_tokens=200, system=DEDUP_SWEEP_SYSTEM,
                messages=[{"role": "user", "content":
                    f"Открытые задачи одного человека:\n{listing}\n\n"
                    'Найди ГРУППЫ дублей (одно дело). Верни JSON: {"groups": [[номера дублей], ...]} '
                    "— только реальные дубли; если их нет, верни пустой список."}],
            )
            await _rec(settings.anthropic_model_smart, resp.usage, "dedup_sweep", db=db)
            groups = loads_loose(_llm_text(resp)).get("groups", [])
        except Exception as exc:
            logger.debug(f"dedup sweep skip uid={uid}: {exc}")
            continue
        closed_ids: set = set()
        for g in groups:
            idxs = list(dict.fromkeys(i for i in g if isinstance(i, int) and 0 <= i < len(tasks)))  # C1: без повторов
            if len(idxs) < 2:
                continue
            keep = tasks[idxs[0]]
            if keep["id"] in closed_ids:
                continue   # C1: «оставить» уже закрыта как дубль другой группы — группу пропускаем
            for i in idxs[1:]:
                t = tasks[i]
                if t["id"] in closed_ids or t["id"] == keep["id"]:
                    continue
                closed_ids.add(t["id"])
                try:
                    if settings.todoist_api_token and t.get("todoist_id"):
                        await complete_todoist_task(settings.todoist_api_token, t["todoist_id"])
                    await mark_sync_done_by_id(db, t["id"])
                    closed.append(f"📌 {t['title']}\n   <i>дубль «{keep['title']}»</i>")
                    closed_rows.append((t["title"], keep["title"]))
                except Exception as exc:
                    logger.debug(f"dedup sweep close fail id={t['id']}: {exc}")
    if closed and bot and owner_id:
        text = "🧹 <b>Ночная зачистка дублей</b>\nЗакрыто как дубли:\n\n" + "\n".join(closed[:15])
        try:
            rich_ok = False
            try:
                import html as _html
                from core import richmsg
                from core.topics import thread_id
                rows = [[_html.escape(a), _html.escape(b)] for a, b in closed_rows[:15]]
                doc = ("<h3>🧹 Ночная зачистка дублей</h3>"
                       + richmsg.table(["Закрыто", "Дубль чего"], rows))
                rich_ok = await richmsg.send_rich(settings.bot_token, owner_id, doc, thread_id("tasks"))
            except Exception as exc:
                logger.debug(f"rich dedup skip: {exc}")
            if not rich_ok:
                from core.topics import send_owner
                await send_owner(bot, owner_id, "tasks", text, parse_mode="HTML")
        except Exception:
            pass
    logger.info(f"Dedup sweep done: closed {len(closed)} duplicate task(s)")


def _match_event_contact(summary: str, named: list) -> Optional[int]:
    """Фолбэк: сопоставить название события с именем/username контакта (по вхождению). None если нет."""
    s = (summary or "").lower()
    if not s:
        return None
    for p in named:
        name = (p.get("name") or "").strip().lower()
        first = name.split()[0] if name else ""
        if (len(name) >= 3 and name in s) or (len(first) >= 3 and first in s):
            return p["user_id"]
        uname = (p.get("username") or "").lower()
        if uname and len(uname) >= 3 and uname in s:
            return p["user_id"]
    return None


def prep_docs(summary: str, unix: int, mins_to: int, when: str, prof: dict | None, ctx: str) -> tuple[str, str]:
    """Подготовка к встрече: (новая вёрстка, прежняя). v4.113 (Bot API 10.5): сверху «кто / зачем» без пустой
    строки заголовка, ниже один раскрывающийся блок «🧠 Память и переписка» — память, обещания, сообщения списком."""
    import html as _html
    from core import richmsg
    top = (f"<h3>🔔 <tg-time unix=\"{unix}\" format=\"r\">через {mins_to} мин</tg-time>: "
           f"{_html.escape(summary)}</h3>"
           f"<p>🕐 <tg-time unix=\"{unix}\" format=\"wt\">{when}</tg-time></p>")
    if not prof:
        tail = "<p><i>контакт не распознан</i></p>"
        return top + tail, top + tail
    rows = []
    who_r = prof.get("name") or ""
    if prof.get("username"):
        who_r = (who_r + f" (@{prof['username']})").strip() if who_r else f"@{prof['username']}"
    if who_r:
        rows.append(["👤 Кто", _html.escape(who_r)])
    if prof.get("intent"):
        rows.append(["🎯 Зачем пишет", _html.escape(str(prof["intent"]))])
    table = richmsg.table([], rows) if rows else ""
    commits = (prof.get("commitments") or "").strip()
    commits = commits if commits not in ("", "—") else ""
    summ = str(prof.get("summary") or "").strip()
    # прежний вид (до 10.5): цитата памяти + свёрнутые сообщения абзацем
    old = top + table
    if summ:
        old += f"<blockquote>🧠 {_html.escape(summ[:600])}</blockquote>"
    if commits:
        old += f"<p>⚠️ Ты обещал: {_html.escape(commits[:300])}</p>"
    if ctx:
        old += f"<details><summary>💬 Последние сообщения</summary><p>{_html.escape(ctx[:700])}</p></details>"
    # новый вид: обещание — на виду (это то, что нельзя забыть), остальное в одном раскрывающемся блоке
    new = top + table
    if commits:
        new += f"<p>⚠️ Ты обещал: {_html.escape(commits[:300])}</p>"
    inner = ""
    if summ:
        inner += f"<p><b>🧠 Память</b></p><p>{_html.escape(summ[:600])}</p>"
    lines = [ln.strip() for ln in (ctx or "").splitlines() if ln.strip()][-5:]
    if lines:
        inner += "<p><b>💬 Последние сообщения</b></p><ul>" + "".join(
            f"<li>{_html.escape(ln[:220])}</li>" for ln in lines) + "</ul>"
    if inner:
        new += f"<blockquote expandable><p><b>Память и переписка</b></p>{inner}</blockquote>"
    return new, old


async def _meeting_prep_check(bot, owner_id: int, db, settings) -> None:
    """Проактивный бриф за settings.meeting_prep_min минут до события календаря.
    Контакт: сначала по task_sync.calendar_event_id (надёжно), фолбэк — имя в названии события.
    Claim-then-send по digest_state (prep_<event_id>) — один бриф на событие."""
    prep_min = getattr(settings, "meeting_prep_min", 0)
    if not prep_min or not settings.google_calendar_id:
        return
    from core.calendar import list_upcoming_events, _parse_rfc3339
    from db import (get_digest_state, set_digest_state, get_user_by_calendar_event,
                    get_named_profiles, get_contact_profile, vault_get_context,
                    task_closed_by_calendar_event)
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz)
    events = await list_upcoming_events(
        settings.google_service_account_path, settings.google_calendar_id,
        now.isoformat(), (now + timedelta(minutes=prep_min)).isoformat())
    if not events:
        return
    named = None
    for ev in events:
        eid = ev.get("id")
        start_dt = _parse_rfc3339(ev.get("start"))
        # v4.112: отметка «подготовка отправлена» — на событие И его время. Раньше только на событие:
        # визит перенесли на четверг, а подготовка к нему уже «была» во вторник и больше не пришла бы.
        prep_key = f"prep_{eid}@{start_dt.isoformat() if start_dt else ''}"
        if not eid or await get_digest_state(db, prep_key):
            continue
        if await task_closed_by_calendar_event(db, eid):
            continue  # задача по этой встрече закрыта/отменена → не напоминаем (баг: «через 30 мин» по закрытой)
        if not start_dt or start_dt <= now:
            continue  # уже началось — поздно
        await set_digest_state(db, prep_key, "sent")  # claim ДО отправки
        summary = ev.get("summary") or "(без названия)"
        when = start_dt.astimezone(tz).strftime("%H:%M")
        mins_to = int((start_dt - now).total_seconds() // 60)

        uid = await get_user_by_calendar_event(db, eid)
        if uid is None:
            if named is None:
                named = await get_named_profiles(db)
            uid = _match_event_contact(summary, named)

        # tg-time format="r" — «через N мин» тикает на стороне клиента
        _unix = int(start_dt.timestamp())
        text = (f"🔔 <b><tg-time unix=\"{_unix}\" format=\"r\">через {mins_to} мин</tg-time>: "
                f"{summary}</b> в <tg-time unix=\"{_unix}\" format=\"t\">{when}</tg-time>\n")
        prof = await get_contact_profile(db, uid) if uid else None
        if prof:
            who = prof.get("name") or (f"@{prof.get('username')}" if prof.get("username") else "")
            if who:
                text += f"👤 {who}"
                if prof.get("name") and prof.get("username"):
                    text += f" (@{prof['username']})"
                text += "\n"
            if prof.get("intent"):
                text += f"🎯 {prof['intent']}\n"
            if prof.get("summary"):
                text += f"\n🧠 {prof['summary'][:600]}\n"
            commits = (prof.get("commitments") or "").strip()
            if commits and commits not in ("", "—"):
                text += f"\n⚠️ Ты обещал: {commits[:300]}\n"
            ctx = await vault_get_context(db, uid, limit=5)
            if ctx:
                text += f"\n💬 Последние сообщения:\n{ctx[:700]}"
        else:
            text += "<i>контакт не распознан</i>"
        try:
            rich_ok = False
            try:
                import html as _html
                from core import richmsg
                from core.topics import thread_id
                _ctx5 = await vault_get_context(db, uid, limit=5) if prof else ""
                doc, old_doc = prep_docs(summary, _unix, mins_to, when, prof, _ctx5)
                rich_ok = await richmsg.send_rich(settings.bot_token, owner_id, doc, thread_id("meetings"))
                if not rich_ok:   # v4.113: новая вёрстка не принята (клиент/сервер) — прежний вид
                    rich_ok = await richmsg.send_rich(settings.bot_token, owner_id, old_doc, thread_id("meetings"))
            except Exception as exc:
                logger.debug(f"rich meeting prep skip: {exc}")
            if not rich_ok:
                from core.topics import send_owner
                await send_owner(bot, owner_id, "meetings", text, parse_mode="HTML", disable_web_page_preview=True)
            logger.info(f"Meeting prep sent for event {eid} ({summary!r}, in {mins_to}m)" + (" (rich)" if rich_ok else ""))
            try:   # v4.107: карточка-виджет с обратным отсчётом и «+15 мин / +1 ч»
                from core.widgets import meeting as _wmeet
                _who = ""
                if prof:
                    _who = prof.get("name") or (f"@{prof['username']}" if prof.get("username") else "")
                await _wmeet.offer(db, settings, owner_id, eid, summary, start_dt, _who)
            except Exception as exc:
                logger.info(f"meeting widget skip: {exc}")
        except Exception as exc:
            logger.error(f"meeting prep send failed: {exc}")


async def _budget_alert(bot, owner_id: int, db, settings) -> None:
    """Один раз за месяц предупреждает владельца, когда расход на Anthropic перешёл бюджет
    (тогда же срабатывает стоп-кран LLM в tasks_loop/business). Дедуп по digest_state."""
    budget = getattr(settings, "anthropic_monthly_budget", 0)
    if not budget:
        return
    from db import is_over_budget, get_spending, get_digest_state, set_digest_state
    if not await is_over_budget(db, budget):
        return
    month_key = today_local().strftime("%Y-%m")
    if await get_digest_state(db, f"budget_alert_{month_key}"):
        return
    await set_digest_state(db, f"budget_alert_{month_key}", "sent")
    sp = await get_spending(db)
    try:
        from core.topics import send_owner
        await send_owner(
            bot, owner_id, "urgent",
            f"🛑 <b>Бюджет Anthropic превышен</b>\n"
            f"Месяц: ${sp['month_usd']:.2f} из ${budget:.2f}.\n"
            f"LLM-извлечение и обогащение памяти приостановлены (стоп-кран). "
            f"Подними ANTHROPIC_MONTHLY_BUDGET в .env или дождись нового месяца.",
            parse_mode="HTML")
        logger.warning(f"Budget exceeded (${sp['month_usd']:.2f}/${budget:.2f}) — owner alerted")
    except Exception as exc:
        logger.error(f"budget alert send failed: {exc}")


async def run_digest_loop(bot, owner_id: int, db, settings) -> None:
    from core import pulse
    from db import get_digest_state, set_digest_state
    tz = ZoneInfo(settings.timezone)
    logger.info(f"Digest loop started (tz={settings.timezone}, digest=09:00, reminder=20:00)")

    tick = 0
    while True:
        tick += 1
        try:
            now = datetime.now(tz)
            today_key = now.date().isoformat()
            week_key = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]}"

            # Claim-then-send: set the flag BEFORE the (slow, multi-request) send so a
            # send that takes >1 min — or restart mid-send — can't trigger a duplicate.
            # Morning digest at 09:00
            if now.hour == 9 and now.minute < 5:
                if not await get_digest_state(db, f"digest_{today_key}"):
                    await set_digest_state(db, f"digest_{today_key}", "sent")
                    try:
                        await _send_morning_digest(bot, owner_id, db, settings)
                    except Exception as exc:
                        # v4.34 (R5): claim-then-send с ОТКАТОМ — иначе сетевой сбой = день без сводки
                        logger.error(f"morning digest failed: {type(exc).__name__}: {exc} — flag rolled back, retry in window")
                        try:
                            await db.execute("DELETE FROM digest_state WHERE key=?", (f"digest_{today_key}",))
                            await db.commit()
                        except Exception:
                            pass

            # v4.44 Ф2: 04:00 — Haiku-сводка вчерашнего диалога с Мирой → факт в памяти владельца
            if now.hour == 4 and now.minute < 5 and getattr(settings, "mira_v2", False):
                if not await get_digest_state(db, f"dialogsum_{today_key}"):
                    await set_digest_state(db, f"dialogsum_{today_key}", "sent")
                    try:
                        from core import mira as _mira
                        await _mira.summarize_day(db, settings)
                    except Exception as exc:
                        logger.warning(f"dialog day summary failed: {type(exc).__name__}: {exc}")

            # v4.53 соцсети: полный сбор 06:00; во время стрима — снимок зрителей каждые 5 мин
            try:
                from core import social as _social
                if now.hour == 6 and now.minute < 5 and not await get_digest_state(db, f"social_{today_key}"):
                    await set_digest_state(db, f"social_{today_key}", "sent")
                    await _social.collect_all(db, settings, bot)
                elif tick % 5 == 0 and _social.tw_enabled(settings):
                    _live = await _social.collect_twitch(db, settings, live_only=True)
                    from core import stream_report
                    if _live.get("stream_id"):
                        await stream_report.on_live(bot, db, settings, _live)
                    else:
                        await stream_report.on_offline(bot, db, settings, datetime.now().timestamp())   # v4.114 пост → «после эфира»
            except Exception as exc:
                logger.debug(f"social collect skip: {exc}")

            # Deadline reminder at 20:00
            if now.hour == 20 and now.minute < 5:
                if not await get_digest_state(db, f"reminder_{today_key}"):
                    await set_digest_state(db, f"reminder_{today_key}", "sent")
                    await _send_deadline_reminder(bot, owner_id, settings, db)

            # 📧 v4.28 почта: раз в email_poll_min минут — важные письма владельцу
            _epm = max(1, getattr(settings, "email_poll_min", 5))
            if tick % _epm == 0 and getattr(settings, "email_accounts", ""):
                try:
                    from core.mailwatch import poll_mail
                    await poll_mail(bot, owner_id, db, settings)
                except Exception as exc:
                    logger.debug(f"mail poll skip: {exc}")

            # 🔔 v4.25 snooze: доставка созревших отложенных карточек (текст+кнопки)
            try:
                import json as _sjson
                import time as _stime
                async with db.execute(
                        "SELECT key, value FROM settings_kv WHERE key LIKE 'rmd:%' LIMIT 60") as cur:
                    _rmds = await cur.fetchall()
                for _k, _v in _rmds:
                    try:
                        _fire = int(_k.split(":")[1])
                    except Exception:
                        _fire = 0
                    if not _fire or _fire > _stime.time():
                        continue
                    _ok = False
                    try:
                        _d = _sjson.loads(_v)
                        from aiogram.types import InlineKeyboardMarkup as _IKM
                        _kb = _IKM.model_validate_json(_d["kb"]) if _d.get("kb") else None
                        _txt = "🔔 <b>Напоминаю:</b>\n" + _d["text"]
                        try:
                            await bot.send_message(_d["chat_id"], _txt, parse_mode="HTML",
                                                   reply_markup=_kb,
                                                   message_thread_id=_d.get("thread"))
                        except Exception:
                            await bot.send_message(_d["chat_id"], _txt, parse_mode="HTML",
                                                   reply_markup=_kb)
                        logger.info("Snoozed card re-delivered")
                        _ok = True
                    except Exception as exc:
                        logger.warning(f"snooze deliver failed (retry next minute): {type(exc).__name__}: {exc}")
                    # C3: удаляем ТОЛЬКО после доставки; вечный ретрай режем TTL 24 ч после срока
                    if _ok or _fire + 86400 < _stime.time():
                        await db.execute("DELETE FROM settings_kv WHERE key=?", (_k,))
                        await db.commit()
            except Exception as exc:
                logger.debug(f"snooze loop skip: {exc}")

            # 🌙 Вечернее закрытие дня (v4.23)
            _ewh = getattr(settings, "evening_wrap_hour", 21)
            if _ewh and now.hour == _ewh and now.minute < 5:
                if not await get_digest_state(db, f"wrap_{today_key}"):
                    await set_digest_state(db, f"wrap_{today_key}", "sent")
                    await _send_evening_wrap(bot, owner_id, db, settings)

            # Weekly report on Sunday at 09:00
            if now.weekday() == 6 and now.hour == 9 and now.minute < 5:
                if not await get_digest_state(db, f"weekly_{week_key}"):
                    await set_digest_state(db, f"weekly_{week_key}", "sent")
                    await _send_weekly_report(bot, owner_id, db, settings)

            # Follow-up check at 10:00 — unanswered BUSINESS > 2 days
            if now.hour == 10 and now.minute < 5:
                if not await get_digest_state(db, f"followup_{today_key}"):
                    await set_digest_state(db, f"followup_{today_key}", "done")
                    await _send_followups(bot, owner_id, db)

            # 🔔 v4.84 дневное напоминание: одно сообщение и только если есть повод
            if now.hour == pulse.SEND_HOUR and now.minute < 5:
                if not await get_digest_state(db, f"pulse_{today_key}"):
                    await set_digest_state(db, f"pulse_{today_key}", "done")
                    await pulse.send(bot, owner_id, db, settings, now)

            # 📡 Радар отношений — воскресенье 18:00 (недельный обзор связей)
            if now.weekday() == 6 and now.hour == 18 and now.minute < 5:
                if not await get_digest_state(db, f"radar_{today_key}"):
                    await set_digest_state(db, f"radar_{today_key}", "done")
                    try:
                        doc, text = await build_radar(db, settings)
                        from core import richmsg
                        from core.topics import send_owner, thread_id
                        if not await richmsg.send_rich(settings.bot_token, owner_id, doc, thread_id("digest")):
                            await send_owner(bot, owner_id, "digest", text)
                        logger.info("Relationship radar sent (weekly)")
                    except Exception as exc:
                        logger.error(f"radar push failed: {exc}")

            # Daily maintenance at 04:00 — vault cleanup + DB backup
            if now.hour == 4 and now.minute < 5:
                if not await get_digest_state(db, f"maint_{today_key}"):
                    await set_digest_state(db, f"maint_{today_key}", "done")
                    await _daily_maintenance(db, settings)
                    await _dedup_sweep(bot, owner_id, db, settings)  # страховочная сетка от дублей
                    await _missed_task_sweep(bot, owner_id, db, settings)  # сеть «возможно пропустил» (v2.9)
                    # Память о САМОМ владельце — раз в день, по его исходящим
                    try:
                        from anthropic import AsyncAnthropic
                        from core.vault import enrich_owner_profile
                        _cli = AsyncAnthropic(api_key=settings.anthropic_api_key)
                        try:
                            await enrich_owner_profile(_cli, settings.anthropic_model_smart, db)
                        finally:
                            await _cli.close()   # C21
                    except Exception as exc:
                        logger.error(f"owner self-profile (daily) failed: {exc}")
                    # Аватары контактов: сверить с Telegram, обновить сменившиеся (ручные не трогает)
                    try:
                        from core.photos import refresh_all_avatars
                        res = await refresh_all_avatars(bot, db, settings)
                        if res.get("changed") or res.get("new"):
                            logger.info(f"Avatars daily refresh: {res}")
                    except Exception as exc:
                        logger.error(f"avatars refresh (daily) failed: {exc}")

            # Подготовка к встрече — раз в 3 тика (~3 мин), не каждые 60с: меньше нагрузка на
            # Google Calendar API (~480 вместо 1440 запросов/день), окно brief'а 30 мин это терпит.
            if tick % 3 == 1:
                await _meeting_prep_check(bot, owner_id, db, settings)

            # v4.106: виджет «Мой день» — подтянуть изменения задач (правки через Миру, Todoist, судью)
            if tick % 2 == 0:
                try:
                    from core import daywidget
                    await daywidget.refresh(db, settings)
                    from core.widgets import habits as _wh
                    await _wh.refresh(db, settings)   # v4.107: отметки привычек через Миру
                except Exception as exc:
                    logger.debug(f"day widget refresh skip: {exc}")

            # Алерт превышения бюджета — проверяем в начале каждого часа (дедуп по месяцу)
            if now.minute < 5:
                await _budget_alert(bot, owner_id, db, settings)

        except Exception as exc:
            logger.error(f"Digest loop error: {exc}")

        await asyncio.sleep(60)


async def _send_followups(bot, owner_id: int, db) -> None:
    """Notify about BUSINESS messages unanswered for >2 days."""
    from db import get_unanswered_business
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    pending = await get_unanswered_business(db, days=2)
    if not pending:
        return
    from core.util import html_due
    for item in pending[:10]:
        uid = item["user_id"]
        uname = item["username"] or f"id{uid}"
        _last_in = item.get("last_in") or ""
        days_ago = html_due(_last_in[:10]) if _last_in else ""  # живая дата (tg-time); None-guard
        from core.util import profile_link, contact_label, has_username
        from db import get_contact_profile
        link = profile_link(item["username"], uid)
        _prof = await get_contact_profile(db, uid) or {}
        rows = [[
            InlineKeyboardButton(text="✅ Ответил", callback_data=f"fu_ok:{uid}", style="success"),
            InlineKeyboardButton(text="💤 Завтра", callback_data="ig", style="primary"),
        ]]
        if not has_username(uname):   # v4.82: контакт без username, ответ через Миру (как в сводке)
            rows.append([InlineKeyboardButton(text="✍️ Ответить", callback_data=f"rep:{uid}", style="primary")])
        from core.topics import send_owner
        await send_owner(
            bot, owner_id, "digest",
            (
                f"💬 {contact_label(uname, uid, _prof.get('name'))} ждёт твой ответ с {days_ago}, напиши\n"
                f"<a href=\"{link}\">Открыть чат</a>"
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    logger.info(f"Follow-ups sent: {len(pending)} contact(s)")


async def _daily_maintenance(db, settings) -> None:
    from datetime import date
    from pathlib import Path
    from db import archive_and_delete_old_conversations

    # 1. Vault cleanup — ARCHIVE to file + Notion, then drop conversations older than 6 months.
    #    Profiles (contact memory) are kept; raw messages saved to archive before delete.
    try:
        archive_dir = str(Path(settings.db_path).parent / "archive")
        res = await archive_and_delete_old_conversations(db, months=6, archive_dir=archive_dir)
        if res and res["count"]:
            logger.info(f"Vault cleanup: archived + removed {res['count']} rows → {archive_dir}")
            # Архив переписки хранится в файле (data/archive/) + Obsidian (Telegram/Archive/).
            # Notion-проекция архива убрана v3.23 (полный ретайр Notion).
    except Exception as exc:
        logger.error(f"Vault archive/cleanup failed: {exc}")

    # 1b. Dedup/vector table cleanup — keep monotonic tables bounded
    try:
        await db.execute("DELETE FROM processed_updates WHERE created_at < datetime('now','-2 days')")
        await db.execute("DELETE FROM processed_media WHERE created_at < datetime('now','-30 days')")
        await db.execute("DELETE FROM message_vectors WHERE created_at < datetime('now','-180 days')")
        await db.execute("DELETE FROM pending_analysis WHERE dirty = 0 AND last_activity < datetime('now','-7 days')")
        # Растущие без TTL таблицы (сканируются на каждом сообщении в бюджет-чеке/статах) — держим в окне.
        # Бюджет/кредиты считаются по месяцу/якорю, поэтому 90 дней истории с запасом достаточно.
        await db.execute("DELETE FROM message_log WHERE created_at < datetime('now','-90 days')")
        await db.execute("DELETE FROM llm_usage WHERE created_at < datetime('now','-90 days')")
        await db.execute("DELETE FROM notification_log WHERE created_at < datetime('now','-90 days')")
        # digest_state: ключи событий календаря (prep_*) копятся навсегда — чистим старые claim'ы.
        await db.execute("DELETE FROM digest_state WHERE key LIKE 'prep_%' AND rowid IN "
                         "(SELECT rowid FROM digest_state WHERE key LIKE 'prep_%' ORDER BY rowid DESC LIMIT -1 OFFSET 200)")
        await db.commit()
        # Checkpoint WAL: keeps bot.db-wal bounded AND makes bot.db self-contained for the vault scp-sync
        await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as exc:
        logger.error(f"dedup/vector cleanup failed: {exc}")

    # 1c. Attachments backstop — обычно vault-синк ПЕРЕНОСИТ их (rsync --remove-source-files),
    # но если Mac выключен и файлы залежались — чистим старше 14 дней, чтобы не копить на сервере.
    try:
        import time
        from pathlib import Path as _AP
        adir = _AP(settings.db_path).parent / "attachments"
        if adir.exists():
            cutoff = time.time() - 14 * 86400
            removed = 0
            for f in adir.rglob("*"):
                if f.is_file() and f.name != "_README.txt" and f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True); removed += 1
            if removed:
                logger.info(f"Attachments backstop: removed {removed} stale file(s) (>14d)")
    except Exception as exc:
        logger.error(f"attachments cleanup failed: {exc}")

    # 2. DB backup via VACUUM INTO — WAL-safe consistent single-file copy.
    # ВАЖНО: на ОТДЕЛЬНОМ соединении — на общем VACUUM падает «SQL statements in
    # progress» (другие циклы держат свои statements на том же соединении); из-за
    # этого бэкапы молча не делались 6 дней (аудит 12.06).
    try:
        import aiosqlite
        from pathlib import Path
        db_path = Path(settings.db_path)
        backup_dir = db_path.parent / "backups"
        backup_dir.mkdir(exist_ok=True)
        backup = backup_dir / f"bot_{today_local().isoformat()}.db"
        backup.unlink(missing_ok=True)
        async with aiosqlite.connect(settings.db_path) as bconn:
            # aiosqlite держит курсор каждого execute() открытым, а незакрытый курсор =
            # «SQL statements in progress» для VACUUM (настоящий корень 6 ночей сбоев).
            # Поэтому каждый курсор закрываем явно.
            cur = await bconn.execute("PRAGMA busy_timeout=10000")
            await cur.close()
            cur = await bconn.execute("VACUUM INTO ?", (str(backup),))
            await cur.close()
        # Prune backups older than 7 days
        backups = sorted(backup_dir.glob("bot_*.db"))
        for old in backups[:-7]:
            old.unlink(missing_ok=True)
        logger.info(f"DB backup saved: {backup.name} ({len(backups)} kept)")
    except Exception as exc:
        logger.error(f"DB backup failed: {exc}")
