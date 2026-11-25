"""
v4.75 Чтение всего Todoist. Бот по-прежнему пишет только в «📱 Telegram Входящие», задачи оттуда
живут в task_sync, поэтому здесь этот проект пропущен. Менять что-либо в Todoist отсюда нельзя.

Группы проектов:
  day      — дела: сроки на неделю попадают в картину мира, план дня, приложение и сводку;
  ondemand — рабочие бэклоги (SN-CONTENT, Vault, Идеи): только когда владелец спросит;
  lists    — списки «что посмотреть» и «куда сходить», это не задачи: только по просьбе.
"""
import time
from datetime import date, timedelta

import aiohttp
from loguru import logger

_API = "https://api.todoist.com/api/v1"
CACHE_SECONDS = 300
AHEAD_DAYS = 7
MAX_CONTEXT = 25
ONDEMAND = {"6gppV96WfH8w5fQG", "6gr6gV43jCmjhPc4", "6gpwch2Q7q47hG9j"}
LISTS = {"6hF332Cpfq5prf3P", "6gp3jM7J3jhjCp44"}

_cache = {"at": 0.0, "data": None}


async def _all(session, path: str, token: str) -> list:
    out, cursor = [], ""
    while True:
        url = f"{_API}/{path}?limit=200" + (f"&cursor={cursor}" if cursor else "")
        async with session.get(url, headers={"Authorization": f"Bearer {token}"}) as resp:
            resp.raise_for_status()
            page = await resp.json()
        out += page.get("results") or []
        cursor = page.get("next_cursor")
        if not cursor:
            return out


def group_of(project_id: str, inbox_id: str) -> str:
    if project_id == inbox_id:
        return "bot"
    if project_id in LISTS:
        return "lists"
    return "ondemand" if project_id in ONDEMAND else "day"


def _shape(raw: list, projects: dict, sections: dict, inbox_id: str) -> list:
    out = []
    for t in raw:
        due = (t.get("due") or {}).get("date") or ""
        deadline = (t.get("deadline") or {}).get("date") or ""
        out.append({"id": t["id"], "title": t.get("content") or "", "desc": (t.get("description") or "").strip(),
                    "due": due, "deadline": deadline, "project": projects.get(t.get("project_id"), "?"),
                    "section": sections.get(t.get("section_id"), ""), "group": group_of(t.get("project_id"), inbox_id),
                    "sub": bool(t.get("parent_id")), "urgent": t.get("priority") == 4})
    return out


async def fetch(settings) -> list:
    """Все открытые задачи. Сбой Todoist отдаёт прошлый снимок, чтобы не ломать ответы Миры."""
    if _cache["data"] is not None and time.time() - _cache["at"] < CACHE_SECONDS:
        return _cache["data"]
    try:
        token = settings.todoist_api_token
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            projects = {p["id"]: p["name"] for p in await _all(session, "projects", token)}
            sections = {s["id"]: s["name"] for s in await _all(session, "sections", token)}
            raw = await _all(session, "tasks", token)
        _cache.update(at=time.time(), data=_shape(raw, projects, sections, settings.todoist_project_id))
    except Exception as exc:
        logger.warning(f"todoist read failed: {type(exc).__name__}: {exc}")
        if _cache["data"] is None:
            return []
    return _cache["data"]


def _day(task: dict) -> str:
    return (task["due"] or task["deadline"])[:10]


def _time(task: dict) -> str:
    return task["due"][11:16] if len(task["due"]) >= 16 else ""


def upcoming(tasks: list, today: date, days: int = AHEAD_DAYS) -> list:
    """Дела из группы day: просроченные и со сроком в ближайшие days дней, по дате. Чистая функция."""
    end = (today + timedelta(days=days)).isoformat()
    rows = [t for t in tasks if t["group"] == "day" and not t["sub"] and _day(t) and _day(t) <= end]
    return sorted(rows, key=lambda t: (_day(t), _time(t) or "99"))


def line(task: dict, today: date, with_desc: bool = True) -> str:
    day = _day(task)
    when = "просрочено с " + day[8:10] + "." + day[5:7] if day and day < today.isoformat() else (
        (day[8:10] + "." + day[5:7] + (" " + _time(task) if _time(task) else "")) if day else "без срока")
    tail = f" ({task['desc'][:160]})" if with_desc and task["desc"] else ""
    return f"[{task['project']}] {when}: {task['title']}{tail}"


async def context_block(settings, today: date) -> str:
    rows = upcoming(await fetch(settings), today)
    if not rows:
        return ""
    return "ДЕЛА ИЗ TODOIST (неделя и просроченное, закрыть их можно только в Todoist): " + "; ".join(
        line(t, today) for t in rows[:MAX_CONTEXT])


async def for_day(settings, day: date) -> list:
    """Дела на дату. На сегодня сюда же входит просроченное."""
    today_mark = day.isoformat()
    rows = upcoming(await fetch(settings), day, days=0)
    return [t for t in rows if _day(t) == today_mark or (t["urgent"] and _day(t) < today_mark)]


def project_rows(tasks: list, name: str) -> list:
    key = name.strip().lower().lstrip("🗂🌌📱 ")
    return [t for t in tasks if key and key in t["project"].lower() and t["group"] != "bot"]


def render(tasks: list, today: date, limit: int = 40) -> str:
    by_section = {}
    for t in tasks:
        by_section.setdefault(t["section"], []).append(t)
    out = [f"всего {len(tasks)}"]
    shown = 0
    for section, rows in by_section.items():
        if section:
            out.append(f"Раздел {section} ({len(rows)}):")
        for t in rows:
            if shown < limit:
                out.append("• " + line(t, today, with_desc=False))
                shown += 1
    if shown < len(tasks):
        out.append(f"и ещё {len(tasks) - shown}, уточни запрос")
    return "\n".join(out)


async def tool(args: dict, settings, today: date) -> str:
    tasks = await fetch(settings)
    action = args.get("action")
    if action == "project":
        rows = project_rows(tasks, args.get("name") or "")
        section = (args.get("section") or "").strip().lower()
        if section:
            rows = [t for t in rows if section in t["section"].lower()]
        return render(rows, today) if rows else "такого проекта или раздела нет"
    if action == "search":
        q = (args.get("query") or "").strip().lower()
        rows = [t for t in tasks if q and t["group"] != "bot" and q in (t["title"] + " " + t["desc"]).lower()]
        return render(rows, today, limit=20) if rows else "ничего не нашла"
    days = max(1, min(int(args.get("days") or AHEAD_DAYS), 60))
    return "\n".join(line(t, today) for t in upcoming(tasks, today, days)) or "дел со сроком нет"
