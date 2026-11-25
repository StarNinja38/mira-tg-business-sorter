"""
v4.47 «Память 2.0» — полная память без лимитов + чтение по смыслу (план: 02-projects/TG-SORTER/ПЛАН-Память-2.0.md).

Принципы («как в полиции»):
  • ХРАНИМ ВСЁ: секции и хронология без клипов; записи датированы ([dd.mm.yy] …); ничего не стирается —
    устаревшее переезжает в хронологию как «✗ устарело: …», а не исчезает.
  • ЭНРИЧ ДОПИСЫВАЕТ, а не переписывает: модель возвращает только add/retire/timeline (append-only diff) →
    выходные токены не растут с размером памяти.
  • ЧИТАЕМ ПО СМЫСЛУ: каждая запись память проиндексирована локальными эмбеддингами (fastembed, $0);
    в промпт попадают summary + «ключевое» + top-k релевантных записей, а не вся память.
  • «Расскажи всё про X» — отдельный дешёвый читатель (Haiku) поверх полного текста памяти.
"""
import json
import re
import time
from datetime import date as _date

try:
    from loguru import logger
except ImportError:   # sync_vault.py на Mac (homebrew python без loguru) — память должна рендериться и там
    import logging as _logging
    logger = _logging.getLogger("contact_memory")

# Порядок = порядок показа (промпт энрича, /about, vault-карточка)
SECTION_LABELS = [
    ("key", "КЛЮЧЕВОЕ"), ("identity", "ЛИЧНЫЕ ДАННЫЕ"), ("history", "ИСТОРИЯ ОТНОШЕНИЙ"),
    ("relations", "СВЯЗИ/ОКРУЖЕНИЕ"), ("personal", "ЛИЧНАЯ ЖИЗНЬ"), ("study_work", "ОБРАЗОВАНИЕ/РАБОТА"),
    ("home", "МЕСТО ЖИТЕЛЬСТВА"), ("property", "ИМУЩЕСТВО/ТРАНСПОРТ"), ("contacts", "КОНТАКТЫ/ИНТЕРНЕТ"),
    ("money", "ДЕНЬГИ"), ("plans", "ПЛАНЫ/ДОГОВОРЁННОСТИ"), ("character", "ХАРАКТЕР/ОБЩЕНИЕ"),
    ("beliefs", "УБЕЖДЕНИЯ/ЦЕЛИ/МЕЧТЫ"), ("habits", "ПРИВЫЧКИ/БЫТ/ХОББИ"), ("prefs", "ПРЕДПОЧТЕНИЯ/ВКУСЫ"),
    ("sensitive", "ЗДОРОВЬЕ/ЛИЧНОЕ"), ("other", "ПРОЧЕЕ"),
]
UI_LABELS = [
    ("key", "📇 Ключевое"), ("identity", "🪪 Личные данные"), ("history", "📖 История отношений"),
    ("relations", "🔗 Связи и окружение"), ("personal", "❤️ Личная жизнь"), ("study_work", "🎓 Образование / работа"),
    ("home", "🏠 Место жительства"), ("property", "🚗 Имущество и транспорт"), ("contacts", "📞 Контакты и интернет"),
    ("money", "💸 Деньги"), ("plans", "📅 Планы и договорённости"), ("character", "💬 Характер и общение"),
    ("beliefs", "🎯 Убеждения, цели, мечты"), ("habits", "☕ Привычки, быт, хобби"), ("prefs", "🎁 Предпочтения и вкусы"),
    ("sensitive", "🩺 Здоровье / личное"), ("other", "🗒 Прочее"),
]
SECTION_KEYS = [k for k, _ in SECTION_LABELS]
_DATE_RE = re.compile(r"^\s*\[\d{2}\.\d{2}(?:\.\d{2,4})?\]\s*")
FACTS_CAP = 200_000   # страховка от битого ответа, не «лимит памяти»


# ── чистые помощники ────────────────────────────────────────────────────────
def split_entries(s) -> list:
    return [x.strip() for x in str(s or "").split(";") if x.strip()]


def join_entries(items: list) -> str:
    return "; ".join(items)


def norm_entry(s: str) -> str:
    """Ключ дедупа: без даты-префикса, регистра и пунктуации."""
    s = _DATE_RE.sub("", str(s or "")).lower()
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


_LABEL_ECHO = re.compile(
    r"^\s*(?:\[[^\]]{1,40}\]\s*)?(?:" + "|".join(re.escape(lbl) for _, lbl in [
        ("key", "КЛЮЧЕВОЕ"), ("history", "ИСТОРИЯ ОТНОШЕНИЙ"), ("relations", "СВЯЗИ И ОКРУЖЕНИЕ"), ("relations", "СВЯЗИ/ОКРУЖЕНИЕ"),
        ("personal", "ЛИЧНАЯ ЖИЗНЬ"), ("study_work", "ОБРАЗОВАНИЕ И РАБОТА"), ("study_work", "УЧЁБА/РАБОТА"), ("study_work", "УЧЁБА/РАБОТА/ПРОЕКТЫ"),
        ("home", "МЕСТО ЖИТЕЛЬСТВА"), ("property", "ИМУЩЕСТВО И ТРАНСПОРТ"), ("contacts", "КОНТАКТЫ И ИНТЕРНЕТ"), ("contacts", "КОНТАКТЫ И ДАТЫ"),
        ("money", "ДЕНЬГИ"), ("plans", "ПЛАНЫ И ДОГОВОРЁННОСТИ"), ("character", "ХАРАКТЕР И ОБЩЕНИЕ"), ("character", "ХАРАКТЕР/СТИЛЬ"),
        ("beliefs", "УБЕЖДЕНИЯ, ЦЕЛИ И МЕЧТЫ"), ("habits", "ПРИВЫЧКИ, БЫТ И ХОББИ"), ("habits", "ПРИВЫЧКИ И БЫТ"), ("prefs", "ПРЕДПОЧТЕНИЯ И ВКУСЫ"),
        ("sensitive", "ЧУВСТВИТЕЛЬНОЕ"), ("sensitive", "ЗДОРОВЬЕ/ЛИЧНОЕ"), ("other", "ПРОЧЕЕ")]) +
    r")(?:\s+через\s*;)?\s*[:—–-]\s*", re.IGNORECASE)


_LABEL_ONLY = re.compile(r"^\s*(?:\[[^\]]{1,40}\]\s*)?[А-ЯЁ][А-ЯЁ /,]{3,40}(?:\s+через)?\s*[:—–-]?\s*$")


def strip_label_echo(entry: str) -> str:
    """Убрать эхо подсказки промпта в начале записи: «КЛЮЧЕВОЕ через ; — Девушка…» → «Девушка…».
    Т.к. секция режется по «;», эхо часто становится ОТДЕЛЬНОЙ записью «КЛЮЧЕВОЕ через» (→ пусто, выбрасывается),
    а следующая начинается с «— …» (тире срезаем)."""
    e = str(entry or "").strip()
    if _LABEL_ONLY.match(e):
        return ""
    m = _LABEL_ECHO.match(e)
    if m:
        lead = m.group(0)
        dm = re.match(r"^\s*(\[[^\]]{1,40}\]\s*)", lead)   # дату-префикс сохранить
        e = ((dm.group(1) if dm else "") + e[len(lead):]).strip()
    dm = re.match(r"^\s*(\[[^\]]{1,40}\]\s*)?[—–-]\s+(.+)$", e)
    if dm:
        e = ((dm.group(1) or "") + dm.group(2)).strip()
    return e


def date_prefix(entry: str, stamp: str) -> str:
    entry = str(entry or "").strip().rstrip(";")
    return entry if _DATE_RE.match(entry) else f"[{stamp}] {entry}"


def _norm_tl(t):
    if not isinstance(t, dict):
        return None
    ev = str(t.get("event") or "").strip()
    if not ev:
        return None
    return {"date": str(t.get("date") or "").strip()[:16], "event": ev[:600]}


def _entry_parts(e):
    """add-элемент: str или {t, c, by, src} → (text, conf, by, src)."""
    if isinstance(e, dict):
        t = str(e.get("t") or e.get("text") or "").strip()
        c = str(e.get("c") or "").strip().lower()
        c = "предп" if c.startswith("предп") or c in ("guess", "догадка") else "факт"
        by = str(e.get("by") or "").strip().lower()
        by = by if by in ("сам", "владелец", "другой") else ""
        src = [int(x) for x in (e.get("src") or []) if str(x).strip().lstrip("#").isdigit()][:5] if isinstance(e.get("src"), list) else []
        return t, c, by, src
    return str(e or "").strip(), "факт", "", []


def merge(old_json, add: dict | None, retire: list | None, timeline_new: list | None,
          stamp: str | None = None, legacy_sections: dict | None = None) -> tuple[dict, list, list, dict]:
    """Append-only слияние. Возвращает (sections, timeline, changed_section_keys, meta).
    add — {section: [записи]} (str или {t,c,by,src}); новое дописываем с датой, ПОВТОР существующей записи
    не выбрасываем молча, а бампаем meta.cf («подтверждено») и дописываем источники (Память 3.0/A);
    retire — [текст устаревшей записи] → убираем из секции, кладём в хронологию «✗ устарело: …»;
    legacy_sections — старый формат «секция целиком»: берём из неё только НОВЫЕ записи, ничего не стираем.
    meta: {norm(entry): {d: дата записи, cf: дата подтверждения, c: факт|предп, by: сам|владелец|другой, src: [id сообщений]}}."""
    stamp = stamp or _date.today().strftime("%d.%m.%y")
    old_sec, old_tl, meta = {}, [], {}
    if old_json:
        try:
            d = json.loads(old_json)
            old_sec = d.get("sections") or {}
            old_tl = d.get("timeline") or []
            meta = dict(d.get("meta") or {})
        except Exception:
            pass
    sec = {k: split_entries(old_sec.get(k)) for k in SECTION_KEYS}
    for k, v in (old_sec or {}).items():          # секции вне схемы (старые) — не теряем
        if k not in sec and split_entries(v):
            sec[k] = split_entries(v)
    known = {norm_entry(e) for items in sec.values() for e in items}
    changed = set()

    def _add(k, raw):
        text, conf, by, src = _entry_parts(raw)
        text = strip_label_echo(text)
        n = norm_entry(text)
        if len(n) < 3:
            return
        if n in known:                       # повтор = подтверждение свежести
            m = meta.setdefault(n, {"d": None})
            m["cf"] = stamp
            if src:
                m["src"] = sorted(set((m.get("src") or []) + src))[-8:]
            if conf == "факт" and m.get("c") == "предп":
                m["c"] = "факт"              # догадка подтвердилась фактом
            return
        sec.setdefault(k, []).append(date_prefix(text, stamp))
        known.add(n)
        changed.add(k)
        m = {"d": stamp, "cf": stamp, "c": conf}
        if by:
            m["by"] = by
        if src:
            m["src"] = src
        meta[n] = m

    if legacy_sections:
        for k, v in legacy_sections.items():
            for e in split_entries(v):
                _add(k if k in sec or k in SECTION_KEYS else "other", e)
    for k, items in (add or {}).items():
        if isinstance(items, str):
            items = split_entries(items)
        for e in items or []:
            _add(k if k in SECTION_KEYS else "other", e)

    retired_events = []
    for r in (retire or []):
        rn = norm_entry(r)
        if len(rn) < 8:
            continue
        for k, items in sec.items():
            keep = []
            for e in items:
                en = norm_entry(e)
                if en == rn or (len(rn) >= 15 and (rn in en or en in rn)):
                    retired_events.append({"date": stamp, "event": f"✗ устарело: {_DATE_RE.sub('', e)}"})
                    changed.add(k)
                    meta.pop(en, None)
                else:
                    keep.append(e)
            sec[k] = keep

    tl, seen = [], set()
    for t in list(old_tl) + list(timeline_new or []) + retired_events:
        it = _norm_tl(t)
        if not it:
            continue
        key = (it["date"], it["event"].lower())
        if key in seen:
            continue
        seen.add(key)
        tl.append(it)
    live = {norm_entry(e) for items in sec.values() for e in items}
    meta = {k: v for k, v in meta.items() if k in live}
    return {k: join_entries(v) for k, v in sec.items()}, tl, sorted(changed), meta


def entry_marks(meta: dict, entry: str) -> str:
    """Пометки к записи для показа: «· предположение», «· со слов другого», «· подтв. 09.09.26», «· #123».
    Пусто, если записана сегодня фактом и подтверждений не было."""
    m = (meta or {}).get(norm_entry(entry)) or {}
    parts = []
    if m.get("c") == "предп":
        parts.append("предположение")
    if m.get("by") == "другой":
        parts.append("со слов третьего лица")
    if m.get("cf") and m.get("d") and m["cf"] != m["d"]:
        parts.append(f"подтв. {m['cf']}")
    if m.get("src"):
        parts.append("источник: " + ", ".join(f"#{i}" for i in m["src"][:3]))
    return (" · " + " · ".join(parts)) if parts else ""


def compose_facts_json(sections: dict, timeline: list, meta: dict, old_json=None, **extra) -> str:
    """Собрать facts_json, сохранив прежние верхние поля (gender, style), если новые не переданы."""
    d = {}
    try:
        d = json.loads(old_json or "{}")
    except Exception:
        pass
    out = {k: v for k, v in d.items() if k not in ("sections", "timeline", "meta")}
    out.update({k: v for k, v in extra.items() if v is not None})
    out.update({"sections": sections, "timeline": timeline, "meta": meta})
    return json.dumps(out, ensure_ascii=False)


def flatten(sections: dict, timeline: list, style: str | None = None) -> str:
    parts = []
    for k, _ in SECTION_LABELS:
        v = str(sections.get(k) or "").strip().rstrip("; ")
        if v:
            parts.append(v)
    for k, v in sections.items():
        if k not in SECTION_KEYS and str(v or "").strip():
            parts.append(str(v).strip().rstrip("; "))
    flat = "; ".join(parts)
    tl = [it for it in (_norm_tl(t) for t in (timeline or [])) if it]
    if tl:
        flat += " | ХРОНОЛОГИЯ: " + "; ".join(f"{it['date']} {it['event']}".strip() for it in tl)
    if style:
        flat += f"; СТИЛЬ ПИСЬМА: {style}"
    return flat.strip()[:FACTS_CAP]


def full_text(profile: dict) -> str:
    """Полная память читаемым текстом (для читателя Haiku и выгрузок)."""
    name = profile.get("name") or f"@{profile.get('username') or '?'}"
    out = [f"ПАМЯТЬ: {name} (@{profile.get('username') or '?'}) — роль: {profile.get('relationship') or '—'}",
           f"Резюме: {profile.get('summary') or '—'}"]
    raw = profile.get("facts_json")
    if raw:
        try:
            d = json.loads(raw)
            secs = d.get("sections") or {}
            for k, label in SECTION_LABELS:
                items = split_entries(secs.get(k))
                if items:
                    out.append(f"\n[{label}]\n" + "\n".join(f"• {e}" for e in items))
            tl = [it for it in (_norm_tl(t) for t in (d.get("timeline") or [])) if it]
            if tl:
                out.append("\n[ХРОНОЛОГИЯ]\n" + "\n".join(f"• {it['date']} {it['event']}" for it in tl))
            if d.get("style"):
                out.append(f"\n[СТИЛЬ ПИСЬМА] {d['style']}")
            return "\n".join(out)
        except Exception:
            pass
    if profile.get("facts"):
        out.append("\n[ФАКТЫ]\n" + profile["facts"])
    return "\n".join(out)


def entries_of(facts_json) -> list:
    """[(section, entry)] всех записей памяти."""
    try:
        d = json.loads(facts_json or "{}")
    except Exception:
        return []
    out = []
    for k, v in (d.get("sections") or {}).items():
        for e in split_entries(v):
            out.append((k, e))
    for t in d.get("timeline") or []:
        it = _norm_tl(t)
        if it:
            out.append(("timeline", f"{it['date']} {it['event']}".strip()))
    return out


# ── индекс записей памяти (локальные эмбеддинги) ─────────────────────────────
async def _ensure_table(db):
    await db.execute("CREATE TABLE IF NOT EXISTS contact_memory_vectors (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "user_id INTEGER, section TEXT, entry TEXT, vector BLOB, created_at REAL)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_contact_memory_vectors_user ON contact_memory_vectors(user_id)")


async def reindex(db, user_id: int, facts_json) -> int:
    """Пересобрать индекс записей одной памяти. Возвращает число записей; 0 при выключенном RAG/сбое."""
    try:
        from core.embeddings import embed, pack
        await _ensure_table(db)
        items = entries_of(facts_json)
        rows = []
        for section, entry in items:
            vec = await embed(f"{section}: {entry}"[:600])
            if vec is not None:
                rows.append((user_id, section, entry[:1000], pack(vec), time.time()))
        await db.execute("DELETE FROM contact_memory_vectors WHERE user_id=?", (user_id,))
        if rows:
            await db.executemany("INSERT INTO contact_memory_vectors(user_id, section, entry, vector, created_at) VALUES (?,?,?,?,?)", rows)
        await db.commit()
        return len(rows)
    except Exception as exc:
        logger.warning(f"contact_memory reindex skip {user_id}: {type(exc).__name__}: {exc}")
        return 0


async def retrieve(db, user_id: int, query: str, k: int = 10, min_score: float = 0.30) -> list:
    """[(section, entry, score)] релевантные записи памяти по смыслу вопроса. [] при любом сбое."""
    try:
        from core.embeddings import embed, unpack, _cosine
        await _ensure_table(db)
        qv = await embed(query[:600])
        if qv is None:
            return []
        async with db.execute("SELECT section, entry, vector FROM contact_memory_vectors WHERE user_id=?", (user_id,)) as cur:
            rows = await cur.fetchall()
        scored = []
        for section, entry, blob in rows:
            try:
                sc = _cosine(qv, unpack(blob))
            except Exception:
                continue
            if sc >= min_score:
                scored.append((sc, section, entry))
        scored.sort(key=lambda x: -x[0])
        return [(s, e, round(sc, 2)) for sc, s, e in scored[:k]]
    except Exception as exc:
        logger.debug(f"contact_memory retrieve skip: {exc}")
        return []


async def brief(db, profile: dict, query: str | None = None, k: int = 10, max_chars: int = 3000) -> str:
    """Короткая справка для промптов: резюме + «ключевое» целиком + релевантные вопросу записи
    (или, без вопроса/индекса, первые записи каждой секции). Не вся память."""
    if not profile:
        return ""
    name = profile.get("name") or f"@{profile.get('username') or '?'}"
    _g = {"м": "мужчина", "ж": "женщина"}.get(gender_of(profile) or "", "пол не известен")
    head = f"{name} (@{profile.get('username') or '?'}, {profile.get('relationship') or '—'}, {_g}): {profile.get('summary') or ''}"
    try:
        d = json.loads(profile.get("facts_json") or "{}")
    except Exception:
        d = {}
    secs = d.get("sections") or {}
    _meta = d.get("meta") or {}
    def _mk(x):   # Память 3.0/A: Мира видит «(предположение)» и давность подтверждения
        m = _meta.get(norm_entry(x)) or {}
        tag = []
        if m.get("c") == "предп": tag.append("предположение")
        if m.get("by") == "другой": tag.append("со слов третьего лица")
        if m.get("cf") and m.get("d") and m["cf"] != m["d"]: tag.append(f"подтв. {m['cf']}")
        return f"• {x}" + (f" ({'; '.join(tag)})" if tag else "")
    lines = [_mk(e) for e in split_entries(secs.get("key"))[:8]]
    picked = set(norm_entry(e) for e in split_entries(secs.get("key")))
    hits = await retrieve(db, profile.get("user_id") or 0, query, k=k) if query else []
    for section, entry, _ in hits:
        if norm_entry(entry) not in picked:
            lines.append(_mk(entry))
            picked.add(norm_entry(entry))
    if not hits:
        for key, _ in SECTION_LABELS[1:]:
            for e in split_entries(secs.get(key))[-3:]:   # свежие — в конце секции
                if norm_entry(e) not in picked:
                    lines.append(f"• {e}")
                    picked.add(norm_entry(e))
    if not lines and profile.get("facts"):
        lines = [profile["facts"][:max_chars]]
    if profile.get("commitments"):
        lines.append(f"⚠️ незакрытые обещания владельца: {profile['commitments']}")
    body = "\n".join(lines)
    return (head + "\n" + body)[:max_chars + 400]


# ── единый рендер памяти для Telegram (rich, страницами) ──────────────────────
def render_rich_pages(profile: dict, open_tasks: list | None = None, ctx: str | None = None,
                      max_chars: int = 3500, title: str | None = None) -> list[str]:
    """Память → список rich-HTML страниц (≤ max_chars каждая): шапка-таблица + резюме + открытые задачи,
    затем 17 разделов сворачиваемыми <details> (ключевое — раскрыто), хронология, последние сообщения.
    Используется в /about, «📋 Память» и инструменте Миры full_contact_memory — один вид везде."""
    import html as _h
    e = lambda x: _h.escape(str(x or ""))
    uname = profile.get("username") or f"id{profile.get('user_id') or '?'}"
    name = profile.get("name") or f"@{uname}"
    rows = []
    if profile.get("name"):
        rows.append(f"<tr><td>Имя</td><td>{e(profile['name'])}</td></tr>")
    rows.append(f"<tr><td>Роль</td><td>{e(profile.get('relationship') or '—')}</td></tr>")
    if profile.get("contact_type"):
        rows.append(f"<tr><td>Тип</td><td>{e(profile['contact_type'])}</td></tr>")
    if profile.get("message_count") is not None:
        rows.append(f"<tr><td>Сообщений</td><td>{int(profile.get('message_count') or 0)}</td></tr>")
    if profile.get("intent"):
        rows.append(f"<tr><td>Зачем пишет</td><td>{e(profile['intent'])}</td></tr>")
    head = (f"<h3>{e(title or ('📋 Память: ' + name))}</h3>"
            f"<table bordered compact><tr><th>Поле</th><th>Значение</th></tr>{''.join(rows)}</table>")
    if profile.get("summary"):
        head += f"<blockquote>🧠 {e(profile['summary'])}</blockquote>"
    if open_tasks:
        head += (f"<p>📋 Открытых задач: <b>{len(open_tasks)}</b></p><ul>"
                 + "".join(f"<li>{e(t.get('title') or t.get('task_title'))}</li>" for t in open_tasks[:6]) + "</ul>")
    blocks = []
    try:
        d = json.loads(profile.get("facts_json") or "{}")
    except Exception:
        d = {}
    secs = d.get("sections") or {}
    total = 0
    for k, label in UI_LABELS:
        items = split_entries(secs.get(k))
        if not items:
            continue
        total += len(items)
        opened = " open" if k == "key" else ""
        blocks.append(f"<details{opened}><summary>{e(label)} ({len(items)})</summary><ul>"
                      + "".join(f"<li>{e(it)}</li>" for it in items) + "</ul></details>")
    if d.get("style"):
        blocks.append(f"<details><summary>✍️ Стиль письма</summary><p>{e(d['style'])}</p></details>")
    tl = [it for it in (_norm_tl(t) for t in (d.get("timeline") or [])) if it]
    if tl:
        blocks.append(f"<details><summary>🕐 Хронология ({len(tl)})</summary><ul>"
                      + "".join(f"<li><b>{e(it['date'])}</b> — {e(it['event'])}</li>" for it in tl) + "</ul></details>")
    if not blocks and profile.get("facts"):
        blocks.append(f"<details open><summary>📌 Факты</summary><p>{e(profile['facts'])}</p></details>")
    if ctx:
        blocks.append(f"<details><summary>💬 Последние сообщения</summary><p>{e(ctx[:900])}</p></details>")
    if total:
        head += f"<p><i>{total} записей в {sum(1 for k, _ in UI_LABELS if split_entries(secs.get(k)))} разделах</i></p>"
    pages, cur = [], head
    for b in blocks:
        if len(cur) + len(b) > max_chars and cur:
            pages.append(cur); cur = ""
        while len(b) > max_chars:   # один гигантский раздел — режем по пунктам
            cut = b.rfind("</li>", 0, max_chars - 30)
            if cut <= 0:
                break
            pages.append(cur + b[:cut + 5] + "</ul></details>"); cur = ""
            b = "<details open><summary>… продолжение</summary><ul>" + b[cut + 5:]
        cur += b
    if cur:
        pages.append(cur)
    return pages


async def send_rich_pages(bot_token: str, chat_id: int, pages: list[str], thread_id=None) -> bool:
    """Отправить страницы памяти rich-сообщениями (False — если первая не ушла → вызывающий шлёт plain)."""
    from core.richmsg import send_rich
    ok_any = False
    for i, pg in enumerate(pages):
        ok = await send_rich(bot_token, chat_id, pg, thread_id)
        if i == 0 and not ok:
            return False
        ok_any = ok_any or ok
    return ok_any


def gender_of(profile: dict) -> str | None:
    """«м»/«ж»/None из facts_json.gender (энрич) с фолбэком по роли (мама/девушка/папа/брат…)."""
    try:
        g = (json.loads(profile.get("facts_json") or "{}").get("gender") or "").strip().lower()[:1]
        if g in ("м", "ж"):
            return g
    except Exception:
        pass
    rel = (profile.get("relationship") or "").lower()
    if any(w in rel for w in ("мам", "девушк", "сестр", "бабушк", "жена", "подруг", "тёт", "преподавательниц", "дочь")):
        return "ж"
    if any(w in rel for w in ("пап", "отец", "брат", "дедушк", "муж", "друг", "парень", "дяд", "сын", "тренер")):
        return "м"
    return None


def gender_label(profile: dict) -> str:
    return {"м": "мужской", "ж": "женский"}.get(gender_of(profile) or "", "не определён")


def render_overview(profile: dict, open_tasks: list | None = None, title: str | None = None,
                    people: list | None = None, rhythm_line: str | None = None) -> str:
    """ОДНА карточка-обзор: шапка, резюме, задачи, таблица разделов «раздел · записей · 📂» (кнопка раскрывает
    раздел отдельным сообщением — dsec:{uid}:{key}). Так память любого размера — один экран, а не 13 сообщений."""
    import html as _h
    from core.richmsg import button
    e = lambda x: _h.escape(str(x or ""))
    uid = profile.get("user_id") or 0
    uname = profile.get("username") or f"id{uid}"
    name = profile.get("name") or f"@{uname}"
    rows = []
    if profile.get("name"):
        rows.append(f"<tr><td>Имя</td><td>{e(profile['name'])} (@{e(uname)})</td></tr>")
    rows.append(f"<tr><td>Роль</td><td>{e(profile.get('relationship') or '—')}</td></tr>")
    rows.append(f"<tr><td>Пол</td><td>{gender_label(profile)}</td></tr>")
    if profile.get("contact_type"):
        rows.append(f"<tr><td>Тип</td><td>{e(profile['contact_type'])}</td></tr>")
    if profile.get("message_count") is not None:
        rows.append(f"<tr><td>Сообщений</td><td>{int(profile.get('message_count') or 0)}</td></tr>")
    if profile.get("intent"):
        rows.append(f"<tr><td>Зачем пишет</td><td>{e(profile['intent'])}</td></tr>")
    if rhythm_line:
        rows.append(f"<tr><td>Ритм</td><td>{e(rhythm_line)}</td></tr>")   # Память 3.0/C
    doc = (f"<h3>{e(title or ('📋 Память: ' + name))}</h3>"
           f"<table bordered compact><tr><th>Поле</th><th>Значение</th></tr>{''.join(rows)}</table>")
    if profile.get("summary"):
        doc += f"<blockquote>🧠 {e(profile['summary'])}</blockquote>"
    if open_tasks:
        doc += (f"<p>📋 Открытых задач: <b>{len(open_tasks)}</b></p><ul>"
                + "".join(f"<li>{e(t.get('title') or t.get('task_title'))}</li>" for t in open_tasks[:6]) + "</ul>")
    if people:   # Память 3.0/B
        doc += "<details><summary>👥 Люди вокруг (" + str(len(people)) + ")</summary><ul>" + "".join(
            f"<li>{e(person_brief(pp, 3))}</li>" for pp in people[:12]) + "</ul></details>"
    try:
        d = json.loads(profile.get("facts_json") or "{}")
    except Exception:
        d = {}
    secs = d.get("sections") or {}
    trs, total = [], 0
    for k, label in UI_LABELS:
        items = split_entries(secs.get(k))
        if not items:
            continue
        total += len(items)
        first = items[-1] if k != "key" else items[0]
        trs.append(f"<tr><td><b>{e(label)}</b><br/><i>{e(first[:70])}{'…' if len(first) > 70 else ''}</i></td>"
                   f"<td>{len(items)}</td><td>{button('📂', f'dsec:{uid}:{k}', 'primary')}</td></tr>")
    tl = [it for it in (_norm_tl(t) for t in (d.get("timeline") or [])) if it]
    if tl:
        trs.append(f"<tr><td><b>🕐 Хронология</b><br/><i>{e((tl[-1]['date'] + ' ' + tl[-1]['event'])[:70])}…</i></td>"
                   f"<td>{len(tl)}</td><td>{button('📂', f'dsec:{uid}:timeline', 'primary')}</td></tr>")
    if d.get("style"):
        trs.append(f"<tr><td><b>✍️ Стиль письма</b></td><td>1</td><td>{button('📂', f'dsec:{uid}:style', 'primary')}</td></tr>")
    if trs:
        doc += (f"<p><b>Разделы</b> — {total} записей; 📂 раскрывает раздел</p>"
                f"<table bordered striped compact><tr><th>Раздел</th><th>Зап.</th><th></th></tr>{''.join(trs)}</table>")
    elif profile.get("facts"):
        doc += f"<details open><summary>📌 Факты</summary><p>{e(profile['facts'][:3000])}</p></details>"
    return doc


def render_section_pages(profile: dict, key: str, max_chars: int = 3500) -> list[str]:
    """Один раздел памяти (или timeline/style) раскрытым списком, страницами."""
    import html as _h
    e = lambda x: _h.escape(str(x or ""))
    name = profile.get("name") or f"@{profile.get('username') or '?'}"
    try:
        d = json.loads(profile.get("facts_json") or "{}")
    except Exception:
        d = {}
    if key == "style":
        return [f"<h4>✍️ Стиль письма — {e(name)}</h4><p>{e(d.get('style') or '—')}</p>"]
    if key == "timeline":
        label = "🕐 Хронология"
        items = [f"<b>{e(it['date'])}</b> — {e(it['event'])}" for it in (_norm_tl(t) for t in (d.get("timeline") or [])) if it]
    else:
        label = dict(UI_LABELS).get(key, key)
        _meta = d.get("meta") or {}
        items = [e(it) + (f"<i>{e(entry_marks(_meta, it))}</i>" if entry_marks(_meta, it) else "")
                 for it in split_entries((d.get("sections") or {}).get(key))]
    head = f"<h4>{e(label)} — {e(name)} ({len(items)})</h4>"
    pages, cur = [], head + "<ul>"
    for it in items:
        li = f"<li>{it}</li>"
        if len(cur) + len(li) + 5 > max_chars:
            pages.append(cur + "</ul>"); cur = f"<h4>{e(label)} — продолжение</h4><ul>"
        cur += li
    pages.append(cur + "</ul>")
    return pages


# ── Память 3.0/B: третьи лица и питомцы без своего чата ──────────────────────
def _norm_name(n: str) -> str:
    return re.sub(r"[^\w]", "", str(n or "").lower())[:40]


async def ensure_persons(db) -> None:
    await db.execute("CREATE TABLE IF NOT EXISTS persons (id INTEGER PRIMARY KEY AUTOINCREMENT, norm_name TEXT UNIQUE, "
                     "name TEXT, kind TEXT, who TEXT, facts TEXT DEFAULT '', links TEXT DEFAULT '[]', created REAL, updated REAL)")


async def upsert_person(db, name: str, kind: str, who: str, facts: list, linked_uid: int | None, stamp: str | None = None) -> bool:
    """Мини-память третьего лица: имя, кто это, чьи (links — uid контактов, в чьих чатах упомянут), факты с датой."""
    name = str(name or "").strip()[:60]
    if len(name) < 2:
        return False
    stamp = stamp or _date.today().strftime("%d.%m.%y")
    await ensure_persons(db)
    n = _norm_name(name)
    async with db.execute("SELECT facts, links, who, kind FROM persons WHERE norm_name=?", (n,)) as cur:
        row = await cur.fetchone()
    old_facts = split_entries(row[0]) if row else []
    known = {norm_entry(f) for f in old_facts}
    for f in facts or []:
        f = strip_label_echo(str(f or ""))
        if len(norm_entry(f)) >= 3 and norm_entry(f) not in known:
            old_facts.append(date_prefix(f, stamp)); known.add(norm_entry(f))
    links = set()
    try:
        links = set(json.loads(row[1]) if row and row[1] else [])
    except Exception:
        pass
    if linked_uid:
        links.add(int(linked_uid))
    who = (str(who or "").strip()[:120]) or (row[2] if row else "")
    kind = (str(kind or "").strip()[:20]) or (row[3] if row else "человек")
    if row:
        await db.execute("UPDATE persons SET name=?, kind=?, who=?, facts=?, links=?, updated=? WHERE norm_name=?",
                         (name, kind, who, join_entries(old_facts), json.dumps(sorted(links)), time.time(), n))
    else:
        await db.execute("INSERT INTO persons(norm_name, name, kind, who, facts, links, created, updated) VALUES (?,?,?,?,?,?,?,?)",
                         (n, name, kind, who, join_entries(old_facts), json.dumps(sorted(links)), time.time(), time.time()))
    await db.commit()
    return True


async def persons_for_text(db, text: str, limit: int = 3) -> list[dict]:
    """Третьи лица, упомянутые в тексте (по стему имени ≥3 симв.)."""
    try:
        await ensure_persons(db)
        low = (text or "").lower()
        async with db.execute("SELECT name, kind, who, facts, links FROM persons") as cur:
            rows = await cur.fetchall()
    except Exception:
        return []
    out = []
    for name, kind, who, facts, links in rows:
        first = str(name).split()[0].lower()
        if len(first) < 3:
            continue
        stem = first[:4] if len(first) >= 5 else first[:3]
        if re.search(r"\b" + re.escape(stem) + r"\w{0,3}\b", low):
            out.append({"name": name, "kind": kind, "who": who, "facts": split_entries(facts), "links": json.loads(links or "[]")})
        if len(out) >= limit:
            break
    return out


async def persons_linked(db, uid: int, limit: int = 12) -> list[dict]:
    try:
        await ensure_persons(db)
        async with db.execute("SELECT name, kind, who, facts, links FROM persons WHERE links LIKE ? ORDER BY updated DESC LIMIT ?",
                              (f"%{int(uid)}%", limit)) as cur:
            rows = await cur.fetchall()
    except Exception:
        return []
    return [{"name": r[0], "kind": r[1], "who": r[2], "facts": split_entries(r[3])} for r in rows
            if int(uid) in (json.loads(r[4] or "[]"))]


def person_brief(p: dict, max_facts: int = 4) -> str:
    icon = "🐾" if (p.get("kind") or "").startswith("пит") else "👤"
    facts = p.get("facts") or []
    return f"{icon} {p['name']} — {p.get('who') or '?'}" + (": " + "; ".join(facts[-max_facts:]) if facts else "")


# ── Память 3.0/C: ритм общения из БД (без модели) ────────────────────────────
async def rhythm(db, uid: int, days: int = 90) -> dict:
    """{msgs30, in_share, top_hours, reply_min (медиана ответа владельца), initiator (доля дней, когда начал контакт),
    pauses7 (пауз >7 дн.), last} — по таблице conversations за N дней (время в БД — UTC, часы показываем МСК)."""
    out = {}
    try:
        async with db.execute("SELECT direction, created_at FROM conversations WHERE user_id=? AND created_at > datetime('now', ?) ORDER BY id",
                              (uid, f"-{days} days")) as cur:
            rows = [(r[0], str(r[1])) for r in await cur.fetchall()]
    except Exception:
        return out
    if not rows:
        return out
    from datetime import datetime, timedelta
    def _dt(s):
        try:
            return datetime.fromisoformat(s.replace(" ", "T")[:19]) + timedelta(hours=3)
        except Exception:
            return None
    pts = [(d, _dt(t)) for d, t in rows if _dt(t)]
    if not pts:
        return out
    n_in = sum(1 for d, _ in pts if d == "in")
    out["msgs"] = len(pts); out["in_share"] = round(n_in / len(pts), 2)
    cutoff30 = pts[-1][1] - timedelta(days=30)
    out["msgs30"] = sum(1 for _, t in pts if t >= cutoff30)
    hours = {}
    for d, t in pts:
        if d == "in":
            hours[t.hour] = hours.get(t.hour, 0) + 1
    if hours:
        top = sorted(hours.items(), key=lambda x: -x[1])[:3]
        out["top_hours"] = sorted(h for h, _ in top)
    # медиана времени ответа владельца на входящее (в пределах суток)
    waits = []
    last_in = None
    for d, t in pts:
        if d == "in":
            last_in = last_in or t
        elif last_in is not None:
            delta = (t - last_in).total_seconds() / 60
            if 0 <= delta <= 1440:
                waits.append(delta)
            last_in = None
    if waits:
        waits.sort(); out["reply_min"] = round(waits[len(waits) // 2])
    days_first = {}
    for d, t in pts:
        days_first.setdefault(t.date(), d)
    if days_first:
        out["initiator"] = round(sum(1 for d in days_first.values() if d == "out") / len(days_first), 2)
    pauses = 0
    for (_, a), (_, b) in zip(pts, pts[1:]):
        if (b - a).days >= 7:
            pauses += 1
    out["pauses7"] = pauses
    out["last"] = pts[-1][1].strftime("%d.%m %H:%M")
    return out


def rhythm_text(r: dict) -> str:
    if not r:
        return ""
    parts = [f"{r.get('msgs30', 0)} сообщ. за 30 дн."]
    if r.get("top_hours"):
        parts.append("пишет обычно в " + "/".join(f"{h:02d}" for h in r["top_hours"]) + " ч")
    if r.get("reply_min") is not None:
        parts.append(f"ты отвечаешь за ~{r['reply_min']} мин")
    if r.get("initiator") is not None:
        parts.append(f"инициатор ты в {int(r['initiator'] * 100)}% дней")
    if r.get("pauses7"):
        parts.append(f"пауз >7 дн.: {r['pauses7']}")
    return "; ".join(parts)


# ── v4.65: выборка памяти для промпта обновления + защита от дублей по смыслу ──────────────────
# Память хранится и показывается ЦЕЛИКОМ. Меняется только то, что уходит модели при каждом обновлении:
# раньше — вся память (~58 тыс. токенов к октябрю 2026, 60% счёта бота), теперь — «ключевое» целиком,
# записи, близкие по смыслу к новой переписке, свежая хронология и счётчики разделов. Полная проверка
# всей памяти — еженедельной сборкой (см. digest). Дубль, который модель не заметила, ловит код.

SLICE_FULL_LIMIT = 40_000   # символов: память короче — отдаём целиком (тест 13.10: выборка проверена на память ~170 тыс.)


SLICE_RECENT = 10   # v4.67: последние записи каждого раздела — всегда в выборке


def _slice_text(d: dict, hits: list, k_tl: int = 8, k_recent: int = SLICE_RECENT) -> str:
    """v4.67: кроме близких по смыслу — последние k_recent записей каждого раздела (merge дописывает в конец).
    Проверка 25.10: общие наблюдения («в ссоре пишет короткими сообщениями») далеки по смыслу от конкретной
    переписки, в выборку не попадали — и модель записала одно и то же 14 раз за 12 дней."""
    secs = d.get("sections") or {}
    by_sec: dict = {}
    for sec, entry, _ in hits:
        if sec != "timeline":
            by_sec.setdefault(sec, []).append(entry)
    out = ["(Показана часть памяти: последние записи каждого раздела и записи, связанные с новой перепиской; "
           "остальное хранится. Не повторяй известное, даже другими словами — добавляй только новое. "
           "Устаревшим помечай только то, что видишь здесь.)"]
    for k, label in SECTION_LABELS:
        items = split_entries(secs.get(k))
        if not items:
            continue
        if k == "key":
            out.append(f"[{label}] " + "; ".join(items))
            continue
        recent = items[-k_recent:] if k_recent > 0 else []   # [-0:] дал бы весь раздел
        want = {norm_entry(x) for x in by_sec.get(k, [])} | {norm_entry(x) for x in recent}
        shown = [e for e in items if norm_entry(e) in want]
        rest = len(items) - len(shown)
        tail = f" (+ ещё {rest} записей раньше, не показаны)" if rest else ""
        out.append(f"[{label}] " + "; ".join(shown) + tail)
    tl = [it for it in (_norm_tl(t) for t in (d.get("timeline") or [])) if it]
    if tl:
        out.append("[ХРОНОЛОГИЯ, последние] " + "; ".join(f"{it['date']} {it['event']}".strip() for it in tl[-k_tl:])
                   + (f" (+ ещё {len(tl) - k_tl} событий раньше)" if len(tl) > k_tl else ""))
    if d.get("style"):
        out.append("[СТИЛЬ] " + str(d.get("style")).strip())
    return "\n".join(out)


def _query_chunks(text: str, size: int = 500, limit: int = 8) -> list:
    """Переписку — на куски по ~size символов (с конца, свежее важнее): каждый ищется отдельно."""
    lines = [ln for ln in str(text or "").splitlines() if ln.strip()]
    chunks, cur = [], ""
    for ln in reversed(lines):
        if cur and len(cur) + len(ln) > size:
            chunks.append(cur)
            if len(chunks) >= limit:
                return chunks
            cur = ""
        cur = (ln + "\n" + cur) if cur else ln
    if cur:
        chunks.append(cur)
    return chunks[:limit]


async def prompt_slice(db, profile: dict, query: str, k: int = 40) -> str | None:
    """Выборка памяти для промпта обновления. None — отдать память целиком (короткое, нет индекса, сбой)."""
    raw = (profile or {}).get("facts_json")
    if not raw:
        return None
    try:
        d = json.loads(raw)
        if len(full_text(profile)) < SLICE_FULL_LIMIT:
            return None
        uid = profile.get("user_id") or 0
        best: dict = {}
        for ch in _query_chunks(query):
            for sec, entry, sc in await retrieve(db, uid, ch, k=12, min_score=0.30):
                key = (sec, entry)
                best[key] = max(best.get(key, 0), sc)
        if not best:
            return None
        hits = [(sec, e, sc) for (sec, e), sc in sorted(best.items(), key=lambda x: -x[1])[:k]]
        return _slice_text(d, hits)
    except Exception as exc:
        logger.debug(f"prompt_slice skip: {exc}")
        return None


async def drop_near_duplicates(db, user_id: int, add: dict | None, threshold: float = 0.92) -> tuple[dict | None, int]:
    """Повторы по смыслу (модель видела не вся память) — локальные эмбеддинги, бесплатно.
    Повтор в ТОЙ ЖЕ секции → подставляем текст существующей записи: merge отметит её «подтверждено»
    и допишет источники (как при дословном повторе). Повтор из другой секции → отбрасываем."""
    if not add:
        return add, 0
    try:
        from core.embeddings import embed, unpack, _cosine
        await _ensure_table(db)
        async with db.execute("SELECT section, entry, vector FROM contact_memory_vectors WHERE user_id=? AND section!='timeline'",
                              (user_id,)) as cur:
            known = [(r[0], r[1], unpack(r[2])) for r in await cur.fetchall()]
        if not known:
            return add, 0
        hits, kept = 0, {}
        for sec, items in add.items():
            keep = []
            for it in items if isinstance(items, list) else [items]:
                text, *_ = _entry_parts(it)
                vec = await embed(f"{sec}: {text}"[:600]) if text else None
                if vec is not None:
                    sc, ksec, kentry = max(((_cosine(vec, kv), ks, ke) for ks, ke, kv in known), default=(0, "", ""))
                    if sc >= threshold:
                        hits += 1
                        if ksec == sec:
                            keep.append(dict(it, t=kentry) if isinstance(it, dict) else kentry)
                        continue
                    known.append((sec, text, vec))   # и повторы внутри одного ответа
                keep.append(it)
            if keep:
                kept[sec] = keep
        return kept, hits
    except Exception as exc:
        logger.debug(f"near-dup skip: {exc}")
        return add, 0
