"""
v4.97 vidIQ для Миры: исследования YouTube через MCP-сервер vidIQ по API-ключу.

Каждый запрос стоит 5 кредитов (на Free 150 в месяц + бонусные), поэтому открыт узкий набор
только для чтения и дневной лимит. Генерация (миниатюры, озвучка, видео) и правки закрыты.
"""
import json
import time

import aiohttp
from loguru import logger

URL = "https://mcp.vidiq.com/mcp"
DAILY_CAP = 3
TOOLS = {
    "keyword_research": "vidiq_keyword_research",
    "outliers": "vidiq_outliers",
    "trending": "vidiq_trending_videos",
    "channel_stats": "vidiq_channel_stats",
    "similar_channels": "vidiq_similar_channels",
    "comment_insights": "vidiq_comment_insights",
    "score_title": "vidiq_score_title",
    "generate_titles": "vidiq_generate_titles",
    "social_outliers": "vidiq_instagram_tiktok_outlier_search",
    "balance": "vidiq_balance",
}
FREE = {"balance"}
_TIMEOUT = aiohttp.ClientTimeout(total=90)
_MAX = 6000


def ready(settings) -> bool:
    return bool(getattr(settings, "vidiq_api_key", None))


def _parse(text: str) -> dict:
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:])
    return json.loads(text)


async def _call(settings, name: str, args: dict) -> str:
    headers = {"Authorization": f"Bearer {settings.vidiq_api_key}", "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "mira", "version": "1"}}}
        async with s.post(URL, headers=headers, json=init) as r:
            r.raise_for_status()
            sid = r.headers.get("Mcp-Session-Id")
        if sid:
            headers["Mcp-Session-Id"] = sid
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": args}}
        async with s.post(URL, headers=headers, json=call) as r:
            data = _parse(await r.text())
    if data.get("error"):
        return f"vidIQ ошибка: {data['error'].get('message', '')[:300]}"
    res = data.get("result") or {}
    text = "\n".join(c.get("text", "") for c in res.get("content") or [] if c.get("type") == "text")
    return (("vidIQ ошибка: " if res.get("isError") else "") + text)[:_MAX] or "пусто"


async def _spent(db, day: str) -> int:
    from db import get_setting
    return int(await get_setting(db, f"vidiq:calls:{day}") or 0)


async def tool(args: dict, db, settings, today) -> str:
    from db import set_setting
    action = args.get("action")
    if action not in TOOLS:
        return "неизвестное действие vidIQ"
    day = today.isoformat()
    paid = action not in FREE
    if paid:
        used = await _spent(db, day)
        if used >= DAILY_CAP:
            return f"лимит vidIQ на сегодня исчерпан ({DAILY_CAP} запроса, по 5 кредитов): скажи владельцу, завтра можно снова"
    params = dict(args.get("params")) if isinstance(args.get("params"), dict) else {}
    if params.get("channelId") == "me":
        params["channelId"] = settings.youtube_channel_id or ""
    started = time.time()
    try:
        out = await _call(settings, TOOLS[action], params)
    except Exception as exc:
        logger.warning(f"vidiq {action} failed: {type(exc).__name__}: {exc}")
        return "vidIQ не ответил, попробуй позже"
    if paid and not out.startswith("vidIQ ошибка"):
        await set_setting(db, f"vidiq:calls:{day}", str(await _spent(db, day) + 1))
    logger.info(f"vidiq {action} {time.time() - started:.1f}s")
    return out
