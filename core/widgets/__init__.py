# -*- coding: utf-8 -*-
"""v4.107: общая основа HTML-виджетов (Bot API 10.4) — отправка, правка данных, разбор нажатий.

Виджет = неизменный HTML + данные (chat_data ≤ 1 КБ), которые пишет только бот. Нажатие в виджете
приходит обновлением widget_query с data ≤ 64 байт. Маршрут по префиксу data: «hb:…» привычки,
«df:…» черновики, «mt:…» встреча, «mk:…» рынок, «cl:…» звонок; без известного префикса — «Мой день» (s/d/u/t).
aiogram 3.31 не знает 10.4, поэтому методы зовём напрямую, а обновление ловим outer-middleware.
"""
import asyncio
import json

import aiohttp
from loguru import logger

_API = "https://api.telegram.org/bot{token}/{method}"
LIMIT = 1000          # chat_data ≤ 1024 байт, с запасом
_KINDS: dict = {}     # префикс → async fn(ctx, act, arg) -> str (текст ответа виджету)

# Общий стиль: отступы пузыря оставляем штатными (margin body), цвета — из темы чата.
BASE_CSS = (
    "body{margin:var(--tg-text-padding,8px 12px);font:var(--tg-text-font-size,15px)/1.3 system-ui;"
    "color:var(--tg-theme-text-color)}"
    ".c,#n{color:var(--tg-theme-hint-color);font-size:.82em;font-variant-numeric:tabular-nums}"
    ".h{display:flex;justify-content:space-between;align-items:baseline;gap:8px}.h b{font-size:1.1em}"
    "button{flex:none;padding:4px 10px;min-height:0;height:auto;font-size:.85em;"
    "background:var(--tg-theme-secondary-bg-color);color:var(--tg-theme-link-color)}"
    "#n{min-height:1.2em;padding-top:4px}"
)
# Общий скрипт: W, $, load/note/ask. ask(q, el) — запрос по нажатию, el гаснет до ответа.
BASE_JS = (
    "const W=window.TelegramWidget,$=i=>document.getElementById(i);let S={},T=0;"
    "function load(){try{const s=JSON.parse(W.chatData);if(s)S=s}catch(e){}}"
    "function note(s){$('n').textContent=s||'';clearTimeout(T);if(s)T=setTimeout(()=>$('n').textContent='',5000)}"
    "function ask(q,el){if(el)el.classList.add('w');W.request(q).then(a=>{if(a.result)note(a.result);"
    "W.HapticFeedback&&W.HapticFeedback.notificationOccurred('success')},e=>{if(el)el.classList.remove('w');"
    "note(e.code=='RATE_LIMITED'?'Слишком часто, подожди секунду':'Не вышло: '+e.code)})}"
    "function el(t,c,x){const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}"
)


def page(css: str, body: str, js: str) -> str:
    return f"<style>{BASE_CSS}{css}</style>{body}<div id=\"n\"></div><script>{BASE_JS}{js}</script>"


def sizes(html: str) -> tuple[int, int]:
    import zlib
    raw = html.encode("utf-8")
    return len(raw), len(zlib.compress(raw, 9))


def dump(state: dict) -> str:
    return json.dumps(state, ensure_ascii=False, separators=(",", ":"))


def nbytes(state: dict) -> int:
    return len(dump(state).encode("utf-8"))


# v4.110: 13.11.2026 Telegram запретил виджеты обычным ботам (INPUT_MEDIA_WIDGET_NOT_ALLOWED, даже в личке).
# Запомнив отказ, не дёргаем sendWidget BLOCK_S секунд, а отвечаем им же сразу — модули шлют текстовый запасной
# вариант. Раз в BLOCK_S пробуем снова: когда Telegram снимет запрет, виджеты вернутся сами.
NOT_ALLOWED = "WIDGET_NOT_ALLOWED"
BLOCK_S = 3 * 3600
_BLOCK = {"until": 0.0}


def blocked() -> bool:
    import time
    return time.time() < _BLOCK["until"]


def refused(err: str) -> bool:
    """Отказ Telegram в виджетах как таковых — тогда нужен текстовый запасной вариант."""
    return NOT_ALLOWED in (err or "")


async def _post(token: str, method: str, payload: dict) -> dict:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as s:
            async with s.post(_API.format(token=token, method=method), json=payload) as resp:
                return await resp.json(content_type=None)
    except Exception as exc:
        return {"ok": False, "description": f"{type(exc).__name__}: {exc}"}


async def call(token: str, method: str, payload: dict) -> dict:
    import time
    if method == "sendWidget" and blocked():
        return {"ok": False, "description": f"Bad Request: INPUT_MEDIA_{NOT_ALLOWED} (запомнила отказ Telegram)"}
    data = await _post(token, method, payload)
    if method == "sendWidget":
        if data.get("ok"):
            _BLOCK["until"] = 0.0
        elif refused(str(data.get("description") or "")):
            if not blocked():
                logger.warning("Telegram не пускает виджеты бота — шлю текстом, повторю через 3 ч")
            _BLOCK["until"] = time.time() + BLOCK_S
    return data


async def send(settings, chat_id: int, html: str, state: dict, thread_id=None,
               caption: str = "") -> tuple[bool, int, str]:
    """sendWidget → (успех, message_id, описание ошибки). Без темы, если тема не принята."""
    payload = {"chat_id": chat_id, "html": html, "chat_data": dump(state),
               "allow_queries": True, "disable_notification": True}
    if caption:
        payload["caption"] = caption
    if thread_id:
        payload["message_thread_id"] = thread_id
    data = await call(settings.bot_token, "sendWidget", payload)
    if not data.get("ok") and thread_id:
        payload.pop("message_thread_id", None)
        data = await call(settings.bot_token, "sendWidget", payload)
    if not data.get("ok"):
        desc = str(data.get("description") or "нет ответа")
        logger.warning(f"sendWidget failed: {desc}")
        return False, 0, desc
    return True, int((data.get("result") or {}).get("message_id") or 0), ""


async def edit(settings, chat_id: int, message_id: int, state: dict) -> tuple[bool, str]:
    data = await call(settings.bot_token, "editMessageWidgetData",
                      {"chat_id": chat_id, "message_id": message_id, "chat_data": dump(state)})
    if data.get("ok"):
        return True, ""
    desc = str(data.get("description") or "")
    return ("not modified" in desc.lower()), desc


async def answer(settings, qid: str, text: str) -> None:
    await call(settings.bot_token, "answerWidgetQuery", {"widget_query_id": qid, "data": (text or "")[:300]})


async def bg(coro, what: str) -> None:
    try:
        await coro
    except Exception as exc:
        logger.warning(f"widget {what}: {exc}")


async def within(coro, slow_text: str, after=None, timeout: float = 10) -> str:
    """Ответить надо за 15 с: ждём до timeout, иначе отвечаем slow_text и досчитываем в фоне (+ after)."""
    job = asyncio.ensure_future(coro)
    try:
        return await asyncio.wait_for(asyncio.shield(job), timeout=timeout)
    except asyncio.TimeoutError:
        async def _later():
            await job
            if after:
                await after()
        asyncio.create_task(bg(_later(), "slow action"))
        return slow_text


async def get_json(db, key: str) -> dict:
    from db import get_setting
    try:
        return json.loads(await get_setting(db, key) or "{}")
    except Exception:
        return {}


async def set_json(db, key: str, value: dict) -> None:
    from db import set_setting
    await set_setting(db, key, json.dumps(value, ensure_ascii=False))


def register(prefix: str, fn) -> None:
    _KINDS[prefix] = fn


def _load_kinds() -> None:
    # v4.107.1: грузим все всегда (повторный импорт бесплатный). Раньше выходили при непустом _KINDS:
    # цикл дайджеста импортирует только habits → «df/mt/mk» не регистрировались → «Не поняла нажатие»
    from core.widgets import habits, drafts, meeting, market
    for prefix, mod in (("hb", habits), ("df", drafts), ("mt", meeting), ("mk", market)):
        _KINDS.setdefault(prefix, mod.on_query)


async def handle(ctx: dict, wq: dict) -> None:
    from core import owners
    settings = ctx["settings"]
    qid = str(wq.get("id") or "")
    if not qid:
        return
    uid = int((wq.get("from") or {}).get("id") or 0)
    if not owners.is_owner(settings.owner_user_id, uid):
        await answer(settings, qid, "Недоступно")
        return
    data = str(wq.get("data") or "")
    _load_kinds()
    prefix, _, rest = data.partition(":")
    fn = _KINDS.get(prefix)
    if fn is None:   # «Мой день» (v4.106) — свои короткие команды без префикса
        from core import daywidget
        await daywidget.handle_query(ctx["db"], settings, wq)
        return
    act, _, arg = rest.partition(":")
    ctx = dict(ctx, wq=wq, chat_id=(wq.get("chat") or {}).get("id"), message_id=wq.get("message_id"))
    try:
        text = await fn(ctx, act, arg)
    except Exception as exc:
        logger.warning(f"widget {prefix}:{act} failed: {type(exc).__name__}: {exc}")
        text = "Не вышло, попробуй ещё раз"
    await answer(settings, qid, text or "")


async def middleware(handler, event, data):
    """outer-middleware на dp.update: перехватывает widget_query до aiogram (он его не знает)."""
    extra = getattr(event, "model_extra", None) or {}
    wq = extra.get("widget_query") or extra.get("shared_widget_query")
    if wq is None:
        return await handler(event, data)
    try:
        await handle({"db": data["db"], "settings": data["settings"], "bot": data.get("bot"),
                      "client": data.get("anthropic_client")}, wq)
    except Exception as exc:
        logger.warning(f"widget_query failed: {type(exc).__name__}: {exc}")
    return None
