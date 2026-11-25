"""Timezone-aware date/time helpers + PII redaction."""
import html
import re
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE_RE = re.compile(r"(?<!\w)(\+?\d[\d\-\s()]{8,}\d)(?!\w)")
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,16}\b")


def has_username(username: str) -> bool:
    """Настоящий username, а не заглушка id123 у контактов без него."""
    return bool(username) and not re.fullmatch(r"id\d+", str(username))


def profile_link(username: str, user_id: int) -> str:
    """Clickable link to a Telegram user. t.me/username works for business contacts.
    v4.82: без username только t.me/@id<id>: tg://user?id= сервер вырезает, если бот не знает
    контакт, а эта ссылка доходит и на iPhone открывает чат (проверено 01.11)."""
    if has_username(username):
        return f"https://t.me/{username}"
    return f"https://t.me/@id{user_id}"


def contact_label(username: str, user_id: int, name: str = "") -> str:
    """Подпись контакта для HTML-карточки: @username, иначе имя, иначе id.
    v4.82: «@id123» Telegram красит как упоминание, но такого username нет и тап никуда не ведёт."""
    if has_username(username):
        return f"@{username}"
    return html.escape(str(name)) if name else f"id{user_id}"


def maybe_redact(text: str) -> str:
    """Mask email/phone/card if REDACT_PII is on. No-op otherwise."""
    if not text:
        return text
    from config import settings
    if not settings.redact_pii:
        return text
    text = _EMAIL_RE.sub("[email]", text)
    text = _CARD_RE.sub("[card]", text)
    text = _PHONE_RE.sub("[phone]", text)
    return text


def loads_loose(raw: str):
    """Parse JSON from an LLM reply robustly. Handles ```json fences (any case),
    leading prose, and trailing text. Replaces fragile `strip('`').lstrip('json')`
    which silently corrupted valid JSON. Raises json.JSONDecodeError if nothing parses.
    Если объектов несколько, берём последний: без мышления модель поправляет себя следующим
    («Исправляю: …» и новый JSON), первый тогда неверный."""
    import json
    s = (raw or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*```$", "", s).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError as exc:
        if not re.search(r"[\{\[]", s):
            raise exc
    decoder, opener, pos, found = json.JSONDecoder(), re.compile(r"[\{\[]"), 0, []
    while m := opener.search(s, pos):
        try:
            obj, pos = decoder.raw_decode(s, m.start())
            found.append(obj)
        except json.JSONDecodeError:
            if not found:
                raise  # обрыв первого JSON: не подменяем его вложенным куском
            pos = m.end()
    return [o for o in found if isinstance(o, type(found[0]))][-1]


def clean_name(s) -> Optional[str]:
    """Return a human name only if it contains real letters. Strips emoji/symbol-only
    display names ('💔', '🔥', '💔xx') so contact cards don't show garbage instead of a name."""
    if not s:
        return None
    # keep letters (any script), digits, spaces, dots, hyphens, apostrophes
    cleaned = re.sub(r"[^\w\s.\-']", "", s, flags=re.UNICODE).strip()
    # must contain at least one alphabetic char (not just digits/underscores)
    if cleaned and any(ch.isalpha() for ch in cleaned):
        return cleaned
    return None


def notion_clamp(s, limit: int = 2000) -> str:
    """Clamp text for Notion rich_text. Notion counts length in UTF-16 code units
    (an emoji = 2 units), so plain str slicing can overshoot by 1+ and trigger a
    400 validation_error. Trims to `limit` UTF-16 units without splitting a surrogate."""
    if not s:
        return ""
    units = s.encode("utf-16-le")
    if len(units) // 2 <= limit:
        return s
    return units[: limit * 2].decode("utf-16-le", errors="ignore")


_RU_MONTHS = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def human_due(due) -> str:
    """'2026-06-08T11:30' → 'завтра, 11:30' / '8 июня, 11:30' / '8 июня' (без сырого ISO)."""
    if not due:
        return ""
    try:
        has_time = "T" in due
        d = datetime.fromisoformat(due[:19] if has_time else due[:10])
        dd = d.date()
        delta = (dd - today_local()).days
        if delta == 0:
            day = "сегодня"
        elif delta == 1:
            day = "завтра"
        elif delta == -1:
            day = "вчера"
        else:
            day = f"{dd.day} {_RU_MONTHS[dd.month]}"
        return f"{day}, {d.strftime('%H:%M')}" if has_time else day
    except Exception:
        return str(due)


def category_emoji(category: str) -> str:
    """Единый язык иконок категорий (Notion/Todoist/vault/бот): 💼 BUSINESS · 🙂 FRIEND · ❓ UNCLEAR · 🚫 SPAM."""
    return {"BUSINESS": "💼", "FRIEND": "🙂", "UNCLEAR": "❓", "SPAM": "🚫"}.get((category or "").upper(), "📌")


def is_emoji(s: str) -> bool:
    """Строго ли это эмодзи (валидация выбора LLM перед записью в Notion-иконку — иначе Notion 400
    на POST страницы → задача не создаётся). Отвергаем буквы/цифры/пробелы/пунктуацию/валюту/матзнаки
    и текстовый мусор; требуем хотя бы один настоящий пиктограммный символ. Допускаем ZWJ-эмодзи
    (👨‍👩‍👧, ✍️, флаги) до 12 кодпойнтов."""
    import unicodedata
    s = (s or "").strip()
    if not s or len(s) > 12:
        return False
    for c in s:
        cat = unicodedata.category(c)
        if c.isalnum() or c.isspace() or cat.startswith("P") or cat in ("Sc", "Sm"):
            return False
    return any(unicodedata.category(c) == "So" or ord(c) >= 0x1F000 for c in s)


# Детерминированный фолбэк: тема задачи → эмодзи по ключевым словам (если LLM эмодзи не дал).
_TOPIC_EMOJI = [
    (("поигра", "игр", "погоня", "катк", "стим", "steam", "доту", "dota", "пострел"), "🎮"),
    (("отвез", "отвести", "забрать", "подвез", "довез", "свозить", "поездк", "поехать", "заехать"), "🚗"),
    (("пиво", "бар", "выпить", "тусов", "погудеть"), "🍺"),
    (("поес", "обед", "ужин", "кафе", "ресторан", "поедим", "перекус"), "🍽"),
    (("куп", "закуп", "заказать товар", "магаз"), "🛒"),
    (("оплат", "заплат", "деньг", "долг", "перевести", "скинуть денег", "бабк", "бенз"), "💰"),
    (("позвон", "созвон", "набрать", "звонок"), "📞"),
    (("написа", "ответ", "напиш", "сообщ", "скинуть текст"), "✍️"),
    (("почин", "ремонт", "сломал", "наладить"), "🔧"),
    (("врач", "медси", "больниц", "клиник", "анализ", "колонос", "гастро", "узи", "приём к"), "🏥"),
    (("учеб", "учёб", "урок", "экзам", "зачёт", "зачет", "лекци", "курсов", "диплом", "сесси", "english", "английск"), "📚"),
    (("посмотр", "глянуть", "глянь", "чекн", "заценить", "глянем", "reel", "видео посмотр"), "👀"),
    (("договор", "документ", "справк", "подписать", "бумаг"), "📄"),
    (("подар", "др ", "день рожд", "днюх"), "🎁"),
    (("шанхай", "перелёт", "перелет", "самолёт", "аэропорт", "командировк"), "✈️"),
    (("зал", "трен", "спорт", "качал", "пробежк", "бег"), "💪"),
    (("стрим", "рилс", "видео", "контент", "ролик", "монтаж"), "🎥"),
    (("гитар", "музык", "трек", "песн"), "🎸"),
    (("встрет", "встреч", "увидеть", "повидать", "сходить"), "🤝"),
]


def topic_emoji(title: str) -> Optional[str]:
    """Тема задачи → эмодзи по ключевым словам. None если не распознано (тогда — категория/дефолт)."""
    t = (title or "").lower()
    for keys, emo in _TOPIC_EMOJI:
        if any(k in t for k in keys):
            return emo
    return None


def html_due(due, fmt=None) -> str:
    """human_due, обёрнутый в date_time-entity (<tg-time>, Bot API 9.5):
    получатель видит дату в своём локальном формате, она «живая» (тап → календарь).
    Формат: r=относительно, w=день недели, d/D=дата, t/T=время. Деградация → plain human_due."""
    label = human_due(due)
    if not label or not due:
        return label
    try:
        has_time = "T" in due
        d = datetime.fromisoformat(due[:19] if has_time else due[:10])
        if d.tzinfo is None:
            d = d.replace(tzinfo=_tz())
        f = fmt if fmt is not None else ("wdt" if has_time else "wd")
        return f'<tg-time unix="{int(d.timestamp())}" format="{f}">{label}</tg-time>'
    except Exception:
        return label


def _tz():
    from config import settings
    return ZoneInfo(settings.timezone)


def now_local() -> datetime:
    return datetime.now(_tz())


def today_local() -> date:
    return datetime.now(_tz()).date()


def today_local_iso() -> str:
    return today_local().isoformat()


_user_locks: dict = {}


def user_lock(user_id: int):
    """Per-user asyncio.Lock (общий реестр) — сериализует создание задач/контактов одного
    пользователя между путями enrich (fire-and-forget) и settle, чтобы гонка не плодила дубли.
    ⚠️ НЕ реентрантный: не вкладывать один и тот же замок (см. reconcile — он БЕЗ замка,
    т.к. вызывается уже под замком enrich)."""
    import asyncio as _aio
    lk = _user_locks.get(user_id)
    if lk is None:
        lk = _aio.Lock()
        _user_locks[user_id] = lk
    return lk
