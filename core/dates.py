"""
v4.30 Дни рождения и важные даты (по запросу владельца).
Хранение: settings_kv `bday:{uid|slug}` → JSON {"who","mmdd","year","uid"}.
Ввод — через чат с Мирой («у Вани др 12 мая»), intent=bday в owner-роутере.
Вывод — блок в утренней сводке (сегодня/завтра) + карточка с кнопкой «✍️ Поздравление».
"""
import json
import re
from typing import Optional

_MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}


def norm_bdate(s: str) -> tuple[Optional[str], Optional[int]]:
    """Нормализовать дату рождения в ("MM-DD", year|None).
    Принимает: YYYY-MM-DD / MM-DD / DD.MM / DD.MM.YYYY / «12 мая» / «12 мая 1999».
    (None, None) если не распарсилось. Чистая функция — покрыта тестами."""
    s = (s or "").strip().lower()
    if not s:
        return None, None
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)          # YYYY-MM-DD
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return (f"{mo:02d}-{d:02d}", y) if _valid(mo, d) else (None, None)
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", s)                  # MM-DD
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        return (f"{mo:02d}-{d:02d}", None) if _valid(mo, d) else (None, None)
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?", s)   # DD.MM[.YYYY]
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        y = int(m.group(3)) if m.group(3) else None
        return (f"{mo:02d}-{d:02d}", y) if _valid(mo, d) else (None, None)
    m = re.fullmatch(r"(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?", s)  # «12 мая [1999]»
    if m:
        d = int(m.group(1))
        mo = _MONTHS.get(m.group(2)[:3])
        y = int(m.group(3)) if m.group(3) else None
        return (f"{mo:02d}-{d:02d}", y) if mo and _valid(mo, d) else (None, None)
    return None, None


def _valid(mo: int, d: int) -> bool:
    """C12: 31.02 / 30.02 не проходят; 29.02 — допускаем (проверка по високосному 2000)."""
    from datetime import date as _date
    try:
        _date(2000, mo, d)
        return True
    except ValueError:
        return False


def fmt_mmdd(mmdd: str) -> str:
    """'05-12' → '12.05' (человеческий вид)."""
    try:
        mo, d = mmdd.split("-")
        return f"{int(d):02d}.{int(mo):02d}"
    except Exception:
        return mmdd


def slug_for(who: str, uid: Optional[int] = None) -> str:
    """Ключ записи (uid контакта или slug имени) — общий для сохранения и callback-кнопок."""
    return str(uid) if uid else (re.sub(r"\W+", "", (who or "").lower())[:24] or "x")


async def save_bday(db, who: str, mmdd: str, year: Optional[int] = None,
                    uid: Optional[int] = None) -> None:
    from db import set_setting
    slug = slug_for(who, uid)
    if not uid:
        slug = f"{slug}-{mmdd.replace('-', '')}"   # C11: два «Вани» без uid не перетирают друг друга
    await set_setting(db, f"bday:{slug}", json.dumps(
        {"who": who[:60], "mmdd": mmdd, "year": year, "uid": uid}, ensure_ascii=False))


async def all_bdays(db) -> list[dict]:
    out = []
    async with db.execute("SELECT key, value FROM settings_kv WHERE key LIKE 'bday:%'") as cur:
        for key, raw in await cur.fetchall():
            try:
                p = json.loads(raw)
                if p.get("mmdd"):
                    p["slug"] = key.split(":", 1)[1]   # C11: реальный ключ — для кнопки bdg:{slug}
                    out.append(p)
            except Exception:
                continue
    return out


def bdays_on(bdays: list[dict], mmdd: str) -> list[dict]:
    return [b for b in bdays if b.get("mmdd") == mmdd]


def turning_age(b: dict, year_now: int) -> Optional[int]:
    """Сколько исполняется в этом году (если год рождения известен)."""
    y = b.get("year")
    return (year_now - int(y)) if y else None
