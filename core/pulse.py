"""
v4.84 Дневное напоминание: одно короткое сообщение в 14:00, только если есть повод.

Повод один: кто-то ждёт ответа, дело на сегодня не закрыто или скоро встреча. Нет повода
или обо всём уже напомнили, Мира молчит. Собирается из базы, без LLM. Один повод не повторяем
REPEAT_DAYS дней, иначе висящий вопрос превратится в ежедневное нытьё.
"""
import json
from datetime import date, datetime, timedelta
from html import escape

from loguru import logger

from core.digest import _plural, _split_due_buckets
from core.util import profile_link, today_local

SEND_HOUR = 14
REPEAT_DAYS = 3
MEETING_HOURS = 3


def _fresh(key: str, seen: dict, today: date) -> bool:
    try:
        return (today - date.fromisoformat(seen.get(key, ""))).days >= REPEAT_DAYS
    except ValueError:
        return True


def pick(waits: list, tasks: list, meetings: list, seen: dict, today: date) -> dict | None:
    """Самый важный повод, о котором не напоминали REPEAT_DAYS дней, или None. Чистая функция."""
    for w in waits:
        key = f"u{w['user_id']}"
        if _fresh(key, seen, today):
            return {"kind": "wait", "key": key, "wait": w}
    for t in tasks:
        key = f"t{t['id']}"
        if _fresh(key, seen, today):
            return {"kind": "task", "key": key, "task": t, "more": len(tasks) - 1}
    for e in meetings:
        key = f"e{e['start']}"
        if _fresh(key, seen, today):
            return {"kind": "meeting", "key": key, "event": e}
    return None


def _since(days: int) -> str:
    return "со вчера" if days <= 1 else f"уже {days} {_plural(days, 'день', 'дня', 'дней')}"


def render(item: dict) -> tuple:
    """(rich-документ, обычный текст того же смысла). Чистая функция."""
    from core import richmsg
    B = richmsg.button
    if item["kind"] == "wait":
        w = item["wait"]
        since, quote = _since(w["age_days"]), (w.get("text") or "")[:120]
        name = f'<a href="{profile_link(w.get("username"), w.get("user_id"))}"><b>{escape(w["who"])}</b></a>'
        first = w["who"].split()[0][:20] if w["who"].strip() else ""
        ask, say = f"{first}: что я обещал?", f"{w['who']} ждёт ответ {since}. Ответить?"
        head = f"<p>{name} ждёт ответ {since}. Ответить?</p><p><i>«{escape(quote)}»</i></p>"
        btns = B("✍️ Ответить", f"rep:{w['user_id']}", "primary")
    elif item["kind"] == "task":
        t, more = item["task"], item["more"]
        at = str(t.get("due_date") or "")[11:16]
        when = f" к {at}" if at else ""
        tail = f" И ещё {more} {_plural(more, 'дело', 'дела', 'дел')} на сегодня." if more else ""
        ask, say = "Что ещё на сегодня?", f"Сегодня{when} осталось: {t['title']}.{tail}"
        head = f"<p>Сегодня{when} осталось: <b>{escape(t['title'])}</b>.{tail}</p>"
        btns = B("✓", f"td:{t['id']}", "success") + " " + B("→ завтра", f"tmr:{t['id']}")
    else:
        e = item["event"]
        at, title = e["start"][11:16], e["summary"][:60]
        ask, say = "Подготовь меня к встрече", f"В {at} встреча: {title}."
        head, btns = f"<p>В <b>{at}</b> встреча: {escape(title)}.</p>", ""
    doc = f"{head}<p>Спроси меня: <code>{escape(ask)}</code></p>"
    return doc + (f"<p>{btns}</p>" if btns else ""), f"{say}\nСпроси меня: {ask}"


async def _meetings(settings, tasks: list, now: datetime) -> list:
    """События с временем на ближайшие часы; у задач свои события в календаре, их не считаем."""
    if not settings.google_calendar_id:
        return []
    from core.calendar import _norm_summary, list_events
    names = {_norm_summary(t.get("title") or "") for t in tasks}
    events = await list_events(settings.google_service_account_path, settings.google_calendar_id,
                               now.isoformat(), (now + timedelta(hours=MEETING_HOURS)).isoformat())
    start = now.strftime("%Y-%m-%dT%H:%M")
    return [e for e in events if "T" in str(e.get("start")) and str(e["start"])[:16] >= start
            and _norm_summary(e.get("summary") or "") not in names]


async def send(bot, owner_id: int, db, settings, now: datetime) -> bool:
    """Отправить напоминание, если есть повод. True, если сообщение ушло."""
    from core import autonomy, mira, richmsg
    from core.topics import send_owner, thread_id
    from db import get_open_tasks_due, get_setting, get_unanswered_questions, set_setting
    if not await autonomy.enabled(db, "pulse"):
        return False
    today = today_local()
    seen = json.loads(await get_setting(db, "pulse_seen") or "{}")
    tasks = _split_due_buckets(await get_open_tasks_due(db), today)[0]
    meetings = []
    try:
        meetings = await _meetings(settings, tasks, now)
    except Exception as exc:
        logger.debug(f"pulse calendar skip: {exc}")
    item = pick(await get_unanswered_questions(db, hours=24, limit=5), tasks, meetings, seen, today)
    if not item:
        logger.info("Pulse: повода нет, молчим")
        return False
    doc, text = render(item)
    if not await richmsg.send_rich(settings.bot_token, owner_id, doc, thread_id("digest")):
        await send_owner(bot, owner_id, "digest", escape(text))
    seen = {k: v for k, v in seen.items() if not _fresh(k, seen, today)}
    seen[item["key"]] = today.isoformat()
    await set_setting(db, "pulse_seen", json.dumps(seen))
    await mira.dialog_add(db, "mira", text)   # ответ «да» должен быть понятен Мире
    logger.info(f"Pulse sent: {item['kind']}")
    return True
