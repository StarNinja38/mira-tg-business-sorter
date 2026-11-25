# -*- coding: utf-8 -*-
"""
Топики в личке владельца (Bot API 9.4+) — потоки уведомлений по темам:
📋 Задачи / ☀️ Дайджест / 💬 Черновики / 🔴 Срочное.

createForumTopic работает в личке бота с пользователем. На старте бот
создаёт темы (один раз, thread_id хранится в settings_kv) и далее шлёт
уведомления с message_thread_id.

Деградация: любой сбой (клиент без поддержки, тема удалена) → уведомление
уходит обычным сообщением без темы и тема инвалидируется. Бот никогда
не теряет уведомление из-за топиков.
"""
from loguru import logger

TOPICS = {
    "tasks":    "📋 Задачи",
    "digest":   "☀️ Дайджест",
    "drafts":   "💬 Черновики",
    "urgent":   "🔴 Срочное",
    "meetings": "📅 Встречи",
    "links":    "🔗 Ссылки",
    "contacts": "👤 Контакты",
    "dm":       "📩 Личка бота",   # v4.8: чужие пишут боту напрямую; ответ владельца — реплаем
    "email":    "📧 Почта",        # v4.28: важные письма из подключённых ящиков
}

_threads: dict[str, int] = {}   # key → message_thread_id
_db = None                      # соединение для инвалидации (одно на процесс)


async def ensure_topics(bot, db, owner_id: int) -> bool:
    """Создать/восстановить темы в личке владельца. Вызывается на старте и из /topics."""
    global _db
    _db = db
    from db import get_setting, set_setting
    # Топик-режим в личке — настройка уровня бота (BotFather → Bot Settings → Topics).
    # API-метода включения нет; без него createForumTopic даёт «chat is not a forum».
    try:
        me = await bot.get_me()
        if getattr(me, "has_topics_enabled", None) is False:
            logger.info("Topics OFF for bot PMs — включи в @BotFather (/mybots → Bot Settings → Topics); уведомления идут без тем")
            return False
    except Exception:
        pass
    for key, name in TOPICS.items():
        saved = await get_setting(db, f"topic_{key}")
        if saved:
            try:
                _threads[key] = int(saved)
                continue
            except (TypeError, ValueError):
                pass
        try:
            topic = await bot.create_forum_topic(chat_id=owner_id, name=name)
            _threads[key] = topic.message_thread_id
            await set_setting(db, f"topic_{key}", str(topic.message_thread_id))
            logger.info(f"Topic created in owner PM: {name} (thread={topic.message_thread_id})")
        except Exception as exc:
            logger.info(f"Topics unavailable in owner PM ({type(exc).__name__}: {exc}) — plain notifications")
            return False
    return True


def thread_id(key: str):
    """message_thread_id темы или None (для прямых вызовов API, напр. rich messages)."""
    return _threads.get(key)


async def _invalidate(key: str) -> None:
    _threads.pop(key, None)
    if _db is not None:
        try:
            cur = await _db.execute("DELETE FROM settings_kv WHERE key = ?", (f"topic_{key}",))
            await cur.close()
            await _db.commit()
        except Exception:
            pass


def snooze_row():
    """v4.25: ряд «напомнить позже» для важных карточек — в срок Мира пришлёт карточку заново
    (текст+кнопки), чтобы ничего не тонуло (боль по данным: somiss 3 предложения / 0 нажатий)."""
    from aiogram.types import InlineKeyboardButton
    return [InlineKeyboardButton(text="🕐 +1ч", callback_data="snz:h1"),
            InlineKeyboardButton(text="🌙 вечером", callback_data="snz:eve"),
            InlineKeyboardButton(text="📅 утром", callback_data="snz:morn")]


def _is_parse_error(exc) -> bool:
    s = str(exc).lower()
    return "parse entities" in s or "can't parse" in s or "unsupported start tag" in s


async def _send_plain_fallback(bot, owner_id: int, text: str, tid, kwargs: dict):
    """v4.33: HTML не распарсился (в тексте «<», «&» из сообщения контакта) → отправить как
    plain text, а не терять сообщение. Дефолт бота — parse_mode=HTML, так что явный None."""
    kw = dict(kwargs)
    kw["parse_mode"] = None
    if tid:
        kw["message_thread_id"] = tid
    return await bot.send_message(chat_id=owner_id, text=text, **kw)


async def send_owner(bot, owner_id: int, key: str, text: str, **kwargs):
    """send_message владельцу в тему `key`; сбой темы → fallback без темы.
    v4.33: тему инвалидируем ТОЛЬКО на ошибки самой темы (thread/topic) — раньше любая ошибка
    (HTML-парсинг, длина, flood) удаляла тему и после рестарта создавалась дублирующая."""
    tid = _threads.get(key)
    if tid:
        try:
            return await bot.send_message(
                chat_id=owner_id, text=text, message_thread_id=tid, **kwargs)
        except Exception as exc:
            if _is_parse_error(exc):
                logger.warning(f"HTML parse failed [{key}] — resend as plain text")
                return await _send_plain_fallback(bot, owner_id, text, tid, kwargs)
            s = str(exc).lower()
            if "thread" in s or "topic" in s or "forum" in s:
                logger.warning(f"Topic send failed [{key}]: {exc} — invalidate, fallback plain")
                await _invalidate(key)
            else:
                logger.warning(f"Send failed [{key}]: {exc} — retry without thread (topic kept)")
    try:
        return await bot.send_message(chat_id=owner_id, text=text, **kwargs)
    except Exception as exc:
        if _is_parse_error(exc):
            logger.warning(f"HTML parse failed [{key}] (no thread) — resend as plain text")
            return await _send_plain_fallback(bot, owner_id, text, None, kwargs)
        raise
