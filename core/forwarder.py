from aiogram import Bot
from aiogram.types import Message
from loguru import logger


async def save_file_to_owner(bot: Bot, message: Message, owner_user_id: int, caption: str) -> bool:
    """Re-send an incoming file (document/photo/video/audio/voice/circle) to owner's Saved Messages.
    Uses file_id resend — reliable for Business-API media. Returns True on success."""
    try:
        cap = caption[:1000]
        if message.document:
            await bot.send_document(owner_user_id, message.document.file_id, caption=cap)
        elif message.photo:
            await bot.send_photo(owner_user_id, message.photo[-1].file_id, caption=cap)
        elif message.video:
            await bot.send_video(owner_user_id, message.video.file_id, caption=cap)
        elif message.video_note:
            await bot.send_message(owner_user_id, cap)
            await bot.send_video_note(owner_user_id, message.video_note.file_id)
        elif message.audio:
            await bot.send_audio(owner_user_id, message.audio.file_id, caption=cap)
        elif message.voice:
            await bot.send_message(owner_user_id, cap)
            await bot.send_voice(owner_user_id, message.voice.file_id)
        else:
            return False
        logger.info(f"File saved to owner's Saved Messages: {cap[:50]}")
        return True
    except Exception as exc:
        logger.error(f"save_file_to_owner failed: {exc}")
        return False


async def save_attachment(bot: Bot, message: Message, username: str) -> str | None:
    """Скачать вложение из сообщения и сохранить в data/attachments/<username>/.
    Эти файлы синкаются в vault (Telegram/Вложения). Дедуп по file_unique_id."""
    from pathlib import Path
    m = message
    fobj = None; suffix = ""
    if m.photo:            fobj, suffix = m.photo[-1], ".jpg"
    elif m.document:       fobj, suffix = m.document, (Path(m.document.file_name or "").suffix or ".bin")
    elif m.voice:          fobj, suffix = m.voice, ".ogg"
    elif m.audio:          fobj, suffix = m.audio, (Path(m.audio.file_name or "").suffix or ".mp3")
    elif m.video:          fobj, suffix = m.video, ".mp4"
    elif m.video_note:     fobj, suffix = m.video_note, ".mp4"
    elif m.animation:      fobj, suffix = m.animation, ".mp4"
    elif m.sticker:        fobj, suffix = m.sticker, ".webp"
    if not fobj:
        return None
    try:
        uid = getattr(fobj, "file_unique_id", "") or "file"
        base = Path("data/attachments") / (username or "unknown")
        base.mkdir(parents=True, exist_ok=True)
        if list(base.glob(f"*_{uid}.*")):       # уже сохраняли этот файл
            return None
        info = await bot.get_file(fobj.file_id)
        if getattr(info, "file_size", 0) and info.file_size > 50 * 1024 * 1024:
            return None                          # не тащим гигантские
        data = await bot.download_file(info.file_path)
        from core.util import now_local
        path = base / f"{now_local().strftime('%Y-%m-%d_%H%M')}_{uid}{suffix}"
        with open(path, "wb") as f:
            f.write(data.read())
        logger.info(f"Attachment saved: {path}")
        return str(path)
    except Exception as exc:
        logger.error(f"save_attachment failed: {exc}")
        return None


async def forward_to_saved(
    bot: Bot,
    message: Message,
    owner_user_id: int,
    category: str,
    summary: str,
    username: str,
) -> None:
    """Forward an incoming business message to the owner's Saved Messages with a caption."""
    tag_map = {
        "BUSINESS": "#входящее",
        "UNCLEAR": "#проверить",
    }
    tag = tag_map.get(category, "#сообщение")
    caption = f"{tag} @{username}\n📝 {summary}"

    text_body = message.text or message.caption or "[нет текста]"
    full_text = f"{caption}\n\n— — —\n{text_body[:3000]}"

    try:
        # Try to forward the original message
        await bot.forward_message(
            chat_id=owner_user_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
        await bot.send_message(chat_id=owner_user_id, text=caption, parse_mode=None)
        logger.debug(f"Forwarded {category} from @{username} to Saved Messages")
    except Exception:
        # Business messages can't always be forwarded — send text instead
        # v4.33: parse_mode=None — текст контакта с «<», «&» ломал HTML-парсинг → входящее терялось
        try:
            await bot.send_message(chat_id=owner_user_id, text=full_text, parse_mode=None)
            logger.debug(f"Sent {category} text from @{username} to Saved Messages")
        except Exception as exc2:
            logger.error(f"Failed to send to Saved Messages: {exc2}")
