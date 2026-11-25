"""
v4.59: подписки владельца — список, суммы, даты списания, напоминания.

Зачем: владелец сказал (22.09.2026), что отслеживание подписок для него важно. Мира ведёт список
(«добавь подписку ChatGPT 20$ списывается 5-го»), показывает его (/subs, «какие у меня подписки»),
считает итог в месяц по валютам и напоминает в утренней сводке за N дней до списания и в день списания.

Хранение: таблица `subscriptions` (одна строка = одна подписка) + `subscription_log` (каждое прошедшее
списание — история расходов). После даты списания `next_charge` сдвигается на период (roll_forward),
списание пишется в лог.
"""
import calendar
import json
import re
import time
from datetime import date, timedelta
from typing import Optional

from loguru import logger

from core.util import today_local

PERIODS = {"month": "в месяц", "year": "в год", "week": "в неделю", "quarter": "в квартал"}
_PERIOD_ALIASES = {
    "month": "month", "monthly": "month", "мес": "month", "месяц": "month", "ежемесячно": "month", "в месяц": "month",
    "year": "year", "yearly": "year", "annual": "year", "год": "year", "ежегодно": "year", "в год": "year",
    "week": "week", "weekly": "week", "нед": "week", "неделя": "week", "еженедельно": "week", "в неделю": "week",
    "quarter": "quarter", "квартал": "quarter", "3 месяца": "quarter",
}
_CUR = {"$": "$", "usd": "$", "дол": "$", "€": "€", "eur": "€", "евро": "€", "₽": "₽", "rub": "₽", "руб": "₽", "р": "₽",
        "₸": "₸", "kzt": "₸", "£": "£", "gbp": "£", "₴": "₴", "uah": "₴"}


async def ensure_tables(db) -> None:
    await db.execute("""CREATE TABLE IF NOT EXISTS subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, amount REAL NOT NULL DEFAULT 0,
        currency TEXT NOT NULL DEFAULT '₽', period TEXT NOT NULL DEFAULT 'month', next_charge TEXT,
        source TEXT, note TEXT, remind_days INTEGER NOT NULL DEFAULT 2, active INTEGER NOT NULL DEFAULT 1,
        created_at REAL, cancelled_at REAL)""")
    await db.execute("""CREATE TABLE IF NOT EXISTS subscription_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, sub_id INTEGER, day TEXT, amount REAL, currency TEXT, name TEXT)""")
    await db.commit()


# ── разбор человеческого ввода ────────────────────────────────────────────────
def norm_period(s: str | None) -> str:
    s = (s or "month").strip().lower()
    return _PERIOD_ALIASES.get(s, "month" if "мес" in s else "year" if "год" in s else "week" if "нед" in s else "month")


def norm_currency(s: str | None) -> str:
    s = (s or "₽").strip().lower().rstrip(".")
    for k, v in _CUR.items():
        if s.startswith(k):
            return v
    return s.upper() if s else "₽"


def parse_amount(s) -> tuple[float, Optional[str]]:
    """«20$» / «199 ₽» / «€9.99» / 20 → (20.0, '$'). Валюта None, если не указана."""
    if isinstance(s, (int, float)):
        return float(s), None
    t = str(s or "").replace(",", ".").replace(" ", "")
    m = re.search(r"(\d+(?:\.\d+)?)", t)
    if not m:
        return 0.0, None
    rest = (t[:m.start()] + t[m.end():]).strip()
    return float(m.group(1)), (norm_currency(rest) if rest else None)


def add_period(d: date, period: str) -> date:
    if period == "week":
        return d + timedelta(days=7)
    months = {"month": 1, "quarter": 3, "year": 12}.get(period, 1)
    mo = d.month - 1 + months
    y, mo = d.year + mo // 12, mo % 12 + 1
    return date(y, mo, min(d.day, calendar.monthrange(y, mo)[1]))


def parse_next_charge(s, period: str = "month", today: date | None = None) -> Optional[date]:
    """«5» / «5-го» → ближайшее 5-е число; «12.10» / «12.10.2026» / ISO / «завтра» / «сегодня»."""
    today = today or today_local()
    t = str(s or "").strip().lower()
    if not t:
        return None
    if t in ("сегодня", "today"):
        return today
    if t in ("завтра", "tomorrow"):
        return today + timedelta(days=1)
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?", t)
    if m:
        d, mo = int(m[1]), int(m[2])
        y = int(m[3]) if m[3] else today.year
        y = y + 2000 if y < 100 else y
        try:
            res = date(y, mo, d)
        except ValueError:
            return None
        if not m[3] and res < today:
            res = date(y + 1, mo, d)
        return res
    m = re.fullmatch(r"(\d{1,2})(?:-?го|-?е|\s*числа)?", t)
    if m:
        d = int(m[1])
        if not 1 <= d <= 31:
            return None
        cand = date(today.year, today.month, min(d, calendar.monthrange(today.year, today.month)[1]))
        if cand < today:
            cand = add_period(date(today.year, today.month, 1), "month")
            cand = date(cand.year, cand.month, min(d, calendar.monthrange(cand.year, cand.month)[1]))
        return cand
    return None


# ── CRUD ─────────────────────────────────────────────────────────────────────
async def add(db, name: str, amount, currency: str | None = None, period: str | None = None,
              next_charge=None, source: str | None = None, note: str | None = None, remind_days: int | None = None) -> dict:
    await ensure_tables(db)
    amt, cur_from_amount = parse_amount(amount)
    cur = norm_currency(currency) if currency else (cur_from_amount or "₽")
    per = norm_period(period)
    nc = parse_next_charge(next_charge, per) if next_charge else None
    existing = await find(db, name)
    if existing:   # та же подписка — обновляем, не плодим
        await update(db, existing["id"], amount=amt, currency=cur, period=per,
                     next_charge=nc.isoformat() if nc else existing.get("next_charge"),
                     source=source or existing.get("source"), note=note or existing.get("note"),
                     remind_days=remind_days if remind_days is not None else existing.get("remind_days", 2), active=1)
        return dict(await get(db, existing["id"]), updated=True)
    await db.execute("INSERT INTO subscriptions(name, amount, currency, period, next_charge, source, note, remind_days, active, created_at) "
                     "VALUES (?,?,?,?,?,?,?,?,1,?)",
                     (name.strip(), amt, cur, per, nc.isoformat() if nc else None, source, note, remind_days if remind_days is not None else 2, time.time()))
    await db.commit()
    async with db.execute("SELECT last_insert_rowid()") as c:
        (sid,) = await c.fetchone()
    logger.info(f"Subscription added #{sid}: {name} {amt} {cur} {per} next={nc}")
    return dict(await get(db, sid), updated=False)


async def get(db, sid: int) -> Optional[dict]:
    async with db.execute("SELECT * FROM subscriptions WHERE id=?", (sid,)) as c:
        cols = [d[0] for d in c.description]
        r = await c.fetchone()
    return dict(zip(cols, tuple(r))) if r else None


async def find(db, name: str, active_only: bool = True) -> Optional[dict]:
    """По имени без регистра, потом по вхождению."""
    await ensure_tables(db)
    q = (name or "").strip().lower()
    if not q:
        return None
    rows = await list_all(db, active_only=active_only)
    for r in rows:
        if r["name"].lower() == q:
            return r
    for r in rows:
        if q in r["name"].lower() or r["name"].lower() in q:
            return r
    return None


async def update(db, sid: int, **fields) -> None:
    allowed = {"name", "amount", "currency", "period", "next_charge", "source", "note", "remind_days", "active", "cancelled_at"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    await db.execute(f"UPDATE subscriptions SET {sets} WHERE id=?", (*fields.values(), sid))
    await db.commit()


async def update_by_args(db, sub: dict, args: dict) -> dict:
    """v4.60: частичное обновление из аргументов инструмента Миры (new_name/amount/currency/period/next_charge/…)."""
    f = {}
    if args.get("new_name"):
        f["name"] = str(args["new_name"]).strip()
    if args.get("amount") is not None:
        amt, cur = parse_amount(args["amount"])
        f["amount"] = amt
        if cur and not args.get("currency"):
            f["currency"] = cur
    if args.get("currency"):
        f["currency"] = norm_currency(args["currency"])
    if args.get("period"):
        f["period"] = norm_period(args["period"])
    if args.get("next_charge"):
        nc = parse_next_charge(args["next_charge"], f.get("period") or sub.get("period") or "month")
        if nc:
            f["next_charge"] = nc.isoformat()
    for k in ("source", "note", "remind_days"):
        if args.get(k) is not None:
            f[k] = args[k]
    await update(db, sub["id"], **f)
    return await get(db, sub["id"])


async def cancel(db, sid: int) -> None:
    await update(db, sid, active=0, cancelled_at=time.time())
    logger.info(f"Subscription cancelled #{sid}")


async def list_all(db, active_only: bool = True) -> list[dict]:
    await ensure_tables(db)
    sql = "SELECT * FROM subscriptions" + (" WHERE active=1" if active_only else "") + " ORDER BY COALESCE(next_charge,'9999'), name"
    async with db.execute(sql) as c:
        cols = [d[0] for d in c.description]
        rows = await c.fetchall()
    return [dict(zip(cols, tuple(r))) for r in rows]


async def roll_forward(db, today: date | None = None) -> int:
    """Прошедшие списания → в лог, next_charge на период вперёд. Возвращает число сдвигов."""
    today = today or today_local()
    n = 0
    for s in await list_all(db):
        if not s.get("next_charge"):
            continue
        try:
            nc = date.fromisoformat(s["next_charge"])
        except ValueError:
            continue
        while nc < today:
            await db.execute("INSERT INTO subscription_log(sub_id, day, amount, currency, name) VALUES (?,?,?,?,?)",
                             (s["id"], nc.isoformat(), s["amount"], s["currency"], s["name"]))
            nc = add_period(nc, s["period"])
            n += 1
        if nc.isoformat() != s["next_charge"]:
            await db.execute("UPDATE subscriptions SET next_charge=? WHERE id=?", (nc.isoformat(), s["id"]))
    if n:
        await db.commit()
    return n


# ── расчёты и тексты ─────────────────────────────────────────────────────────
def monthly_equiv(s: dict) -> float:
    k = {"month": 1.0, "year": 1 / 12, "week": 52 / 12, "quarter": 1 / 3}.get(s.get("period") or "month", 1.0)
    return float(s.get("amount") or 0) * k


def _money(v: float, cur: str) -> str:
    v = round(v, 2)
    s = f"{v:,.0f}" if v == int(v) else f"{v:,.2f}"
    return f"{s.replace(',', ' ')} {cur}"


def totals(subs: list[dict]) -> str:
    """«≈ 1 297 ₽ + 40 $ в месяц» по валютам."""
    by = {}
    for s in subs:
        by[s["currency"]] = by.get(s["currency"], 0.0) + monthly_equiv(s)
    return " + ".join(_money(v, c) for c, v in sorted(by.items(), key=lambda x: -x[1])) if by else "0"


def _when(s: dict, today: date) -> tuple[str, Optional[int]]:
    if not s.get("next_charge"):
        return "дата не указана", None
    try:
        d = date.fromisoformat(s["next_charge"])
    except ValueError:
        return s["next_charge"], None
    left = (d - today).days
    if left == 0:
        return "сегодня", 0
    if left == 1:
        return "завтра", 1
    return f"{d.strftime('%d.%m')} (через {left} дн.)", left


async def summary(db) -> str:
    """Текст для Миры / plain-ответа."""
    await roll_forward(db)
    subs = await list_all(db)
    today = today_local()
    if not subs:
        return "подписок в списке нет"
    lines = []
    for s in subs:
        when, _ = _when(s, today)
        extra = f", {s['source']}" if s.get("source") else ""
        lines.append(f"• {s['name']} — {_money(s['amount'], s['currency'])} {PERIODS.get(s['period'], s['period'])}, списание {when}{extra}")
    return f"Подписки ({len(subs)}), итого ≈ {totals(subs)} в месяц:\n" + "\n".join(lines)


async def spent(db, days: int = 30) -> str:
    since = (today_local() - timedelta(days=days)).isoformat()
    async with db.execute("SELECT currency, SUM(amount), COUNT(*) FROM subscription_log WHERE day>=? GROUP BY currency", (since,)) as c:
        rows = await c.fetchall()
    if not rows:
        return f"за {days} дн. списаний в истории нет"
    return f"за {days} дн.: " + " + ".join(f"{_money(v, cur)} ({n} спис.)" for cur, v, n in rows)


async def digest_lines(db) -> list[str]:
    """Для утренней сводки: подписки, до списания которых ≤ remind_days, и сегодняшние."""
    try:
        await roll_forward(db)
        today = today_local()
        out = []
        for s in await list_all(db):
            when, left = _when(s, today)
            if left is not None and left <= int(s.get("remind_days") or 2):
                out.append(f"{s['name']} — {_money(s['amount'], s['currency'])}, {when}")
        return out
    except Exception as exc:
        logger.debug(f"subs digest skip: {exc}")
        return []


async def context_line(db) -> str:
    """Строка в картину мира Миры: ближайшие 14 дней + итог."""
    try:
        await roll_forward(db)
        subs = await list_all(db)
        if not subs:
            return ""
        today = today_local()
        soon = []
        for s in subs:
            when, left = _when(s, today)
            if left is not None and left <= 14:
                soon.append(f"{s['name']} {_money(s['amount'], s['currency'])} {when}")
        return (f"ПОДПИСКИ: {len(subs)} шт., ≈ {totals(subs)} в месяц" + (f"; ближайшие списания: " + "; ".join(soon) if soon else "")
                + ". Полный список — инструмент subscriptions(action=list).")
    except Exception as exc:
        logger.debug(f"subs ctx skip: {exc}")
        return ""


def render_rich(subs: list[dict], today: date | None = None) -> str:
    """Rich-карточка (Bot API 10.3): таблица + кнопка «✖» на каждую подписку."""
    import html as _h
    from core import richmsg as _rm
    today = today or today_local()
    if not subs:
        return "<p>💳 Подписок пока нет. Скажи Мире: «добавь подписку Spotify 199 ₽, списание 15-го».</p>"
    rows = []
    for s in subs:
        when, left = _when(s, today)
        mark = "🔴 " if left == 0 else "🟡 " if left is not None and left <= int(s.get("remind_days") or 2) else ""
        rows.append([_h.escape(s["name"]), _h.escape(_money(s["amount"], s["currency"])),
                     _h.escape(PERIODS.get(s["period"], s["period"]).replace("в ", "")),
                     mark + _h.escape(when), _rm.button("✖", f"sub:del:{s['id']}", style="danger")])
    tbl = _rm.table(["Сервис", "Сумма", "Период", "Списание", ""], rows, compact=True)
    return f"<p><b>💳 Подписки</b> — {len(subs)} шт., итого ≈ <b>{_h.escape(totals(subs))}</b> в месяц</p>{tbl}"


def plain_list(subs: list[dict], today: date | None = None) -> str:
    today = today or today_local()
    if not subs:
        return "💳 Подписок пока нет."
    return f"💳 Подписки ({len(subs)}), итого ≈ {totals(subs)} в месяц:\n" + "\n".join(
        f"• {s['name']} — {_money(s['amount'], s['currency'])} {PERIODS.get(s['period'], s['period'])}, {_when(s, today)[0]}" for s in subs)
