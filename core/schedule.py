"""
v4.31 Расписание МИРЭА (одобрено владельцем, гибрид).
Публичное API вуза мертво (расписание увели в мессенджер MAX) → источник = data/schedule.json,
заполняется вручную из скрина владельца; обновления — правкой файла. Ноль LLM, ноль сети.

Формат data/schedule.json:
{
  "group": "БСБО-22-24",
  "semester_start": "2026-09-01",
  "weeks": {
    "odd":  {"1": [{"n": 2, "subject": "Матанализ", "type": "лк", "room": "А-100"}], ... },
    "even": { ... }
  }
}
Ключи дней — ISO-номер дня недели строкой ("1"=пн … "6"=сб). Неделя, содержащая
semester_start, — ПЕРВАЯ (нечётная, "odd"). Время пары — по номеру n (сетка МИРЭА),
либо явным полем "time": "HH:MM-HH:MM".
"""
import json
import re
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

from loguru import logger

# Звонковая сетка МИРЭА (Вернадского): номер пары → время
PAIR_TIMES = {
    1: "09:00-10:30", 2: "10:40-12:10", 3: "12:40-14:10", 4: "14:20-15:50",
    5: "16:20-17:50", 6: "18:00-19:30", 7: "19:40-21:10",
}


def _load(path: str = "./data/schedule.json") -> dict:
    try:
        p = Path(path)
        if not p.exists():
            return {}
        return _load_cached(str(p), p.stat().st_mtime)
    except Exception as exc:
        logger.debug(f"schedule load skip: {exc}")
        return {}


@lru_cache(maxsize=4)
def _load_cached(path: str, mtime: float) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def week_number(d: date, semester_start: date) -> int:
    """Номер учебной недели (1-based). Неделя с semester_start = 1."""
    monday0 = semester_start - timedelta(days=semester_start.weekday())
    monday = d - timedelta(days=d.weekday())
    return max(1, (monday - monday0).days // 7 + 1)


def parity(d: date, semester_start: date) -> str:
    return "odd" if week_number(d, semester_start) % 2 == 1 else "even"


def pair_time(p: dict) -> str:
    """'09:00-10:30' — явное поле time или по номеру пары."""
    t = p.get("time") or PAIR_TIMES.get(int(p.get("n") or 0), "")
    return re.sub(r"\b(\d):", r"0\1:", t) if t else t   # C15: «9:00-10:30» → «09:00-10:30» (сортировка/срезы)


def pairs_for(d: date, path: str = "./data/schedule.json") -> list[dict]:
    """Пары на дату (отсортированы по времени). [] если расписания нет/каникулы/воскресенье."""
    data = _load(path)
    if not data:
        return []
    try:
        start = date.fromisoformat(data["semester_start"])
        if d < start:
            return []
        day = data.get("weeks", {}).get(parity(d, start), {}).get(str(d.isoweekday()), [])
        return sorted([p for p in day if pair_time(p)], key=lambda p: pair_time(p))
    except Exception as exc:
        logger.debug(f"schedule pairs skip: {exc}")
        return []


def busy_blocks(d: date, path: str = "./data/schedule.json") -> list[tuple]:
    """[(start_min, end_min)] пар — занятость для поиска слотов и конфликтов."""
    out = []
    for p in pairs_for(d, path):
        t = pair_time(p)
        try:
            a, b = t.split("-")
            out.append((int(a[:2]) * 60 + int(a[3:5]), int(b[:2]) * 60 + int(b[3:5])))
        except Exception:
            continue
    return out


def digest_lines(d: date, path: str = "./data/schedule.json") -> list[str]:
    """Строки для сводки: '09:00 Матанализ (лк, А-100)'."""
    out = []
    for p in pairs_for(d, path):
        t = pair_time(p)[:5]
        tail = ", ".join(x for x in [p.get("type"), p.get("room")] if x)
        out.append(f"{t} {p.get('subject', '?')}" + (f" ({tail})" if tail else ""))
    return out


def first_pair(d: date, path: str = "./data/schedule.json") -> dict | None:
    ps = pairs_for(d, path)
    return ps[0] if ps else None
