"""
v4.72 «Я напишу сама»: регулярные сообщения от Миры по просьбе владельца.

Два вида:
  remind — готовая фраза, без LLM («выпей воды», «как прошёл день?»);
  order  — поручение: в срок Мира выполняет запрос и присылает результат.
Поручения идут только с инструментами чтения: картина мира содержит чужие сообщения,
поэтому по расписанию Мира ничего не меняет и никому не пишет.

Таблица routines: schedule = JSON {"every": once|daily|weekly|monthly|hours, ...},
next_run = unix-время ближайшего запуска. Простой бота дольше LATE_LIMIT не догоняем.
"""
import asyncio
import calendar
import json
import re
from datetime import date, datetime, timedelta

from loguru import logger

from core.util import now_local

MAX_ACTIVE = 30
MAX_ORDERS = 8           # поручения и скиллы по расписанию тратят кредиты, напоминания нет
LATE_LIMIT = 2 * 3600
ORDER_TIMEOUT = 120
TICK_SECONDS = 30
BRIEF_EVERY_MIN = 5
_WD = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
_MONTH_AHEAD = 13
_ORDER_PREFIX = "ПОРУЧЕНИЕ ПО РАСПИСАНИЮ (владелец сейчас не пишет; выполни и коротко отчитайся): "


async def _ensure(db) -> None:
    await db.execute("""CREATE TABLE IF NOT EXISTS routines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        text TEXT NOT NULL,
        schedule TEXT NOT NULL,
        next_run INTEGER NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    await db.commit()


def _hhmm(value, field: str = "time") -> str:
    m = re.fullmatch(r"(\d{1,2})(?:[:.](\d{2}))?", str(value or "").strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2) or 0) > 59:
        raise ValueError(f"не хватает времени {field} в формате ЧЧ:ММ")
    return f"{int(m.group(1)):02d}:{m.group(2) or '00'}"


def _days(value) -> list:
    out = set()
    for d in (value or []):
        s = str(d).strip().lower()[:2]
        if s in _WD:
            out.add(_WD.index(s))
        elif s.isdigit() and 1 <= int(s) <= 7:
            out.add(int(s) - 1)
    if not out:
        raise ValueError("не хватает дней недели: пн, вт, ср, чт, пт, сб, вс")
    return sorted(out)


def build_schedule(args: dict, today: date | None = None) -> dict:
    """Аргументы инструмента → проверенное расписание; ValueError с текстом для модели."""
    every = (args.get("every") or "").lower()
    if every == "daily":
        return {"every": every, "time": _hhmm(args.get("time"))}
    if every == "weekly":
        return {"every": every, "time": _hhmm(args.get("time")), "days": _days(args.get("days"))}
    if every == "monthly":
        day = int(args.get("day") or 0)
        if not 1 <= day <= 31:
            raise ValueError("не хватает числа месяца day (1-31)")
        return {"every": every, "time": _hhmm(args.get("time")), "day": day}
    if every == "hours":
        hours = int(args.get("hours") or 0)
        if not 1 <= hours <= 24:
            raise ValueError("не хватает интервала hours (1-24)")
        start, end = _hhmm(args.get("from") or "09:00", "from"), _hhmm(args.get("to") or "22:00", "to")
        if start >= end:
            raise ValueError("окно from должно начинаться раньше to")
        return {"every": every, "hours": hours, "from": start, "to": end}
    if every == "once":
        try:
            day = date.fromisoformat(str(args.get("date") or ""))
        except ValueError:
            raise ValueError("не хватает даты date в формате ГГГГ-ММ-ДД") from None
        if day < (today or now_local().date()):
            raise ValueError("эта дата уже прошла")
        return {"every": every, "time": _hhmm(args.get("time")), "date": day.isoformat()}
    raise ValueError("every: once | daily | weekly | monthly | hours")


def _at(day: date, hhmm: str, tz) -> datetime:
    h, m = hhmm.split(":")
    return datetime(day.year, day.month, day.day, int(h), int(m), tzinfo=tz)


def next_fire(schedule: dict, after: datetime):
    """Ближайший запуск строго после after; None — разовое уже прошло. Чистая функция."""
    tz, every, today = after.tzinfo, schedule["every"], after.date()
    if every == "once":
        when = _at(date.fromisoformat(schedule["date"]), schedule["time"], tz)
        return when if when > after else None
    if every == "daily":
        when = _at(today, schedule["time"], tz)
        return when if when > after else when + timedelta(days=1)
    if every == "weekly":
        for i in range(8):
            when = _at(today + timedelta(days=i), schedule["time"], tz)
            if when.weekday() in schedule["days"] and when > after:
                return when
    if every == "monthly":
        y, m = today.year, today.month
        for _ in range(_MONTH_AHEAD):
            last = calendar.monthrange(y, m)[1]     # 31-е в коротком месяце = последний день
            when = _at(date(y, m, min(schedule["day"], last)), schedule["time"], tz)
            if when > after:
                return when
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    if every == "hours":
        for i in range(2):                          # слоты от начала окна: 09, 11, 13… без сдвига
            day = today + timedelta(days=i)
            when, end = _at(day, schedule["from"], tz), _at(day, schedule["to"], tz)
            while when <= end:
                if when > after:
                    return when
                when += timedelta(hours=schedule["hours"])
    raise ValueError(f"расписание без следующего запуска: {schedule}")


def describe(schedule: dict) -> str:
    every = schedule["every"]
    if every == "daily":
        return f"каждый день в {schedule['time']}"
    if every == "weekly":
        return f"{', '.join(_WD[d] for d in schedule['days'])} в {schedule['time']}"
    if every == "monthly":
        return f"{schedule['day']}-го числа в {schedule['time']}"
    if every == "hours":
        return f"каждые {schedule['hours']} ч с {schedule['from']} до {schedule['to']}"
    return f"{schedule['date']} в {schedule['time']}"


async def _count(db, where: str = "1=1") -> int:
    async with db.execute(f"SELECT COUNT(*) FROM routines WHERE enabled=1 AND {where}") as cur:
        return (await cur.fetchone())[0]


async def add(db, args: dict) -> str:
    """Добавить из аргументов инструмента; вернуть текст для модели. ValueError = что поправить."""
    rid, note = await add_row(db, args)
    return f"добавила #{rid}: {note}"


async def add_row(db, args: dict) -> tuple:
    """Создать запись; вернуть (id, описание срока)."""
    await _ensure(db)
    text = (args.get("text") or "").strip()
    kind = args.get("kind") if args.get("kind") in ("remind", "order") else "remind"
    if not text:
        raise ValueError("не хватает текста")
    schedule = build_schedule(args)
    if kind == "order" and schedule["every"] == "hours":
        raise ValueError("поручения запускаются не чаще раза в день, каждые N часов можно только напоминать")
    if await _count(db) >= MAX_ACTIVE:
        raise ValueError(f"уже {MAX_ACTIVE} регулярных сообщений, сначала убери лишние")
    if kind == "order" and await _count(db, "kind='order'") >= MAX_ORDERS:
        raise ValueError(f"уже {MAX_ORDERS} поручений, они тратят кредиты; сначала убери лишнее")
    first = next_fire(schedule, now_local())
    if first is None:
        raise ValueError("это время уже прошло")
    cur = await db.execute("INSERT INTO routines (kind, text, schedule, next_run) VALUES (?, ?, ?, ?)",
                           (kind, text[:1000], json.dumps(schedule, ensure_ascii=False), int(first.timestamp())))
    await db.commit()
    return cur.lastrowid, f"{describe(schedule)}, первый раз {first.strftime('%d.%m %H:%M')}"


async def remove(db, rid) -> str:
    await _ensure(db)
    cur = await db.execute("DELETE FROM routines WHERE id=?", (int(rid or 0),))
    await db.commit()
    return f"убрала #{rid}" if cur.rowcount else f"#{rid} нет в списке"


async def list_text(db) -> str:
    await _ensure(db)
    async with db.execute("SELECT id, kind, text, schedule FROM routines WHERE enabled=1 ORDER BY next_run") as cur:
        rows = await cur.fetchall()
    if not rows:
        return "регулярных сообщений нет"
    return "\n".join(f"#{r[0]} {'🤖' if r[1] == 'order' else '🔔'} {describe(json.loads(r[3]))}: {r[2][:120]}"
                     for r in rows)


async def _advance(db, rid: int, schedule: dict, now: datetime) -> None:
    nxt = next_fire(schedule, now)
    if nxt is None:
        await db.execute("DELETE FROM routines WHERE id=?", (rid,))
    else:
        await db.execute("UPDATE routines SET next_run=? WHERE id=?", (int(nxt.timestamp()), rid))
    await db.commit()


async def _say(bot, db, settings, text: str) -> None:
    from core import mira
    await bot.send_message(settings.owner_user_id, text[:4000], parse_mode=None)
    await mira.dialog_add(db, "mira", text, ["routine"])    # ответ владельца будет понят как продолжение


async def _run_order(bot, db, settings, client, text: str) -> None:
    from core import mira
    from core.mira_api import _OwnerMessage
    from db import is_over_budget
    if await is_over_budget(db, settings.anthropic_monthly_budget):
        await _say(bot, db, settings, f"🪙 Бюджет API исчерпан, поручение не выполнила: {text[:200]}")
        return
    if text.startswith("skill:"):
        from core import skills
        sid = text[len("skill:"):]
        text = await skills.prompt_of(db, sid)
        if not text:
            raise ValueError(f"нет инструкции скилла {sid}")
        await skills.mark_run(db, sid)
    ctx = await mira.assistant_context(db, settings, text)
    reply, _, _ = await asyncio.wait_for(
        mira.think(client, settings, ctx, _ORDER_PREFIX + text, message=_OwnerMessage(bot, settings.owner_user_id),
                   bot=bot, db=db, read_only=True),
        timeout=ORDER_TIMEOUT)
    await _say(bot, db, settings, reply)


async def tick(bot, db, settings, client, now: datetime | None = None) -> int:
    """Выполнить созревшее; вернуть число отправленных. Сбой одной записи не держит остальные."""
    await _ensure(db)
    now = now or now_local()
    ts, sent = int(now.timestamp()), 0
    async with db.execute("SELECT id, kind, text, schedule, next_run FROM routines "
                          "WHERE enabled=1 AND next_run<=? ORDER BY next_run LIMIT 10", (ts,)) as cur:
        rows = await cur.fetchall()
    for r in rows:
        rid, kind, text, schedule = r[0], r[1], r[2], json.loads(r[3])
        if ts - r[4] > LATE_LIMIT:
            logger.warning(f"routine #{rid}: пропущена, опоздание {ts - r[4]} с")
            await _advance(db, rid, schedule, now)
            continue
        try:
            if kind == "order":     # сначала сдвиг: сбой LLM не должен повторять платный запрос каждую минуту
                await _advance(db, rid, schedule, now)
                await _run_order(bot, db, settings, client, text)
            else:                   # сначала доставка: при сбое Telegram повторим на следующем проходе
                await _say(bot, db, settings, text)
                await _advance(db, rid, schedule, now)
            sent += 1
        except Exception as exc:
            logger.warning(f"routine #{rid} failed: {type(exc).__name__}: {exc}")
            if kind == "order":
                try:
                    await _say(bot, db, settings, f"⚠️ Не вышло выполнить поручение: {text[:200]}")
                except Exception as note_exc:
                    logger.warning(f"routine #{rid} note failed: {note_exc}")
    return sent


async def _meeting_briefs(bot, db, settings, client, now: datetime) -> None:
    """За час до встречи из календаря присылает справку, если стоит скилл «Подготовка к встрече»."""
    from core import skills
    from core.calendar import list_upcoming_events
    from db import get_digest_state, set_digest_state
    if not settings.google_calendar_id or now.minute % BRIEF_EVERY_MIN or not await skills.is_installed(db, "meeting"):
        return
    start, end = now + timedelta(minutes=50), now + timedelta(minutes=70)
    events = await list_upcoming_events(settings.google_service_account_path, settings.google_calendar_id,
                                        start.isoformat(), end.isoformat())
    for event in events:
        key = f"brief:{event['id']}:{event['start'][:16]}"
        if await get_digest_state(db, key):
            continue
        await set_digest_state(db, key, "sent")
        when = event["start"][11:16]
        prompt = await skills.prompt_of(db, "meeting")
        await _run_order(bot, db, settings, client, f"Скоро встреча «{event['summary']}» в {when}. {prompt}")


async def run_loop(bot, db, settings, client) -> None:
    logger.info("Routines loop started")
    while True:
        from core import autonomy
        try:
            if await autonomy.enabled(db, "routines"):
                await tick(bot, db, settings, client)
        except Exception as exc:
            logger.error(f"routines tick failed: {type(exc).__name__}: {exc}")
        try:
            from core import group_mira
            now = now_local()
            if await autonomy.enabled(db, "groups"):
                await group_mira.daily_tick(bot, db, settings, client, now)
            if await autonomy.enabled(db, "briefs"):
                await _meeting_briefs(bot, db, settings, client, now)
            from core import content_board, stream_report
            if await autonomy.enabled(db, "board"):
                await content_board.tick(bot, db, settings, now)
            if now.minute % 5 == 0:
                await stream_report.tick(bot, db, settings, now.timestamp())
        except Exception as exc:
            logger.warning(f"routines extras failed: {type(exc).__name__}: {exc}")
        await asyncio.sleep(TICK_SECONDS)
