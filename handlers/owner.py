"""
Личный ассистент в личке бота: владелец пишет/кидает боту что угодно (текст/голос/фото/документ),
бот распознаёт намерение и:
  • capture — заводит задачу/встречу (с подтверждением кнопкой) → Todoist (+ Calendar)
  • manage  — закрывает/переносит существующую задачу
  • query   — показывает дела на сегодня/неделю
Только owner_user_id, только личка. Бизнес-входящие (от контактов) не затрагиваются.
"""
import asyncio
import html as _html   # v4.33: модульный импорт (раньше только локальные в части функций)
import re
import time
from datetime import timedelta
from typing import Optional

from aiogram import Router, F, Bot
from aiogram.types import (Message, CallbackQuery,
                           InlineKeyboardMarkup, InlineKeyboardButton)
from anthropic import AsyncAnthropic
from loguru import logger

from config import Settings
from core import llm
from core.util import loads_loose, human_due, today_local, today_local_iso
from core.usage import record as _rec
from core.llm import text_of as _llm_text   # v4.61: читать ответ по типу блока (Claude 5 может начать с thinking)

router = Router()
router.message.filter(F.chat.type == "private")  # только личка бота

_STOPPED: set = set()   # B8: draft_id, для которых владелец нажал «Стоп» (Bot API 10.2)


@router.stopped_message_generation()
async def on_stream_stopped(event, settings: Settings) -> None:
    """Владелец нажал ⏹ у потокового черновика → стрим оборвём, частичный ответ сохраним."""
    _STOPPED.add(getattr(event, "draft_id", None))

_pending: dict = {}      # token -> {tasks: [...], text, ts}
_counter = [0]
_LINK_RE = re.compile(r"https?://[^\s]+|t\.me/[^\s]+|www\.[^\s]+", re.IGNORECASE)
_PENDING_TTL = 6 * 3600  # карточки-подтверждения живут 6ч


def _prune_pending() -> None:
    now = time.monotonic()
    for k in [k for k, v in _pending.items() if now - v.get("ts", now) > _PENDING_TTL]:
        _pending.pop(k, None)


# ── распознавание намерения + извлечение ─────────────────────────────────────
def _eff(settings, kind: str = "ok") -> dict:
    """Bot API message-эффект на ПОЗИТИВНЫХ подтверждениях в личке (в тему, не на каждом сообщении).
    kind: ok=🔥 (по конфигу), celebrate=🎉, like=👍. Пусто → без эффекта."""
    base = getattr(settings, "message_effect_id", "") or ""
    if not base:
        return {}
    ids = {"ok": base, "celebrate": "5046509860389126442", "like": "5107584321108051014"}
    return {"message_effect_id": ids.get(kind, base)}


ROUTE_SYSTEM = """Ты — личный ассистент владельца (его собственный инбокс задач). По сообщению определи НАМЕРЕНИЕ и извлеки данные. Отвечай строго JSON без markdown.

Намерения:
- "capture" — создать ОДНУ или НЕСКОЛЬКО задач/встреч (длинное сообщение или голосовое может содержать несколько дел — раздели их). Поле tasks: список объектов {title (кратко, от лица владельца), due_date ('YYYY-MM-DD' или 'YYYY-MM-DDTHH:MM'; конвертируй «сегодня/завтра/в пятницу/в 20»; иначе null), is_meeting (true если встреча/событие с конкретным временем)}.
- "query" — показать СВОИ дела/расписание/планы. Поле: period ("today" | "tomorrow" | "week") — «завтра», «на завтра» → tomorrow.
- "manage" — изменить существующую задачу. Поля: action ("close"|"reschedule"), target (краткое описание какой задачи), new_due (для reschedule; иначе null).
- "ask" — ВОПРОС о прошлом/переписке/контактах («что я обещал маме?», «о чём говорили с Никой про вуз?», «когда договорились с @X встретиться?», «что писал @Y про зеркало?»). Это поиск по памяти, а не дело.
- "freetime" — НАЙТИ свободное время в календаре («когда я свободен?», «найди время завтра на час», «есть окно на этой неделе?», «подбери слот на 30 минут сегодня»). Поля: period ("today"|"tomorrow"|"week"), duration_min (длительность нужного окна в минутах; по умолчанию 60).
- "chat" — разговор С АССИСТЕНТОМ: вопрос о ТЕКУЩИХ задачах/долгах по конкретному человеку («что у меня по Ивану?», «что я должен Нике?»), «что мне не забыть?», вопрос про дни рождения («когда у Вани др?»), просьба совета/мнения, приветствие или вопрос к самому ассистенту («ты кто?», «что умеешь?»). НЕ дело (capture) и НЕ вопрос о прошлой переписке (ask).
- "bday" — ЗАПОМНИТЬ день рождения / важную дату человека («у Вани др 12 мая», «запомни: день рождения мамы 3 января 1975», «др Ники 07.11»). Поля: person (имя/@username как назвал владелец), bdate (дата как в сообщении, например «12 мая 1999» или «07.11»).

КОНТЕКСТ ДИАЛОГА (важно): если владелец недавно общался с ассистентом (ниже дан предыдущий обмен и его давность) и это сообщение — ОТВЕТ/продолжение: поправляет её слова («я пока не устроился…»), рассказывает о себе, отвечает на её вопрос, комментирует — intent=chat, ДАЖЕ если внутри есть дата или дело: ассистент сама предложит записать задачу кнопкой. capture — только при ЯВНОЙ просьбе («запиши», «напомни», «поставь задачу», «добавь», «создай») или когда сообщение — самостоятельная заметка-дело без связи с диалогом.

По умолчанию, если похоже на дело/напоминание — capture; если это ВОПРОС о том, что было — ask; если просят найти свободное время/окно/слот — freetime; если это обращение к ассистенту/болтовня/продолжение разговора — chat."""

ROUTE_USER = """Сегодня: {today}. Предыдущая реплика владельца (для коротких уточнений вроде «а завтра?», «и что там?»): {prev}
Сообщение владельца:
---
{text}
---
Верни JSON: {{"intent":"capture|manage|query|ask|freetime|chat|bday","tasks":[{{"title":"...","due_date":null,"is_meeting":false}}],"period":"today","duration_min":60,"action":null,"target":null,"new_due":null,"person":null,"bdate":null}}"""


async def _route(client: AsyncAnthropic, model: str, text: str, prev: str = "") -> dict:
    try:
        resp = await llm.create(client, 
            model=model, max_tokens=300, system=ROUTE_SYSTEM,
            messages=[{"role": "user", "content": ROUTE_USER.format(
                today=today_local_iso(), text=text[:1500], prev=(prev or "—")[:300])}],
        )
        await _rec(model, resp.usage, "owner_route")
        return loads_loose(_llm_text(resp)) or {}
    except Exception as exc:
        logger.error(f"owner route failed: {exc}")
        return {}


DUE_PARSE_SYSTEM = """Извлеки дату и время из сообщения владельца. Верни строго JSON без markdown:
{"due": "YYYY-MM-DDTHH:MM" если есть время, "YYYY-MM-DD" если только дата, null если даты/времени нет}.
Тебе дадут ТЕКУЩИЕ дату и время — резолвь относительные выражения от них:
- «через N часов / через час / через полчаса / через N минут» = ТЕКУЩЕЕ ВРЕМЯ + смещение (с переходом на след. день при необходимости);
- «через N дней / завтра / послезавтра / в пятницу» = смещение по ДАТЕ;
- «в 13:00 / 13:00 / к 14» = абсолютное время сегодня.
ВАЖНО: «через 3 часа» = сейчас+3ч (НЕ 03:00); «в 3 часа»/«в 15:00» = абсолютное время."""


async def _parse_due_text(client: AsyncAnthropic, model: str, text: str) -> Optional[str]:
    """Распарсить свободный текст владельца («завтра 14:00», «через 3 часа», «18 июня 13:00») в ISO. None если не дата."""
    try:
        from core.util import now_local
        _wd = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]
        _now = now_local()
        now_str = f"{_now.isoformat(timespec='minutes')} ({_wd[_now.weekday()]})"
        resp = await llm.create(client, 
            model=model, max_tokens=60, system=DUE_PARSE_SYSTEM,
            messages=[{"role": "user", "content": f"Сейчас: {now_str}.\nСообщение: {text[:200]}"}],
        )
        await _rec(model, resp.usage, "due_parse")
        due = (loads_loose(_llm_text(resp)) or {}).get("due")
        return due if isinstance(due, str) and due[:4].isdigit() else None
    except Exception as exc:
        logger.debug(f"due parse failed: {exc}")
        return None


async def _apply_reschedule(db, settings: Settings, sync_id: int, due: str) -> Optional[str]:
    """Перенести задачу на due (Todoist+календарь+БД), сохранив время если было. Вернёт due или None."""
    from db import get_sync_by_id, set_task_due
    from core.actions import update_todoist_task, update_task_calendar, _normalize_due
    row = await get_sync_by_id(db, sync_id)
    if not row or row.get("synced"):
        return None  # нет задачи ИЛИ уже закрыта — не воскрешаем переносом
    due = _normalize_due(due) or due
    # если новый due без времени, но у задачи было время — сохранить старое время
    if "T" not in str(due):
        old = row.get("due_date")
        if old and "T" in str(old):
            due = f"{due}T{str(old).split('T', 1)[1]}"
    ok = False
    if settings.todoist_api_token and row["todoist_id"]:
        ok |= await update_todoist_task(settings.todoist_api_token, row["todoist_id"], due)
    try:
        await set_task_due(db, sync_id, due)
    except Exception:
        pass
    try:
        await update_task_calendar(db, row, row["title"], due)
    except Exception as exc:
        logger.debug(f"reschedule calendar skip: {exc}")
    return due  # БД/календарь обновлены даже если внешние API молчат (ok влияет лишь на лог)


# ── извлечение текста из любого формата ──────────────────────────────────────
async def _gather_text(bot: Bot, message: Message, settings: Settings,
                       client: AsyncAnthropic, db=None) -> str:
    if message.text:
        return message.text
    cap = message.caption or ""
    try:
        if message.voice or message.audio or message.video_note:
            from core.transcribe import transcribe_voice
            fid = (message.voice or message.audio or message.video_note).file_id
            t = await transcribe_voice(bot, fid, db=db)   # v4.50: словарь имён контактов
            return (t or cap).strip()
        if message.photo:
            from handlers.business import _analyze_photo
            desc = await _analyze_photo(client, settings.anthropic_model_smart, bot, message, "сам")
            return f"{cap}\n{desc}".strip() if cap else (desc or "")
        if message.document:
            from core.documents import extract_document_text
            res = await extract_document_text(bot, message.document)
            doc_text = res[1] if res else ""
            return f"{cap}\n{doc_text}".strip() if cap else doc_text
    except Exception as exc:
        logger.error(f"owner gather_text failed: {exc}")
    return cap


async def _resend_media(bot: Bot, message: Message, chat_id: int,
                        thread_id: int = None, with_caption: bool = True):
    """v4.10: переслать медиа из message в chat_id по file_id (надёжно для Business-API).
    Все типы: фото/стикер/голос/кружок/видео/GIF/аудио/документ. Возвращает Message или None."""
    m = message
    kw = {}
    if thread_id:
        kw["message_thread_id"] = thread_id
    cap = ((m.caption or "").strip()[:1000] or None) if with_caption else None
    try:
        if m.photo:        return await bot.send_photo(chat_id, m.photo[-1].file_id, caption=cap, parse_mode=None, **kw)
        if m.sticker:      return await bot.send_sticker(chat_id, m.sticker.file_id, **kw)
        if m.voice:        return await bot.send_voice(chat_id, m.voice.file_id, caption=cap, parse_mode=None, **kw)
        if m.video_note:   return await bot.send_video_note(chat_id, m.video_note.file_id, **kw)
        if m.video:        return await bot.send_video(chat_id, m.video.file_id, caption=cap, parse_mode=None, **kw)
        if m.animation:    return await bot.send_animation(chat_id, m.animation.file_id, caption=cap, parse_mode=None, **kw)
        if m.audio:        return await bot.send_audio(chat_id, m.audio.file_id, caption=cap, parse_mode=None, **kw)
        if m.document:     return await bot.send_document(chat_id, m.document.file_id, caption=cap, parse_mode=None, **kw)
    except Exception as exc:
        logger.warning(f"relay media resend failed: {exc}")
    return None


def _media_kind(m: Message):
    """Человекочитаемый тип медиа (для подтверждения) или None, если медиа нет."""
    if m.photo: return "Фото"
    if m.sticker: return "Стикер"
    if m.voice: return "Голосовое"
    if m.video_note: return "Кружок"
    if m.video: return "Видео"
    if m.animation: return "GIF"
    if m.audio: return "Аудио"
    if m.document: return "Документ"
    return None


# ── Слой «контакты лички бота» (v4.11): блок / метка / вечный ответ ──────────────
_DM_LABEL_RE = re.compile(r"^/(?:имя|метка|label)\s+(.+)", re.IGNORECASE | re.DOTALL)
_DM_ID_RE = re.compile(r"\bid(\d{4,})\b")


async def _dmc_get(db, uid: int) -> dict:
    """Контакт лички: {name, uname, label, blocked, first_seen, last_seen} или {}."""
    if db is None:
        return {}
    import json as _json
    from db import get_setting
    raw = await get_setting(db, f"dmc:{uid}")
    try:
        return _json.loads(raw) if raw else {}
    except Exception:
        return {}


async def _dmc_set(db, uid: int, **fields) -> None:
    if db is None:
        return
    import json as _json
    from db import set_setting
    d = await _dmc_get(db, uid)
    d.update({k: v for k, v in fields.items() if v is not None})
    await set_setting(db, f"dmc:{uid}", _json.dumps(d, ensure_ascii=False))


async def _resolve_dm_target(db, replied: Message):
    """#3 ВЕЧНЫЙ резолв адресата ответа: сперва быстрая привязка relay:{id} (работает и для
    медиа), иначе — парсим idN из текста карточки (карточка всегда содержит idN). Fallback
    срабатывает ТОЛЬКО на настоящих карточках лички (маркер), чтобы обычный реплай с «idN»
    в тексте не улетел человеку."""
    import json as _json
    from db import get_setting
    raw = await get_setting(db, f"relay:{replied.message_id}")
    if raw:
        try:
            return int(_json.loads(raw)["id"])
        except Exception:
            try:
                return int(raw)
            except Exception:
                pass
    txt = replied.text or replied.caption or ""
    # оба маркера: старые карточки («Пишут боту напрямую») и новые персона-карточки («Тебе пишут»)
    if "Пишут боту напрямую" in txt or "Тебе пишут" in txt:
        m = _DM_ID_RE.search(txt)
        if m:
            return int(m.group(1))
    return None


@router.callback_query(F.data.startswith("dmblock:"))
async def cb_dmblock(query: CallbackQuery, db, settings: Settings) -> None:
    from core.owners import is_owner as _is_owner
    if not _is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    uid = int(query.data.split(":", 1)[1])
    await _dmc_set(db, uid, blocked=True)
    dmc = await _dmc_get(db, uid)
    _nm = dmc.get("label") or dmc.get("name") or f"id{uid}"
    try:
        await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="♻️ Разблокировать", callback_data=f"dmunblock:{uid}")]]))
    except Exception:
        pass
    await query.answer(f"🚫 {_nm} заблокирован — сообщения больше не дойдут", show_alert=True)
    logger.info(f"DM contact blocked id={uid}")


@router.callback_query(F.data.startswith("dmunblock:"))
async def cb_dmunblock(query: CallbackQuery, db, settings: Settings) -> None:
    from core.owners import is_owner as _is_owner
    if not _is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    uid = int(query.data.split(":", 1)[1])
    await _dmc_set(db, uid, blocked=False)
    try:
        await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"dmblock:{uid}")]]))
    except Exception:
        pass
    await query.answer("♻️ Разблокирован")
    logger.info(f"DM contact unblocked id={uid}")


@router.callback_query(F.data.startswith("dmtask:"))
async def cb_dmtask(query: CallbackQuery, db, settings: Settings) -> None:
    """#1 «📋 В задачи»: превратить сообщение человека из лички в задачу (Todoist+календарь).
    Тело берём из текста карточки (между первым и последним разделителем абзацев)."""
    if query.from_user.id != settings.owner_user_id:
        await query.answer("Недоступно"); return
    uid = int(query.data.split(":", 1)[1])
    txt = query.message.text or query.message.caption or ""
    parts = txt.split("\n\n")
    body = ("\n\n".join(parts[1:-1]).strip() if len(parts) >= 3 else "")
    if not body or re.fullmatch(r"\[.+\]", body):
        await query.answer("Нет текста для задачи", show_alert=True); return
    dmc = await _dmc_get(db, uid)
    who = dmc.get("label") or dmc.get("name") or f"id{uid}"
    title = f"{body[:120]} (от {who})"
    from core.actions import create_task_from_suggestion
    try:
        res = await create_task_from_suggestion(
            db, settings, query.message.bot, settings.owner_user_id,
            uid, dmc.get("uname") or who, "BUSINESS",
            {"title": title, "due_date": None, "topic": ""})
    except Exception as exc:
        logger.warning(f"dm→task failed id={uid}: {exc}")
        await query.answer("Не записалось, нажми ещё раз", show_alert=True); return
    if res == "ok":
        await query.answer(f"✅ Завела задачу: {body[:40]}")
    elif res == "dup":
        await query.answer("Уже есть такая задача")
    else:
        await query.answer("Не записалось, нажми ещё раз", show_alert=True)
    logger.info(f"DM→task from id={uid}: {title[:60]!r} ({res})")


async def _relay_stranger_dm(bot: Bot, settings: Settings, message: Message, db=None) -> None:
    """v4.7: чужой человек написал боту в ЛС напрямую → карточка владельцу (релей).
    Бот САМ не отвечает незнакомцу; приём от всех (решение владельца). LLM на чужих НЕ тратим
    (только текст/тип медиа) — дёшево и без абьюза. Ботов игнорируем.
    v4.8: карточка запоминается (relay:{message_id} → user_id), чтобы ОТВЕТ владельца реплаем
    на неё ушёл этому человеку от имени бота (двусторонний канал: он ↔ бот ↔ ты)."""
    import html as _html
    u = message.from_user
    if u.is_bot:
        return
    # #1: заблокированный контакт → не релеим (тихо)
    dmc = await _dmc_get(db, u.id)
    if dmc.get("blocked"):
        logger.info(f"DM from BLOCKED user id={u.id} — dropped")
        return
    # запись/обновление контакта лички (имя/username/времена)
    from core.util import now_local
    _now = now_local().strftime("%Y-%m-%d %H:%M")
    _is_new = not dmc                                   # #4: впервые пишет?
    await _dmc_set(db, u.id, name=u.full_name or "?", uname=u.username or "",
                   first_seen=dmc.get("first_seen") or _now, last_seen=_now)
    body = (message.text or message.caption or "").strip()
    _has_text = bool(body)                             # #1: задачу делаем только из текста
    if not body:
        body = f"[{(_media_kind(message) or 'сообщение').lower()}]"
    label = dmc.get("label") or ""
    disp = _html.escape(label or u.full_name or "?")
    unamepart = _html.escape(f"@{u.username} · ") if u.username else ""
    # id в карточке — обязателен: по нему работает ВЕЧНЫЙ ответ (#3), даже когда привязка ротировалась
    card = (f"📩 <b>Тебе пишут</b>{' 🆕' if _is_new else ''}\n"
            f"От: <b>{disp}</b> ({unamepart}id{u.id})\n\n"
            f"{_html.escape(body)[:3400]}\n\n"
            f"<i>↩️ Ответь реплаем — передам · «/имя текст» — подписать контакт</i>")
    _btns = [InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"dmblock:{u.id}")]
    if _has_text:                                      # #1: «В задачи» — только когда есть текст
        _btns.insert(0, InlineKeyboardButton(text="📋 В задачи", callback_data=f"dmtask:{u.id}"))
    kb = InlineKeyboardMarkup(inline_keyboard=[_btns])
    try:
        from core.topics import send_owner, thread_id
        sent = await send_owner(bot, settings.owner_user_id, "dm", card,
                                parse_mode="HTML", reply_markup=kb)
        _ids = [sent.message_id] if sent else []
        # v4.9/v4.10: медиа (фото/голос/видео/кружок/GIF/аудио/док/стикер) шлём КАК ЕСТЬ под
        # карточкой, в ту же тему (caption уже в карточке → тут без него, чтобы не дублировать).
        media = await _resend_media(bot, message, settings.owner_user_id,
                                    thread_id=thread_id("dm"), with_caption=False)
        if media:
            _ids.append(media.message_id)
        # v4.8: привязка карточка/медиа → автор; реплай на ЛЮБОЕ из них уйдёт этому человеку
        if _ids and db is not None:
            import json as _json
            from db import set_setting
            # храним и имя/@username — чтобы в подтверждении показать, КОМУ ушёл ответ
            _payload = _json.dumps(
                {"id": u.id, "name": u.full_name or "?", "uname": u.username or ""},
                ensure_ascii=False)
            for _mid in _ids:
                await set_setting(db, f"relay:{_mid}", _payload)
            try:    # держим последние 1000 (fast-path для реплая на медиа); старше — резолв по id из карточки (#3)
                await db.execute(
                    "DELETE FROM settings_kv WHERE key LIKE 'relay:%' AND rowid NOT IN "
                    "(SELECT rowid FROM settings_kv WHERE key LIKE 'relay:%' ORDER BY rowid DESC LIMIT 1000)")
                await db.commit()
            except Exception:
                pass
        logger.info(f"Relayed stranger DM to owner from id={u.id} (@{u.username or ''})")
    except Exception as exc:
        logger.warning(f"relay stranger DM failed: {exc}")


# ── главный хендлер лички ─────────────────────────────────────────────────────
@router.message()
async def owner_inbox(message: Message, bot: Bot, db, settings: Settings,
                      anthropic_client: AsyncAnthropic) -> None:
    if not message.from_user:
        return
    # v4.12: ассистент/захват — ТОЛЬКО основной аккаунт. Любой другой (в т.ч. твой второй
    # аккаунт-владелец @starninjaMAN) пишущий боту → релей карточкой, а не админка/захват.
    if message.from_user.id != settings.owner_user_id:
        # чужой/второй аккаунт в ЛС бота → релей владельцу (бот сам не отвечает).
        await _relay_stranger_dm(bot, settings, message, db)
        return
    # v4.37: ожидаем «свою роль» текстом после [✏️ Своя роль…] на карточке нового контакта (10 мин)
    if message.text and not message.text.startswith("/"):
        try:
            import json as _json, time as _time
            from db import get_setting as _gs, set_setting as _ss
            _rw = _json.loads(await _gs(db, f"rolwait:{settings.owner_user_id}") or "null")
            if _rw and _time.time() - (_rw.get("ts") or 0) < 600:
                await _ss(db, f"rolwait:{settings.owner_user_id}", "")
                from core import roles as _roles
                _cat = await _roles.apply_role(db, int(_rw["uid"]), message.text.strip()[:60])
                await message.reply(f"✓ Роль записана: <b>{_html.escape(message.text.strip()[:60])}</b> ({_roles.cat_label(_cat)})",
                                    parse_mode="HTML", **_eff(settings, "like"))
                try:
                    await bot.edit_message_reply_markup(chat_id=_rw["chat_id"], message_id=_rw["msg_id"], reply_markup=None)
                except Exception:
                    pass
                return
        except Exception as exc:
            logger.debug(f"rolwait skip: {exc}")
        # v4.48: «✍️ ответить» из сводки — текст владельца уходит контакту от его имени (Business API)
        try:
            import json as _json, time as _time
            from db import get_setting as _gs, set_setting as _ss
            _rp = _json.loads(await _gs(db, f"repwait:{settings.owner_user_id}") or "null")
            if _rp and _time.time() - (_rp.get("ts") or 0) < 600:
                await _ss(db, f"repwait:{settings.owner_user_id}", "")
                _bc = await _gs(db, "bc_id")
                _txt = message.text.strip()
                try:
                    await bot.send_message(chat_id=int(_rp["uid"]), text=_txt, business_connection_id=_bc, parse_mode=None)
                    try:
                        from db import vault_add
                        await vault_add(db, int(_rp["uid"]), _rp.get("username") or f"id{_rp['uid']}", "out", _txt, None)
                    except Exception:
                        pass
                    await message.reply(f"📤 Отправлено {_html.escape(str(_rp.get('who') or ''))}", parse_mode="HTML", **_eff(settings, "like"))
                except Exception as _se:
                    if "BUSINESS_PEER_USAGE_MISSING" in str(_se):
                        from aiogram.types import CopyTextButton, InlineKeyboardMarkup as _IKM, InlineKeyboardButton as _IKB
                        await message.reply("⚠️ Telegram не даёт боту писать этому контакту (давно не писал тебе). Скопируй и отправь сам:",
                                            reply_markup=_IKM(inline_keyboard=[[_IKB(text="📋 Скопировать текст", copy_text=CopyTextButton(text=_txt[:256]))]]))
                    else:
                        await message.reply(f"⚠️ Не отправила: {_html.escape(str(_se)[:150])}", parse_mode="HTML")
                return
        except Exception as exc:
            logger.debug(f"repwait skip: {exc}")
    # v4.8: владелец ответил РЕПЛАЕМ на карточку «Пишут боту напрямую» → передать человеку
    # от имени бота. Срабатывает ТОЛЬКО если для этого сообщения есть привязка relay:* —
    # обычные реплаи владельца не перехватываем.
    if message.reply_to_message:
        _uid = await _resolve_dm_target(db, message.reply_to_message)   # #3 вечный резолв
        if _uid:
            _dmc = await _dmc_get(db, _uid)
            _un = _dmc.get("uname") or ""
            _name = _dmc.get("label") or _dmc.get("name") or "?"
            _who = _html.escape(f"{_name}" + (f" (@{_un})" if _un else f" (id{_uid})"))  # v4.33: имя контакта → HTML-safe
            # #2: назвать контакт — реплай «/имя <текст>» (не уходит человеку)
            _lm = _DM_LABEL_RE.match((message.text or "").strip())
            if _lm:
                await _dmc_set(db, _uid, label=_lm.group(1).strip()[:60])
                await message.reply(f"🏷 Метка сохранена: <b>{_lm.group(1).strip()[:60]}</b> (id{_uid})",
                                    parse_mode="HTML")
                return
            # v4.9/v4.10: ответить можно любым медиа (фото/голос/видео/кружок/GIF/аудио/док/стикер)
            _kind = _media_kind(message)
            if _kind:
                res = await _resend_media(bot, message, _uid, with_caption=True)
                if res:
                    await message.reply(f"✅ Передала ({_kind.lower()}) → {_who}")
                    logger.info(f"Owner replied ({_kind}) via bot to stranger id={_uid}")
                else:
                    await message.reply(f"❌ Не смогла доставить ({_who}) — возможно, он заблокировал бота")
                return
            _txt = (message.text or message.caption or "").strip()
            if not _txt:
                await message.reply("↩️ Отправь текст или медиа")
                return
            try:
                await bot.send_message(_uid, _txt, parse_mode=None)   # v4.33: текст владельца «как есть», без HTML-парсинга
                await message.reply(f"✅ Передала → {_who}")
                logger.info(f"Owner replied via bot to stranger id={_uid} ({_un or _name})")
            except Exception as exc:
                await message.reply(f"❌ Не смогла доставить ({_who}) — возможно, он заблокировал бота")
                logger.warning(f"relay owner→stranger failed (id={_uid}): {exc}")
            return
    if message.text and message.text.startswith("/"):
        return  # слэш-команда — не наш кейс (ловит commands.router)

    # v4.57 соцсети: CSV-выгрузка TikTok Studio / Meta Business Suite → в статистику (API этих площадок закрыты)
    if message.document and str(message.document.file_name or "").lower().endswith((".csv", ".tsv", ".txt")):
        try:
            from core import social as _social
            _fn = message.document.file_name or ""
            _plat = _social.detect_platform(_fn, message.caption or "")
            if _plat:
                _fi = await bot.get_file(message.document.file_id)
                _raw = (await bot.download_file(_fi.file_path)).read()
                try:
                    _txt = _raw.decode("utf-8")
                except UnicodeDecodeError:
                    _txt = _raw.decode("utf-16", errors="ignore") if _raw[:2] in (b"\xff\xfe", b"\xfe\xff") else _raw.decode("cp1251", errors="ignore")
                _res = await _social.import_csv(db, _plat, _txt, _fn)
                if _res.get("rows"):
                    await message.reply(f"📊 {_plat}: записала {_res['rows']} значений за {_res['days']} дн. ({_res['from']} → {_res['to']}), метрики: {', '.join(_res['metrics'])}"
                                        + (f"\nНе распознала колонки: {', '.join(_res['unknown'][:6])}" if _res.get("unknown") else ""), parse_mode=None)
                else:
                    await message.reply("📊 Не нашла в файле строк «дата + числа». Пришли выгрузку из TikTok Studio (Analytics → Download data) или Meta Business Suite (Insights → Export)"
                                        + (f"; колонки: {', '.join(_res.get('unknown', [])[:8])}" if _res.get("unknown") else ""), parse_mode=None)
                return
            await message.reply("📊 Это выгрузка TikTok или Instagram? Пришли файл ещё раз с подписью «tiktok» или «instagram».", parse_mode=None)
            return
        except Exception as exc:
            logger.warning(f"social csv import failed: {exc}")
    text = await _gather_text(bot, message, settings, anthropic_client, db=db)
    if not text or len(text.strip()) < 2:
        return

    # Перехват ручного переноса: владелец нажал «✏️ Своя дата/время» → ждём его дату/время.
    from db import get_setting, set_setting
    pend = await get_setting(db, "reschedule_await")
    if pend:
        await set_setting(db, "reschedule_await", "")  # потребляем (один ввод)
        try:
            sid_s, ts_s = pend.split(":", 1)
            from datetime import datetime, timezone
            fresh = (datetime.now(timezone.utc) - datetime.fromisoformat(ts_s)).total_seconds() < 900  # 15 мин
        except Exception:
            sid_s, fresh = pend, True
        if fresh and not (message.text and message.text.startswith("/")):
            low = text.strip().lower()
            if low in ("отмена", "cancel", "отменить"):
                await message.reply("Ок, перенос отменён.")
                return
            due = await _parse_due_text(anthropic_client, settings.anthropic_model_smart, text)
            if not due:
                await message.reply("Не понял дату/время. Нажми «📅 Перенести» → «✏️ Своя дата/время» ещё раз "
                                    "и напиши, напр. «завтра 14:00» или «18 июня 13:00».")
                return
            applied = await _apply_reschedule(db, settings, int(sid_s), due)
            if applied:
                from core.util import html_due
                await message.reply(f"📅 Перенесено на {html_due(applied)}", **_eff(settings))
            else:
                await message.reply("Задача не найдена — возможно, уже закрыта.")
            return

    # Обучение черновиков (path A): после кнопки «✍️ Отвечу сам» владелец прислал свой
    # вариант ответа боту в личку → запоминаем как пример стиля (перехват ДО роутинга интента).
    from core import drafts
    teach = drafts.pop_teach_pm(settings.owner_user_id)
    if teach and not (message.text and message.text.startswith("/")):
        from db import save_draft_example
        await save_draft_example(db, teach.get("user_id"), teach.get("incoming") or "", text)
        uname = teach.get("username") or ""
        await message.reply(
            f"✅ Запомнил твой стиль — учту в будущих черновиках"
            + (f" для @{uname} и похожих." if uname else "."), **_eff(settings, "celebrate"))
        return

    # сохраняем любые ссылки из присланного/пересланного (форвард статьи → ссылка не потеряется)
    urls = _LINK_RE.findall(text)
    if urls:
        try:
            from db import save_links
            await save_links(db, settings.owner_user_id, "сам", urls, text[:300], "SELF")
        except Exception as _e:
            logger.debug(f"owner save_links skip: {_e}")

    # v4.44 «Мира-чат 2.0» (флаг MIRA_V2): единый разговорный цикл с инструментами (Ф1–Ф4).
    # Пересланное от ЧЕЛОВЕКА — тоже в диалог (разбор с память автора); пост из КАНАЛА — старый capture (контент).
    if getattr(settings, "mira_v2", False):
        from core import mira as _mira
        if _mira.forward_meta(message).get("kind") != "channel":
            if await _mira.converse(message, bot, db, settings, anthropic_client, text):
                return
    _prev = await _dialog_route_ctx(db)   # v4.41/4.43: предыдущий обмен + давность — для уточнений и продолжения
    data = await _route(anthropic_client, settings.anthropic_model_smart, text, prev=_prev)
    intent = (data.get("intent") or "capture").lower()

    # Пересланный боту пост/сообщение — это КОНТЕНТ на сохранение, а не вопрос к памяти
    # и не запрос расписания. Никогда не уводим форвард в ask/query/freetime
    # (баг: на пересланный пост из канала бот отвечал «в переписке ничего не нашёл 🤔»).
    is_forwarded = bool(getattr(message, "forward_origin", None))
    if is_forwarded and intent in ("ask", "query", "freetime"):
        intent = "capture"

    if intent == "query":
        await _do_query(message, settings, data, db=db)
    elif intent == "manage":
        await _do_manage(message, db, settings, data)
    elif intent == "ask":
        await _do_ask(message, db, settings, anthropic_client, text)
    elif intent == "freetime":
        await _do_freetime(message, settings, data)
    elif intent == "chat":
        await _do_chat(message, db, settings, anthropic_client, text)
    elif intent == "bday":
        await _do_bday(message, db, settings, data)
    else:
        await _do_capture(message, settings, data, text, is_forwarded=is_forwarded, db=db)


# ── v4.41: память диалога с Мирой (settings_kv «mira_dialog») ────────────────
_DIALOG_KEY, _DIALOG_TTL, _DIALOG_MAX = "mira_dialog", 3 * 3600, 8


async def _dialog_get(db) -> list:
    try:
        import json as _j
        from db import get_setting
        items = _j.loads(await get_setting(db, _DIALOG_KEY) or "[]")
        now = time.time()
        return [i for i in items if now - i.get("ts", 0) < _DIALOG_TTL][-_DIALOG_MAX:]
    except Exception:
        return []


async def _dialog_push(db, r1: str, t1: str, r2: str, t2: str) -> None:
    """Запомнить пару реплик (владелец → Мира), чтобы работали уточнения «а завтра?», «а он?»."""
    try:
        import json as _j
        from db import set_setting
        items = await _dialog_get(db)
        items.append({"ts": time.time(), "q": (t1 or "")[:300], "a": (t2 or "")[:500]})
        await set_setting(db, _DIALOG_KEY, _j.dumps(items[-_DIALOG_MAX:], ensure_ascii=False))
    except Exception as exc:
        logger.debug(f"dialog push skip: {exc}")


async def _dialog_text(db) -> str:
    items = await _dialog_get(db)
    if not items:
        return "(пусто)"
    return "\n".join(f"Владелец: {i['q']}\nМира: {i['a']}" for i in items[-5:])


async def _dialog_last_owner(db) -> str:
    items = await _dialog_get(db)
    return items[-1]["q"] if items else ""


async def _dialog_route_ctx(db) -> str:
    """Для роутера: последний обмен + сколько минут назад (диалог «активен», если < 15 мин)."""
    items = await _dialog_get(db)
    if not items:
        return "нет"
    last = items[-1]
    mins = int((time.time() - last.get("ts", 0)) // 60)
    state = "ДИАЛОГ АКТИВЕН" if mins < 15 else "давно"
    return f"{state} ({mins} мин назад). Владелец: «{last['q'][:200]}» → Ассистент: «{last['a'][:250]}»"


# ── chat: разговор с Мирой (v4.17, слой 2 персоны) ───────────────────────────
MIRA_CHAT_SYSTEM = """Ты — Мира, личный ассистент владельца. Говоришь от первого лица женского рода, тепло и по-деловому, по-русски, кратко (до ~120 слов). Владелец — {owner_name}, обращайся на «ты» и зови его «босс» (не по имени).
Владелец — МУЖЧИНА (о нём/к нему в мужском роде: «ты говорил», «ты работаешь»). В памяти факты с датой в квадратных скобках ([08.09] …) — САМЫЕ СВЕЖИЕ, они перекрывают более старые утверждения (устроился/не устроился и т.п.). Отвечай как живой человек-ассистент, а не справочник: связно, по делу, 1–5 предложений, без списков-простыней и канцелярита, можно одно эмодзи. Учитывай НЕДАВНИЙ ДИАЛОГ (короткие уточнения относятся к нему). Отвечай ТОЛЬКО на основе данных ниже. Не выдумывай задач, дат и фактов; чего в данных нет — честно скажи. Про себя: ты ведёшь его входящие, задачи (Todoist+календарь), обещания и памяти контактов; команды — /help.
Верни СТРОГО JSON без markdown: {{"reply": "твой ответ владельцу — живой текст без markdown, эмодзи умеренно", "remember": ["0-3 НОВЫХ или ИЗМЕНИВШИХСЯ факта о САМОМ владельце из его сообщения (работа/учёба/планы/статус) — кратко; [] если нет"], "suggest_task": {{"title": "дело от лица владельца", "due_date": "YYYY-MM-DD или YYYY-MM-DDTHH:MM или null"}} или null — ТОЛЬКО если в сообщении есть реальное будущее дело/встреча (собеседование, созвон, сдать, купить) и его ещё нет в ОТКРЫТЫХ ЗАДАЧАХ; иначе null}}"""


async def _chat_answer(db, settings: Settings, client: AsyncAnthropic, text: str) -> str:
    """Собрать контекст (открытые задачи, обещания, память упомянутых людей) и ответить как Мира."""
    from db import get_open_commitments
    lines = []
    # v4.41: короткая память владельца + недавний диалог с Мирой
    try:
        async with db.execute("SELECT facts FROM contact_profiles WHERE user_id=?", (settings.owner_user_id,)) as cur:
            _orow = await cur.fetchone()
        if _orow and _orow[0]:
            lines.append(f"ПРО ВЛАДЕЛЬЦА ({settings.owner_name}, мужчина): {_orow[0][:1500]}")
        lines.append("НЕДАВНИЙ ДИАЛОГ:\n" + await _dialog_text(db))
    except Exception as exc:
        logger.debug(f"chat owner contact_memory skip: {exc}")
    # открытые задачи (все контакты) + карта username
    unames = {}
    async with db.execute(
            "SELECT user_id, username, name FROM contact_profiles") as cur:
        for uid, un, nm in await cur.fetchall():
            unames[uid] = un or nm or str(uid)
    async with db.execute(
            "SELECT task_title, due_date, user_id FROM task_sync WHERE synced=0 "
            "ORDER BY due_date IS NULL, due_date LIMIT 25") as cur:
        rows = await cur.fetchall()
    if rows:
        lines.append("ОТКРЫТЫЕ ЗАДАЧИ:")
        for title, due, uid in rows:
            lines.append(f"- {title}" + (f" · до {due}" if due else "") + f" · @{unames.get(uid, uid)}")
    commits = await get_open_commitments(db)
    if commits:
        lines.append("ОБЕЩАНИЯ ВЛАДЕЛЬЦА:")
        for c in commits[:8]:
            who = c.get("name") or (f"@{c['username']}" if c.get("username") else "?")
            lines.append(f"- {who}: {(c.get('commitments') or '')[:120]}")
    # память упомянутых контактов (по @username или имени в тексте)
    low = text.lower()
    async with db.execute(
            "SELECT username, name, relationship, facts FROM contact_profiles "
            "WHERE facts IS NOT NULL AND length(facts) > 0") as cur:
        for un, nm, rel, facts in await cur.fetchall():
            hit = (un and f"@{un}".lower() in low) or (un and un.lower() in low) or \
                  (nm and len(nm) >= 3 and nm.split()[0].lower() in low)
            if hit:
                lines.append(f"ПАМЯТЬ {nm or ''} (@{un}, {rel or '—'}): {facts[:2500]}")  # v4.32: 1200→2500
                if sum(1 for l in lines if l.startswith("ПАМЯТЬ")) >= 2:
                    break
    # 🎂 v4.30: известные дни рождения — чтобы «когда у Вани др?» отвечалось из памяти
    try:
        from core.dates import all_bdays, fmt_mmdd
        bds = await all_bdays(db)
        if bds:
            lines.append("ДНИ РОЖДЕНИЯ: " + "; ".join(
                f"{b['who']} — {fmt_mmdd(b['mmdd'])}" + (f".{b['year']}" if b.get("year") else "")
                for b in bds[:20]))
    except Exception as exc:
        logger.debug(f"chat bdays skip: {exc}")
    # 🎓 v4.31: пары сегодня/завтра — «сколько пар завтра?», «когда первая пара?»
    try:
        from datetime import timedelta as _td
        from core import schedule as _sched
        from core.util import today_local as _tl
        for label, dd in (("сегодня", _tl()), ("завтра", _tl() + _td(days=1))):
            pls = _sched.digest_lines(dd)
            lines.append(f"ПАРЫ {label.upper()} ({dd.strftime('%d.%m')}): "
                         + ("; ".join(pls) if pls else "нет"))
    except Exception as exc:
        logger.debug(f"chat schedule skip: {exc}")
    ctx = "\n".join(lines) or "(данных нет)"
    resp = await llm.create(client,
        model=settings.anthropic_model_smart, max_tokens=500,
        system=MIRA_CHAT_SYSTEM.format(owner_name=settings.owner_name),
        messages=[{"role": "user", "content":
                   f"Сегодня: {today_local_iso()}.\n\n{ctx}\n\nСообщение владельца: {text[:800]}"}])
    await _rec(settings.anthropic_model_smart, resp.usage, "chat")
    out = (_llm_text(resp) or "").strip()
    try:
        data = loads_loose(out)          # v4.43.1: и в ```json-обёртке тоже (раньше ждали «{» в начале → сырой JSON владельцу)
    except Exception:
        data = None
    if not isinstance(data, dict) or not data.get("reply"):
        clean = re.sub(r"^```\w*\s*|\s*```$", "", out.strip())
        data = {"reply": clean, "remember": [], "suggest_task": None}
    # модель иногда игнорирует «без markdown» — звёздочки в Telegram покажутся как есть
    data["reply"] = str(data.get("reply") or "").replace("**", "").replace("__", "")
    return data


async def _do_chat(message: Message, db, settings: Settings,
                   client: AsyncAnthropic, text: str) -> None:
    try:
        data = await _chat_answer(db, settings, client, text)
    except Exception as exc:
        logger.warning(f"mira chat failed: {exc}")
        await message.reply("Не получилось ответить — попробуй ещё раз чуть позже 🙈")
        return
    answer = data.get("reply") or "…"
    await _dialog_push(db, "owner", text, "mira", answer)
    await message.reply(answer[:4000], parse_mode=None)
    # v4.43: новое о владельце → сразу в памяти
    try:
        if data.get("remember"):
            from core.vault import remember_owner_facts
            n = await remember_owner_facts(db, data["remember"])
            if n:
                await message.reply("📝 Запомнила: " + "; ".join(str(f)[:80] for f in data["remember"][:3]), parse_mode=None)
    except Exception as exc:
        logger.debug(f"chat remember skip: {exc}")
    # v4.43: дело внутри разговора → ПРЕДЛОЖЕНИЕ кнопкой, не молчаливая карточка вместо ответа
    try:
        st = data.get("suggest_task")
        if isinstance(st, dict) and (st.get("title") or "").strip():
            await _offer_task_card(message, settings, db, st["title"].strip(), st.get("due_date"))
    except Exception as exc:
        logger.debug(f"chat suggest skip: {exc}")


async def _offer_task_card(message: Message, settings: Settings, db, title: str, due_raw) -> None:
    """Мини-карточка «💡 Записать задачу?» — тот же cap:-flow, что у capture."""
    from core.actions import _normalize_due
    from core.util import html_due
    due = _normalize_due(due_raw) if due_raw else None
    tasks = [{"title": title[:120], "due": due, "is_meeting": False}]
    _prune_pending()
    token = __import__('secrets').token_hex(4)
    _pending[token] = {"tasks": tasks, "text": (message.text or "")[:1000], "ts": time.monotonic()}
    if db:
        try:
            import json as _json
            from datetime import datetime as _dt, timezone as _tz
            from db import set_setting
            await set_setting(db, f"cap:{token}", _json.dumps(
                {"tasks": tasks, "text": (message.text or "")[:1000], "ts": _dt.now(_tz.utc).isoformat()}, ensure_ascii=False))
        except Exception:
            pass
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Записать", callback_data=f"cap:{token}:ok", style="success"),
        InlineKeyboardButton(text="✖ Не надо", callback_data=f"cap:{token}:no", style="danger")]])
    await message.reply(f"💡 Записать задачу?\n📌 {_html.escape(title[:120])}" + (f" — {html_due(due)}" if due else ""),
                        reply_markup=kb, parse_mode="HTML")


# ── bday: запомнить день рождения (v4.30) ────────────────────────────────────
async def _do_bday(message: Message, db, settings: Settings, data: dict) -> None:
    from core.dates import fmt_mmdd, norm_bdate, save_bday
    person = (data.get("person") or "").strip().lstrip("@")
    mmdd, year = norm_bdate(str(data.get("bdate") or ""))
    if not person or not mmdd:
        await message.reply("Не разобрала, у кого и когда др 🙈 Скажи, например: «у Вани день рождения 12 мая»")
        return
    # привязка к контакту: имя/username → uid (чтобы кнопка поздравления могла отправить в чат)
    uid, who = None, person
    low = person.lower()
    async with db.execute("SELECT user_id, username, name FROM contact_profiles") as cur:
        for cuid, un, nm in await cur.fetchall():
            if (un and un.lower() == low) or \
               (nm and len(low) >= 3 and nm.lower().split()[0].startswith(low[:6])):
                uid, who = cuid, (nm or f"@{un}")
                break
    await save_bday(db, who, mmdd, year=year, uid=uid)
    tail = f" ({year} г.)" if year else ""
    link = " — нашла его в контактах, в день Х предложу поздравление 📨" if uid else ""
    await message.reply(f"Запомнила: 🎂 {who} — {fmt_mmdd(mmdd)}{tail}.{link}"
                        f"\nНапомню накануне и утром в сводке.", **_eff(settings, "like"))


# ── ask: вопрос к памяти (RAG Q&A) ───────────────────────────────────────────
ASK_SYSTEM = """Ты — Мира, память владельца (говоришь о себе в ЖЕНСКОМ роде: «нашла», «не нашла», «помню»). Владелец — {owner_name}, МУЖЧИНА: о нём и к нему — в мужском роде («ты говорил», «ты работаешь»); «Ты:» во фрагментах — это он. Вопросы «где я работаю / что я …» — про владельца: отвечай по ПАМЯТЬ ВЛАДЕЛЬЦА; факты с датой в скобках ([08.09] …) — самые свежие и перекрывают старые. Отвечай как живой человек-ассистент: связно, 1–6 предложений, без списков-простыней и канцелярита; учитывай НЕДАВНИЙ ДИАЛОГ (уточнения «а он?», «а завтра?» относятся к нему). Отвечай на его вопрос ТОЛЬКО по приведённым данным: ПАМЯТЬ контактов (накопленная структурная память — приоритетный источник для вопросов «кто/где работает/чем занимается») и фрагменты переписки (цитаты — для «кто что говорил/когда»). Связно, по делу, по-русски, без markdown-звёздочек. Где уместно — указывай, КТО и что говорил. Если ни в памяти, ни во фрагментах ответа нет — честно скажи «в памяти не нашла». Не выдумывай."""


async def _fts_fallback_hits(db, question: str, k: int = 6) -> list:
    """Фолбэк, когда RAG выключен: полнотекстовый поиск по ключевым словам вопроса (как /find).
    Возвращает hits в форме retrieve_global ({who, text}) — чтобы переиспользовать ASK-флоу."""
    import re as _re
    from db import search_conversations
    words = _re.findall(r"\w{4,}", (question or "").lower())[:6]
    seen, hits = set(), []
    for w in words:
        for r in await search_conversations(db, w, limit=10):
            key = (r.get("user_id"), (r.get("text") or "")[:60])
            if key in seen:
                continue
            seen.add(key)
            who = "Ты" if r.get("direction") == "out" else f"@{r.get('username') or '?'}"
            hits.append({"who": who, "text": (r.get("text") or "")[:200]})
            if len(hits) >= k:
                return hits
    return hits


async def _do_ask(message: Message, db, settings: Settings,
                  client: AsyncAnthropic, question: str) -> None:
    if settings.enable_rag:
        from core.embeddings import retrieve_global
        hits = await retrieve_global(db, question, k=6)
    else:
        hits = await _fts_fallback_hits(db, question, k=6)  # RAG off → полнотекстовый фолбэк (не глухой отказ)
    # v4.39: ПАМЯТЬ упомянутых контактов — раньше ask смотрел только во фрагменты переписки (RAG),
    # а структурная память («Ника учится в X, работает в Y») лежала рядом и не использовалась.
    contact_memory_lines = []
    try:
        low = question.lower()
        # v4.41: память САМОГО владельца («где я работаю», «что у меня…») — раньше не попадало вовсе
        _about_me = bool(re.search(r"\b(я|мне|меня|мой|моя|мои|моё|сам|себе|у меня|про меня)\b", low))
        async with db.execute("SELECT facts FROM contact_profiles WHERE user_id=?", (settings.owner_user_id,)) as cur:
            _row = await cur.fetchone()
        if _row and _row[0]:
            contact_memory_lines.append(f"ПАМЯТЬ ВЛАДЕЛЬЦА ({settings.owner_name}, мужчина — тот, кто спрашивает): "
                                 f"{_row[0][:2500 if _about_me else 900]}")
        async with db.execute(
                "SELECT username, name, relationship, facts FROM contact_profiles "
                "WHERE facts IS NOT NULL AND length(facts) > 0") as cur:
            for un, nm, rel, facts in await cur.fetchall():
                first = (nm or "").split()[0].lower() if (nm or "").strip() else ""
                hit = (un and f"@{un}".lower() in low) or (un and un.lower() in low) or \
                      (first and len(first) >= 3 and (first in low or first[:-1] in low))   # «Ника» / «Ники»
                if hit:
                    contact_memory_lines.append(f"ПАМЯТЬ {nm or ''} (@{un}, {rel or '—'}): {facts[:2500]}")
                    if len(contact_memory_lines) >= 2:
                        break
    except Exception as exc:
        logger.debug(f"ask contact_memory skip: {exc}")
    if not hits and not contact_memory_lines:
        await message.reply("В памяти по этому вопросу ничего не нашла 🤔"); return
    snippets = "\n".join(f"[{h['who']}]: {h['text']}" for h in hits)
    if contact_memory_lines:
        snippets = "\n".join(contact_memory_lines) + "\n\nФрагменты переписки:\n" + snippets
    try:
        # Стриминг ответа черновиком: rich-вариант (sendRichMessageDraft, 10.1) с живым
        # блоком <tg-thinking>; не взлетел → plain sendMessageDraft (9.3). Черновик
        # эфемерный — в конце обязателен обычный send.
        import html as _h
        import time as _time
        from core import richmsg
        draft_id = int(_time.time()) % 2_000_000_000 or 1
        buf, last_push, use_rich, _stopped = "", 0.0, True, False
        # Сразу показать «думаю» ещё до первого токена
        use_rich = await richmsg.send_rich_draft(
            settings.bot_token, message.chat.id, draft_id,
            "<tg-thinking>🧠 ищу в памяти…</tg-thinking>", message.message_thread_id, can_stop=True)
        async with client.messages.stream(
            model=settings.anthropic_model_smart, max_tokens=400, system=ASK_SYSTEM.format(owner_name=settings.owner_name),
            messages=[{"role": "user", "content":
                f"НЕДАВНИЙ ДИАЛОГ с владельцем:\n{await _dialog_text(db)}\n\nВопрос: {question}\n\nДанные памяти (память + релевантные фрагменты):\n{snippets}"}],
        ) as stream:
            async for chunk in stream.text_stream:
                buf += chunk
                if draft_id in _STOPPED:          # B8: ⏹ нажат — обрываем, оставляем что успели
                    _STOPPED.discard(draft_id)
                    _stopped = True
                    break
                now = _time.monotonic()
                if now - last_push >= 0.6 and buf.strip():
                    last_push = now
                    try:
                        if use_rich:
                            ok = await richmsg.send_rich_draft(
                                settings.bot_token, message.chat.id, draft_id,
                                f"<p>🧠 {_h.escape(buf[:3500])}</p>"
                                f"<tg-thinking>печатаю…</tg-thinking>",
                                message.message_thread_id, can_stop=True)
                            if not ok:
                                use_rich = False
                        if not use_rich:
                            await message.bot.send_message_draft(
                                chat_id=message.chat.id, draft_id=draft_id,
                                text=f"🧠 {buf[:3900]}", parse_mode=None, can_stop=True)
                    except Exception:
                        pass  # стриминг — украшение; финальный ответ уйдёт в любом случае
            resp = None if _stopped else await stream.get_final_message()
        if resp is not None:
            await _rec(settings.anthropic_model_smart, resp.usage, "ask")
            answer = _llm_text(resp).strip()
        else:
            answer = (buf.strip() or "…") + "\n\n⏹ остановлено"
    except Exception as exc:
        logger.error(f"ask failed: {exc}")
        await message.reply("Не смогла обработать вопрос, попробуй переформулировать."); return
    answer = answer.replace('**', '').replace('__', '')
    await _dialog_push(db, "owner", question, "mira", answer)
    await message.reply(f"🧠 {answer}", parse_mode=None)   # v4.39: без markdown-мусора и HTML-падений


# ── freetime: найти свободные окна в календаре ───────────────────────────────
_WD_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


async def _do_freetime(message: Message, settings: Settings, data: dict) -> None:
    if not settings.google_calendar_id:
        await message.reply("Google Calendar не настроен — не могу искать свободное время."); return
    from datetime import datetime, time as dtime, timedelta as _td
    from zoneinfo import ZoneInfo
    from collections import OrderedDict
    from core.util import now_local
    from core.calendar import get_busy_intervals, compute_free_slots

    period = (data.get("period") or "today").lower()
    if period not in ("today", "tomorrow", "week"):
        period = "today"
    try:
        duration = int(data.get("duration_min") or 60)
    except (TypeError, ValueError):
        duration = 60
    duration = max(15, min(duration, 480))

    tz = ZoneInfo(settings.timezone)
    now = now_local()
    ws_h, we_h = settings.work_start_hour, settings.work_end_hour
    today = now.date()
    if period == "tomorrow":
        d = today + _td(days=1)
        window_start = datetime.combine(d, dtime(0, 0), tzinfo=tz)
        window_end = datetime.combine(d, dtime(23, 59), tzinfo=tz)
        head = "🗓 <b>Свободно завтра</b>"
    elif period == "week":
        window_start = now
        window_end = now + _td(days=7)
        head = "🗓 <b>Свободно на неделе</b>"
    else:  # today
        window_start = now
        window_end = datetime.combine(today, dtime(23, 59), tzinfo=tz)
        head = "🗓 <b>Свободно сегодня</b>"

    try:
        busy = await get_busy_intervals(
            settings.google_service_account_path, settings.google_calendar_id,
            window_start.isoformat(), window_end.isoformat())
    except Exception as exc:
        logger.error(f"freetime busy fetch failed: {exc}")
        await message.reply("Не смог получить календарь, попробуй позже."); return

    slots = compute_free_slots(busy, window_start, window_end, ws_h, we_h, duration)
    head += f" (окна ≥ {duration} мин, рабочие часы {ws_h:02d}:00–{we_h:02d}:00)"
    if not slots:
        await message.reply(f"{head}\n\nСвободных окон нет 😬", parse_mode="HTML"); return

    by_day: "OrderedDict" = OrderedDict()
    for s, e in slots:
        by_day.setdefault(s.date(), []).append((s, e))

    lines = [head, ""]
    shown = 0
    for d, day_slots in by_day.items():
        if period == "week":
            lines.append(f"<b>{d.strftime('%d.%m')} {_WD_RU[d.weekday()]}</b>")
        for s, e in day_slots:
            mins = int((e - s).total_seconds() // 60)
            lines.append(f"• {s.strftime('%H:%M')}–{e.strftime('%H:%M')} <i>({mins} мин)</i>")
            shown += 1
            if shown >= 30:
                break
        if shown >= 30:
            lines.append("…")
            break
    rich_ok = False
    try:
        from core import richmsg
        rows_t, cnt = [], 0
        for d, day_slots in by_day.items():
            for s_, e_ in day_slots:
                mins = int((e_ - s_).total_seconds() // 60)
                rows_t.append([f"{d.strftime('%d.%m')} {_WD_RU[d.weekday()]}",
                               f"{s_.strftime('%H:%M')}–{e_.strftime('%H:%M')}", f"{mins} мин"])
                cnt += 1
                if cnt >= 30:
                    break
            if cnt >= 30:
                break
        doc = (f"<h3>🗓 Свободные окна</h3>"
               f"<p>≥ {duration} мин · рабочие часы {ws_h:02d}:00–{we_h:02d}:00</p>"
               + richmsg.table(["День", "Окно", "Длительность"], rows_t))
        rich_ok = await richmsg.try_rich(message, doc)
    except Exception as exc:
        logger.debug(f"rich freetime skip: {exc}")
    if not rich_ok:
        await message.reply("\n".join(lines), parse_mode="HTML")


# ── capture: показать карточку-подтверждение ─────────────────────────────────
async def _do_capture(message: Message, settings: Settings, data: dict, src_text: str,
                      is_forwarded: bool = False, db=None) -> None:
    from core.actions import _normalize_due
    tasks = []
    for t in (data.get("tasks") or []):
        title = (t.get("title") or "").strip()
        if title:
            tasks.append({"title": title, "due": _normalize_due(t.get("due_date")),
                          "is_meeting": bool(t.get("is_meeting"))})
    # Форвард в self-канал: один пересланный пост/док не должен взрываться в N задач
    # (кейс «18 задач из одного changelog»). Оставляем 1 — остальное уже в инбоксе/ссылках.
    if is_forwarded and len(tasks) > 1:
        tasks = tasks[:1]
    if not tasks:
        if is_forwarded:
            # форвард без явной задачи — это просто контент в инбокс (ссылка уже сохранена выше)
            await message.reply("📎 Принял, сохранил в инбокс. Нужна задача по нему — напиши, "
                                "что сделать (напр. «посмотреть это на выходных»).")
        else:
            await message.reply("Не понял, что за задача 🤔 Напр.: «купить молоко», «встреча завтра в 20».")
        return

    # анти-конфликт: для встреч со временем проверяем пересечение в календаре
    if settings.google_calendar_id:
        try:
            from core.calendar import check_conflict
            for t in tasks:
                if t["is_meeting"] and t["due"] and "T" in t["due"]:
                    t["conflict"] = await check_conflict(
                        settings.google_service_account_path, settings.google_calendar_id,
                        settings.timezone, t["due"])
        except Exception as _e:
            logger.debug(f"conflict check skip: {_e}")

    _prune_pending()
    token = __import__('secrets').token_hex(4)   # v4.36 (B2): случайный токен — счётчик с 1 после рестарта путал карточки
    _pending[token] = {"tasks": tasks, "text": src_text[:1000], "ts": time.monotonic()}
    # Персист карточки в settings_kv — чтобы пережила рестарт/деплой (in-memory иначе теряется).
    if db:
        try:
            import json as _json
            from datetime import datetime as _dt, timezone as _tz
            from db import set_setting
            await set_setting(db, f"cap:{token}", _json.dumps(
                {"tasks": tasks, "text": src_text[:1000], "ts": _dt.now(_tz.utc).isoformat()},
                ensure_ascii=False))
        except Exception as _e:
            logger.debug(f"capture persist skip: {_e}")

    from core.util import html_due
    lines = []
    for t in tasks:
        due = f" — {html_due(t['due'])}" if t["due"] else ""
        meet = " 🗓" if (t["is_meeting"] and t["due"] and "T" in t["due"]) else ""
        conflict = f"\n   ⚠️ пересекается с «{t['conflict']}»" if t.get("conflict") else ""
        lines.append(f"📌 {t['title']}{due}{meet}{conflict}")
    head = "🆕 <b>Записала задачу</b>" if len(tasks) == 1 else f"🆕 <b>Записала задачи ({len(tasks)})</b>"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Создать" + ("" if len(tasks) == 1 else " все"), callback_data=f"cap:{token}:ok", style="success"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"cap:{token}:no", style="danger"),
    ]])
    await message.reply(head + "\n" + "\n".join(lines), reply_markup=kb, parse_mode="HTML")


@router.callback_query(lambda c: c.data and c.data.startswith("cap:"))
async def cb_capture(query: CallbackQuery, db, settings: Settings) -> None:
    from core.owners import is_owner as _is_owner
    if not _is_owner(settings.owner_user_id, query.from_user.id):
        await query.answer("Недоступно"); return
    _, token, action = query.data.split(":")
    item = _pending.pop(token, None)
    if not item:
        # Фолбэк: карточка пережила рестарт (in-memory очистилось) — восстановим из settings_kv
        try:
            import json as _json
            from datetime import datetime as _dt, timezone as _tz
            from db import get_setting
            raw = await get_setting(db, f"cap:{token}")
            if raw:
                cand = _json.loads(raw)
                ts = cand.get("ts")
                fresh = True if not ts else (
                    (_dt.now(_tz.utc) - _dt.fromisoformat(ts)).total_seconds() < 6 * 3600)
                if fresh:
                    item = cand
        except Exception as _e:
            logger.debug(f"capture restore skip: {_e}")
    if not item:
        await query.answer("Карточка устарела"); return
    if db:  # карточка использована — снять персист
        try:
            from db import set_setting
            await set_setting(db, f"cap:{token}", "")
        except Exception:
            pass
    if action == "no":
        await query.answer("Отменила")
        try:
            await query.message.edit_text(query.message.html_text + "\n\n❌ <b>Отменила</b>", reply_markup=None)
        except Exception:
            pass
        return

    from core.actions import create_todoist_task
    from db import save_task_sync, task_exists_recently
    created = 0
    skipped_dup = 0
    for t in item["tasks"]:
        title, due, is_meeting = t["title"], t["due"], t["is_meeting"]
        # Backstop-дедуп (как у settle-пути): то же название за 24ч → не плодить дубль
        if await task_exists_recently(db, settings.owner_user_id, title):
            skipped_dup += 1
            logger.info(f"Capture dedup: '{title}' уже создавалась за 24ч — пропуск")
            continue
        todoist_id = None
        if settings.todoist_api_token:
            from core.actions import todoist_section
            todoist_id = await create_todoist_task(
                settings.todoist_api_token, title, "Создано владельцем (личный инбокс)",
                due, settings.todoist_project_id, ["telegram", "сам"],
                todoist_section(settings, due=due, owner_created=True))   # v4.46: Встречи / Личное
        if not todoist_id:
            continue
        sync_id = await save_task_sync(db, todoist_id, None, user_id=settings.owner_user_id, title=title, topic="", due_date=due)
        created += 1
        if is_meeting and due and "T" in due and settings.google_calendar_id:
            try:
                from core.calendar import create_calendar_event
                from db import set_calendar_event_id
                eid = await create_calendar_event(
                    settings.google_service_account_path, settings.google_calendar_id,
                    settings.timezone, summary=title, due=due, description="Из личного инбокса")
                if eid and sync_id:
                    await set_calendar_event_id(db, sync_id, eid)
            except Exception as exc:
                logger.debug(f"calendar skip: {exc}")
        logger.info(f"Owner capture: {title!r} → todoist={todoist_id}")

    if not created:
        if skipped_dup:
            await query.answer("♻️ Уже есть такая задача (создана за последние 24ч)", show_alert=True)
            try:
                await query.message.edit_text(
                    query.message.html_text + "\n\n♻️ <b>Дубль</b> — задача уже создавалась", reply_markup=None)
            except Exception:
                pass
            return
        await query.answer("Не записалось, нажми ещё раз", show_alert=True); return
    dup_note = f" · ♻️ дублей пропущено: {skipped_dup}" if skipped_dup else ""
    await query.answer(f"✅ Создано: {created}")
    try:
        await query.message.edit_text(
            query.message.html_text + f"\n\n✅ <b>Создано: {created}</b> → Todoist{dup_note}", reply_markup=None)
    except Exception:
        pass


# ── query: что сегодня / на неделе (из bot.db; Notion ретайрнут v3.23) ────────
async def _query_db_tasks(settings: Settings, period: str, today) -> list[dict]:
    """Открытые задачи с дедлайном из bot.db (источник истины; Notion ретайрнут v3.22).
    today → дедлайн == сегодня; week → дедлайн в окне [сегодня, +7 дней]."""
    import aiosqlite
    from datetime import date as _date, timedelta
    end = today + timedelta(days=7)
    try:
        db = await aiosqlite.connect(f"file:{settings.db_path}?mode=ro", uri=True)
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT task_title, due_date FROM task_sync "
            "WHERE synced = 0 AND due_date IS NOT NULL AND due_date != '' ORDER BY due_date")).fetchall()
        await db.close()
    except Exception as exc:
        logger.error(f"db tasks query failed: {exc}")
        return []
    out = []
    for r in rows:
        due = r["due_date"]
        try:
            dd = _date.fromisoformat(str(due)[:10])
        except Exception:
            continue
        if period == "week" and not (today <= dd <= end):
            continue
        if period == "today" and dd != today:
            continue
        if period == "tomorrow" and dd != today + timedelta(days=1):
            continue
        out.append({"title": r["task_title"], "due": due})
    return out


async def _do_query(message: Message, settings: Settings, data: dict, db=None) -> None:
    period = data.get("period") if data.get("period") in ("today", "tomorrow", "week") else "today"
    today = today_local()
    day = today + timedelta(days=1) if period == "tomorrow" else today
    tasks = await _query_db_tasks(settings, period, today)
    head = {"week": "🗓 <b>На неделю</b>", "tomorrow": "📅 <b>Завтра</b>"}.get(period, "📅 <b>Сегодня</b>")
    # v4.41: день = задачи + пары + календарь (ассистент отвечает про ДЕНЬ, а не только про дедлайны)
    extra = []
    if period in ("today", "tomorrow"):
        try:
            from core import schedule as _sched
            for pl in _sched.digest_lines(day):
                extra.append(f"🎓 {pl}")
        except Exception:
            pass
        try:
            if settings.google_calendar_id:
                from core.calendar import list_events
                from core.digest import _day_start
                for e in await list_events(settings.google_service_account_path, settings.google_calendar_id,
                                           _day_start(day, settings), _day_start(day + timedelta(days=1), settings)):
                    st = str(e.get("start") or "")
                    extra.append(f"📆 {st[11:16] if 'T' in st else 'весь день'} {e.get('summary', '')[:60]}")
        except Exception:
            pass
    if not tasks and not extra:
        await message.reply(f"{head}\n\nНичего не запланировано — свободный день ✨", parse_mode="HTML")
        await _dialog_push(db, "owner", message.text or "", "mira", f"{period}: пусто")
        return
    lines = [f"• {_html.escape(t['title'])}{(' — ' + human_due(t['due'])) if t.get('due') else ''}" for t in tasks]
    lines += [_html.escape(x) for x in extra]
    await _dialog_push(db, "owner", message.text or "", "mira", f"{period}: " + "; ".join(t['title'] for t in tasks[:6]) + ("; " + "; ".join(extra[:4]) if extra else ""))
    rich_ok = False
    try:
        import html as _html
        from core import richmsg
        from core.util import html_due as _hd
        rows_t = [[_html.escape(t["title"]), _hd(t["due"]) if t.get("due") else "—"]
                  for t in tasks[:25]]
        title_r = "🗓 На неделю" if period == "week" else "📅 Сегодня"
        doc = f"<h3>{title_r}</h3>" + richmsg.table(["Задача", "Дедлайн"], rows_t)
        rich_ok = await richmsg.try_rich(message, doc)
    except Exception as exc:
        logger.debug(f"rich query skip: {exc}")
    if not rich_ok:
        await message.reply(f"{head}\n\n" + "\n".join(lines[:25]), parse_mode="HTML")


# ── manage: закрыть / перенести ──────────────────────────────────────────────
async def _open_tasks(db) -> list[dict]:
    async with db.execute(
        "SELECT id, todoist_task_id, notion_page_id, task_title, calendar_event_id FROM task_sync WHERE synced = 0"
    ) as cur:
        rows = await cur.fetchall()
    return [{"id": r[0], "todoist_id": r[1], "notion_page_id": r[2], "title": r[3],
             "calendar_event_id": r[4]} for r in rows]


def _best_match(target: str, tasks: list[dict]) -> list[dict]:
    """Вернуть задачи, отсортированные по совпадению слов с target (лучшее — первым)."""
    tw = {w for w in target.lower().split() if len(w) > 2}
    scored = []
    for t in tasks:
        title_w = set(t["title"].lower().split())
        score = sum(1 for w in tw if any(w in x or x in w for x in title_w))
        if score:
            scored.append((score, t))
    scored.sort(key=lambda x: -x[0])
    return [t for _, t in scored]


async def _do_manage(message: Message, db, settings: Settings, data: dict) -> None:
    action = (data.get("action") or "").lower()
    target = (data.get("target") or "").strip()
    if not target:
        await message.reply("Какую задачу? Уточни, напр. «закрой задачу про вуз»."); return
    matches = _best_match(target, await _open_tasks(db))
    if not matches:
        await message.reply(f"Не нашёл открытую задачу по «{target}» 🤔"); return
    if len(matches) > 1 and matches[0]["title"].lower() != target.lower():
        lst = "\n".join(f"• {m['title']}" for m in matches[:5])
        await message.reply(f"Нашёл несколько — уточни точнее:\n{lst}"); return
    task = matches[0]

    if action == "reschedule":
        from core.actions import _normalize_due, update_todoist_task, update_task_calendar
        due = _normalize_due(data.get("new_due"))
        if not due:
            await message.reply("На когда перенести? Укажи дату/время."); return
        if settings.todoist_api_token and task["todoist_id"]:
            await update_todoist_task(settings.todoist_api_token, task["todoist_id"], due)
        await update_task_calendar(db, task, task["title"], due)
        from core.util import html_due
        await message.reply(f"📅 Перенёс: <b>{task['title']}</b> → {html_due(due)}", parse_mode="HTML")
    else:  # close
        from core.sync import complete_todoist_task
        from db import mark_sync_done_by_id
        if settings.todoist_api_token and task["todoist_id"]:
            await complete_todoist_task(settings.todoist_api_token, task["todoist_id"])
        await mark_sync_done_by_id(db, task["id"])
        await message.reply(f"✅ Закрыл: <b>{task['title']}</b>", parse_mode="HTML")
