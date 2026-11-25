# -*- coding: utf-8 -*-
"""
Фото в памяти контакта. Источник: аватар (getUserProfilePhotos) ИЛИ присланное фото
(с подтверждением владельца «это он?»). Хранение: contact_profiles.photo_file_id +
файл data/attachments/contact_memory_<uid>.jpg (его синк переносит в vault) + обложка Notion.

Pending-фото держим в памяти по короткому токену — file_id длиннее лимита callback_data (64Б).
"""
import os
import time

from loguru import logger

_pending: dict = {}      # token -> {user_id, username, file_id, ts}
_counter = [0]
_TTL = 6 * 3600


def _prune() -> None:
    now = time.monotonic()
    for k in [k for k, v in _pending.items() if now - v["ts"] > _TTL]:
        _pending.pop(k, None)


def stash(user_id: int, username: str, file_id: str) -> str:
    _prune()
    token = __import__('secrets').token_hex(4)   # v4.36 (B2): случайный токен — счётчик с 1 после рестарта путал карточки
    _pending[token] = {"user_id": user_id, "username": username, "file_id": file_id, "ts": time.monotonic()}
    return token


def pop(token: str):
    return _pending.pop(token, None)


def should_refresh_avatar(has_photo: bool, src, stored_uid, new_uid) -> bool:
    """Чистое решение: обновлять ли фото памяти на текущий аватар Telegram.
    - manual / confirmed → НЕТ (выбор владельца не перетираем).
    - нет фото → ДА (поставить).
    - есть фото (src=avatar или старое без src): ДА только если file_unique_id сменился."""
    if has_photo and src in ("manual", "confirmed"):
        return False
    if not has_photo:
        return True
    return stored_uid != new_uid


async def attach_photo(bot, db, settings, user_id: int, username: str, file_id: str,
                       unique_id: str | None = None, src: str = "avatar") -> None:
    """Сделать фото фотом памяти: БД + локальный файл для vault. Best-effort.
    src: avatar (авто, обновляемое) | manual (/setphoto) | confirmed (подтверждённое присланное)."""
    from db import set_contact_photo
    await set_contact_photo(db, user_id, file_id, unique_id=unique_id, src=src)

    try:
        os.makedirs("data/attachments", exist_ok=True)
        dest = f"data/attachments/contact_memory_{user_id}.jpg"   # стабильное имя → vault embed ![[contact_memory_<uid>.jpg]]
        await bot.download(file_id, destination=dest)
    except Exception as exc:
        logger.debug(f"contact_memory photo download skip @{username}: {exc}")


async def refresh_all_avatars(bot, db, settings) -> dict:
    """Сверить аватары всех контактов с Telegram и обновить изменившиеся.
    Трогает только фото с src='avatar' или без src (старые авто); manual/confirmed не перетирает.
    Также ставит аватар тем, у кого фото ещё нет. Best-effort (Business API часто 'user not found')."""
    cur = await db.execute(
        "SELECT user_id, username, photo_file_id, photo_unique_id, photo_src FROM contact_profiles")
    rows = [dict(r) for r in await cur.fetchall()]
    new = changed = unchanged = manual = err = 0
    for r in rows:
        uid = r["user_id"]
        uname = r.get("username") or str(uid)
        has_photo = bool(r.get("photo_file_id"))
        src = r.get("photo_src")
        if has_photo and src in ("manual", "confirmed"):
            manual += 1
            continue  # ручное/подтверждённое — не перетираем
        try:
            pp = await bot.get_user_profile_photos(uid, limit=1)
        except Exception:
            err += 1
            continue
        if not (pp and pp.total_count and pp.photos and pp.photos[0]):
            continue
        ph = pp.photos[0][-1]
        if not should_refresh_avatar(has_photo, src, r.get("photo_unique_id"), ph.file_unique_id):
            unchanged += 1
            continue  # аватар не менялся
        await attach_photo(bot, db, settings, uid, uname, ph.file_id,
                           unique_id=ph.file_unique_id, src="avatar")
        if has_photo:
            changed += 1
            logger.info(f"Avatar CHANGED for @{uname} — обновлён")
        else:
            new += 1
    return {"new": new, "changed": changed, "unchanged": unchanged, "manual": manual, "err": err}
