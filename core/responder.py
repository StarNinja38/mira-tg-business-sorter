import random
from aiogram import Bot
from loguru import logger

SPAM_REPLIES = [
    "Спасибо, не интересно 🙏",
    "Сейчас не рассматриваю подобные предложения",
    "🤖 Я сейчас занят, если что-то важное — напишите позже",
    "Спасибо за предложение, но мне это не подходит",
    "Не интересует, удачи!",
]


async def send_spam_reply(
    bot: Bot,
    chat_id: int,
    business_connection_id: str,
) -> None:
    text = random.choice(SPAM_REPLIES)
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            business_connection_id=business_connection_id,
            parse_mode=None,
        )
        logger.debug(f"Spam reply sent to {chat_id}: {text!r}")
    except Exception as exc:
        logger.error(f"Failed to send spam reply to {chat_id}: {exc}")


async def mark_as_read(
    bot: Bot,
    business_connection_id: str,
    chat_id: int,
    message_id: int,
) -> None:
    try:
        await bot.read_business_message(
            business_connection_id=business_connection_id,
            chat_id=chat_id,
            message_id=message_id,
        )
    except Exception as exc:
        logger.debug(f"mark_as_read failed (non-critical): {exc}")
