"""
Умные ответы (Draft Replies): на важное входящее бот пишет ЧЕРНОВИК ответа в стиле владельца
и шлёт его ВЛАДЕЛЬЦУ на одобрение. Отправка контакту — ТОЛЬКО по кнопке [📤 Отправить],
никогда не авто. Через Business API (business_connection_id).
"""
import html
import secrets
from core import llm
import time

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from loguru import logger

from core.usage import record as _rec
from core.llm import text_of as _llm_text   # v4.61: читать ответ по типу блока (Claude 5 может начать с thinking)

_pending: dict = {}      # token -> {bc_id, chat_id, text, username, user_id, incoming, ts}
_counter = [0]   # legacy; токены — secrets (v4.36 B2: после рестарта счётчик с 1 выдавал чужие черновики)


def _new_token() -> str:
    return secrets.token_hex(4)
_PENDING_TTL = 12 * 3600  # черновики живут 12ч

# Обучающая петля: после кнопки «✍️ Отвечу сам» бот ждёт, как владелец ответит —
# текстом в личку боту (path A) ИЛИ реальным ответом контакту (path B) — и запоминает.
_teach_chat: dict = {}   # contact user_id -> {incoming, username, ts}
_teach_pm: dict = {}     # owner_id     -> {user_id, incoming, username, ts}
_TEACH_TTL = 1800        # 30 минут на «покажи как»


def _prune() -> None:
    now = time.monotonic()
    for k in [k for k, v in _pending.items() if now - v.get("ts", now) > _PENDING_TTL]:
        _pending.pop(k, None)

DRAFT_SYSTEM = """Ты пишешь ЧЕРНОВИК ответа от лица владельца на входящее сообщение.
Коротко и по-человечески, в его тоне: деловое — вежливо и по делу; личное — тепло и просто.
Владелец — МУЖЧИНА: о себе в прошедшем времени — мужской род (сделал, был, готов, успел — НЕ сделала/была/готова).
1–3 предложения, без воды, без «Здравствуйте, я рассмотрю ваш запрос». Верни ТОЛЬКО текст ответа."""


async def make_draft(client, model: str, name, relationship, incoming_text: str,
                     examples: list | None = None, owner_style: str = "") -> str:
    ex_block = ""
    if examples:
        lines = []
        for e in examples[:5]:
            inc = (e.get("incoming") or "").strip()[:120]
            rep = (e.get("reply") or "").strip()[:140]
            if rep:
                lines.append(f"— на «{inc}» он ответил: «{rep}»")
        if lines:
            ex_block = ("\n\nКАК ВЛАДЕЛЕЦ ОТВЕЧАЕТ САМ (подражай длине, тону, манере — "
                        "это важнее любых шаблонов вежливости):\n" + "\n".join(lines))
    style_block = (f"\n\nКТО ТЫ (владелец) и твой стиль письма — пиши именно так:\n{owner_style[:450]}"
                   if owner_style else "")
    prompt = (f"Контакт: {name or '—'} ({relationship or '—'}). Пишет:\n«{incoming_text[:600]}»{ex_block}{style_block}\n\n"
              f"Напиши черновик ответа от лица владельца.")
    resp = await llm.create(client, 
        model=model, max_tokens=200, system=DRAFT_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    await _rec(model, resp.usage, "draft")
    return (_llm_text(resp) or "").strip()


async def offer_draft(bot, client, model: str, owner_id: int, bc_id: str, chat_id: int,
                      username: str, profile: dict, incoming_text: str,
                      db=None, user_id=None, acct_tag: str = "") -> None:
    """Сделать черновик (с учётом выученного стиля) и прислать владельцу карточку. Best-effort."""
    if not bc_id:
        return  # без business-connection отправить не сможем
    try:
        prof = profile or {}
        examples = []
        owner_style = ""
        if db is not None:
            try:
                from db import get_draft_examples
                examples = await get_draft_examples(db, limit=5)
            except Exception:
                pass
            try:
                from db import get_contact_profile
                from config import settings as _s
                op = await get_contact_profile(db, _s.owner_user_id)
                if op:
                    owner_style = (op.get("summary") or "").strip()
                    facts = op.get("facts") or ""
                    if "СТИЛЬ ПИСЬМА" in facts:
                        owner_style = (owner_style + " " + facts.split("СТИЛЬ ПИСЬМА:", 1)[1][:220]).strip()
            except Exception:
                pass
        from core import richmsg
        from core.topics import thread_id as _tid
        async with richmsg.thinking(bot, owner_id, _tid("drafts"), "🤖 пишу черновик…"):
            _voice = await owner_voice(db, user_id) if (db is not None and user_id) else ""
            draft = await make_draft(client, model, prof.get("name"), prof.get("relationship"),
                                     incoming_text, examples,
                                     owner_style=(owner_style + "\n" + _voice).strip()[:1400])
    except Exception as exc:
        logger.debug(f"draft skip: {exc}")
        return
    if not draft:
        return
    _prune()
    token = _new_token()
    _pending[token] = {"bc_id": bc_id, "chat_id": chat_id, "text": draft,
                       "username": username, "user_id": user_id,
                       "incoming": (incoming_text or "")[:400], "ts": time.monotonic()}
    if not acct_tag:   # v4.107: карточка-виджет (Bot API 10.4); не вышло — обычная карточка ниже
        try:
            from core.widgets import drafts as _wdrafts
            if await _wdrafts.offer(owner_id, token):
                logger.info(f"Draft reply offered for @{username} (widget)")
                return
        except Exception as exc:
            logger.info(f"draft widget skip: {exc}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Отправить", callback_data=f"drf:{token}:ok", style="success"),
         InlineKeyboardButton(text="🗑 Игнор", callback_data=f"drf:{token}:no", style="danger")],
        [InlineKeyboardButton(text="✍️ Отвечу сам (запомни мой стиль)", callback_data=f"drf:{token}:teach", style="primary")],
    ])
    from core.topics import send_owner
    from core.util import profile_link
    await send_owner(
        bot, owner_id, "drafts",
        f'💬 <b>Черновик ответа <a href="{profile_link(username, user_id or chat_id)}">@{html.escape(str(username))}</a></b>{acct_tag}\n\n'
        f"«{html.escape(draft)}»\n\n<i>на сообщение: {html.escape(incoming_text[:140])}</i>",
        reply_markup=kb, parse_mode="HTML",
    )
    logger.info(f"Draft reply offered for @{username}")


# ── Обучающая петля ──────────────────────────────────────────────────────────
def _fresh(d) -> bool:
    return bool(d) and (time.monotonic() - d.get("ts", 0) < _TEACH_TTL)


def arm_teach_from_token(token: str, owner_id: int):
    """Кнопка «✍️ Отвечу сам»: взвести ожидание ответа владельца (оба пути). Вернёт username."""
    item = _pending.pop(token, None)
    if not item:
        return None
    payload = {"user_id": item.get("user_id"), "incoming": item.get("incoming") or "",
               "username": item.get("username") or "", "ts": time.monotonic()}
    if item.get("user_id") is not None:
        _teach_chat[item["user_id"]] = payload
    _teach_pm[owner_id] = payload
    return item.get("username")


def pop_teach_pm(owner_id: int):
    """Path A: владелец написал свой вариант боту в личку."""
    d = _teach_pm.pop(owner_id, None)
    if _fresh(d):
        _teach_chat.pop(d.get("user_id"), None)
        return d
    return None


def pop_teach_chat(user_id):
    """Path B: владелец реально ответил контакту в бизнес-чате.
    Заодно снимаем PM-ожидание по этому же контакту, чтобы оно не словило случайный текст."""
    d = _teach_chat.pop(user_id, None)
    for oid in [k for k, v in _teach_pm.items() if v.get("user_id") == user_id]:
        _teach_pm.pop(oid, None)
    return d if _fresh(d) else None


async def owner_voice(db, user_id: int, limit: int = 5) -> str:
    """v4.42: «голос владельца» для черновиков/пингов/поздравлений — стиль из памяти + его ПОСЛЕДНИЕ
    сообщения ИМЕННО ЭТОМУ человеку (обращение, ты/вы, приветствие, длина, эмодзи). Владелец: «я так
    никогда не писал» на пинг «Кристин, хай! Ты как, жива? 🙂»."""
    parts = []
    try:
        from config import settings as _s
        from db import get_contact_profile
        op = await get_contact_profile(db, _s.owner_user_id) or {}
        style = (op.get("summary") or "").strip()[:300]
        facts = op.get("facts") or ""
        if "СТИЛЬ ПИСЬМА" in facts:
            style = (style + " " + facts.split("СТИЛЬ ПИСЬМА:", 1)[1][:300]).strip()
        if style:
            parts.append(f"СТИЛЬ ВЛАДЕЛЬЦА: {style}")
        async with db.execute(
                "SELECT text FROM conversations WHERE user_id=? AND direction='out' AND text IS NOT NULL "
                "AND length(text) > 3 ORDER BY id DESC LIMIT ?", (user_id, limit * 2)) as cur:
            outs = [r[0].strip()[:160] for r in await cur.fetchall()
                    if r[0] and not r[0].lstrip().startswith("[")][:limit]
        if outs:
            parts.append("КАК ВЛАДЕЛЕЦ ПИШЕТ ЭТОМУ ЧЕЛОВЕКУ (его реальные сообщения — подражай обращению, "
                         "ты/вы, приветствию, длине, эмодзи; НЕ добавляй сленг, которого тут нет):\n"
                         + "\n".join(f"— «{o}»" for o in reversed(outs)))
    except Exception:
        pass
    return "\n".join(parts)


def stash_fixed(bc_id: str, chat_id: int, username: str, draft: str):
    """v4.40 (B7): положить готовый черновик в pending БЕЗ отправки карточки — вызывающий сам
    рисует его в НУЖНОМ пузыре (редактирует исходную карточку). Возвращает token или None."""
    if not bc_id or not draft:
        return None
    _prune()
    token = _new_token()
    _pending[token] = {"bc_id": bc_id, "chat_id": chat_id, "text": draft,
                       "username": username, "user_id": chat_id, "incoming": "", "ts": time.monotonic()}
    return token


def fixed_kb(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📤 Отправить", callback_data=f"drf:{token}:ok", style="success"),
        InlineKeyboardButton(text="🗑 Игнор", callback_data=f"drf:{token}:no", style="danger"),
    ]])


async def offer_fixed_draft(bot, owner_id: int, bc_id: str, chat_id: int,
                            username: str, draft: str, note: str = "") -> bool:
    """Предложить владельцу ГОТОВЫЙ текст для отправки контакту (smart scheduling).
    Отправка контакту — только по кнопке 📤 (тот же flow одобрения, что и у обычных черновиков)."""
    if not bc_id or not draft:
        return False
    _prune()
    token = _new_token()
    _pending[token] = {"bc_id": bc_id, "chat_id": chat_id, "text": draft,
                       "username": username, "user_id": chat_id, "incoming": "", "ts": time.monotonic()}
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📤 Отправить", callback_data=f"drf:{token}:ok", style="success"),
        InlineKeyboardButton(text="🗑 Игнор", callback_data=f"drf:{token}:no", style="danger"),
    ]])
    from core.topics import send_owner
    from core.util import profile_link
    # v4.35: имя-ссылка в чат (для «@id123…» — t.me/@id, иначе тап по @id ничего не открывал)
    _lbl = f"@{html.escape(username)}" if not str(username).startswith("id") else f"id{html.escape(str(chat_id))}"
    await send_owner(
        bot, owner_id, "drafts",
        f'💬 <b>Предложить <a href="{profile_link(username, chat_id)}">{_lbl}</a>:</b>\n\n«{html.escape(draft)}»'
        + (f"\n\n<i>{html.escape(note)}</i>" if note else ""),
        reply_markup=kb, parse_mode="HTML")
    return True


def peek_draft(token: str) -> dict | None:
    """Черновик по токену (для карточки «скопируй сам», когда Telegram не даёт отправить)."""
    return _pending.get(token)


async def send_draft(bot, token: str) -> tuple[bool, str, str]:
    """Отправить черновик контакту через Business API. (ok, username, error).
    v4.34.1: error наверх — колбэк объясняет владельцу причину (BUSINESS_PEER_USAGE_MISSING =
    Telegram не даёт боту писать контакту без недавней активности в диалоге)."""
    item = _pending.pop(token, None)   # v4.36 (B1): забираем ДО сети — второй клик не отправит дважды
    if not item:
        return False, "", "expired"
    try:
        await bot.send_message(chat_id=item["chat_id"], text=item["text"],
                               business_connection_id=item["bc_id"], parse_mode=None)  # v4.33: «R&D», «<3» не ломают отправку
        logger.info(f"Draft reply SENT to @{item['username']}")
        return True, item["username"], ""
    except Exception as exc:
        _pending[token] = item   # сбой → вернуть для повтора / кнопки «скопировать»
        logger.error(f"send draft failed: {exc}")
        return False, item["username"], str(exc)


def drop_draft(token: str) -> None:
    _pending.pop(token, None)


_REWRITE = {"k": "короче: одно-два коротких предложения, тот же смысл",
            "m": "мягче и теплее, тот же смысл и примерно та же длина"}


async def rewrite(client, model: str, token: str, mode: str) -> str | None:
    """v4.107 виджет: переписать черновик «короче» (k) или «мягче» (m). Новый текст или None."""
    item = _pending.get(token)
    if not item or mode not in _REWRITE:
        return None
    prompt = (f"Входящее сообщение: «{(item.get('incoming') or '')[:400]}»\n"
              f"Черновик ответа владельца: «{item['text']}»\n\nПерепиши черновик {_REWRITE[mode]}.")
    resp = await llm.create(client, model=model, max_tokens=200, system=DRAFT_SYSTEM,
                            messages=[{"role": "user", "content": prompt}])
    await _rec(model, resp.usage, "draft")
    text = (_llm_text(resp) or "").strip().strip("«»\"")
    if not text or token not in _pending:
        return None
    _pending[token]["text"] = text
    _pending[token]["ts"] = time.monotonic()
    return text
