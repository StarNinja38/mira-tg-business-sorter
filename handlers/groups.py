import aiosqlite
from anthropic import AsyncAnthropic
from aiogram import Bot, F, Router
from aiogram.filters import ChatMemberUpdatedFilter, JOIN_TRANSITION
from aiogram.types import ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup, Message
from loguru import logger

from config import Settings
from core.actions import process_action_items
from core.classifier import CASUAL_PATTERNS, URL_RE, SPAM_KEYWORDS

router = Router(name="groups")
# Only this router's message handler fires for group/supergroup chats
router.message.filter(F.chat.type.in_({"group", "supergroup"}))

# Module-level cache: bot/owner usernames are static, fetch once
_cached_bot_username = None
_cached_owner_username = None


async def _get_cached_usernames(bot, owner_id: int) -> tuple[str, str]:
    global _cached_bot_username, _cached_owner_username
    # Don't cache empty strings: a one-off network error would disable group handling forever
    if not _cached_bot_username:
        try:
            _cached_bot_username = (await bot.get_me()).username or ""
        except Exception:
            _cached_bot_username = ""
    if not _cached_owner_username:
        try:
            _cached_owner_username = (await bot.get_chat(owner_id)).username or ""
        except Exception:
            _cached_owner_username = ""
    return _cached_bot_username, _cached_owner_username


def _is_relevant_group_message(message: Message, owner_username: str, bot_username: str) -> bool:
    """True if the group message is addressed to the owner or mentions the bot."""
    text = (message.text or message.caption or "").lower()
    entities = message.entities or message.caption_entities or []

    # Check for @mentions in entities
    for entity in entities:
        if entity.type == "mention":
            mention = entity.extract_from(message.text or message.caption or "").lstrip("@").lower()   # C16: caption + UTF-16 офсеты
            if mention in (owner_username.lower(), bot_username.lower()):
                return True

    # Reply to owner's message
    if message.reply_to_message and message.reply_to_message.from_user:
        if message.reply_to_message.from_user.username and \
           message.reply_to_message.from_user.username.lower() == owner_username.lower():
            return True

    return False


def _tags_bot(message: Message, bot_username: str, bot_id: int) -> bool:
    """Владелец обратился к Мире: тег бота или ответ на её сообщение."""
    source = message.text or message.caption or ""
    for entity in message.entities or message.caption_entities or []:
        if entity.type == "mention" and entity.extract_from(source).lstrip("@").lower() == bot_username.lower():
            return True
    replied = message.reply_to_message
    return bool(replied and replied.from_user and replied.from_user.id == bot_id)


def _approve_card(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Разрешить", callback_data=f"grp:yes:{chat_id}"),
         InlineKeyboardButton(text="🌙 И сводка в 21:00", callback_data=f"grp:day:{chat_id}")],
        [InlineKeyboardButton(text="🚪 Выйти из группы", callback_data=f"grp:no:{chat_id}")]])


async def _owner_tag(message: Message, bot: Bot, db, settings: Settings, client, text: str) -> None:
    """Тег владельца в группе. Здесь работает только отдельная Мира без доступа к его данным."""
    from core import group_mira
    from core.topics import send_owner
    from core.util import today_local
    if not await group_mira.is_allowed(db, message.chat.id):
        await send_owner(bot, settings.owner_user_id, "urgent", f"Ты позвал меня в группе «{message.chat.title}», но я там ещё не разрешена. Разрешить?",
                         reply_markup=_approve_card(message.chat.id), parse_mode=None)
        return
    quoted = (message.reply_to_message.text or message.reply_to_message.caption or "") if message.reply_to_message else ""
    answer = await group_mira.reply_text(db, settings, client, message.chat.id, text, quoted, today_local())
    if answer:
        sent = await message.reply(answer, parse_mode=None)
        await group_mira.log_message(db, message.chat.id, "Мира", sent.text or answer)
    else:
        logger.info(f"Group {message.chat.id}: лимит ответов на сегодня")


@router.my_chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def bot_added(event: ChatMemberUpdated, bot: Bot, db: aiosqlite.Connection, settings: Settings) -> None:
    """Бота добавили в группу. Чужой — бот выходит сам, владелец — решает кнопкой."""
    from core import owners
    from core.topics import send_owner
    if event.chat.type not in ("group", "supergroup"):
        return
    title = event.chat.title or str(event.chat.id)
    if not owners.is_owner(settings.owner_user_id, event.from_user.id):
        await bot.leave_chat(event.chat.id)
        await send_owner(bot, settings.owner_user_id, "urgent", f"Меня добавили в чужую группу «{title}», я вышла 🚪", parse_mode=None)
        return
    await send_owner(bot, settings.owner_user_id, "urgent",
                     f"Я в группе «{title}». Разрешить отвечать на твой тег?\nМоих данных о тебе там не будет. Чтобы делать сводки, сделай меня админом этой группы.",
                     reply_markup=_approve_card(event.chat.id), parse_mode=None)


def _is_worth_processing(text: str) -> bool:
    """Skip clearly useless messages to save API costs."""
    stripped = text.strip()
    if not stripped or len(stripped) < 5:
        return False
    if CASUAL_PATTERNS.match(stripped):
        return False
    return True


@router.message()
async def handle_group_message(
    message: Message,
    bot: Bot,
    db: aiosqlite.Connection,
    settings: Settings,
    anthropic_client: AsyncAnthropic,
) -> None:
    # Only process group/supergroup messages
    if message.chat.type not in ("group", "supergroup"):
        return

    if getattr(message, "is_automatic_forward", False):   # v4.79: новый пост канала в группе обсуждения
        from core import channel, greet
        await greet.post_links(message, bot, db, settings)   # v4.91: ссылки первым комментарием
        await channel.on_post(message, bot, db, settings, anthropic_client)
        return

    if isinstance(getattr(message, "new_chat_members", None), list):   # v4.91: приветствие новичкам в обсуждении канала
        from core import greet, moderation
        try:   # v4.109: время вступления — чтобы узнать новичка со спамом и сразу забанить
            await moderation.note_join(db, message.chat.id, message.new_chat_members)
        except Exception as exc:
            logger.debug(f"mod join note skip: {exc}")
        await greet.welcome(message, bot, db, settings)
        return

    from_user = message.from_user
    from core import group_mira, owners
    text = message.text or message.caption
    if not from_user or not text:
        return
    await group_mira.log_message(db, message.chat.id, from_user.first_name or from_user.username or "участник", text)

    from core import moderation   # v4.89: обсуждение канала, только карточка владельцу
    await moderation.check(message, bot, db, settings, anthropic_client)

    # Cached bot/owner usernames — static, fetched once per process
    bot_username, owner_username = await _get_cached_usernames(bot, settings.owner_user_id)

    if owners.is_owner(settings.owner_user_id, from_user.id):
        if _tags_bot(message, bot_username, bot.id):
            await _owner_tag(message, bot, db, settings, anthropic_client, text)
        return

    if not _is_relevant_group_message(message, owner_username, bot_username):
        return

    if not _is_worth_processing(text):
        logger.debug(f"Group: skipped casual message from @{from_user.username} in {message.chat.title}")
        return

    username = from_user.username or f"id{from_user.id}"
    chat_title = message.chat.title or str(message.chat.id)
    logger.info(f"Group message from @{username} in '{chat_title}': {text[:80]}")

    # Те же guard'ы, что в личке/business: правило контакта 'never' и стоп-кран бюджета.
    from db import get_contact_rule, is_over_budget
    if await get_contact_rule(db, from_user.id) == "never":
        logger.debug(f"Group: rule=never for @{username} — skip task extraction"); return
    _over = await is_over_budget(db, settings.anthropic_monthly_budget)

    # extract теперь пробрасывает сбой LLM (overload/500) вместо тихого []. У групп нет
    # retry-через-dirty (one-shot), поэтому ловим здесь, чтобы сбой не всплыл в хендлер aiogram.
    try:
        created = await process_action_items(
            client=anthropic_client,
            model=settings.anthropic_model,
            username=f"{username} (группа: {chat_title})",
            user_id=from_user.id,
            category="BUSINESS",
            text=text,
            todoist_token=settings.todoist_api_token,
            notion_token=settings.notion_token,
            notion_database_id=settings.notion_database_id,
            db=db,
            over_budget=_over,
        )
    except Exception as exc:
        logger.warning(f"Group: extraction failed for @{username} in '{chat_title}': {exc}")
        created = 0
    if created:
        logger.info(f"Group: created {created} task(s) from @{username} in '{chat_title}'")
