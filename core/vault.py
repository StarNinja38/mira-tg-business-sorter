"""
Vault — permanent contact memory.
After each interaction, LLM enriches the contact profile:
who they are, why they write, what topics they discuss.
"""

import json
import re
from core import llm
from datetime import date
from core.util import today_local_iso, notion_clamp, clean_name, loads_loose
from typing import Optional

import aiohttp
from anthropic import AsyncAnthropic
from loguru import logger

from core.usage import record as _rec
from core.llm import text_of as _llm_text   # v4.61: читать ответ по типу блока (Claude 5 может начать с thinking)

PROFILE_SYSTEM = """Ты ведёшь память о контактах для владельца (стример/контент-мейкер).
Понять: кто человек, кем приходится владельцу, зачем пишет, о чём общается.

ПРАВИЛА:
- РОДСТВО/РОЛЬ бери из ФАКТОВ переписки, НЕ выдумывай. Если владелец обращается «пап/папа/мам/бро/сестрёнка» или прямо называет роль («мой препод», «начальник») — зафиксируй это точно в facts (напр. «Папа владельца», «Преподаватель английского»). Если родство НЕ названо — НЕ пиши «друг семьи»/«родственник» наугад, пиши нейтрально («близкий человек»).
- name — настоящее имя СОБЕСЕДНИКА (@username), НЕ владельца. Бери его ТОЛЬКО если: собеседник сам представился, ИЛИ ВЛАДЕЛЕЦ (Ты) обращается к нему по имени. ⚠️ Если имя звучит в сообщении ОТ собеседника как обращение к владельцу («Миш, привет», «Кать, ты тут?») — это имя ВЛАДЕЛЬЦА, НЕ собеседника, его НЕ бери. Имя владельца НИКОГДА не попадает в name контакта. Имя НЕ эмодзи, НЕ ник, НЕ выдумка. Не уверен или имя нигде не звучало — null.
- ВЛАДЕЛЕЦ — это «Ты» в истории, НЕ контакт. Никогда не подставляй имя или @username владельца в поля контакта (name/facts). Ссылаясь на владельца в facts, пиши «владелец» (он — МУЖЧИНА, всегда мужской род, НИКОГДА «владелица»/«она»), а не его имя/ник/username. Контакт часто обращается к владельцу по имени — это имя владельца, не данные контакта.
- Накапливай facts, НО ретайри устаревшее: если новое сообщение ОТМЕНЯЕТ или ПРОТИВОРЕЧИТ старому факту (статус отношений, работа, город, планы) — ЗАМЕНИ или УБЕРИ устаревший факт, не тащи его дальше. Свежее сообщение важнее старого факта. Пример: было «расстаются», затем «решили не расставаться» → факт о расставании УБРАТЬ. НЕ выдумывай деталей, которых нет в переписке.
- ПАМЯТЬ ПО СЕКЦИЯМ — БОГАТО, но КУРИРОВАНО. Веди подробную, но чистую карточку, раскладывая факты по секциям (формат ниже): ключевое, связи/окружение, учёба-работа, характер-стиль, привычки-быт, предпочтения, чувствительное, прочее; а ДАТИРУЕМЫЕ события (договорённости, встречи, вехи, смены статуса) — в timeline. Подробно и конкретно, СОХРАНЯЯ названия/имена/числа/цитаты ДОСЛОВНО (не обобщай специфику в общие слова), НО без воды и повторов — каждый факт клади в СВОЮ секцию, не дублируй между секциями. Фиксируй и чувствительное, если это РЕАЛЬНО видно из переписки (здоровье, вредные привычки) — это приватная память владельца. Единственное ограничение: НЕ выдумывай — только подтверждённое перепиской; домыслы помечай как предположение.
Отвечай строго JSON без markdown."""

PROFILE_USER = """Текущее резюме о @{username}:
{current_summary}

Текущие факты: {current_facts}
Текущие обязательства владельца: {current_commitments}

Уже существующие РОЛИ (узкие; используй подходящую, НЕ плоди синонимы): {known_roles}
Уже существующие ТИПЫ (широкие группы по 2+ человека; объединяй в них): {known_types}

⚠️ Владельца (тебя в истории) зовут: {owner_name}. Если собеседник обращается этим именем («Миш, привет», «Миша, ты тут?») — это обращение К ВЛАДЕЛЬЦУ, это НЕ имя собеседника и НЕ его данные.

История последних сообщений (Ты = владелец, @{username} = собеседник):
{conversation}

Новое сообщение (категория бота: {category}):
{text}

Обнови память о человеке. ПАМЯТЬ — КАК В ПОЛИЦИИ: ПОЛНОЕ, без сокращений, без экономии слов, ничего не «уплотняй» и не обобщай. Каждый новый факт — ОТДЕЛЬНАЯ запись с конкретикой ДОСЛОВНО: имена, суммы, даты, названия (игр, фильмов, мест, брендов, техники), цитаты. Запись — законченная мысль (1–2 предложения), понятная без контекста. Существующие записи НЕ переписывай и НЕ повторяй — верни ТОЛЬКО НОВОЕ (append-only diff):
- add: по секциям — новые записи, которых ещё нет в текущей памяти (дубли/перефразы существующего НЕ добавляй);
- retire: ТОЧНЫЙ текст существующих записей, которые новыми сообщениями ОПРОВЕРГНУТЫ/ОТМЕНЕНЫ (статус, работа, планы) — они уйдут в хронологию как «устарело»; не ретайри то, что просто давно;
- timeline: только НОВЫЕ датируемые события.
Если по сообщениям ничего память-значимого — add верни пустым объектом {{}}. Верни JSON:
{{
  "relationship": "РОЛЬ контакта (УЗКАЯ) — кратко (1-3 слова), своими словами по смыслу переписки. Если из списка существующих ролей подходит — возьми ТУ ЖЕ формулировку, иначе придумай новую краткую.",
  "contact_type": "ТИП контакта (ШИРОКАЯ группа над несколькими ролями) — 1-2 слова; бери подходящий из существующих, иначе новый общий (2+ человек).",
  "topics": ["активные темы общения"],
  "intent": "зачем обычно пишет — одно предложение",
  "summary": "кто это и характер общения — 2-4 предложения (актуальное состояние)",
  "name": "имя САМОГО @{username} (собеседника): если он представился ИЛИ владелец (Ты) звал его по имени. НЕ имя владельца из обращений к нему, НЕ третьи лица. Не уверен — null",
  "gender": "пол собеседника: «м» или «ж» — по имени, окончаниям глаголов в ЕГО сообщениях («сделала», «пошёл»), обращениям владельца; не уверен — null",
  "add": {{
    "key": [{{"t": "КЛЮЧЕВОЕ: кем приходится владельцу, как обращается к нему, чем занимается/известен — суть в 2-4 записях", "c": "факт|предп", "by": "сам|владелец|другой", "src": [номера #сообщений]}}],
    "identity": [{{"t": "ЛИЧНЫЕ ДАННЫЕ: пол, ФИО/прозвище и откуда оно, возраст/дата рождения, знак зодиака если сам упоминал, национальность/язык, семейное положение, внешность (рост, волосы, татуировки, манера одеваться) — если звучало", "c": "факт|предп", "by": "сам|владелец|другой", "src": [номера #сообщений]}}],
    "history": [{{"t": "ИСТОРИЯ ОТНОШЕНИЙ С ВЛАДЕЛЬЦЕМ: как и когда познакомились, ключевые эпизоды, ссоры/примирения, что делали вместе, как менялась близость", "c": "факт|предп", "by": "сам|владелец|другой", "src": [номера #сообщений]}}],
    "relations": [{{"t": "СВЯЗИ И ОКРУЖЕНИЕ: конкретные люди вокруг (ИМЕНА), кто кому приходится, лучший друг, враги/конфликты, общие с владельцем знакомые", "c": "факт|предп", "by": "сам|владелец|другой", "src": [номера #сообщений]}}],
    "personal": [{{"t": "ЛИЧНАЯ ЖИЗНЬ: отношения (с кем, статус), бывшие, дети, родители и отношения с ними, домашние животные", "c": "факт|предп", "by": "сам|владелец|другой", "src": [номера #сообщений]}}],
    "study_work": [{{"t": "ОБРАЗОВАНИЕ И РАБОТА: где учился/учится, специальность, где и кем работает, нравится ли, проекты, бизнес, доход если звучал, дедлайны, навыки и способности, иностранные языки и уровень", "c": "факт|предп", "by": "сам|владелец|другой", "src": [номера #сообщений]}}],
    "home": [{{"t": "МЕСТО ЖИТЕЛЬСТВА: страна/город/район, жильё (своё/съём/с родителями), с кем живёт, переезды и почему", "c": "факт|предп", "by": "сам|владелец|другой", "src": [номера #сообщений]}}],
    "property": [{{"t": "ИМУЩЕСТВО И ТРАНСПОРТ: машина (марка, номер), техника, собственность, на чём передвигается", "c": "факт|предп", "by": "сам|владелец|другой", "src": [номера #сообщений]}}],
    "contacts": [{{"t": "КОНТАКТЫ И ИНТЕРНЕТ: телефоны, e-mail, адреса, ники и аккаунты в соцсетях/играх, каналы, где активен онлайн", "c": "факт|предп", "by": "сам|владелец|другой", "src": [номера #сообщений]}}],
    "money": [{{"t": "ДЕНЬГИ: кто кому сколько должен, оплаты, переводы, цены, подарки, на что живёт — с суммами и датами", "c": "факт|предп", "by": "сам|владелец|другой", "src": [номера #сообщений]}}],
    "plans": [{{"t": "ПЛАНЫ И ДОГОВОРЁННОСТИ: что собирается делать, поездки, встречи, о чём договорились с владельцем — с датами", "c": "факт|предп", "by": "сам|владелец|другой", "src": [номера #сообщений]}}],
    "character": [{{"t": "ХАРАКТЕР И ОБЩЕНИЕ: темперамент, черты, как реагирует, что ценит в людях, тон, сленг, типичные фразы, эмодзи, длина сообщений", "c": "факт|предп", "by": "сам|владелец|другой", "src": [номера #сообщений]}}],
    "beliefs": [{{"t": "УБЕЖДЕНИЯ, ЦЕЛИ И МЕЧТЫ: цели в жизни, мечты, что мотивирует, кому доверяет/кто авторитет, откуда берёт информацию, отношение к религии/политике/деньгам — если звучало", "c": "факт|предп", "by": "сам|владелец|другой", "src": [номера #сообщений]}}],
    "habits": [{{"t": "ПРИВЫЧКИ, БЫТ И ХОББИ: распорядок (сова/жаворонок), еда, спорт, отдых, игры/книги/кино, как проводит время, порядок/бардак", "c": "факт|предп", "by": "сам|владелец|другой", "src": [номера #сообщений]}}],
    "prefs": [{{"t": "ПРЕДПОЧТЕНИЯ И ВКУСЫ: что любит/не любит — конкретные игры, фильмы, музыка, бренды, места, еда; идеи для подарков", "c": "факт|предп", "by": "сам|владелец|другой", "src": [номера #сообщений]}}],
    "sensitive": [{{"t": "ЧУВСТВИТЕЛЬНОЕ: здоровье, болезни, вредные привычки, личные трудности, страхи — ЕСЛИ реально видно", "c": "факт|предп", "by": "сам|владелец|другой", "src": [номера #сообщений]}}],
    "other": [{{"t": "ПРОЧЕЕ важное, не вошедшее выше", "c": "факт|предп", "by": "сам|владелец|другой", "src": [номера #сообщений]}}]
  }},
  "retire": ["точный текст устаревшей записи из текущей памяти"],
  "timeline": [
    {{"date": "MM-DD или YYYY-MM-DD", "event": "датируемое событие: договорённость/встреча/веха/смена статуса"}}
  ],
  "people": [
    {{"name": "имя третьего лица или питомца, упомянутого в переписке (НЕ владелец, НЕ сам собеседник)", "kind": "человек|питомец", "who": "кто это по отношению к собеседнику/владельцу (мама собеседника, собака, коллега)", "facts": ["новые факты о нём, кратко"]}}
  ],
  "mentions": [
    {{"contact": "имя/@username ДРУГОГО контакта владельца (не собеседника), о котором тут прозвучал факт", "fact": "сам факт, кратко"}}
  ],
  "commitments": "ТОЛЬКО АКТУАЛЬНЫЕ, ещё НЕ выполненные обязательства ВЛАДЕЛЬЦА перед этим человеком через ; — только ЯВНОЕ конкретное действие, которое владелец прямо обещал и пока не сделал. Пусто, если нет"
}}"""

# v4.47: метки секций — в core/contact_memory.py (единый источник для промпта, /about и vault-карточки)
from core import contact_memory as _contact_memory
_SECTION_LABELS = _contact_memory.SECTION_LABELS


def _clip(s, n: int) -> str:
    """Обрезка по границе СЛОВА, не посреди него (v4.3 fix «обрезаются слова»).
    Если в пределах лимита есть пробел близко к концу — режем по нему; очень длинный
    единый токен (нет пробела) режем жёстко. Хвостовую пунктуацию/тире убираем."""
    s = str(s or "").strip()
    if len(s) <= n:
        return s
    cut = s[:n]
    sp = cut.rfind(" ")
    if sp > 0 and sp >= n - 80:   # есть пробел близко к лимиту → режем по слову (sp=-1 → жёстко)
        cut = cut[:sp]
    return cut.rstrip(" ;,.—-")


def _clip_section(s, n: int) -> str:
    """v4.32: обрезка СЕКЦИИ память по границе ЗАПИСИ («; »), не слова — иначе хвост
    оставался оборванной мыслью («…и её нельзя»). Нет «; » близко к лимиту → по слову."""
    s = str(s or "").strip()
    if len(s) <= n:
        return s
    cut = s[:n]
    sep = cut.rfind("; ")
    if sep > 0 and sep >= n - 300:   # есть конец записи в пределах хвоста → режем по записи
        return cut[:sep].rstrip(" ;,.—-")
    return _clip(s, n)


def _norm_tl_item(t):
    """Нормализовать элемент хронологии в чистый {date,event}. str()-обёртка (fix #9): не-строковый
    event из ручной правки БД / старой схемы больше не роняет `.strip()` → весь enrich-проход."""
    if not isinstance(t, dict):
        return None
    ev = _clip(t.get("event") or "", 300)   # было [:200] по символу → рубило события на полуслове
    if not ev:
        return None
    return {"date": str(t.get("date") or "").strip()[:16], "event": ev}


def _flatten_struct(sections: dict, timeline: list) -> str:
    """Плоское зеркало структуры (обратная совместимость: current_facts, черновики, /me-fallback, дедуп)."""
    return _contact_memory.flatten(sections, timeline)


def _merge_struct(old_json, new_sections: dict, new_timeline) -> tuple[dict, list]:
    """v4.47: слияние БЕЗ клипов и БЕЗ стирания — старый формат «секция целиком» трактуется как
    append-only (берём только новые записи). Полная версия с add/retire — contact_memory.merge."""
    sec, tl, _, _ = _contact_memory.merge(old_json, None, None, new_timeline, legacy_sections=new_sections)
    return sec, tl


def _current_struct(profile: dict) -> str:
    """Текущая память для промпта энрича. Есть facts_json → показываем ПО СЕКЦИЯМ (чтобы модель их обновляла
    адресно); иначе плоский facts с пометкой разложить (первый структурный проход мигрирует старую память)."""
    raw = profile.get("facts_json")
    if raw:
        try:
            d = json.loads(raw)
            secs = d.get("sections") or {}
            tl = d.get("timeline") or []
            lines = [f"[{label}] {(secs.get(k) or '').strip()}" for k, label in _SECTION_LABELS if (secs.get(k) or "").strip()]
            if tl:
                lines.append("[ХРОНОЛОГИЯ] " + "; ".join(
                    f"{t.get('date','')} {t.get('event','')}".strip() for t in tl if t.get("event")))
            if d.get("style"):
                lines.append("[СТИЛЬ] " + str(d.get("style")).strip())
            if lines:
                return "\n".join(lines)
        except Exception:
            pass
    return (profile.get("facts") or "—") + "\n(ещё не разложено по секциям — разложи при обновлении)"


async def enrich_profile(
    client: AsyncAnthropic,
    model: str,
    user_id: int,
    username: str,
    text: str,
    category: str,
    conversation_context: str,
    db,
    force: bool = False,
) -> None:
    """Update contact profile with LLM after every Nth message. force=True — обойти каденс (миграция/ручной пересбор)."""
    from db import get_contact_profile, upsert_contact_profile

    profile = await get_contact_profile(db, user_id) or {}
    msg_count = profile.get("message_count", 0)

    # Enrich on 1st & 3rd message, then every Nth — saves API cost.
    # FRIEND болтает много (1500+ сообщений/день у активных), а память друга меняется медленно →
    # каждое 20-е; деловые/непонятные — каждое 10-е (там апдейты ценнее). Enrich — главный
    # Sonnet-расход (выходные токены полной памяти), каденс — основной рычаг его стоимости.
    cadence = 20 if category == "FRIEND" else 10
    should_enrich = force or msg_count in (1, 3) or msg_count % cadence == 0
    if not should_enrich:
        return
    # B1: спам/боты не нуждаются в богатой памяти — классификатор их уже пометил. Энричим ОДИН
    # раз (1-е сообщение) для базовой карточки, дальше Sonnet/Haiku на них не тратим.
    if category == "SPAM" and msg_count > 1 and not force:
        return

    # Стоп-кран бюджета: enrich — самый дорогой Sonnet-вызов (1500-2200 ток.);
    # раньше он шёл МИМО стоп-крана и жёг деньги после превышения. Проверяем только
    # когда реально собираемся обогащать (1/3/10-е сообщение), чтобы не сканировать БД зря.
    from db import is_over_budget
    from config import settings as _s
    if await is_over_budget(db, _s.anthropic_monthly_budget):
        logger.info(f"Profile enrich skipped (over budget) @{username}")
        return

    # B2: тонкие контакты (мало истории / спам / неясные) — на Haiku (дёшево); богатые активные
    # (FRIEND/BUSINESS с историей) — Sonnet (нужно качество судейства секций/ретайра).
    if msg_count < 8 or category in ("SPAM", "UNCLEAR"):
        model = _s.anthropic_model

    current_summary = profile.get("summary") or "Нет данных"
    # Существующие роли — чтобы бот объединял людей в общие роли, а не плодил синонимы
    try:
        from db import get_distinct_roles, get_distinct_types
        roles = await get_distinct_roles(db)
        types = await get_distinct_types(db)
    except Exception:
        roles, types = [], []
    known_roles = ", ".join(roles) if roles else "(пока нет — создай первую)"
    known_types = ", ".join(types) if types else "(пока нет — создай первый общий)"
    # Память 3.0/A: история с НОМЕРАМИ сообщений (#id) — модель ссылается на них в src → у записи есть источник
    try:
        async with db.execute("SELECT id, direction, username AS un, text, media_type, created_at FROM conversations "
                              "WHERE user_id=? ORDER BY id DESC LIMIT 14", (user_id,)) as _cur:
            _rows = await _cur.fetchall()
        _num = []
        for _r in reversed(_rows):
            _who = "Ты" if _r[1] == "out" else f"@{_r[2] or username}"
            _body = _r[3] or ("[" + (_r[4] or "медиа") + "]")
            _num.append(f"[#{_r[0]} {str(_r[5])[5:16]}] {_who}: {_body[:400]}")
        _conv_numbered = "\n".join(_num) or (conversation_context or "Нет истории")
    except Exception:
        _conv_numbered = conversation_context or "Нет истории"
    # v4.65: модели — выборка памяти по смыслу новой переписки (память хранится целиком; полная проверка — раз в неделю)
    _facts_for_prompt = None
    if getattr(_s, "contact_memory_slice", False):
        _facts_for_prompt = await _contact_memory.prompt_slice(db, dict(profile, user_id=user_id), _conv_numbered)
    prompt = PROFILE_USER.format(
        username=username,
        owner_name=_s.owner_name or "владелец",
        current_summary=current_summary,
        current_facts=_facts_for_prompt or _current_struct(profile),
        current_commitments=profile.get("commitments") or "—",
        known_roles=known_roles,
        known_types=known_types,
        conversation=_conv_numbered,
        category=category,
        text=text[:1000],
    )

    try:
        # До 2 попыток: если модель вернула невалидный JSON (неэкранированная кавычка/перенос
        # в памяти) — переспрашиваем со строгой инструкцией. Иначе профиль молча не обновлялся.
        _STRICT = ("\n\nКРИТИЧНО: верни СТРОГО валидный JSON. Экранируй двойные кавычки внутри "
                   "значений как \\\" и не вставляй сырые переводы строк внутри строк.")
        data = None
        last_err = None
        for attempt in range(2):
            # 1500 хватает подробной памяти (Nika ~916 ток.); 700 ОБРЕЗАЛ JSON → не парсился.
            # На ретрае поднимаем лимит ещё + просим строго экранировать (на случай длинных/кавычных).
            # PROFILE_SYSTEM кэшируем (1h): он ~постоянный и большой, переменная часть — current_*/история.
            # На ретрае добавляем _STRICT отдельным блоком (кэш базовой части не ломается).
            _sys = [{"type": "text", "text": PROFILE_SYSTEM,
                     "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
            if attempt:
                _sys.append({"type": "text", "text": _STRICT})
            response = await llm.create(client, 
                model=model, max_tokens=(4096 if attempt == 0 else 6000),
                system=_sys,
                messages=[{"role": "user", "content": prompt}],
            )
            await _rec(model, response.usage, "enrich")
            try:
                data = loads_loose(_llm_text(response))
                break
            except Exception as pe:
                last_err = pe
                logger.debug(f"enrich JSON parse failed (attempt {attempt+1}) for @{username}: {pe}")
        if data is None:
            raise last_err or ValueError("enrich: no JSON")

        # разбор мог снова вычитать закрытое из старых сообщений: такие обещания не возвращаем
        from db import get_recently_closed_tasks, without_done
        _closed = [t["title"] for t in await get_recently_closed_tasks(db, user_id, days=30)]
        data["commitments"] = without_done(data.get("commitments") or "", _closed)
        # commitments можно очищать; facts/topics — НЕ затирать пустым
        # (кривой/обрезанный enrich-ответ с facts:"" иначе стирал бы накопленная память).
        fields = dict(commitments=_clip(data.get("commitments") or "", 800))
        _topics = data.get("topics")
        if isinstance(_topics, list) and _topics:
            fields["topics"] = json.dumps(_topics, ensure_ascii=False)
        # Структурная память, ДИФФ-режим (P1.1 плана 07/2026, −$7/мес на output):
        # модель возвращает ТОЛЬКО изменённые секции и НОВЫЕ события; сливаем со старым facts_json.
        # Пропущенная секция = без изменений (сохраняем старую); пустая строка = игнор (защита от
        # ленивого затирания — ретайр устаревшего происходит через переписанную секцию целиком).
        # v4.47 «Память 2.0»: append-only diff — add/retire/timeline; старый формат sections — как add (без стирания)
        _add = data.get("add") if isinstance(data.get("add"), dict) else None
        _retire = data.get("retire") if isinstance(data.get("retire"), list) else None
        _sections = data.get("sections") if isinstance(data.get("sections"), dict) else None
        _timeline = data.get("timeline")
        if _add:   # v4.65: дубли по смыслу (модель видела не вся память) — отсекает код
            _add, _dups = await _contact_memory.drop_near_duplicates(db, user_id, _add)
            if _dups:
                logger.info(f"ContactMemory @{username}: повторов по смыслу: {_dups} (свёрнуты в существующие)")
        if _add or _retire or _sections or _timeline:
            _had_json = bool(profile.get("facts_json"))
            sec, tl, _changed, _meta = _contact_memory.merge(profile.get("facts_json"), _add, _retire, _timeline, legacy_sections=_sections)
            _g = str(data.get("gender") or "").strip().lower()[:1]
            fields["facts_json"] = _contact_memory.compose_facts_json(sec, tl, _meta, profile.get("facts_json"),
                                                               gender=(_g if _g in ("м", "ж") else None))   # gender/style наследуются
            # Память 3.0/C: день рождения из «личных данных» → хранилище дней рождения (сводка/поздравления)
            try:
                from core.dates import norm_bdate, save_bday
                for _raw in (_add or {}).get("identity") or []:
                    _t = _raw.get("t") if isinstance(_raw, dict) else str(_raw)
                    _m = re.search(r"(?:день рождения|др|родил[ае]сь|дата рождения)[^\d]{0,20}(\d{1,2}[.\s](?:\d{1,2}|[а-яё]+)(?:[.\s]\d{4})?)", (_t or "").lower())
                    if _m:
                        _mmdd, _y = norm_bdate(_m.group(1).replace(" ", ".") if _m.group(1)[2:3] == "." else _m.group(1))
                        if _mmdd:
                            await save_bday(db, profile.get("name") or f"@{username}", _mmdd, year=_y, uid=user_id)
                            logger.info(f"Birthday from contact_memory @{username}: {_mmdd}")
            except Exception as _be:
                logger.debug(f"bday from contact_memory skip: {_be}")
            _new_flat = _flatten_struct(sec, tl)
            if _had_json or len(_new_flat) >= len(profile.get("facts") or "") * 0.7:
                fields["facts"] = _new_flat
            logger.info(f"ContactMemory @{username}: +{sum(len(v) if isinstance(v, list) else 1 for v in (_add or {}).values())} записей, "
                        f"retire={len(_retire or [])}, изменены секции {_changed}")
        elif (data.get("facts") or "").strip():   # фолбэк: модель вернула старый плоский формат
            fields["facts"] = _clip(data["facts"], _contact_memory.FACTS_CAP)
        # Память 3.0/B: третьи лица (Веста, Гриша, мама…) → persons; факты про ДРУГИХ контактов → в их память «со слов»
        try:
            _stamp = __import__("datetime").date.today().strftime("%d.%m.%y")
            for _pp in (data.get("people") or [])[:6]:
                if isinstance(_pp, dict) and _pp.get("name"):
                    await _contact_memory.upsert_person(db, _pp["name"], _pp.get("kind") or "человек", _pp.get("who") or "",
                                                 [str(x) for x in (_pp.get("facts") or []) if x][:6], user_id, _stamp)
            _ment = [m for m in (data.get("mentions") or []) if isinstance(m, dict) and m.get("contact") and m.get("fact")][:5]
            if _ment:
                from core.mira import mentioned_contacts as _mc
                async with db.execute("SELECT user_id, username, name, relationship, '' FROM contact_profiles WHERE user_id NOT IN (?, ?)",
                                      (user_id, _s.owner_user_id)) as _cur:
                    _rows = [tuple(r) for r in await _cur.fetchall()]
                for _m in _ment:
                    _hit = _mc(str(_m["contact"]), _rows)
                    if _hit:
                        await remember_facts(db, _hit[0][0], [f"{_m['fact']} (со слов @{username})"], by="другой")
                        logger.info(f"Cross-mention @{username} → @{_hit[0][1]}: {str(_m['fact'])[:60]}")
        except Exception as _pe:
            logger.debug(f"people/mentions skip @{username}: {_pe}")
        # Роль/Тип/имя/intent/summary НЕ затираем пустым, если модель не вернула поле
        # (иначе хорошая роль «папа» спорадически превращается в «unknown» на кривом enrich).
        if (data.get("relationship") or "").strip():
            from db import get_setting as _gs
            if not await _gs(db, f"rolelock:{user_id}"):   # v4.37: роль, подтверждённую владельцем кнопкой, не переписываем
                fields["relationship"] = data["relationship"].strip()[:100]
        if (data.get("contact_type") or "").strip():          # Тип — широкая группа (бот сам, 2+)
            fields["contact_type"] = data["contact_type"].strip()[:60]
        if (data.get("intent") or "").strip():
            fields["intent"] = data["intent"].strip()
        if (data.get("summary") or "").strip():
            fields["summary"] = data["summary"].strip()
        if data.get("name"):                                  # не затираем имя при null
            fields["name"] = data["name"]
        # Per-user lock: сериализует запись контакта + reconcile обещаний + sync в Notion
        # одного пользователя между параллельными enrich (fire-and-forget на разных сообщениях)
        # → закрывает дубль Notion-контакта и дубль задачи-обещания при гонке.
        from core.util import user_lock
        async with user_lock(user_id):
            await upsert_contact_profile(db, user_id, username, increment=False, **fields)
            if fields.get("facts_json"):
                await _contact_memory.reindex(db, user_id, fields["facts_json"])   # v4.47: записи → локальный индекс

            # ЕДИНЫЙ ПРИНЦИП: обещание = закрываемая задача. enrich нашёл обещания (широко) →
            # завести задачей-обещанием те, которых ещё нет (закрытие — вручную/auto_close).
            # Так уходит «узкий охват», а «протухание» невозможно (всё закрываемо).
            if category != "SPAM" and (data.get("commitments") or "").strip():
                try:
                    from core.actions import reconcile_commitment_tasks
                    await reconcile_commitment_tasks(db, user_id, username, category, data.get("commitments") or "",
                                                     client=client, model=_s.anthropic_model)   # v4.49: судья дублей (Haiku)
                except Exception as exc:
                    logger.debug(f"reconcile commitments skip @{username}: {exc}")

        logger.info(f"Vault profile updated: @{username} → {data.get('relationship')} | {data.get('intent', '')[:60]}")

    except Exception as exc:
        logger.error(f"Profile enrichment failed for @{username}: {exc}")


OWNER_SYSTEM = """Ты ведёшь память О САМОМ ВЛАДЕЛЬЦЕ бота — по ЕГО ИСХОДЯЩИМ сообщениям разным людям.
Цель: понять владельца как человека — характер, манеру и СТИЛЬ ПИСЬМА, текущий жизненный контекст
(учёба/работа/проекты), ключевые отношения и людей вокруг, привычки, ценности, что его волнует.
ПРАВИЛА:
- ВЛАДЕЛЕЦ — МУЖЧИНА (Михаил). Пиши о нём СТРОГО в мужском роде (он, владелец, готов, рад, сделал, занят). НИКОГДА не используй женский род («владелица», «готова», «сделала», «была») — это всегда ошибка, даже если он много пишет женщинам.
- Это сообщения, которые ПИШЕТ владелец (не ему). Реконструируй портрет по тому, ЧТО и КАК он пишет.
- РОЛИ ПО НАПРАВЛЕНИЮ ДЕЙСТВИЯ: если владелец что-то делает ДЛЯ человека (чинит, помогает, везёт, учит, консультирует) — это НАВЫК/ЗАНЯТИЕ ВЛАДЕЛЬЦА, а человек — получатель/клиент. НЕ приписывай умение человеку. Пример: «владелец чинит плиту Гале» → у ВЛАДЕЛЬЦА навык ремонта техники, Галя — его КЛИЕНТ, а НЕ мастер.
- ПАМЯТЬ ПО СЕКЦИЯМ — БОГАТО, но КУРИРОВАНО: раскладывай по секциям (формат ниже), подробно но без воды и повторов. РЕТАЙРИ устаревшее: факт, отменённый/опровергнутый свежими сообщениями — замени или убери. НЕ выдумывай — только то, что видно из сообщений.
- Датируемые события (вехи, договорённости, смены статуса) — в timeline.
- Стиль письма опиши отдельно в поле style (тон, длина, обращения, эмодзи, манера) — это нужно, чтобы писать черновики его голосом.
Отвечай строго JSON без markdown."""

OWNER_USER = """Имя владельца: {owner_name}.
Текущая память: {current_summary}
Текущие факты: {current_facts}

Недавние ИСХОДЯЩИЕ сообщения владельца (кому → текст):
{messages}

Обнови память о владельце. ПАМЯТЬ ПОЛНОЕ, без сокращений и «уплотнения»; каждый новый факт — отдельная запись с конкретикой ДОСЛОВНО (имена, суммы, даты, названия, места). Существующие записи НЕ переписывай — верни ТОЛЬКО НОВОЕ: add по секциям (без дублей существующего), retire — точный текст опровергнутых записей, timeline — новые датируемые события. style верни только если манера письма реально изменилась. Если нового нет — add: {{}}. Верни JSON:
{{
  "summary": "кто владелец и какой он — 2-4 предложения (актуальное состояние)",
  "add": {{
    "key": [{{"t": "КЛЮЧЕВОЕ: кто он, чем занимается, чем известен — суть в 2-4 записях", "c": "факт|предп", "by": "сам|владелец|другой"}}],
    "identity": [{{"t": "ЛИЧНЫЕ ДАННЫЕ: пол, ФИО/прозвище и откуда оно, возраст/дата рождения, знак зодиака если сам упоминал, национальность/язык, семейное положение, внешность (рост, волосы, татуировки, манера одеваться) — если звучало", "c": "факт|предп", "by": "сам|владелец|другой"}}],
    "history": [{{"t": "ВЕХИ ЖИЗНИ: переезды, поступление, работа/увольнение, начало и этапы отношений, крупные события — как менялось", "c": "факт|предп", "by": "сам|владелец|другой"}}],
    "relations": [{{"t": "СВЯЗИ И ОКРУЖЕНИЕ: конкретные люди вокруг (ИМЕНА), кто кому приходится, лучший друг, враги/конфликты, клиенты, коллеги", "c": "факт|предп", "by": "сам|владелец|другой"}}],
    "personal": [{{"t": "ЛИЧНАЯ ЖИЗНЬ: отношения (с кем, статус), бывшие, дети, родители и отношения с ними, домашние животные", "c": "факт|предп", "by": "сам|владелец|другой"}}],
    "study_work": [{{"t": "ОБРАЗОВАНИЕ И РАБОТА: где учился/учится, специальность, где и кем работает, нравится ли, проекты, бизнес, доход если звучал, дедлайны, навыки и способности, иностранные языки и уровень", "c": "факт|предп", "by": "сам|владелец|другой"}}],
    "home": [{{"t": "МЕСТО ЖИТЕЛЬСТВА: страна/город/район, жильё (своё/съём/с родителями), с кем живёт, переезды и почему", "c": "факт|предп", "by": "сам|владелец|другой"}}],
    "property": [{{"t": "ИМУЩЕСТВО И ТРАНСПОРТ: машина (марка, номер), техника, собственность, на чём передвигается", "c": "факт|предп", "by": "сам|владелец|другой"}}],
    "contacts": [{{"t": "КОНТАКТЫ И ИНТЕРНЕТ: телефоны, e-mail, адреса, ники и аккаунты в соцсетях/играх, каналы, где активен онлайн", "c": "факт|предп", "by": "сам|владелец|другой"}}],
    "money": [{{"t": "ДЕНЬГИ: кто кому сколько должен, оплаты, переводы, цены, подарки, на что живёт — с суммами и датами", "c": "факт|предп", "by": "сам|владелец|другой"}}],
    "plans": [{{"t": "ПЛАНЫ И ДОГОВОРЁННОСТИ: что собирается делать, поездки, встречи, кому что обещал — с датами", "c": "факт|предп", "by": "сам|владелец|другой"}}],
    "character": [{{"t": "ХАРАКТЕР И ОБЩЕНИЕ: темперамент, черты, как реагирует, что ценит в людях, тон, сленг, типичные фразы, эмодзи, длина сообщений", "c": "факт|предп", "by": "сам|владелец|другой"}}],
    "beliefs": [{{"t": "УБЕЖДЕНИЯ, ЦЕЛИ И МЕЧТЫ: цели в жизни, мечты, что мотивирует, кому доверяет/кто авторитет, откуда берёт информацию, отношение к религии/политике/деньгам — если звучало", "c": "факт|предп", "by": "сам|владелец|другой"}}],
    "habits": [{{"t": "ПРИВЫЧКИ, БЫТ И ХОББИ: распорядок (сова/жаворонок), еда, спорт, отдых, игры/книги/кино, как проводит время, порядок/бардак", "c": "факт|предп", "by": "сам|владелец|другой"}}],
    "prefs": [{{"t": "ПРЕДПОЧТЕНИЯ И ВКУСЫ: что любит/не любит — конкретные игры, фильмы, музыка, бренды, места, еда; идеи для подарков", "c": "факт|предп", "by": "сам|владелец|другой"}}],
    "sensitive": [{{"t": "ЧУВСТВИТЕЛЬНОЕ: здоровье, болезни, вредные привычки, личные трудности, страхи — ЕСЛИ реально видно", "c": "факт|предп", "by": "сам|владелец|другой"}}],
    "other": [{{"t": "ПРОЧЕЕ важное, не вошедшее выше", "c": "факт|предп", "by": "сам|владелец|другой"}}]
  }},
  "retire": ["точный текст устаревшей записи"],
  "timeline": [
    {{"date": "MM-DD или YYYY-MM-DD", "event": "датируемое событие: веха/договорённость/смена статуса"}}
  ],
  "style": "манера письма владельца в 1-2 предложениях (тон, длина, обращения, эмодзи)"
}}"""


async def enrich_owner_profile(client: AsyncAnthropic, model: str, db) -> bool:
    """Собрать/обновить память о САМОМ владельце из его исходящих. Профиль — обычная строка
    contact_profiles (user_id=owner_user_id, relationship='владелец'), синк в Notion как у всех."""
    from db import get_owner_outgoing, get_contact_profile, upsert_contact_profile, is_over_budget
    from config import settings as _s
    if await is_over_budget(db, _s.anthropic_monthly_budget):
        logger.info("Owner self-profile enrich skipped (over budget)")
        return False
    msgs = await get_owner_outgoing(db, limit=80)
    if len(msgs) < 5:
        logger.info(f"Owner self-profile: too few outgoing ({len(msgs)})")
        return False
    oid = _s.owner_user_id
    prof = await get_contact_profile(db, oid) or {}
    convo = "\n".join(f"→@{m.get('username') or '?'}: {(m.get('text') or '')[:140]}" for m in msgs)
    prompt = OWNER_USER.format(
        owner_name=_s.owner_name or "владелец",
        current_summary=prof.get("summary") or "Нет данных",
        current_facts=_current_struct(prof),
        messages=convo[:6000],
    )
    # До 2 попыток: max_tokens=1500 ОБРЕЗАЛ подробную память владельца → незакрытый JSON
    # → loads_loose давился («Unterminated string»), память молча не обновлялось 3 ночи.
    # Тот же паттерн, что в enrich контактов: ретрай со строгой инструкцией + больший лимит.
    _STRICT = ("\n\nКРИТИЧНО: верни СТРОГО валидный JSON. Экранируй двойные кавычки внутри "
               "значений как \\\" и не вставляй сырые переводы строк внутри строк.")
    try:
        data = None
        last_err = None
        for attempt in range(2):
            resp = await llm.create(client, 
                model=model, max_tokens=(4096 if attempt == 0 else 6000),
                system=OWNER_SYSTEM + (_STRICT if attempt else ""),
                messages=[{"role": "user", "content": prompt}],
            )
            await _rec(model, resp.usage, "owner_enrich")
            try:
                data = loads_loose(_llm_text(resp))
                break
            except Exception as pe:
                last_err = pe
                logger.debug(f"owner enrich JSON parse failed (attempt {attempt+1}): {pe}")
        if data is None:
            raise last_err or ValueError("owner enrich: no JSON")
        style = (data.get("style") or "").strip()
        # ДИФФ (P1.1): style возвращается только при изменении — иначе наследуем старый из facts_json
        if not style and prof.get("facts_json"):
            try:
                style = (json.loads(prof["facts_json"]).get("style") or "").strip()
            except Exception:
                pass
        owner_fields = dict(name=_s.owner_name or "Владелец", relationship="владелец")
        # ДИФФ-защита: пустое от модели = «без изменений», НЕ затираем накопленное
        if (data.get("summary") or "").strip():
            owner_fields["summary"] = data["summary"].strip()
        _add = data.get("add") if isinstance(data.get("add"), dict) else None
        _retire = data.get("retire") if isinstance(data.get("retire"), list) else None
        _sections = data.get("sections") if isinstance(data.get("sections"), dict) else None
        _timeline = data.get("timeline")
        if _add or _retire or _sections or _timeline:
            sec, tl, _, _meta = _contact_memory.merge(prof.get("facts_json"), _add, _retire, _timeline, legacy_sections=_sections)
            owner_fields["facts_json"] = _contact_memory.compose_facts_json(sec, tl, _meta, prof.get("facts_json"), style=style, gender="м")
            owner_fields["facts"] = _contact_memory.flatten(sec, tl, style)   # СТИЛЬ ПИСЬМА: в плоском — drafts.py парсит оттуда
        elif (data.get("facts") or "").strip():  # фолбэк: старый плоский формат
            facts = _clip(data.get("facts") or "", _contact_memory.FACTS_CAP)
            if style:
                facts = f"{facts}; СТИЛЬ ПИСЬМА: {style}"
            owner_fields["facts"] = facts
        # add={} и facts пусто = дифф «ничего не изменилось» → facts/facts_json не трогаем
        await upsert_contact_profile(db, oid, "me", increment=False, **owner_fields)
        if owner_fields.get("facts_json"):
            await _contact_memory.reindex(db, oid, owner_fields["facts_json"])
        logger.info("Owner self-profile updated")
        return True
    except Exception as exc:
        logger.error(f"Owner self-profile enrich failed: {exc}")
        return False


def _coarse_notion_type(role: str) -> str:
    """Свободная роль (бот создаёт сам) → грубый «Тип» для Notion-select (фикс-набор опций).
    По ключевым словам, чтобы любая эмёрджентная роль попала в валидную опцию, а не ломала select."""
    r = (role or "").lower()
    if "владел" in r:
        return "🧑‍💼 Владелец"
    if any(w in r for w in ("семь", "мам", "пап", "брат", "сестр", "девушк", "парень", "жена", "муж", "родител", "родств")):
        return "👨‍👩‍👧 Семья"
    if any(w in r for w in ("клиент", "заказчик", "спонсор")):
        return "💰 Спонсор"
    if any(w in r for w in ("коллег", "партн", "делов")):
        return "🤝 Партнёр"
    if "спам" in r:
        return "❓ Незнакомец"
    if any(w in r for w in ("друг", "приятел", "знаком", "фанат", "подписчик")):
        return "👤 Знакомый"
    return "❓ Незнакомец"


async def remember_owner_facts(db, facts: list[str]) -> int:
    """v4.43: факты о владельце из диалога → его память (см. remember_facts)."""
    from config import settings as _s
    return await remember_facts(db, _s.owner_user_id, facts)


async def remember_facts(db, user_id: int, facts: list[str], by: str = "владелец") -> int:
    """v4.44: факт(ы) из диалога с Мирой → память контакта/владельца: секция «прочее» с датой, flat пересобирается.
    Не ждём ночного энрича; энрич увидит датированный факт и при необходимости уплотнит."""
    import json as _json
    from datetime import date as _date
    from db import get_contact_profile, upsert_contact_profile
    facts = [f.strip()[:220] for f in (facts or []) if isinstance(f, str) and f.strip()][:3]
    if not facts:
        return 0
    prof = await get_contact_profile(db, user_id) or {}
    stamp = _date.today().strftime("%d.%m")
    new_items = "; ".join(f"[{stamp}] {f}" for f in facts)
    fields = {}
    raw = prof.get("facts_json")
    if raw:
        try:
            d = _json.loads(raw)
            sec = d.get("sections") or {}
            cur = (sec.get("other") or "").rstrip("; ")
            sec["other"] = (cur + "; " if cur else "") + new_items   # v4.47: без клипа
            d["sections"] = sec
            _meta = d.get("meta") or {}
            for _f in facts:   # Память 3.0/A: сказано владельцем Мире = факт, источник — диалог
                _meta[_contact_memory.norm_entry(_f)] = {"d": stamp, "cf": stamp, "c": "факт", "by": by}
            d["meta"] = _meta
            fields["facts_json"] = _json.dumps(d, ensure_ascii=False)
            fields["facts"] = _contact_memory.flatten(sec, d.get("timeline") or [], d.get("style"))
        except Exception as exc:
            logger.debug(f"remember_owner_facts json skip: {exc}")
    if "facts" not in fields:
        cur = (prof.get("facts") or "").rstrip("; ")
        fields["facts"] = ((cur + "; " if cur else "") + new_items)[:_contact_memory.FACTS_CAP]
    await upsert_contact_profile(db, user_id, prof.get("username") or f"id{user_id}", increment=False, **fields)
    if fields.get("facts_json"):
        await _contact_memory.reindex(db, user_id, fields["facts_json"])
    logger.info(f"Facts remembered from dialog for {user_id}: {facts}")
    return len(facts)


async def forget_fact(db, user_id: int, fact: str) -> bool:
    """v4.44 Ф2 «↩️ Отменить»: убрать из памяти факт, записанный remember_facts сегодня ([dd.mm] fact).
    Правим и sections.other в facts_json, и flat facts. True — что-то удалили."""
    import json as _json
    import re as _re
    from db import get_contact_profile, upsert_contact_profile
    prof = await get_contact_profile(db, user_id) or {}
    if not prof or not fact:
        return False
    pat = _re.compile(r";?\s*\[\d{2}\.\d{2}\]\s*" + _re.escape(fact.strip()[:220]) + r"(?=;|$)")
    fields, removed = {}, False
    raw = prof.get("facts_json")
    if raw:
        try:
            d = _json.loads(raw)
            sec = d.get("sections") or {}
            new_other, n = pat.subn("", sec.get("other") or "")
            if n:
                removed = True
                sec["other"] = new_other.strip("; ")
                d["sections"] = sec
                fields["facts_json"] = _json.dumps(d, ensure_ascii=False)
                fields["facts"] = _contact_memory.flatten(sec, d.get("timeline") or [], d.get("style"))
        except Exception as exc:
            logger.debug(f"forget_fact json skip: {exc}")
    if "facts" not in fields:
        new_flat, n = pat.subn("", prof.get("facts") or "")
        if n:
            removed = True
            fields["facts"] = new_flat.strip("; ")
    if removed:
        await upsert_contact_profile(db, user_id, prof.get("username") or f"id{user_id}", increment=False, **fields)
        if fields.get("facts_json"):
            await _contact_memory.reindex(db, user_id, fields["facts_json"])
        logger.info(f"Fact forgotten for {user_id}: {fact[:80]}")
    return removed


async def get_profile_context(db, user_id: int, facts_limit: int | None = None, query: str | None = None) -> str:
    """Returns formatted profile string to prepend to LLM prompts.
    facts_limit (v4.33): обрезать факты по границе записи — для извлечения задач полная память
    близкого контакта вредно: модель тонет в справке и пропускает договорённости.
    query (v4.47): вместо обрезки — «ключевое» + записи памяти, релевантные ВОПРОСУ/переписке (индекс contact_memory_vectors)."""
    from db import get_contact_profile
    profile = await get_contact_profile(db, user_id)
    if not profile or not profile.get("summary"):
        return ""
    if query:
        profile = dict(profile)
        profile["user_id"] = user_id
        profile["facts"] = await _contact_memory.brief(db, profile, query=query, k=8, max_chars=facts_limit or 1500)
    elif facts_limit and profile.get("facts"):
        profile = dict(profile)
        profile["facts"] = _clip_section(profile["facts"], facts_limit)
    parts = [f"📋 Что известно о собеседнике (@{profile.get('username', '')}):"]
    if profile.get("name"):
        parts.append(f"Имя: {profile['name']}")
    if profile.get("relationship"):
        parts.append(f"Роль: {profile['relationship']}")
    if profile.get("intent"):
        parts.append(f"Зачем пишет: {profile['intent']}")
    if profile.get("summary"):
        parts.append(f"Резюме: {profile['summary']}")
    if profile.get("facts"):
        parts.append(f"Факты: {profile['facts']}")
    if profile.get("commitments"):
        parts.append(f"⚠️ Твои незакрытые обязательства перед ним: {profile['commitments']}")
    try:
        topics = json.loads(profile.get("topics") or "[]")
        if not isinstance(topics, list):
            topics = []
    except Exception:
        topics = []  # битый/не-JSON (напр. ручная правка прод-БД) не должен ронять синк/контекст
    if topics:
        parts.append(f"Темы: {', '.join(topics)}")
    return "\n".join(parts)
