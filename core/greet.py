"""
v4.91 Приветствие новичков и ссылки под постами в обсуждении своего канала.

Только группа, привязанная к post_channel, как у модерации. Без модели: тексты постоянные.
Приветствие: одно сообщение с именами и кнопками ссылок, через KEEP_SECONDS удаляется. Пришло новое —
старое удаляется сразу, так что в чате всегда не больше одного.
Ссылки: первый комментарий под каждым новым постом канала, вместо такого же у Group Help.
"""
import asyncio
import html
from contextlib import suppress

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger

from core import autonomy, owners
from core.moderation import _discussion_id

KEEP_SECONDS = 600
TEXT = ("👋 Привет, {names}!\n\n"
        "Это чат канала <b>st4rninja38</b>: инди-хоррор стримы на Twitch и YouTube одновременно 🩸\n\n"
        "Здесь без рекламы, спама и оскорблений, остальное можно. Все ссылки разом: sn-links.ru")
# третий элемент цвет кнопки: primary синий, success зелёный, danger красный, без него обычная
LINKS = [
    [("🟣 Twitch", "https://twitch.tv/st4rninja38", "primary"), ("▶️ YouTube", "https://youtube.com/StarNinja38", "danger")],
    [("💎 Boosty", "https://boosty.to/st4rninja38", "success"), ("💰 Донат", "https://donatex.gg/donate/StarNinja38", "success")],
    [("💬 Discord", "https://discord.gg/PBArxTYrhZ"), ("🎯 Все ссылки", "https://sn-links.ru")],
]
POST_TEXT = ("🌟 <b>st4rninja38</b> · инди-хоррор стримы 🩸\n"
             "Twitch и YouTube одновременно, все ссылки на sn-links.ru")
POST_LINKS = LINKS + [[("🤡 Мемы на стрим", "https://memealerts.com/68ac1c542cf1469d2a58011b", "success"),
                       ("💡 Предложить контент", "https://sn-content.ru/new")],
                      [("✍️ Для связи", "https://t.me/starninjaMAN")]]
_shown: dict[int, int] = {}   # chat_id -> id приветствия, которое сейчас в чате


def _keyboard(rows=LINKS) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=b[0], url=b[1], style=b[2] if len(b) > 2 else None)
                                                  for b in row] for row in rows])


async def post_links(message, bot, db, settings) -> bool:
    """Новый пост канала пришёл в обсуждение: первым комментарием ссылки. True, если ушло."""
    try:
        if not await autonomy.enabled(db, "postlinks") or message.chat.id != await _discussion_id(bot, db, settings):
            return False
        await bot.send_message(message.chat.id, POST_TEXT, parse_mode="HTML", reply_markup=_keyboard(POST_LINKS),
                               reply_to_message_id=message.message_id, disable_web_page_preview=True)
        return True
    except Exception as exc:
        logger.warning(f"post links failed: {type(exc).__name__}: {exc}")
        return False


async def _remove(bot, chat_id: int, msg_id: int, delay: float = 0) -> None:
    if delay:
        await asyncio.sleep(delay)
    if _shown.get(chat_id) == msg_id:
        _shown.pop(chat_id, None)
    with suppress(Exception):
        await bot.delete_message(chat_id, msg_id)


async def welcome(message, bot, db, settings) -> bool:
    """Служебное «вступил в группу»: приветствие новичкам. True, если сообщение ушло."""
    people = [u for u in (message.new_chat_members or [])
              if not getattr(u, "is_bot", False) and not owners.is_owner(settings.owner_user_id, u.id)]
    if not people:
        return False
    try:
        if not await autonomy.enabled(db, "greet") or message.chat.id != await _discussion_id(bot, db, settings):
            return False
        names = ", ".join(f'<a href="tg://user?id={u.id}">{html.escape(u.first_name or u.username or "друг")}</a>'
                          for u in people[:5])
        sent = await bot.send_message(message.chat.id, TEXT.format(names=names), parse_mode="HTML",
                                      reply_markup=_keyboard(), disable_web_page_preview=True)
        old = _shown.get(message.chat.id)
        _shown[message.chat.id] = sent.message_id
        if old:
            await _remove(bot, message.chat.id, old)
        asyncio.create_task(_remove(bot, message.chat.id, sent.message_id, KEEP_SECONDS))
        return True
    except Exception as exc:
        logger.warning(f"greet failed: {type(exc).__name__}: {exc}")
        return False
