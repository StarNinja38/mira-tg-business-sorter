"""
v4.26 «Переговоры о встрече под ключ» — первый шаг «ассистент ДЕЛАЕТ, а не записывает».

Контакт спрашивает «когда встретимся?» → Мира сама находит 2-3 свободных слота
(рабочие часы минус события Google Calendar минус задачи со временем) → владельцу
карточка с кнопками → по тапу Мира пишет контакту от имени владельца.

Ноль автоотправки (принцип tutor_lena: отправка только по кнопке), ноль LLM
(regex-триггер + арифметика слотов + шаблонная фраза).
"""
import re
from datetime import timedelta

from loguru import logger

from core.util import profile_link   # v4.35: имя контакта в карточке — ссылка в чат

MEETING_ASK_RE = re.compile(
    r"когда\s+(встрет|можем|удобно|свобод|созвон|пересеч)"
    r"|во сколько\s+(тебе\s+|вам\s+)?(удобно|можешь|встрет)"
    r"|давай\s+(встрет|созвон|пересеч)"
    r"|найд[ёе]м время|когда тебе удобно(?!\s+(?:скин|присл|отправ|сдел|ответ|перевед|оплат))"
    r"|когда сможешь(?!\s+(?:скин|присл|отправ|сдел|ответ|перевед|оплат|посмотр|глян))",   # C19: не про встречу — мимо
    re.IGNORECASE)

_WD = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def fmt_slot(iso: str) -> str:
    """'2026-08-31T15:00' → 'пн 31.08 15:00'."""
    try:
        from datetime import date
        d = date.fromisoformat(iso[:10])
        return f"{_WD[d.weekday()]} {d.strftime('%d.%m')} {iso[11:16]}"
    except Exception:
        return iso


async def find_slots(settings, db, days: int = 3, count: int = 3, dur_min: int = 60, only_day=None) -> list[str]:
    """Свободные слоты на ближайшие days дней (по одному в день — разнообразие).
    Занятость = события GCal со временем + открытые задачи со временем.
    v4.61.2: only_day — все свободные часы ОДНОГО дня (горизонт растягивается до него). Раньше Мира
    искала только 4 дня вперёд: «когда я свободен в среду» за 5 дней → «окон нет»."""
    from core.util import now_local, today_local
    now = now_local()
    if only_day is not None:
        days = max(days, min((only_day - today_local()).days + 1, 31))
    busy = []  # (iso-date, start_min, end_min)
    try:
        if settings.google_calendar_id:
            from core.calendar import list_events
            from core.digest import _day_start
            t0 = _day_start(today_local(), settings)                       # C2: локальные сутки
            t1 = _day_start(today_local() + timedelta(days=days + 1), settings)
            for e in await list_events(settings.google_service_account_path,
                                       settings.google_calendar_id, t0, t1):
                st, en = str(e.get("start") or ""), str(e.get("end") or "")
                if "T" in st:
                    m = int(st[11:13]) * 60 + int(st[14:16])
                    m_end = int(en[11:13]) * 60 + int(en[14:16]) if ("T" in en and en[:10] == st[:10]) else m + 60
                    busy.append((st[:10], m, max(m_end, m + 15)))            # C5: реальная длительность
                elif st:
                    busy.append((st[:10], 0, 24 * 60))                        # C5: all-day блокирует день
    except Exception as exc:
        logger.debug(f"meeting slots calendar skip: {exc}")
    try:
        from db import get_open_tasks_due
        for t in await get_open_tasks_due(db):
            dd = str(t.get("due_date") or "")
            if "T" in dd:
                m = int(dd[11:13]) * 60 + int(dd[14:16])
                busy.append((dd[:10], m, m + 60))
    except Exception:
        pass
    try:  # v4.31: пары МИРЭА — тоже занятость (не предлагаем встречу во время лекции)
        from core import schedule as _sched
        for d in range(days):
            day = today_local() + timedelta(days=d)
            for m0, m1 in _sched.busy_blocks(day):
                busy.append((day.isoformat(), m0, m1))
    except Exception as exc:
        logger.debug(f"meeting slots schedule skip: {exc}")
    slots = []
    for d in range(days):
        day = today_local() + timedelta(days=d)
        if only_day is not None and day != only_day:
            continue
        ds = day.isoformat()
        for h in range(settings.work_start_hour, settings.work_end_hour):
            if d == 0 and h <= now.hour + 1:
                continue  # сегодня — не раньше чем через ~2 часа
            m0 = h * 60
            if any(b[0] == ds and not (m0 + dur_min <= b[1] or m0 >= b[2]) for b in busy):
                continue
            slots.append(f"{ds}T{h:02d}:00")
            if only_day is None:
                break
        if len(slots) >= count:
            break
    return slots[:count]


async def offer_meeting(bot, db, settings, user_id: int, username: str, text: str, bc_id: str | None = None) -> None:
    """Контакт спросил о встрече → карточка владельцу со слотами-кнопками.
    Гейты: кулдаун 12ч на контакт, есть хотя бы 1 слот."""
    try:
        from db import can_notify, log_notification, set_setting
        if not await can_notify(db, user_id, "mtg", cooldown_minutes=12 * 60):
            return
        slots = await find_slots(settings, db)
        if not slots:
            return
        import hashlib
        import json as _json
        import time as _time
        token = hashlib.md5(f"{user_id}:{_time.time()}".encode()).hexdigest()[:10]
        await set_setting(db, f"mtgo:{token}", _json.dumps(
            {"uid": user_id, "username": username, "slots": slots, "ts": int(_time.time()), "bc_id": bc_id},
            ensure_ascii=False))
        import html as _html
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        from core.topics import send_owner, snooze_row
        row = [InlineKeyboardButton(text=fmt_slot(s), callback_data=f"mtg:{token}:{i}")
               for i, s in enumerate(slots)]
        kb = InlineKeyboardMarkup(inline_keyboard=[
            row, [InlineKeyboardButton(text="✍️ Отвечу сам", callback_data=f"mtgno:{token}")],
            snooze_row()])
        await send_owner(
            bot, settings.owner_user_id, "meetings",
            f'🤝 <b><a href="{profile_link(username, user_id)}">@{_html.escape(username)}</a> спрашивает о встрече</b>\n'
            f"«{_html.escape((text or '')[:140])}»\n\n"
            f"<i>Нашла свободные окна — предложить ему? (отправлю от твоего имени)</i>",
            parse_mode="HTML", reply_markup=kb)
        await log_notification(db, user_id, "mtg")
        logger.info(f"Meeting slots offered to owner for @{username}: {slots}")
    except Exception as exc:
        logger.debug(f"meeting offer skip @{username}: {exc}")
