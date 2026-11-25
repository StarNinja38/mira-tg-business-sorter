import aiosqlite
from typing import Optional
from db import (
    is_whitelisted,
    add_to_whitelist,
    remove_from_whitelist,
    get_whitelist,
)


async def check(db: aiosqlite.Connection, user_id: int) -> bool:
    return await is_whitelisted(db, user_id)


async def add(
    db: aiosqlite.Connection,
    user_id: int,
    username: Optional[str],
    reason: str = "manual",
) -> None:
    await add_to_whitelist(db, user_id, username, reason)


async def remove(db: aiosqlite.Connection, user_id: int) -> bool:
    return await remove_from_whitelist(db, user_id)


async def list_all(db: aiosqlite.Connection) -> list[dict]:
    return await get_whitelist(db)
