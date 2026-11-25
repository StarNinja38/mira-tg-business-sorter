"""v4.77 Трекер привычек: отметки по дням. Напоминания ставятся обычными регулярными сообщениями."""
from datetime import date, timedelta

MAX_HABITS = 15


async def _ensure(db) -> None:
    await db.execute("CREATE TABLE IF NOT EXISTS habits (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE)")
    await db.execute("CREATE TABLE IF NOT EXISTS habit_log (habit_id INTEGER NOT NULL, day TEXT NOT NULL, PRIMARY KEY (habit_id, day))")
    await db.commit()


async def _find(db, name: str):
    key = (name or "").strip().lower()
    async with db.execute("SELECT id, name FROM habits") as cur:
        rows = await cur.fetchall()
    return next((r for r in rows if key and (key in r[1].lower() or r[1].lower() in key)), None)


async def add(db, name: str) -> str:
    await _ensure(db)
    name = (name or "").strip()[:60]
    if not name:
        return "не хватает названия"
    if await _find(db, name):
        return f"привычка {name} уже есть"
    async with db.execute("SELECT COUNT(*) FROM habits") as cur:
        if (await cur.fetchone())[0] >= MAX_HABITS:
            return f"уже {MAX_HABITS} привычек, сначала убери лишние"
    await db.execute("INSERT INTO habits (name) VALUES (?)", (name,))
    await db.commit()
    return f"добавила привычку {name}"


def streak(days: set, today: date) -> int:
    """Сколько дней подряд до сегодня. Сегодня ещё можно успеть, поэтому пропуск сегодня серию не рвёт."""
    cur = today if today.isoformat() in days else today - timedelta(days=1)
    count = 0
    while cur.isoformat() in days:
        count += 1
        cur -= timedelta(days=1)
    return count


async def done(db, name: str, today: date) -> str:
    await _ensure(db)
    habit = await _find(db, name)
    if not habit:
        return f"привычки {name} нет, сначала добавь её"
    await db.execute("INSERT OR IGNORE INTO habit_log (habit_id, day) VALUES (?,?)", (habit[0], today.isoformat()))
    await db.commit()
    return f"отметила {habit[1]}, серия {streak(await _days(db, habit[0]), today)} дн."


async def _days(db, habit_id: int) -> set:
    async with db.execute("SELECT day FROM habit_log WHERE habit_id=?", (habit_id,)) as cur:
        return {r[0] for r in await cur.fetchall()}


async def remove(db, name: str) -> str:
    await _ensure(db)
    habit = await _find(db, name)
    if not habit:
        return f"привычки {name} нет"
    await db.execute("DELETE FROM habit_log WHERE habit_id=?", (habit[0],))
    await db.execute("DELETE FROM habits WHERE id=?", (habit[0],))
    await db.commit()
    return f"убрала привычку {habit[1]}"


async def report(db, today: date) -> str:
    await _ensure(db)
    async with db.execute("SELECT id, name FROM habits ORDER BY id") as cur:
        rows = await cur.fetchall()
    if not rows:
        return "привычек пока нет"
    week = [(today - timedelta(days=i)).isoformat() for i in range(7)]
    lines = []
    for hid, name in rows:
        days = await _days(db, hid)
        lines.append(f"{name}: сегодня {'да' if today.isoformat() in days else 'нет'}, за неделю {sum(d in days for d in week)}/7, серия {streak(days, today)}")
    return "\n".join(lines)


async def toggle(db, habit_id: int, day: str) -> tuple[str, bool] | None:
    """v4.107 виджет: отметить день или снять отметку. (название, отмечен ли теперь) или None."""
    await _ensure(db)
    async with db.execute("SELECT name FROM habits WHERE id=?", (habit_id,)) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    async with db.execute("SELECT 1 FROM habit_log WHERE habit_id=? AND day=?", (habit_id, day)) as cur:
        had = await cur.fetchone() is not None
    if had:
        await db.execute("DELETE FROM habit_log WHERE habit_id=? AND day=?", (habit_id, day))
    else:
        await db.execute("INSERT OR IGNORE INTO habit_log (habit_id, day) VALUES (?,?)", (habit_id, day))
    await db.commit()
    return row[0], not had


async def week(db, today: date) -> list[tuple]:
    """v4.107 виджет: [(id, название, серия, '1010011' за 7 дней от старого к сегодня)]."""
    await _ensure(db)
    async with db.execute("SELECT id, name FROM habits ORDER BY id") as cur:
        rows = await cur.fetchall()
    days7 = [(today - timedelta(days=6 - i)).isoformat() for i in range(7)]
    out = []
    for hid, name in rows:
        days = await _days(db, hid)
        out.append((hid, name, streak(days, today), "".join("1" if d in days else "0" for d in days7)))
    return out
