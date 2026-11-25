import asyncio
import sys
import time
from pathlib import Path

import aiosqlite
from anthropic import AsyncAnthropic
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeChat, MenuButtonCommands, MenuButtonWebApp, WebAppInfo
from loguru import logger

from config import settings
from db import init_db
from handlers import business, commands, groups, callbacks, owner
from core import routines, twitch_chat, youtube_chat
from core.sync import run_sync_loop
from core.digest import run_digest_loop
from core.tasks_loop import run_task_settle_loop


def setup_logging() -> None:
    logger.remove()
    logger.add(
        sys.stdout,
        level=settings.log_level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{line}</cyan> — <level>{message}</level>",
        colorize=True,
    )
    Path("./logs").mkdir(exist_ok=True)
    logger.add(
        "./logs/bot.log",
        level=settings.log_level,
        rotation="10 MB",
        retention=5,
        compression="gz",
        encoding="utf-8",
    )


async def main() -> None:
    setup_logging()
    logger.info("Starting TG Business Sorter…")

    # Validate required configuration before doing anything
    missing = []
    if not settings.bot_token:
        missing.append("BOT_TOKEN")
    if not settings.owner_user_id:
        missing.append("OWNER_USER_ID")
    if not settings.anthropic_api_key:
        missing.append("ANTHROPIC_API_KEY")
    if missing:
        logger.critical(f"Missing required env: {', '.join(missing)}. Заполни .env и перезапусти.")
        sys.exit(1)

    anthropic_client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    try:
        await anthropic_client.messages.create(
            model=settings.anthropic_model,
            max_tokens=10,
            messages=[{"role": "user", "content": "ping"}],
        )
        logger.info(f"Anthropic API key valid, model={settings.anthropic_model!r}")
    except Exception as exc:
        logger.warning(f"Anthropic API check failed: {exc} — стартую всё равно (C10: раньше sys.exit → рестарт-петля systemd при 529/DNS)")

    await init_db(settings.db_path)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    # «Бот думает…» — живой draft (Bot API 9.3/10.1) во время генерации показывается
    # точечно в LLM-ответах через core.richmsg.thinking(...), а не глобальным middleware.
    from core.command_log import log_commands
    dp.message.outer_middleware(log_commands)
    from core import widgets as _widgets
    dp.update.outer_middleware(_widgets.middleware)   # v4.106/107: widget_query (Bot API 10.4) до aiogram

    dp.include_router(commands.router)
    dp.include_router(callbacks.router)
    dp.include_router(business.router)
    dp.include_router(groups.router)
    dp.include_router(owner.router)   # личный инбокс владельца (capture/manage/query в личке)

    db = await aiosqlite.connect(settings.db_path)
    db.row_factory = aiosqlite.Row
    # Per-connection pragmas (init_db's connection is separate/closed): without these
    # the 5s lock-wait the whole design relies on is NOT active on the runtime connection.
    await db.execute("PRAGMA busy_timeout=5000")
    await db.execute("PRAGMA synchronous=NORMAL")

    # Единый учёт LLM-стоимости: регистрируем соединение, чтобы record() писал в llm_usage
    from core.usage import set_db as _usage_set_db
    _usage_set_db(db)

    start_time = time.monotonic()

    dp.workflow_data.update(
        db=db,
        settings=settings,
        anthropic_client=anthropic_client,
        start_time=start_time,
    )

    bot_commands = [
        BotCommand(command="start",     description="Приветствие и статус"),
        BotCommand(command="help",      description="Меню: все команды по категориям"),
        BotCommand(command="health",    description="Статус бота и расходы API"),
        BotCommand(command="credits",   description="Баланс кредитов Anthropic"),
        BotCommand(command="stats",     description="Статистика за 7 дней"),
        BotCommand(command="last",      description="Последние сообщения"),
        BotCommand(command="about",     description="Память на контакт"),
        BotCommand(command="find",      description="Поиск по переписке"),
        BotCommand(command="links",     description="Сохранённые ссылки"),
        BotCommand(command="mode",      description="Режим: observe | active"),
        BotCommand(command="drafts",    description="Умные черновики ответов: on | off"),
        BotCommand(command="setreply",  description="Настроить авто-ответ BUSINESS"),
        BotCommand(command="correct",   description="Исправить категорию контакта"),
        BotCommand(command="friend",    description="Добавить в whitelist"),
        BotCommand(command="unfriend",  description="Удалить из whitelist"),
        BotCommand(command="whitelist", description="Список разрешённых"),
        BotCommand(command="summary",   description="Умная память отношений: /summary @user"),
        BotCommand(command="insights",  description="Аналитика за 30 дней (категории/топ/задачи)"),
        BotCommand(command="schedule",  description="Подобрать время встречи: /schedule @user"),
        BotCommand(command="radar",     description="Радар отношений (остывшие/обещания/без ответа)"),
        BotCommand(command="day",       description="Мой день: задачи на сегодня виджетом"),
        BotCommand(command="habits",    description="Привычки: неделя кружками, отметить нажатием"),
        BotCommand(command="market",    description="Рынок: крипта, индексы, золото, доллар с графиками"),
        BotCommand(command="promises",  description="Твои обещания (задачи) — закрыть ✅ / перенести 📅"),
        BotCommand(command="rule",      description="Правило по контакту: vip|task|never|mute|clear"),
        BotCommand(command="me",        description="Память о тебе (по твоим исходящим)"),
        BotCommand(command="setname",   description="Исправить имя контакта: /setname @user Имя"),
        BotCommand(command="addfact",   description="Добавить факт в памяти: /addfact @user текст"),
        BotCommand(command="setphoto",  description="Фото в памяти: пришли фото с подписью /setphoto @user"),
        BotCommand(command="topics",    description="Топики уведомлений в личке"),
        BotCommand(command="owner",     description="Мультиаккаунт: /owner add ID"),
        BotCommand(command="twitch",    description="Войти Мирой в чат Twitch"),
        BotCommand(command="youtube",   description="Войти Мирой в чат YouTube"),
        BotCommand(command="roles",     description="Роли контактов и кто в них"),
        BotCommand(command="subs",      description="Подписки: суммы, даты списания, итог в месяц"),
    ]
    await bot.set_my_commands(bot_commands, scope=BotCommandScopeChat(chat_id=settings.owner_user_id))
    menu_button = (MenuButtonWebApp(text="Mira", web_app=WebAppInfo(url=settings.webapp_url))
                   if settings.webapp_url else MenuButtonCommands())
    await bot.set_chat_menu_button(chat_id=settings.owner_user_id, menu_button=menu_button)

    me = await bot.get_me()
    logger.info(f"Bot @{me.username} (id={me.id}) ready. Mode={settings.mode}")

    # Мультиаккаунт: загрузить аккаунты-владельцы (подключившие бота) в кэш
    try:
        from core import owners
        await owners.load(db)
    except Exception as exc:
        logger.warning(f"owners.load failed: {exc}")

    # Топики в личке владельца (Bot API 9.4+): потоки уведомлений по темам.
    # Сбой не критичен — уведомления пойдут обычными сообщениями.
    try:
        from core.topics import ensure_topics
        await ensure_topics(bot, db, settings.owner_user_id)
    except Exception as exc:
        logger.warning(f"ensure_topics failed: {exc}")

    # Background loops with supervision — log loudly if one dies instead of failing silently
    def _supervise(name):
        def _cb(task):
            try:
                exc = task.exception()
            except asyncio.CancelledError:
                return
            if exc:
                logger.error(f"Background loop '{name}' DIED: {exc!r}")
        return _cb

    _bg_tasks = [
        asyncio.create_task(run_sync_loop(db, settings.todoist_api_token, settings.notion_token, bot=bot, owner_id=settings.owner_user_id)),
        asyncio.create_task(run_digest_loop(bot, settings.owner_user_id, db, settings)),
        asyncio.create_task(run_task_settle_loop(bot, db, settings, anthropic_client)),
        asyncio.create_task(routines.run_loop(bot, db, settings, anthropic_client)),
        asyncio.create_task(twitch_chat.run_loop(bot, db, settings, anthropic_client)),   # v4.93: Mira в чате Twitch
        asyncio.create_task(youtube_chat.run_loop(bot, db, settings, anthropic_client)),  # v4.95: Mira в чате YouTube
    ]
    for _t, _name in zip(_bg_tasks, ("sync", "digest", "settle", "routines", "twitch", "youtube")):
        _t.add_done_callback(_supervise(_name))

    # v4.62: «Мира в чёлке» — локальный вход для Mac (только 127.0.0.1, через SSH-туннель)
    _mira_api = None
    try:
        from core import mira_api
        _mira_api = await mira_api.start(bot, db, settings, anthropic_client)
    except Exception as exc:
        logger.warning(f"Mira API не поднялся: {type(exc).__name__}: {exc}")

    try:
        await dp.start_polling(
            bot,
            allowed_updates=[
                "stopped_message_generation",   # B8: кнопка Стоп у потоковых черновиков
                "message",
                "business_message",
                "business_connection",
                "edited_business_message",
                "deleted_business_messages",
                "callback_query",
                "my_chat_member",
                "widget_query",                  # v4.106: нажатия в виджете «Мой день»
            ],
        )
    finally:
        logger.info("Shutting down…")
        await db.close()
        await bot.session.close()
        logger.info("Goodbye.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
