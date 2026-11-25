"""
v4.37 Роль нового контакта: «бот говорит — ты киваешь».
Карточка «Новый контакт» показывает ДОГАДКУ (категория + роль из твоего же CRM) и кнопки
[✓ Верно] [✏️ Поправить] [🚫 Спам]. «Поправить» → 3 самые частые роли той же категории из
contact_profiles + [👥 Друг] + [✏️ Своя роль…] (текстом). Подтверждённая роль ЗАМОРАЖИВАЕТСЯ
(rolelock) — энрич её больше не переписывает.
"""
import json
import time
from typing import Optional

from loguru import logger

from core import llm
from core.usage import record as _rec
from core.util import loads_loose
from core.llm import text_of as _llm_text   # v4.61: читать ответ по типу блока (Claude 5 может начать с thinking)

ROLE_SYSTEM = """Ты определяешь РОЛЬ нового контакта владельца по его первому сообщению и описанию профиля.
Роль — 1-3 слова, кем человек приходится владельцу: «HR / рекрутер», «клиент», «преподаватель английского»,
«одногруппник», «подписчик», «мама», «менеджер по рекламе». Если среди СУЩЕСТВУЮЩИХ ролей есть подходящая —
возьми ТУ ЖЕ формулировку (это важно для единства CRM). Категория: BUSINESS (работа/учёба/сервисы),
FRIEND (личное, семья, друзья, аудитория), UNCLEAR (непонятно). Отвечай строго JSON без markdown:
{"role": "…", "category": "BUSINESS|FRIEND|UNCLEAR", "confidence": 0.0-1.0}"""

_CAT_LABEL = {"BUSINESS": "💼 работа", "FRIEND": "🙂 личное", "UNCLEAR": "❓ непонятно", "SPAM": "🚫 спам"}


def cat_label(cat: str) -> str:
    return _CAT_LABEL.get(cat or "", "📋")


def pick_roles(rows: list[tuple], category: str, limit: int = 3) -> list[str]:
    """Чистая: [(role, count), …] по убыванию → до limit ролей, подходящих категории (или любые для UNCLEAR)."""
    from core.actions import _category_from_relationship
    exact, neutral = [], []
    for role, _cnt in rows:
        r = (role or "").strip()
        if not r or r.lower() in ("unknown", "неизвестно", "—", "спамер", "владелец"):
            continue
        rc = _category_from_relationship(r)
        if rc == "SPAM":
            continue
        if category in ("BUSINESS", "FRIEND"):
            if rc == category and r not in exact:
                exact.append(r)
            elif rc is None and r not in neutral:
                neutral.append(r)      # нейтральные («сосед») — только если точных не хватило
        elif r not in exact:
            exact.append(r)
    return (exact + neutral)[:limit]


async def top_roles(db, category: str, limit: int = 3) -> list[str]:
    async with db.execute(
            "SELECT relationship, COUNT(*) c FROM contact_profiles "
            "WHERE relationship IS NOT NULL AND relationship != '' GROUP BY relationship ORDER BY c DESC LIMIT 40") as cur:
        rows = [(r[0], r[1]) for r in await cur.fetchall()]
    return pick_roles(rows, category, limit)


async def guess_role(client, model: str, db, text: str, category: str, display_name: str,
                     bio: str = "") -> Optional[dict]:
    """Haiku: роль+категория по первому сообщению (+bio профиля). None при сбое."""
    try:
        existing = []
        try:
            from db import get_distinct_roles
            existing = [r for r in (await get_distinct_roles(db) or []) if r][:30]
        except Exception:
            pass
        prompt = (f"Имя: {display_name or '—'}\nОписание профиля (bio): {bio or '—'}\n"
                  f"Категория по классификатору: {category}\nПервое сообщение:\n«{(text or '')[:600]}»\n\n"
                  f"Существующие роли в CRM: {', '.join(existing) or '—'}")
        resp = await llm.create(client, model=model, max_tokens=80, system=ROLE_SYSTEM,
                                messages=[{"role": "user", "content": prompt}])
        await _rec(model, resp.usage, "role_guess")
        data = loads_loose(_llm_text(resp)) or {}
        role = str(data.get("role") or "").strip()
        role = role.split(" или ")[0].split(" / ")[0].strip(" .«»")[:60]   # «рекрутер или HR-специалист» → «рекрутер»
        cat = str(data.get("category") or category or "UNCLEAR").upper()
        if cat not in ("BUSINESS", "FRIEND", "UNCLEAR"):
            cat = category or "UNCLEAR"
        return {"role": role, "category": cat} if role else None
    except Exception as exc:
        logger.warning(f"role guess failed: {type(exc).__name__}: {exc}")
        return None


async def remember_guess(db, user_id: int, guess: dict) -> None:
    from db import set_setting
    await set_setting(db, f"roleg:{user_id}", json.dumps({**guess, "ts": int(time.time())}, ensure_ascii=False))


async def load_guess(db, user_id: int) -> dict:
    from db import get_setting
    try:
        return json.loads(await get_setting(db, f"roleg:{user_id}") or "{}")
    except Exception:
        return {}


async def apply_role(db, user_id: int, role: str) -> str:
    """Записать роль в памяти, заморозить от энрича, друг → whitelist. Возвращает категорию."""
    from core.actions import _category_from_relationship
    from db import get_contact_profile, set_setting, upsert_contact_profile
    prof = await get_contact_profile(db, user_id) or {}
    uname = prof.get("username") or f"id{user_id}"
    role = role.strip()[:100]
    await upsert_contact_profile(db, user_id, uname, increment=False, relationship=role)
    await set_setting(db, f"rolelock:{user_id}", role)
    cat = _category_from_relationship(role) or "UNCLEAR"
    if cat == "FRIEND":
        from core import whitelist
        await whitelist.add(db, user_id=user_id, username=uname, reason="role")
    logger.info(f"Role set by owner: @{uname} → {role!r} ({cat})")
    return cat
