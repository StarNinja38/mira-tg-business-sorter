"""
v4.99 Стрим целиком: анонс в канал на старте и один итог после.
v4.114 Анонс — карточка с картинкой (stream_card), кнопки площадок, правка по ходу эфира, версия «после эфира».

Старт: digest раз в 5 минут снимает зрителей Twitch, новый stream:<id> → карточка владельцу с готовым анонсом
для post_channel. В канал бот пишет только по кнопке «Опубликовать», текст без модели.
Итог: зрителей нет ENDED_AFTER (стрим кончился) и прошло REPORT_AFTER → одно сообщение: длительность, зрители,
прирост фолловеров, сводки чатов Twitch и YouTube и вопрос «какая это была карточка на доске».
Новых вызовов модели нет: сводки чатов уже считают twitch_chat и youtube_chat, пока идёт итог, сами они не шлют.
"""
import html
import json
import time
from datetime import datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger

from core import autonomy, content_board
from db import get_digest_state, get_setting, set_digest_state, set_setting

REPORT_AFTER = 50 * 60        # сводка чата приходит после 40 минут тишины, ждём её
LOOK_BACK = 8 * 3600
_SUM_MAX = 1200


PAUSE = 15 * 60     # v4.114: обрыв короче — тот же эфир, второй пост не публикуем; дольше — эфир кончился


def _yt_link() -> str:
    from core import youtube_chat
    return f"https://youtu.be/{youtube_chat._state['video']}" if youtube_chat._state["chat"] and youtube_chat._state["video"] else ""


def caption(info: dict, ended: dict | None = None) -> str:
    """v4.114 Подпись поста (HTML): в эфире — «Стрим уже запущен», после — «Эфир окончен» с длительностью и пиком."""
    lines = ["<b>Эфир окончен, спасибо ❤️</b>" if ended else "<b>🔴 Стрим уже запущен!</b>"]
    if (info.get("title") or "").strip():
        lines.append(f"👉 {html.escape(info['title'].strip()[:200])}")
    if info.get("game") and not ended:
        lines.append(f"🎮 {html.escape(info['game'][:80])}")
    if ended:
        peak = int(ended.get("peak") or 0)
        tail = f", пик {peak} {_plural(peak, 'зритель', 'зрителя', 'зрителей')}" if peak else ""
        lines.append(f"⏱ {_dur(ended.get('sec') or 0)}{tail}")
    return "\n".join(lines)


def buttons(settings, yt: str = "", vod: str | None = None) -> InlineKeyboardMarkup:
    """Кнопки площадок, где идёт эфир; после — запись на каждой площадке, где он был.
    Логотипы на кнопках (icon_custom_emoji_id) в канале Telegram не показывает, поэтому площадка — в тексте и цвете."""
    if vod is not None:
        row = [InlineKeyboardButton(text="▶️ Запись на Twitch", url=vod or f"https://twitch.tv/{settings.twitch_login}/videos",
                                    style="primary")]
        if yt:
            row.append(InlineKeyboardButton(text="▶️ Запись на YouTube", url=yt, style="danger"))
        return InlineKeyboardMarkup(inline_keyboard=[row])
    row = [InlineKeyboardButton(text="Twitch", url=f"https://twitch.tv/{settings.twitch_login}", style="primary")]
    if yt:
        row.append(InlineKeyboardButton(text="YouTube", url=yt, style="danger"))
    return InlineKeyboardMarkup(inline_keyboard=[row])


async def _fetch(url: str) -> bytes | None:
    if not url:
        return None
    import aiohttp
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.get(url) as r:
                return await r.read() if r.status == 200 else None
    except Exception as exc:
        logger.debug(f"stream card fetch skip: {exc}")
        return None


async def _card(db, settings, info: dict, live: bool = True) -> bytes:
    import asyncio
    from core import social, stream_card
    try:
        prof = await social.twitch_profile(db, settings)
    except Exception as exc:
        logger.warning(f"stream card profile skip: {exc}")
        prof = {"nick": settings.twitch_login, "avatar": ""}
    thumb = info.get("thumb") or ""
    frame = await _fetch(f"{thumb}?t={int(time.time())}" if thumb else "")   # Twitch кэширует превью — сбиваем кэш
    avatar = await _fetch(prof.get("avatar") or "")
    return await asyncio.to_thread(stream_card.render, frame, avatar, prof.get("nick") or settings.twitch_login, live)


async def _json(db, key: str) -> dict:
    raw = await get_setting(db, key)
    return json.loads(raw) if raw else {}


async def _owner_photo(bot, settings, photo: bytes, text: str, kb) -> None:
    """Черновик анонса владельцу картинкой в тему «Сводки»; не вышло — текстом."""
    from aiogram.types import BufferedInputFile
    from core import topics
    tid = topics._threads.get("digest")
    for thread in ((tid, None) if tid else (None,)):
        try:
            await bot.send_photo(settings.owner_user_id, BufferedInputFile(photo, "stream.jpg"), caption=text,
                                 parse_mode="HTML", reply_markup=kb, message_thread_id=thread)
            return
        except Exception as exc:
            logger.warning(f"stream preview photo failed (thread={thread}): {exc}")
    await topics.send_owner(bot, settings.owner_user_id, "digest", text, reply_markup=kb, parse_mode="HTML")


async def on_live(bot, db, settings, live: dict) -> None:
    """Каждый снимок живого стрима (раз в 5 мин). Первый — фолловеры на старте и черновик анонса владельцу;
    дальше — правка поста в канале, если сменились название, игра или появился эфир на YouTube."""
    from core import social
    sid, now = live["stream_id"], time.time()
    info = {"title": live.get("title") or "", "game": live.get("game") or "", "thumb": live.get("thumb") or ""}
    await set_setting(db, f"streaminfo:{sid}", json.dumps(info))
    prev = await _json(db, "stream:last_live")
    await set_setting(db, "stream:last_live", json.dumps({"sid": sid, "ts": now}))
    if await get_digest_state(db, f"live:{sid}"):
        await _refresh(bot, db, settings, sid, info)
        return
    await set_digest_state(db, f"live:{sid}", "seen")
    if prev.get("sid") and prev["sid"] != sid and not prev.get("done") and now - prev.get("ts", 0) < PAUSE:
        # обрыв: Twitch дал новый id, но это тот же эфир — переносим старт и пост, второй анонс не шлём
        for key in ("streamstart", "streampost"):
            raw = await get_setting(db, f"{key}:{prev['sid']}")
            if raw:
                await set_setting(db, f"{key}:{sid}", raw)
        if await get_digest_state(db, f"announced:{prev['sid']}"):
            await set_digest_state(db, f"announced:{sid}", "sent")
        await _refresh(bot, db, settings, sid, info)
        return
    try:
        followers = await social.twitch_followers(db, settings)
    except Exception as exc:
        logger.warning(f"stream start followers skip: {exc}")
        followers = None
    try:
        start = datetime.fromisoformat(str(live.get("started_at")).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        start = now
    await set_setting(db, f"streamstart:{sid}", json.dumps({"followers": followers, "ts": start}))
    if not await autonomy.enabled(db, "announce") or not getattr(settings, "post_channel", None):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📣 Опубликовать", callback_data=f"sra:{sid}"),
                                                InlineKeyboardButton(text="Не надо", callback_data="sra:none")]])
    yt = _yt_link()
    note = (f"\n\n<i>Так будет в {html.escape(settings.post_channel)}, с кнопкой Twitch"
            f"{' и YouTube' if yt else ', YouTube добавлю, если там тоже эфир'}. По ходу эфира пост сам обновит название "
            "и игру, после — покажет длительность и запись.</i>")
    try:
        photo = await _card(db, settings, info)
    except Exception as exc:
        logger.warning(f"stream card render failed: {type(exc).__name__}: {exc}")
        photo = None
    if photo:
        await _owner_photo(bot, settings, photo, caption(info) + note, kb)
    else:
        from core.topics import send_owner
        await send_owner(bot, settings.owner_user_id, "digest", caption(info) + note, reply_markup=kb, parse_mode="HTML")


async def _refresh(bot, db, settings, sid: str, info: dict) -> None:
    post = await _json(db, f"streampost:{sid}")
    if not post or post.get("ended"):
        return
    yt = _yt_link()
    if (post.get("title"), post.get("game"), post.get("yt", "")) == (info["title"], info["game"], yt):
        return
    try:
        if post.get("photo"):
            await bot.edit_message_caption(chat_id=post["chat"], message_id=post["msg"], caption=caption(info),
                                           parse_mode="HTML", reply_markup=buttons(settings, yt))
        else:
            await bot.edit_message_text(caption(info), chat_id=post["chat"], message_id=post["msg"], parse_mode="HTML",
                                        reply_markup=buttons(settings, yt), disable_web_page_preview=True)
    except Exception as exc:
        if "not modified" not in str(exc):
            logger.warning(f"stream post refresh failed: {type(exc).__name__}: {exc}")
            return
    post.update(title=info["title"], game=info["game"], yt=yt)
    if yt:
        post["yt_rec"] = yt                  # кнопка эфира уходит вместе с эфиром, а запись на YouTube остаётся
    await set_setting(db, f"streampost:{sid}", json.dumps(post))


async def publish(bot, db, settings, sid: str) -> str:
    if await get_digest_state(db, f"announced:{sid}"):
        return "анонс уже в канале"
    info = await _json(db, f"streaminfo:{sid}")
    if not info:
        async with db.execute("SELECT title FROM social_items WHERE platform='twitch' AND item_id=?", (f"stream:{sid}",)) as cur:
            row = await cur.fetchone()
        info = {"title": row[0] if row else "", "game": "", "thumb": ""}
    yt = _yt_link()
    try:
        photo = await _card(db, settings, info)
    except Exception as exc:
        logger.warning(f"stream card render failed: {type(exc).__name__}: {exc}")
        photo = None
    if photo:
        from aiogram.types import BufferedInputFile
        msg = await bot.send_photo(settings.post_channel, BufferedInputFile(photo, "stream.jpg"), caption=caption(info),
                                   parse_mode="HTML", reply_markup=buttons(settings, yt))
    else:
        msg = await bot.send_message(settings.post_channel, caption(info), parse_mode="HTML",
                                     reply_markup=buttons(settings, yt), disable_web_page_preview=True)
    post = json.dumps({"chat": settings.post_channel, "msg": msg.message_id, "photo": bool(photo),
                       "title": info.get("title", ""), "game": info.get("game", ""), "yt": yt, "yt_rec": yt})
    await set_setting(db, f"streampost:{sid}", post)
    await set_digest_state(db, f"announced:{sid}", "sent")
    cur_sid = (await _json(db, "stream:last_live")).get("sid")
    if cur_sid and cur_sid != sid:            # нажал кнопку после обрыва — пост ведёт уже новый id
        await set_setting(db, f"streampost:{cur_sid}", post)
        await set_digest_state(db, f"announced:{cur_sid}", "sent")
    return "Анонс в канале"


async def on_offline(bot, db, settings, now_ts: float) -> None:
    """Стрима нет дольше PAUSE → пост в канале становится версией «после эфира»: серая картинка,
    длительность, пик зрителей и кнопка «Смотреть запись»."""
    from core import social
    last = await _json(db, "stream:last_live")
    if not last.get("sid") or last.get("done") or now_ts - last.get("ts", 0) < PAUSE:
        return
    last["done"] = True
    await set_setting(db, "stream:last_live", json.dumps(last))
    sid = last["sid"]
    post = await _json(db, f"streampost:{sid}")
    if not post or post.get("ended"):
        return
    info = await _json(db, f"streaminfo:{sid}") or {"title": post.get("title", ""), "game": post.get("game", "")}
    start = (await _json(db, f"streamstart:{sid}")).get("ts") or last["ts"]
    async with db.execute("SELECT MAX(value) FROM social_stats WHERE platform='twitch' AND metric='viewers' "
                          "AND entity LIKE 'stream:%' AND ts>=?", (start - 60,)) as cur:
        peak = (await cur.fetchone())[0]
    ended = {"sec": max(0, last["ts"] - start), "peak": peak}
    try:
        vod = await social.twitch_last_vod(db, settings, since=start)
    except Exception as exc:
        logger.warning(f"stream vod link skip: {exc}")
        vod = ""
    kb = buttons(settings, yt=post.get("yt_rec") or post.get("yt", ""), vod=vod)
    try:
        if post.get("photo"):
            from aiogram.types import BufferedInputFile, InputMediaPhoto
            photo = await _card(db, settings, info, live=False)
            await bot.edit_message_media(chat_id=post["chat"], message_id=post["msg"], reply_markup=kb,
                                         media=InputMediaPhoto(media=BufferedInputFile(photo, "stream.jpg"),
                                                               caption=caption(info, ended), parse_mode="HTML"))
        else:
            await bot.edit_message_text(caption(info, ended), chat_id=post["chat"], message_id=post["msg"],
                                        parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    except Exception as exc:
        logger.warning(f"stream post finish failed: {type(exc).__name__}: {exc}")
    post["ended"] = True
    await set_setting(db, f"streampost:{sid}", json.dumps(post))


async def _last_stream(db, now_ts: float):
    """(entity, начало, конец, пик, среднее) последнего стрима Twitch за LOOK_BACK или None."""
    async with db.execute("SELECT entity, MIN(ts), MAX(ts), MAX(value), AVG(value) FROM social_stats WHERE platform='twitch' "
                          "AND metric='viewers' AND entity LIKE 'stream:%' AND ts>? GROUP BY entity ORDER BY MAX(ts) DESC LIMIT 1",
                          (now_ts - LOOK_BACK,)) as cur:
        return await cur.fetchone()


async def covers(db) -> bool:
    """Сводку чата забирает итог стрима: он включён, недавно был стрим Twitch и итог по нему ещё не ушёл."""
    if not await autonomy.enabled(db, "streamreport"):
        return False
    row = await _last_stream(db, time.time())
    return bool(row) and not await get_digest_state(db, f"report:{row[0]}")


def _dur(sec: float) -> str:
    h, m = int(sec // 3600), int(sec % 3600 // 60)
    return f"{h} ч {m} мин" if h else f"{m} мин"


async def _summary(db, key: str, since: float):
    raw = await get_setting(db, key)
    s = json.loads(raw) if raw else None
    return s if s and s["ts"] >= since else None


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    return one if n % 10 == 1 and n % 100 != 11 else few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


async def build(db, settings, row, now_ts: float) -> str:
    from core import social
    entity, first_ts, last_ts, peak, avg = row
    async with db.execute("SELECT title, published FROM social_items WHERE platform='twitch' AND item_id=?", (entity,)) as cur:
        item = await cur.fetchone()
    title = (item[0] if item else "") or "без названия"
    try:
        start = datetime.fromisoformat(str(item[1]).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, AttributeError):
        start = first_ts
    stats = f"⏱ {_dur(last_ts - start)} · пик {int(peak or 0)}, в среднем {round(avg or 0)} зрителей"
    begin = json.loads(await get_setting(db, f"streamstart:{entity.split(':', 1)[1]}") or "{}")
    try:
        now_f = await social.twitch_followers(db, settings)
    except Exception as exc:
        logger.warning(f"stream report followers skip: {exc}")
        now_f = None
    if now_f is not None and begin.get("followers") is not None:
        d = int(now_f) - int(begin["followers"])
        stats += f" · {'+' if d >= 0 else ''}{d} {_plural(d, "фолловер", "фолловера", "фолловеров")}"
    parts = [f"🎬 Итог стрима: {title[:120]}", stats]
    yt_live = json.loads(await get_setting(db, "youtube:last_live") or "{}")
    if yt_live.get("end", 0) >= start:
        parts.append(f"▶️ На YouTube тоже был эфир, в чате {yt_live.get('n', 0)} {_plural(yt_live.get('n', 0), 'сообщение', 'сообщения', 'сообщений')}")
    for key, head in (("twitch:last_summary", "🟣 Чат Twitch"), ("youtube:last_summary", "▶️ Чат YouTube")):
        s = await _summary(db, key, start)
        if s:
            parts.append(f"{head}, {s['n']} {_plural(s['n'], 'сообщение', 'сообщения', 'сообщений')}:\n{s['text'][:_SUM_MAX]}")
    return "\n\n".join(parts)


async def tick(bot, db, settings, now_ts: float) -> None:
    from core.topics import send_owner
    if not await autonomy.enabled(db, "streamreport"):
        return
    row = await _last_stream(db, now_ts)
    if not row or now_ts - row[2] < REPORT_AFTER or await get_digest_state(db, f"report:{row[0]}"):
        return
    await set_digest_state(db, f"report:{row[0]}", "sent")
    text = await build(db, settings, row, now_ts)
    kb = None
    if await autonomy.enabled(db, "board") and settings.github_token and settings.vault_repo \
            and not await get_digest_state(db, f"boardask:{row[0]}"):
        await set_digest_state(db, f"boardask:{row[0]}", "sent")
        try:
            title = text.split("\n", 1)[0].replace("🎬 Итог стрима: ", "")
            kb = await content_board.board_keyboard(db, settings, title, now_ts)
            text += "\n\nКакая это была карточка на доске?"
        except Exception as exc:
            logger.warning(f"stream report board skip: {type(exc).__name__}: {exc}")
    await send_owner(bot, settings.owner_user_id, "digest", text[:4000], reply_markup=kb, parse_mode=None)
