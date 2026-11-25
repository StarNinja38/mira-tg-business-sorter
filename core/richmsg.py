# -*- coding: utf-8 -*-
"""
Rich messages (Bot API 10.1) — таблицы и расширенная вёрстка в сообщениях.

aiogram 3.28.2 покрывает Bot API 10.0 и метода sendRichMessage ещё не имеет,
поэтому зовём метод напрямую (raw HTTP). InputRichMessage — это HTML-строка
с поддержкой <table>, <details>, <tg-time> и т.п.

Деградация: любой сбой (метод недоступен, клиент старый, HTML не принят) →
возвращаем False, вызывающий код шлёт обычное текстовое сообщение.
После выхода aiogram с Bot API 10.1 модуль можно заменить на bot.send_rich_message.
"""
import asyncio
import time
from core import llm

import aiohttp
from loguru import logger

_API = "https://api.telegram.org/bot{token}/sendRichMessage"
_draft_seq = [1000]
_active_thinking: dict = {}   # chat_id → живой индикатор «Думаю», чтобы карточка могла его погасить


def _next_draft_id() -> int:
    _draft_seq[0] += 1
    return _draft_seq[0]


async def pause_thinking(chat_id) -> None:
    """v4.86.2: перед карточкой посреди ответа гасим «Думаю». Иначе таймер перерисовывает
    черновик после карточки, и в Telegram остаётся висеть плашка «Думаю 6с»."""
    t = _active_thinking.get(chat_id)
    if t is not None:
        await t.stop()


async def send_rich(token: str, chat_id: int, html: str,
                    thread_id=None, disable_notification: bool = False) -> bool:
    """Отправить rich-сообщение (HTML с таблицами). True = успех."""
    payload = {
        "chat_id": chat_id,
        "rich_message": {"html": html},
    }
    if thread_id:
        payload["message_thread_id"] = thread_id
    if disable_notification:
        payload["disable_notification"] = True
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(_API.format(token=token), json=payload) as resp:
                data = await resp.json(content_type=None)
        if data.get("ok"):
            return True
        logger.warning(f"sendRichMessage failed: {data.get('description')}")
        return False
    except Exception as exc:
        logger.warning(f"sendRichMessage error: {exc}")
        return False


def first_text(resp) -> str:
    """C24: первый текстовый блок ответа (пустой content не роняет вызывающего)."""
    try:
        return next((getattr(b, "text", None) for b in (resp.content or []) if getattr(b, "text", None)), "") or ""
    except Exception:
        return ""


async def send_rich_draft(token: str, chat_id: int, draft_id: int, html: str,
                          thread_id=None, can_stop: bool = False, keep_on_stop: bool = True) -> bool:
    """Стрим частичного rich-сообщения (sendRichMessageDraft, 10.1).
    Черновик эфемерен (~30с) — финал обязан уйти обычным сообщением. True = успех."""
    payload = {
        "chat_id": chat_id,
        "draft_id": draft_id,
        "rich_message": {"html": html},
    }
    if thread_id:
        payload["message_thread_id"] = thread_id
    if can_stop:   # B8 (Bot API 10.2): кнопка «Стоп» у потокового черновика
        payload["can_stop"] = True
        payload["keep_on_stop"] = bool(keep_on_stop)
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(_API.format(token=token).replace(
                    "sendRichMessage", "sendRichMessageDraft"), json=payload) as resp:

                data = await resp.json(content_type=None)
        if data.get("ok"):
            return True
        logger.debug(f"sendRichMessageDraft failed: {data.get('description')}")
        return False
    except Exception as exc:
        logger.debug(f"sendRichMessageDraft error: {exc}")
        return False


async def _push_draft(bot, chat_id, draft_id, thread, rich_html, plain_text, use_rich: bool) -> bool:
    """Обновить живой draft: rich (10.1) пока работает, иначе plain send_message_draft (9.3)."""
    try:
        if use_rich:
            ok = await send_rich_draft(bot.token, chat_id, draft_id, rich_html, thread)
            if ok:
                return True
        # fallback / основной путь: 9.3 (самый поддерживаемый — как в /ask)
        kw = {"chat_id": chat_id, "draft_id": draft_id, "text": plain_text[:3900], "parse_mode": None}
        if thread:
            kw["message_thread_id"] = thread
        await bot.send_message_draft(**kw)
    except Exception:
        pass
    return False


MIRA_EMOJI_ID = "5418061652895513703"   # огонёк из набора mira_flame_by_Sort38_bot


def mira_emoji(fallback: str = "🔥") -> str:
    return f'<tg-emoji emoji-id="{MIRA_EMOJI_ID}">{fallback}</tg-emoji>'


def thinking_label(text: str, seconds: float) -> str:
    return f"{text} {int(seconds)}с"


class thinking:
    """async-контекст «бот думает…»: статичный живой draft, пока крутится не-стримовая операция
    (напр. сбор памяти из JSON). Использует send_message_draft (9.3) как у /ask + 10.1-попытку.
        async with richmsg.thinking(bot, chat_id, thread, "🤖 собираю память…"):
            await op()
        await send_real_message(...)
    """
    def __init__(self, bot, chat_id: int, thread=None, text: str = "🤖 думаю…", timer: bool = False):
        self.timer = timer    # v4.77: «🔥 Думаю 3с», счётчик секунд как живой индикатор
        self.bot = bot
        self.chat_id = chat_id
        self.thread = thread
        self.text = text
        self.draft_id = _next_draft_id()
        self._task = None
        self._stop = asyncio.Event()
        self._use_rich = True

    async def _loop(self):
        try:
            started = time.monotonic()
            while not self._stop.is_set():
                label = thinking_label(self.text, time.monotonic() - started) if self.timer else self.text
                shown = label.replace("🔥", mira_emoji(), 1) if self.timer else label
                self._use_rich = await _push_draft(
                    self.bot, self.chat_id, self.draft_id, self.thread,
                    f"<tg-thinking>{shown}</tg-thinking>", label, self._use_rich)   # C23: rich упал → дальше plain
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=2 if self.timer else 4)
                except asyncio.TimeoutError:
                    pass
        except Exception:
            pass

    async def __aenter__(self):
        try:
            self._task = asyncio.create_task(self._loop())
            _active_thinking[self.chat_id] = self
        except Exception:
            self._task = None
        return self

    async def stop(self):
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=1)
            except Exception:
                self._task.cancel()

    async def __aexit__(self, *exc):
        await self.stop()
        if _active_thinking.get(self.chat_id) is self:
            _active_thinking.pop(self.chat_id, None)
        return False


async def stream_to_draft(bot, client, chat_id, thread, *, model, system, content,
                          max_tokens: int = 700, label: str = "🤖 пишу…", kind: str = "stream") -> str:
    """Стримит ответ Claude в ЖИВОЙ draft (как /ask): токены появляются в реальном времени.
    Draft эфемерный — caller обязан отправить настоящее сообщение. Возвращает финальный текст.
    Фолбэк: если стрим/draft не вышел — обычный messages.create."""
    import time as _t
    import html as _h
    from core.usage import record as _rec
    draft_id = _next_draft_id()
    buf, last, use_rich = "", 0.0, True
    try:
        use_rich = await send_rich_draft(bot.token, chat_id, draft_id,
                                         f"<tg-thinking>{label}</tg-thinking>", thread)
        if not use_rich:
            await _push_draft(bot, chat_id, draft_id, thread, "", label, False)
        async with client.messages.stream(model=model, max_tokens=max_tokens, system=system,
                                           messages=[{"role": "user", "content": content}]) as stream:
            async for chunk in stream.text_stream:
                buf += chunk
                now = _t.monotonic()
                if now - last >= 0.6 and buf.strip():
                    last = now
                    use_rich = await _push_draft(
                        bot, chat_id, draft_id, thread,
                        f"<p>{_h.escape(buf[:3500])}</p><tg-thinking>печатаю…</tg-thinking>",
                        buf, use_rich)
            resp = await stream.get_final_message()
        await _rec(model, resp.usage, kind)
        return first_text(resp).strip()
    except Exception as exc:
        logger.debug(f"stream_to_draft fallback: {exc}")
        resp = await llm.create(client, model=model, max_tokens=max_tokens, system=system,
                                            messages=[{"role": "user", "content": content}])
        await _rec(model, resp.usage, kind)
        return first_text(resp).strip()


async def try_rich(message, html: str) -> bool:
    """Ответить rich-сообщением в тот же чат/тему, где пришла команда.
    False → вызывающий код шлёт обычный текст."""
    return await send_rich(message.bot.token, message.chat.id, html,
                           getattr(message, "message_thread_id", None))


def button(text: str, data: str, style: str | None = None, disabled: bool = False) -> str:
    """v4.34 (Bot API 10.3): callback-кнопка ВНУТРИ rich-сообщения (в <p>, ячейке таблицы
    или <tg-button-row>). style: success|primary|danger|link. disabled → серая, ничего не делает.
    text — уже экранирован; data ≤ 64 байт."""
    if disabled:
        return f'<tg-button type="disabled">{text}</tg-button>'
    st = f' style="{style}"' if style else ""
    return f'<tg-button type="callback_data" data="{data}"{st}>{text}</tg-button>'


def button_row(buttons: list[str], align: str = "left") -> str:
    """Ряд из 1-8 кнопок (InputRichBlockButtons ↔ <tg-button-row>)."""
    return f'<tg-button-row align="{align}">' + "".join(buttons[:8]) + "</tg-button-row>"


def expandable_quote(text: str) -> str:
    """Раскрываемая цитата (10.3) — длинный текст свёрнут, разворачивается тапом."""
    return f"<blockquote expandable>{text}</blockquote>"


def table(headers: list[str], rows: list[list[str]], caption: str = "",
          bordered: bool = True, striped: bool = True, compact: bool = False) -> str:
    """Собрать HTML-таблицу для rich-сообщения (синтаксис Bot API 10.1/10.3:
    <table bordered striped compact>). Ячейки должны быть уже экранированы."""
    attrs = ("" if not bordered else " bordered") + ("" if not striped else " striped") + (" compact" if compact else "")
    cap = f"<caption>{caption}</caption>" if caption else ""
    # v4.113: пустые заголовки не рисуем — Telegram показывал их пустой первой строкой (подготовка к встрече)
    head = "<tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr>" if any(str(h).strip() for h in headers) else ""
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table{attrs}>{cap}{head}{body}</table>"
