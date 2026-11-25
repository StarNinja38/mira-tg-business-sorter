"""
v4.62: «Мира в чёлке» — локальный HTTP-вход к Мире для приложения на Mac владельца (форк Coucou).

Слушает ТОЛЬКО 127.0.0.1:MIRA_API_PORT (наружу порт не открыт); Mac приходит через SSH-туннель
(launchd на Mac: ssh -L 8787:127.0.0.1:8787 sn-ams). Доступ — Bearer MIRA_API_TOKEN (без токена вход выключен).

POST /mira/chat   {"text": "...", "context": "окно/файл (необяз.)"} → {"reply": "...", "tools": [...]}
GET  /mira/status → {"lines": [...]} — коротко на сегодня: дела, пары, ближайшие списания
GET  /mira/ping   → {"ok": true}

Та же Мира, что в Telegram: assistant_context + think, общий диалог (память). Карточки подтверждения
(закрыть/перенести задачу, черновик, «Запомнила» с отменой) уходят владельцу в Telegram — чёлка
показывает только текст ответа.
"""
import asyncio
import hmac
import json

from loguru import logger

_lock = asyncio.Lock()   # одна реплика за раз — как в чате Telegram


class _Chat:
    def __init__(self, cid):
        self.id = cid


class _OwnerMessage:
    """Минимальная замена aiogram.Message для инструментов Миры: карточки идут владельцу в личку."""
    def __init__(self, bot, owner_id):
        self.bot = bot
        self.chat = _Chat(owner_id)
        self.message_thread_id = None

    async def reply(self, text, **kw):
        return await self.bot.send_message(self.chat.id, text, **kw)

    answer = reply


def _authorized(request, token: str) -> bool:
    got = request.headers.get("Authorization", "")
    return bool(token) and hmac.compare_digest(got.encode(), f"Bearer {token}".encode())


async def start(bot, db, settings, client):
    """Поднять вход. None — если выключен (нет токена или Мира v2 выключена)."""
    token = getattr(settings, "mira_api_token", "") or ""
    if not token or not getattr(settings, "mira_v2", False):
        logger.info("Mira API off (нет MIRA_API_TOKEN или MIRA_V2=false)")
        return None
    from aiohttp import web
    from core import mira

    async def ping(request):
        if not _authorized(request, token):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"ok": True})

    async def status(request):
        if not _authorized(request, token):
            return web.json_response({"error": "unauthorized"}, status=401)
        lines = []
        try:
            from core.util import today_local
            day = (await mira._day_block(db, settings, today_local())).strip()
            if day and day != "ничего не запланировано":
                lines += day.splitlines()[:6]
        except Exception as exc:
            logger.debug(f"mira api status day skip: {exc}")
        try:
            from core import subscriptions as _subs
            lines += [f"💳 {l}" for l in (await _subs.digest_lines(db))[:3]]
        except Exception:
            pass
        return web.json_response({"lines": lines})

    async def chat(request):
        if not _authorized(request, token):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        text = str(body.get("text") or "").strip()[:4000]
        if not text:
            return web.json_response({"error": "empty"}, status=400)
        extra = str(body.get("context") or "").strip()[:300]
        query = f"{text}\n(контекст на Mac: {extra})" if extra else text
        msg = _OwnerMessage(bot, settings.owner_user_id)
        async with _lock:
            try:
                ctx = await mira.assistant_context(db, settings, query)
                reply, used, notes = await asyncio.wait_for(
                    mira.think(client, settings, ctx, query, message=msg, bot=bot, db=db), timeout=120)
            except Exception as exc:
                logger.warning(f"mira api chat failed: {type(exc).__name__}: {exc}")
                return web.json_response({"error": f"{type(exc).__name__}"}, status=500)
            await mira.dialog_add(db, "owner", f"[чёлка] {text}")
            await mira.dialog_add(db, "mira", reply, used)
            if notes:   # «📝 Запомнила» с отменой — в Telegram, как из обычного чата
                try:
                    import secrets
                    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
                    from db import set_setting
                    tok = secrets.token_hex(3)
                    await set_setting(db, f"mundo:{tok}", json.dumps([[n[0], n[1]] for n in notes], ensure_ascii=False))
                    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ Отменить", callback_data=f"mundo:{tok}")]])
                    await msg.reply("📝 Запомнила (из чёлки): " + "; ".join(n[1][:90] for n in notes[:3]), reply_markup=kb, parse_mode=None)
                except Exception as exc:
                    logger.debug(f"mira api notes skip: {exc}")
        logger.info(f"mira_api: tools={used or '-'} len={len(reply)}")
        return web.json_response({"reply": reply, "tools": used})

    app = web.Application(client_max_size=64 * 1024)
    from core import webapp_api
    webapp_api.setup(app, bot, db, settings, client)
    app.router.add_get("/mira/ping", ping)
    app.router.add_get("/mira/status", status)
    app.router.add_post("/mira/chat", chat)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    port = int(getattr(settings, "mira_api_port", 8787) or 8787)
    await web.TCPSite(runner, "127.0.0.1", port).start()
    logger.info(f"Mira API listening on 127.0.0.1:{port}")
    return runner
