"""
v4.71: лог команд владельца, чтобы видеть, чем он реально пользуется. Пишем только название:
в аргументах бывают имена и текст запросов. Просмотровые команды иначе следа не оставляют.
"""
import re

from loguru import logger

_COMMAND_RE = re.compile(r"/(\w{1,32})(?:@\w+)?(?:\s|$)")


def command_name(text) -> str:
    """Название команды из начала сообщения или ''. Пути вроде /opt/x не считаются."""
    m = _COMMAND_RE.match(text or "")
    return m.group(1).lower() if m else ""


async def log_commands(handler, event, data):
    """Outer-middleware на dp.message: запись до фильтров, сбой лога команду не ломает."""
    try:
        name = command_name(event.text or event.caption)
        user = event.from_user
        if name and user and user.id == data["settings"].owner_user_id:
            from db import log_command
            await log_command(data["db"], name)
    except Exception as exc:
        logger.debug(f"command log skip: {exc}")
    return await handler(event, data)
