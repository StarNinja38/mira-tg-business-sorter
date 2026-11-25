"""
v4.86 Браузер Obscura для Миры: поиск, чтение страниц, формы брони.

Obscura крутится на этом же сервере в Docker, порт виден только локально, вход по токену
из /etc/obscura/cdp-token. Говорим с ним по CDP через вебсокет aiohttp, без Playwright.
Форма никогда не отправляется из диалога: Мира показывает карточку, отправляет кнопка владельца.
Платёжные данные, пароли и документы в формы не вводятся вообще.
"""
import asyncio
import json
import re
from itertools import count
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import aiohttp

OBSCURA_URL = "http://127.0.0.1:9222"
TOKEN_FILE = "/etc/obscura/cdp-token"
_NAV_TIMEOUT = 30
_SEARCH = "https://html.duckduckgo.com/html/?q="
_EXPLORE_TIMEOUT = 25
_BOT_WALL = re.compile(r"не робот|captcha|капч|verify you are human|are you a robot|checking your browser|unusual traffic"
                       r"|безопасное соединение|just a moment|ddos-guard|access denied|доступ ограничен", re.I)
_SENSITIVE = re.compile(r"card|cvv|cvc|cc-|passw|парол|карт[аыу]|iban|паспорт|passport|снилс|инн\b|pin\b", re.I)


class BrowserError(RuntimeError):
    pass


def token() -> str:
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def available() -> bool:
    return bool(token())


def check_url(url: str) -> str:
    """Только http(s) с именем хоста. Частные адреса Obscura режет сама."""
    url = (url or "").strip()
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise BrowserError("нужна ссылка вида https://…")
    return url


def luhn(digits: str) -> bool:
    total, odd = 0, True
    for ch in reversed(digits):
        d = int(ch)
        if not odd:
            d = d * 2 - 9 if d > 4 else d * 2
        total, odd = total + d, not odd
    return total % 10 == 0


def looks_sensitive(field: dict, value: str = "") -> bool:
    """Поле или значение похоже на карту, пароль или документ: такое не заполняем."""
    if (field.get("type") or "").lower() in ("password", "file"):
        return True
    meta = " ".join(str(field.get(k) or "") for k in ("key", "name", "id", "label", "placeholder", "autocomplete"))
    if _SENSITIVE.search(meta):
        return True
    digits = re.sub(r"[\s-]", "", value or "")
    return digits.isdigit() and 13 <= len(digits) <= 19 and luhn(digits)


def ddg_link(href: str) -> str:
    """У DuckDuckGo ссылки идут через редирект //duckduckgo.com/l/?uddg=…"""
    if "uddg=" in href:
        q = parse_qs(urlparse(href if href.startswith("http") else "https:" + href).query)
        return unquote(q.get("uddg", [href])[0])
    return href


class Page:
    """Одна вкладка Obscura на время одного действия."""

    def __init__(self):
        self._ids = count(1)
        self._http = None
        self._ws = None
        self._target = None
        self._session = None

    async def __aenter__(self):
        tok = token()
        if not tok:
            raise BrowserError("браузер не подключён на сервере")
        headers = {"Authorization": f"Bearer {tok}"}
        self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=_NAV_TIMEOUT + 15))
        try:
            async with self._http.get(OBSCURA_URL + "/json/version", headers=headers) as r:
                if r.status != 200:
                    raise BrowserError(f"браузер ответил {r.status}")
                ws_url = (await r.json(content_type=None))["webSocketDebuggerUrl"]
            self._ws = await self._http.ws_connect(ws_url, headers=headers, max_msg_size=32 * 1024 * 1024)
            self._target = (await self.call("Target.createTarget", {"url": "about:blank"}, session=False))["targetId"]
            self._session = (await self.call("Target.attachToTarget", {"targetId": self._target, "flatten": True},
                                             session=False))["sessionId"]
        except BrowserError:
            await self._close()
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError) as exc:
            await self._close()
            raise BrowserError(f"браузер недоступен: {type(exc).__name__}") from exc
        return self

    async def __aexit__(self, *exc):
        if self._ws is not None and self._target:
            try:
                await self.call("Target.closeTarget", {"targetId": self._target}, session=False)
            except Exception:
                pass
        await self._close()

    async def _close(self):
        if self._ws is not None:
            await self._ws.close()
        if self._http is not None:
            await self._http.close()

    async def call(self, method: str, params: dict | None = None, *, session: bool = True) -> dict:
        mid = next(self._ids)
        msg = {"id": mid, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = self._session
        try:
            await self._ws.send_str(json.dumps(msg))
            async with asyncio.timeout(_NAV_TIMEOUT):
                while True:
                    raw = await self._ws.receive()
                    if raw.type != aiohttp.WSMsgType.TEXT:
                        raise BrowserError("связь с браузером оборвалась")
                    data = json.loads(raw.data)
                    if data.get("id") == mid:
                        if "error" in data:
                            raise BrowserError(data["error"].get("message", "ошибка браузера"))
                        return data.get("result") or {}
        except TimeoutError as exc:   # медленный сайт не должен ронять ответ Миры целиком
            raise BrowserError(f"сайт не ответил за {_NAV_TIMEOUT} с") from exc
        except aiohttp.ClientError as exc:
            raise BrowserError(f"связь с браузером оборвалась: {type(exc).__name__}") from exc

    async def open(self, url: str) -> None:
        res = await self.call("Page.navigate", {"url": check_url(url), "waitUntil": "load"})
        if res.get("errorText"):
            raise BrowserError(f"страница не открылась: {res['errorText']}")

    async def js(self, expr: str):
        res = await self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
        if res.get("exceptionDetails"):
            raise BrowserError("скрипт на странице упал")
        return (res.get("result") or {}).get("value")

    async def markdown(self) -> str:
        return (await self.call("LP.getMarkdown")).get("markdown") or ""


_RESULTS_JS = """[...document.querySelectorAll('.result')].slice(0, 8).map(r => ({
  title: (r.querySelector('.result__a') || {}).innerText || '',
  href: (r.querySelector('.result__a') || {}).href || '',
  snippet: (r.querySelector('.result__snippet') || {}).innerText || ''}))"""

# поля видимых форм: индекс формы, ключ поля, подпись и варианты выбора
_FIELDS_JS = """[...document.forms].map((f, fi) => [...f.elements].filter(e =>
  e.name && !['hidden', 'submit', 'button', 'reset', 'image'].includes(e.type)).map(e => ({
  form: fi, key: e.name, type: e.type || e.tagName.toLowerCase(), id: e.id || '', name: e.name,
  autocomplete: e.autocomplete || '', placeholder: e.placeholder || '', required: !!e.required,
  label: ((e.labels && e.labels[0] && e.labels[0].innerText) || e.getAttribute('aria-label') || '').trim().slice(0, 80),
  value: ['radio', 'checkbox'].includes(e.type) ? e.value : '',
  options: e.options ? [...e.options].slice(0, 15).map(o => o.value + '=' + o.text.trim()) : []}))).flat()"""


def clean_md(md: str) -> str:
    """Картинки (часто base64 прямо в тексте) Мире не нужны и съедают лимит."""
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md or "")
    md = re.sub(r"\[\s*\]\([^)]*\)", "", md)            # пустые ссылки-иконки
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in md.splitlines()]
    lines = [ln for ln in lines if len(ln) <= 3 or re.search(r"\w", ln)]   # строки из одних точек (2ГИС рисует карту ими)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


_STREET = re.compile(r"ул\.|улица|бульвар|бул\.|б-р|проезд|туп\.|пер\.|переулок|просп|наб\.|шоссе|площадь|пл\.", re.I)
_PLACE = re.compile(r"\+7[\s(]*\d|8 ?\(\d{3}\)|₽|рейтинг|\d[.,]\d ?★", re.I)


def _is_place_line(ln: str) -> bool:
    """Адрес — улица с номером дома; меню фильтров со станциями («Бульвар Рокоссовского») цифр не содержит."""
    return bool(_PLACE.search(ln) or (_STREET.search(ln) and re.search(r"\d", ln)))


def excerpt(md: str, limit: int = 6000) -> str:
    """Для поиска мест: страница без адресов ссылок и только куски вокруг адресов, телефонов и цен.
    У агрегаторов список мест идёт после меню и навигации, первые символы страницы его не захватывают."""
    md = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", md or "")
    if len(md) <= limit:
        return md
    lines = md.splitlines()
    keep = set()
    for i, ln in enumerate(lines):
        if _is_place_line(ln):
            keep.update(range(max(0, i - 2), min(len(lines), i + 2)))   # название обычно строкой-двумя выше адреса
    if not keep:
        return md[:limit]
    out, prev = [], -2
    for i in sorted(keep):
        if i != prev + 1:
            out.append("…")
        out.append(lines[i])
        prev = i
    return "\n".join(ln for ln in out if ln.strip())[:limit]


def is_bot_wall(md: str) -> bool:
    """Короткая страница с проверкой на робота вместо содержимого."""
    return len(md) < 1500 and bool(_BOT_WALL.search(md))


def merge_fields(rows: list[dict]) -> list[dict]:
    """Переключатели и галочки с одним именем → одно поле со списком значений."""
    out, seen = [], {}
    for r in rows:
        k = (r.get("form"), r.get("key"))
        if k in seen and r.get("type") in ("radio", "checkbox"):
            seen[k]["options"].append(r.get("value") or "")
            continue
        r = {**r, "options": list(r.get("options") or ([r["value"]] if r.get("value") else []))}
        seen[k] = r
        out.append(r)
    return out


def _fill_js(form: int, values: dict, submit: bool) -> str:
    return f"""(() => {{
  const f = document.forms[{int(form)}]; if (!f) return {{ok: false, missing: ['форма']}};
  const vals = {json.dumps(values, ensure_ascii=False)}; const missing = [];
  for (const [k, v] of Object.entries(vals)) {{
    const e = f.elements[k]; if (!e) {{ missing.push(k); continue; }}
    const el = e.length && !e.tagName ? e[0] : e;
    if (el.type === 'checkbox' || el.type === 'radio') {{
      const opt = e.length && !e.tagName ? [...e].find(x => x.value == v) : el;
      if (opt) opt.checked = true; else missing.push(k);
    }} else {{ el.value = v; }}
    el.dispatchEvent(new Event('input', {{bubbles: true}})); el.dispatchEvent(new Event('change', {{bubbles: true}}));
  }}
  if ({'true' if submit else 'false'} && !missing.length) {{ f.requestSubmit ? f.requestSubmit() : f.submit(); }}
  return {{ok: !missing.length, missing}};
}})()"""


async def search(query: str) -> list[dict]:
    async with Page() as p:
        await p.open(_SEARCH + quote_plus(query[:300]))
        rows = await p.js(_RESULTS_JS) or []
        if not rows and is_bot_wall(clean_md(await p.markdown())):
            raise BrowserError("поисковик попросил проверку «я не робот», попробуй web_search")
    return [{"title": r["title"].strip(), "url": ddg_link(r["href"]), "snippet": r["snippet"].strip()}
            for r in rows if r.get("href")]


async def read(url: str) -> str:
    async with Page() as p:
        await p.open(url)
        md = clean_md(await p.markdown())
    if is_bot_wall(md):
        raise BrowserError(f"сайт закрыт проверкой «я не робот», такие проверки я не прохожу, открой сам: {url}")
    return md


async def explore(query: str, pages: int = 3, tries: int = 6) -> dict:
    """Поиск и чтение первых читаемых страниц выдачи. Закрытые проверкой, пустые и медленные пропускаем.
    Читаем параллельно и отвечаем, как только прочитано pages страниц: медленный сайт не держит ответ.
    Недочитанные уходят в «more», страницы — в порядке выдачи."""
    rows = await search(query)

    async def one(i, r):
        try:
            md = await asyncio.wait_for(read(r["url"]), _EXPLORE_TIMEOUT)
        except (BrowserError, TimeoutError) as exc:
            return i, None, str(exc) or "сайт не ответил"
        return (i, md, None) if len(md) >= 300 else (i, None, "страница пустая")

    tasks = [asyncio.create_task(one(i, r)) for i, r in enumerate(rows[:tries])]
    read_ok, skipped, finished = [], [], set()
    try:
        for fut in asyncio.as_completed(tasks):
            i, md, err = await fut
            finished.add(i)
            if md:
                read_ok.append((i, md))
            else:
                skipped.append((rows[i]["url"], err))
            if len(read_ok) >= pages:
                break
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    unread = [r for i, r in enumerate(rows[:tries]) if i not in finished]
    return {"pages": [(rows[i], md) for i, md in sorted(read_ok)], "skipped": skipped, "more": unread + rows[tries:]}


async def form_fields(url: str) -> list[dict]:
    async with Page() as p:
        await p.open(url)
        return merge_fields(await p.js(_FIELDS_JS) or [])


def plan_problems(fields: list[dict], form: int, values: dict) -> list[str]:
    """Что мешает заполнить: поля нет, поле или значение чувствительное."""
    by_key = {f["key"]: f for f in fields if f.get("form") == form}
    problems = []
    for k, v in values.items():
        f = by_key.get(k)
        if f is None:
            problems.append(f"нет поля {k}")
        elif looks_sensitive(f, str(v)):
            problems.append(f"поле {k} похоже на платёжные данные, пароль или документ, его заполняет только владелец")
    return problems


def payment_fields(fields: list[dict], form: int) -> list[str]:
    """Обязательные поля карты, пароля или документа в самой форме: без них её не отправить, а вписывает их только владелец."""
    return [f["key"] for f in fields if f.get("form") == form and f.get("required") and looks_sensitive(f)]


_PAY_URL = re.compile(r"(?<![a-z])pay|checkout|oplat|оплат|billing|yoomoney|yookassa|cloudpayments|tinkoff|tbank|sberbank|stripe|paypal|3ds|acs\.", re.I)
_PAY_TEXT = re.compile(r"перейти к оплате|к оплате|оплатить|предоплат|внесите депозит|оплата картой|pay now|proceed to payment|card number|номер карты", re.I)
_AFTER_JS = """({url: location.href, frames: [...document.querySelectorAll('iframe')].map(f => f.src || '').slice(0, 10)})"""


def needs_payment(url: str, frames: list[str], fields: list[dict], text: str) -> bool:
    """Сайт после отправки ждёт оплату: страница банка, платёжный виджет, поля карты или кнопка «оплатить»."""
    if _PAY_URL.search(url or "") or any(_PAY_URL.search(f or "") for f in frames):
        return True
    if any(looks_sensitive(f) and f.get("type") != "password" for f in fields):
        return True
    return bool(_PAY_TEXT.search(text or ""))


async def submit(url: str, form: int, values: dict) -> dict:
    """Только по кнопке владельца: открыть заново, заполнить, отправить. Вернуть начало ответа сайта,
    адрес, куда он привёл, и ждёт ли он оплату (тогда платит владелец сам по ссылке)."""
    async with Page() as p:
        await p.open(url)
        fields = await p.js(_FIELDS_JS) or []
        problems = plan_problems(fields, form, values)
        if problems:
            raise BrowserError("; ".join(problems))
        res = await p.js(_fill_js(form, values, submit=True)) or {}
        if not res.get("ok"):
            raise BrowserError("не нашла поля: " + ", ".join(res.get("missing") or []))
        await asyncio.sleep(4)   # сайт обрабатывает отправку и показывает ответ
        text = clean_md(await p.markdown())
        after = await p.js(_AFTER_JS) or {}
        fields_after = await p.js(_FIELDS_JS) or []
    final = after.get("url") or url
    return {"text": text[:800], "url": final if final.startswith("http") else url,
            "pay": needs_payment(final, after.get("frames") or [], fields_after, text[:3000])}
