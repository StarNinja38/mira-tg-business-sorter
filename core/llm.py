"""
Гибрид-фолбэк LLM (v4.3).

Основной путь — ПРЯМОЙ Anthropic: сохраняет prompt-кэш (TTL=1h на EXTRACT_SYSTEM,
~30% экономии на input) и минимальную задержку. OpenRouter включается ТОЛЬКО когда
прямой вызов Anthropic упал (сеть/overload/5xx после ретраев SDK). Фолбэк редкий →
его наценка (~5%) и отсутствие кэша НЕ влияют на месячный счёт; он лишь страхует от
простоя Anthropic (bus-factor по LLM-провайдеру — замечание council).

Без OPENROUTER_API_KEY фолбэк выключен: create() ведёт себя ровно как
client.messages.create() — при сбое пробрасывает исходное исключение Anthropic
(settle-петля НЕ снимет dirty и повторит проход, задачи не теряются).

Возвращаемый при фолбэке объект совместим ПО ЧТЕНИЮ с ответом Anthropic:
.content[0].text и .usage (.input_tokens/.output_tokens). Кэш-поля отсутствуют →
usage.record() прочитает их через getattr как 0 (в фолбэке кэша и нет).
"""
import asyncio
import re
import json
import urllib.request
from typing import Optional

from loguru import logger

from config import settings

_OR_URL = "https://openrouter.ai/api/v1/chat/completions"

# Anthropic model id → OpenRouter slug. Фолбэк идёт на ТОТ ЖE Claude (тот же интеллект),
# чтобы поведение бота в аварийном режиме не менялось.
_OR_SLUG = {
    "claude-haiku-4-5": "anthropic/claude-haiku-4.5",
    "claude-sonnet-4-5": "anthropic/claude-sonnet-4.5",
    "claude-sonnet-5-5": "anthropic/claude-sonnet-5.5",
    "claude-haiku-5-5": "anthropic/claude-haiku-5.5",
}


def _or_slug(model: str) -> str:
    if model in _OR_SLUG:
        return _OR_SLUG[model]
    if model.startswith("anthropic/"):
        return model
    # общий случай: claude-<name>-<maj>-<min> → anthropic/claude-<name>-<maj>.<min>
    if model.startswith("claude-"):
        model = re.sub(r"-\d{8}$", "", model)   # C22: датированный id (…-20250929) → без даты
        parts = model.rsplit("-", 2)
        if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
            return f"anthropic/{parts[0]}-{parts[1]}.{parts[2]}"
        return f"anthropic/{model}"
    return model


class _Usage:
    __slots__ = ("input_tokens", "output_tokens")

    def __init__(self, i: int, o: int):
        self.input_tokens = i
        self.output_tokens = o


class _Block:
    __slots__ = ("text", "type")

    def __init__(self, text: str):
        self.text = text
        self.type = "text"


class _Resp:
    """Мини-объект, совместимый по чтению с ответом Anthropic: .content[0].text и .usage."""
    __slots__ = ("content", "usage", "stop_reason")

    def __init__(self, text: str, usage: _Usage):
        self.content = [_Block(text)]
        self.usage = usage
        self.stop_reason = "end_turn"


def _sys_to_text(system) -> str:
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n".join(b.get("text", "") for b in system if isinstance(b, dict))
    return ""


def _msgs_to_openai(system, messages) -> list[dict]:
    """Anthropic messages (+ system-блоки с cache_control) → OpenAI-совместимый формат.
    cache_control отбрасывается (в фолбэке кэш не нужен)."""
    out: list[dict] = []
    sys_text = _sys_to_text(system)
    if sys_text:
        out.append({"role": "system", "content": sys_text})
    for m in messages or []:
        c = m.get("content")
        if isinstance(c, list):
            c = "\n".join(
                b.get("text", "") for b in c
                if isinstance(b, dict) and b.get("type") == "text"
            )
        out.append({"role": m.get("role", "user"), "content": c})
    return out


def _openrouter_call(model: str, max_tokens: int, system, messages, temperature) -> _Resp:
    """Блокирующий HTTP к OpenRouter (только stdlib). Вызывать через asyncio.to_thread."""
    key = settings.openrouter_api_key
    body = json.dumps({
        "model": _or_slug(model),
        "max_tokens": max_tokens,
        "temperature": 0 if temperature is None else temperature,
        "messages": _msgs_to_openai(system, messages),
    }).encode("utf-8")
    req = urllib.request.Request(_OR_URL, data=body, headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://t.me/Sort38_bot",
        "X-Title": "tg-sorter-fallback",
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode("utf-8"))
    text = data["choices"][0]["message"]["content"]
    u = data.get("usage") or {}
    usage = _Usage(int(u.get("prompt_tokens", 0) or 0), int(u.get("completion_tokens", 0) or 0))
    return _Resp(text, usage)


async def openrouter_direct(model: str, system: str, user: str, max_tokens: int = 1024):
    """Прямой вызов diverse-модели через OpenRouter (v4.4, «второе мнение» — НЕ фолбэк).
    Модель передаётся слагом as-is (напр. 'google/gemini-2.5-flash'). Возвращает объект,
    совместимый по чтению с ответом Anthropic (.content[0].text / .usage). Бросает при сбое.
    Caller ОБЯЗАН проверить settings.openrouter_api_key заранее."""
    try:
        return await asyncio.to_thread(
            _openrouter_call, model, max_tokens, system,
            [{"role": "user", "content": user}], 0)
    except Exception as exc:
        if _is_payment_error(exc):   # v4.33: пустой OpenRouter → алерт владельцу (раньше — только warning в лог)
            await asyncio.to_thread(_billing_alert, str(exc), "openrouter")
        raise


def _is_payment_error(exc) -> bool:
    s = str(exc).lower()
    return ("402" in s or "payment required" in s or "insufficient" in s
            or "credit balance" in s or "billing" in s)


# v4.19 биллинг-алерт: бот 3 дня МОЛЧА жил на резерве (Anthropic кончился 18.08, владелец
# узнал случайно). Шлём владельцу алерт напрямую через Telegram HTTP API (без bot/db —
# llm.py вызывается отовсюду), не чаще раза в 6 часов.
_billing_alerted = {"anthropic": 0.0, "openrouter": 0.0}

_ALERT_TEXT = {
    "anthropic": ("🚨 Кредиты Anthropic закончились — перехожу на резерв (OpenRouter), если он есть. "
                  "Извлечение задач работает, остальное может деградировать.\n"
                  "Пополни: console.anthropic.com → Plans & Billing"),
    # v4.33: OpenRouter пустой = мёртв и фолбэк, и «второе мнение» — раньше молча (04.09–08.09, $25/$25)
    "openrouter": ("🚨 Баланс OpenRouter пуст (402) — резервный фолбэк и «второе мнение» не работают. "
                   "Пока Anthropic жив, задачи извлекаются, но без страховки.\n"
                   "Пополни: openrouter.ai → Credits"),
}


def _alert_recent(source: str, hours: int = 6) -> bool:
    """v4.40: дедуп алерта на ДИСКЕ (data/.billing_alert_<source>) — in-memory сбрасывался каждым
    рестартом, и владелец получал алерт после каждого деплоя (08.09 — трижды за вечер)."""
    import os, time as _t
    path = os.path.join(os.path.dirname(settings.db_path) or ".", f".billing_alert_{source}")
    try:
        if _t.time() - os.stat(path).st_mtime < hours * 3600:
            return True
    except FileNotFoundError:
        pass
    except Exception:
        return False
    try:
        with open(path, "w") as f:
            f.write(str(int(_t.time())))
    except Exception:
        pass
    return False


def _send_owner_raw(text: str) -> bool:
    import urllib.parse
    try:
        body = urllib.parse.urlencode({"chat_id": settings.owner_user_id, "text": text}).encode()
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{settings.bot_token}/sendMessage", data=body), timeout=15)
        return True
    except Exception as exc:
        logger.warning(f"owner alert send failed: {exc}")
        return False


def _billing_alert(err_text: str, source: str = "anthropic") -> None:
    import time as _t
    if _alert_recent(source):
        return
    _billing_alerted[source] = _t.time()
    if _send_owner_raw(_ALERT_TEXT.get(source, _ALERT_TEXT["anthropic"])):
        logger.warning("Billing-alert отправлен владельцу")


# v4.66: доступ к Claude закрыт (401/403). 29.10 Anthropic потребовал верификацию организации — бот
# 10,5 часа отвечал 403 на всё и молчал (биллинг-алерт ловит только «credit balance»). Теперь: тревога
# сразу (повтор не чаще раза в 6 ч, пока не починится) и «снова работает» на первом успешном вызове.
# Метка простоя — файл рядом с БД: переживает рестарт/деплой.

def _is_access_error(exc) -> bool:
    name = type(exc).__name__
    s = str(exc)
    return (name in ("PermissionDeniedError", "AuthenticationError")
            or "permission_error" in s or "authentication_error" in s
            or getattr(exc, "status_code", None) in (401, 403))


def _access_marker() -> str:
    import os
    return os.path.join(os.path.dirname(settings.db_path) or ".", ".access_down_anthropic")


def _access_reason(exc) -> str:
    s = str(exc)
    m = re.search(r"'message':\s*['\"](.+?)['\"],\s*'", s) or re.search(r'"message":\s*"(.+?)"', s)
    msg = (m.group(1) if m else s)
    msg = re.sub(r"https?://\S+", "", msg).strip(" .:")[:220]   # ссылки — только из консоли, не из алерта
    code = re.search(r"'error_code':\s*'([a-z_]+)'", s)
    status = getattr(exc, "status_code", None)
    return f"{status or type(exc).__name__}{' · ' + code.group(1) if code else ''}: {msg}"


def _access_alert(exc) -> None:
    import os, time as _t
    path = _access_marker()
    if not os.path.exists(path):
        try:
            with open(path, "w") as f:
                f.write(str(int(_t.time())))
        except Exception:
            pass
    if _alert_recent("access"):
        return
    text = ("🚨 Claude недоступен: Anthropic отклоняет запросы бота.\n"
            f"Причина: {_access_reason(exc)}\n"
            "Стоят задачи, Мира и памяти; переписки копятся в очереди и разберутся сами, когда доступ вернётся.\n"
            "Что делать: зайди в консоль platform.claude.com — там будет, что требуется (верификация, ключ, оплата).")
    if _send_owner_raw(text):
        logger.warning("Access-alert отправлен владельцу")


def _access_recovered() -> None:
    """Первый успешный вызов после простоя: снять метку и сказать владельцу."""
    import os, time as _t
    path = _access_marker()
    try:
        with open(path) as f:
            since = int((f.read() or "0").strip() or 0)
        os.remove(path)
    except FileNotFoundError:
        return
    except Exception:
        since = 0
    try:   # следующий простой — тревога сразу, без 6-часового дедупа
        os.remove(os.path.join(os.path.dirname(settings.db_path) or ".", ".billing_alert_access"))
    except Exception:
        pass
    dur = ""
    if since:
        mins = int((_t.time() - since) / 60)
        dur = f" (простой ~{mins // 60} ч {mins % 60} мин)" if mins >= 60 else f" (простой ~{mins} мин)"
    if _send_owner_raw(f"✅ Claude снова отвечает{dur}. Очередь переписок разбирается."):
        logger.info("Access восстановлен — владелец уведомлён")


def _access_down_flag() -> bool:
    import os
    return os.path.exists(_access_marker())


def _is_next_gen(model: str) -> bool:
    """v4.61/v4.63: модели поколения Claude 5 (Sonnet 5.5, Haiku 5.5 и новее): адаптивное мышление по умолчанию,
    temperature/top_p/top_k ≠ дефолта → 400, {"type":"disabled"} → 400."""
    m = (model or "").lower()
    return m.startswith(("claude-sonnet-5", "claude-opus-5", "claude-haiku-5", "claude-fable", "claude-mythos"))


def adapt_params(model: str, kwargs: dict) -> dict:
    """v4.61: параметры запроса под модель (миграция Sonnet 4.5 → Sonnet 5.5, 30.09.2026).

    У Sonnet 5.5 мышление включено по умолчанию и считается в max_tokens: наши фоновые вызовы
    (классификатор, судьи, извлечение) живут с max_tokens 60–1024 — мысли съели бы бюджет и
    оборвали JSON. Поэтому по умолчанию `thinking: between_tools` (мышления нет, только короткие
    заметки между вызовами инструментов) и effort из настроек (SMART_EFFORT, по умолчанию low —
    рекомендация Anthropic для классификации/извлечения). Вызов, которому нужно своё
    (Мира), передаёт thinking/output_config явно — их не трогаем.
    temperature на Claude 5 не передаём вовсе (≠ дефолта → 400)."""
    if not _is_next_gen(model):
        return kwargs
    kwargs.pop("temperature", None)
    kwargs.pop("top_p", None)
    kwargs.pop("top_k", None)
    if "thinking" not in kwargs and model.lower().startswith("claude-sonnet-5-5"):
        kwargs["thinking"] = {"type": "between_tools"}
    # v4.63: Haiku 5.5 — мышление по умолчанию включено, between_tools не принимает (400), а disabled — да.
    # Классификатор/судьи живут с max_tokens 60–300: без этого мысли съедали бы ответ.
    if "thinking" not in kwargs and model.lower().startswith("claude-haiku-5"):
        kwargs["thinking"] = {"type": "disabled"}
    # v4.63: новый токенизатор (+30% к Haiku 4.5) — лимиты, подобранные под 4.5, обрезали JSON
    # (классификатор 300 → 4 обрыва из 60). Платим только за реально выданное, поэтому запас бесплатный.
    if model.lower().startswith("claude-haiku-5") and isinstance(kwargs.get("max_tokens"), int):
        kwargs["max_tokens"] = max(kwargs["max_tokens"] * 2, 600)
    oc = dict(kwargs.get("output_config") or {})
    if "effort" not in oc:
        oc["effort"] = getattr(settings, "smart_effort", "low") or "low"
        kwargs["output_config"] = oc
    return kwargs


def text_of(resp) -> str:
    """Текст ответа по ТИПУ блоков (на Claude 5 ответ может начинаться с блока thinking)."""
    out = [getattr(b, "text", "") for b in (getattr(resp, "content", None) or []) if getattr(b, "type", "text") == "text"]
    return "\n".join(t for t in out if t).strip()


async def create(client, *, model: str, max_tokens: int, messages: list,
                 system=None, temperature: Optional[float] = None, **kw):
    """Прямой Anthropic (с кэшем) → при сбое фолбэк на OpenRouter (если задан ключ).

    Совместимо по чтению с ответом Anthropic. При выключенном фолбэке (нет ключа) —
    полностью прозрачна: та же ошибка пробрасывается наружу."""
    kwargs = dict(model=model, max_tokens=max_tokens, messages=messages, **kw)
    if system is not None:
        kwargs["system"] = system
    if temperature is not None:
        kwargs["temperature"] = temperature
    kwargs = adapt_params(model, kwargs)
    try:
        resp = await client.messages.create(**kwargs)
        if _access_down_flag():
            await asyncio.to_thread(_access_recovered)
        return resp
    except Exception as exc:
        if "credit balance" in str(exc).lower() or "billing" in str(exc).lower():
            await asyncio.to_thread(_billing_alert, str(exc), "anthropic")
        elif _is_access_error(exc):
            await asyncio.to_thread(_access_alert, exc)
        if not settings.openrouter_api_key:
            raise
        logger.warning(f"Anthropic упал ({type(exc).__name__}: {exc}); фолбэк → OpenRouter")
        try:
            resp = await asyncio.to_thread(
                _openrouter_call, model, max_tokens, system, messages, temperature)
            logger.info(f"OpenRouter фолбэк OK ({_or_slug(model)})")
            return resp
        except Exception as exc2:
            logger.error(f"OpenRouter фолбэк тоже упал: {type(exc2).__name__}: {exc2}")
            if _is_payment_error(exc2):
                await asyncio.to_thread(_billing_alert, str(exc2), "openrouter")
            raise exc  # исходная ошибка Anthropic → settle не снимет dirty, повторит
