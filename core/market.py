"""
v4.100 Финансы: CoinMarketCap, TON Wallet и новости рынка через Perplexity, плюс блок «Рынок» в утренней сводке.

Только цифры и новости: советов, торговых идей и сделок нет. Кошелёк смотрим по публичному адресу, ключи не нужны.
CoinMarketCap без ключа → CoinGecko и alternative.me. Perplexity без денег на ключе → веб-поиск Миры.
Индексы, золото, нефть и курс рубля — Yahoo Finance без ключа.
"""
import aiohttp
from loguru import logger

from core.util import today_local

_TIMEOUT = aiohttp.ClientTimeout(total=15)
_UA = {"User-Agent": "Mozilla/5.0 mira-bot"}
_CMC = "https://pro-api.coinmarketcap.com"
_GECKO = {"BTC": "bitcoin", "ETH": "ethereum", "TON": "the-open-network", "SOL": "solana", "USDT": "tether"}
_YAHOO = [("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq"), ("^DJI", "Dow Jones"), ("GC=F", "Золото"), ("CL=F", "Нефть WTI"),
          ("^TNX", "Доходность 10Y"), ("RUB=X", "Доллар")]
_FNG_RU = {"extreme fear": "сильный страх", "fear": "страх", "neutral": "нейтрально", "greed": "жадность", "extreme greed": "сильная жадность"}
NEWS_DAILY = 10
_NEWS_SYSTEM = ("Ты финансовый новостной редактор. По-русски, 5 коротких пунктов о главном на финансовых рынках за последние сутки: "
                "акции, крипта, сырьё, ставки. В каждом пункте цифра и причина. Только факты с датой, без советов, прогнозов и торговых идей. Без markdown.")


async def _get(url: str, headers: dict | None = None):
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
        async with s.get(url, headers={**_UA, **(headers or {})}) as r:
            if r.status >= 400:
                raise RuntimeError(f"{url.split('/')[2]} ответил {r.status}")
            return await r.json(content_type=None)


def _money(v: float) -> str:
    if v >= 1e12:
        return f"${v / 1e12:.2f} трлн".replace(".", ",")
    if v >= 1e9:
        return f"${v / 1e9:.1f} млрд".replace(".", ",")
    return (f"${v:,.0f}" if v >= 100 else f"${v:,.2f}" if v >= 1 else f"${v:.4f}").replace(",", " ").replace(".", ",")


def _pct(v) -> str:
    return f"{v:+.1f}%".replace(".", ",").replace("-", "−") if v is not None else ""


def cmc_on(settings) -> bool:
    return bool(getattr(settings, "coinmarketcap_api_key", None))


async def coins(settings, symbols: list[str]) -> list[dict]:
    """[{sym, price, d24, d7, cap}] — CoinMarketCap с ключом, иначе CoinGecko (без d7)."""
    syms = [s.upper() for s in symbols if s.upper() in _GECKO] or ["BTC", "ETH", "TON"]
    if cmc_on(settings):
        try:
            data = (await _get(f"{_CMC}/v1/cryptocurrency/quotes/latest?symbol={','.join(syms)}&convert=USD",
                               {"X-CMC_PRO_API_KEY": settings.coinmarketcap_api_key}))["data"]
            return [{"sym": s, "price": q["price"], "d24": q.get("percent_change_24h"), "d7": q.get("percent_change_7d"),
                     "cap": q.get("market_cap")} for s in syms if s in data for q in [data[s]["quote"]["USD"]]]
        except Exception as exc:
            logger.warning(f"cmc quotes skip, coingecko: {exc}")
    ids = ",".join(_GECKO[s] for s in syms)
    data = await _get(f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd&include_24hr_change=true&include_market_cap=true")
    return [{"sym": s, "price": d["usd"], "d24": d.get("usd_24h_change"), "d7": None, "cap": d.get("usd_market_cap")}
            for s in syms if (d := data.get(_GECKO[s]))]


async def crypto_global(settings) -> dict:
    """{cap, d24, btc_dom, fng, fng_label} — общая капитализация, доля биткоина, индекс страха и жадности."""
    out = {}
    if cmc_on(settings):
        h = {"X-CMC_PRO_API_KEY": settings.coinmarketcap_api_key}
        try:
            g = (await _get(f"{_CMC}/v1/global-metrics/quotes/latest", h))["data"]
            out.update(cap=g["quote"]["USD"]["total_market_cap"], d24=g["quote"]["USD"].get("total_market_cap_yesterday_percentage_change"),
                       btc_dom=g.get("btc_dominance"))
            f = (await _get(f"{_CMC}/v3/fear-and-greed/latest", h))["data"]
            out.update(fng=int(f["value"]), fng_label=f.get("value_classification", ""))
        except Exception as exc:
            logger.warning(f"cmc global skip, coingecko: {exc}")
    if "cap" not in out:
        g = (await _get("https://api.coingecko.com/api/v3/global"))["data"]
        out.update(cap=g["total_market_cap"]["usd"], d24=g.get("market_cap_change_percentage_24h_usd"),
                   btc_dom=(g.get("market_cap_percentage") or {}).get("btc"))
    if "fng" not in out:
        try:
            f = (await _get("https://api.alternative.me/fng/?limit=1"))["data"][0]
            out.update(fng=int(f["value"]), fng_label=f.get("value_classification", ""))
        except Exception as exc:
            logger.debug(f"fng skip: {exc}")
    return out


async def _quote(symbol: str):
    from urllib.parse import quote
    res = (await _get(f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol)}?range=5d&interval=1d"))["chart"]["result"][0]
    meta = res["meta"]
    price = meta["regularMarketPrice"]
    pct = meta.get("regularMarketChangePercent")
    if pct is None:
        closes = [c for c in res["indicators"]["quote"][0].get("close") or [] if c]
        prev = closes[-2] if len(closes) >= 2 and abs(closes[-1] - price) < 1e-6 else (closes[-1] if closes else None)
        pct = (price / prev - 1) * 100 if prev else None
    return price, pct


async def stocks() -> list[tuple]:
    """[(название, цена, изменение %)] индексов, золота, нефти, доходности и доллара к рублю."""
    out = []
    for sym, name in _YAHOO:
        try:
            out.append((name, *await _quote(sym)))
        except Exception as exc:
            logger.debug(f"yahoo {sym} skip: {exc}")
    return out


def _stock_line(name: str, price: float, pct) -> str:
    if name == "Доходность 10Y":
        return f"{name} {price:.2f}%".replace(".", ",")
    if name == "Доллар":
        return f"Доллар {price:.1f} ₽ ({_pct(pct)})".replace(".", ",", 1)
    val = f"${price:,.2f}".replace(",", " ").replace(".", ",") if name in ("Золото", "Нефть WTI") else f"{price:,.0f}".replace(",", " ")
    return f"{name} {val} ({_pct(pct)})"


async def news(db, settings, client, query: str = "") -> str:
    """Новости рынка: Perplexity (sonar), при ошибке или пустом балансе — веб-поиск Миры."""
    from db import get_setting, set_setting
    key = f"pplx:{today_local().isoformat()}"
    used = int(await get_setting(db, key) or 0)
    ask = f"Главное на финансовых рынках за сутки{': ' + query[:200] if query else ''}"
    if settings.perplexity_api_key and used < NEWS_DAILY:
        await set_setting(db, key, str(used + 1))
        try:
            body = {"model": "sonar", "max_tokens": 600, "web_search_options": {"search_context_size": "low"},
                    "messages": [{"role": "system", "content": _NEWS_SYSTEM}, {"role": "user", "content": ask}]}
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as s:
                async with s.post("https://api.perplexity.ai/chat/completions", json=body,
                                  headers={"Authorization": f"Bearer {settings.perplexity_api_key}"}) as r:
                    if r.status >= 400:
                        raise RuntimeError(f"Perplexity ответил {r.status}")
                    d = await r.json(content_type=None)
            cost = ((d.get("usage") or {}).get("cost") or {}).get("total_cost")
            logger.info(f"market news perplexity ok, cost={cost}")
            import re
            text = re.sub(r"\[\d+\]", "", d["choices"][0]["message"]["content"]).replace("**", "").strip()
            src = ", ".join(u.split("/")[2] for u in (d.get("citations") or [])[:3])
            return text + (f"\n\nИсточники: {src}" if src else "")
        except Exception as exc:
            logger.warning(f"market news perplexity skip, web search: {exc}")
    from core import mira_tools
    return await mira_tools._web({"query": ask + ". Только факты с цифрами, без советов."}, db, settings, client)


async def wallet(db) -> str:
    """Кошелёк TON по публичному адресу: баланс в долларах и рублях, токены, последние операции."""
    from datetime import datetime
    from db import get_setting
    address = await get_setting(db, "ton_address")
    if not address:
        return "адрес кошелька не задан, попроси у владельца публичный адрес на UQ или EQ"
    api = "https://tonapi.io/v2"
    acc = await _get(f"{api}/accounts/{address}")
    ton = int(acc.get("balance") or 0) / 1e9
    lines = []
    try:
        r = (await _get(f"{api}/rates?tokens=ton&currencies=usd,rub"))["rates"]["TON"]
        usd, rub = r["prices"]["USD"], r["prices"]["RUB"]
        lines.append(f"Баланс {ton:.2f} TON ≈ ${ton * usd:,.0f} ≈ {ton * rub:,.0f} ₽ (TON ${usd:.2f}, за сутки {r['diff_24h']['USD']})".replace(",", " "))
    except Exception as exc:
        logger.debug(f"ton rates skip: {exc}")
        lines.append(f"Баланс {ton:.2f} TON")
    try:
        jets = (await _get(f"{api}/accounts/{address}/jettons?currencies=usd"))["balances"]
        toks = []
        for j in jets:
            amount = int(j["balance"]) / 10 ** int(j["jetton"].get("decimals", 9))
            if amount > 0:
                usd_v = amount * ((j.get("price") or {}).get("prices") or {}).get("USD", 0)
                toks.append((usd_v, f"{j['jetton'].get('symbol', '?')} {amount:,.2f}".replace(",", " ") + (f" (${usd_v:,.0f})".replace(",", " ") if usd_v >= 1 else "")))
        if toks:
            lines.append("Токены: " + ", ".join(t for _, t in sorted(toks, reverse=True)[:6]))
    except Exception as exc:
        logger.debug(f"ton jettons skip: {exc}")
    try:
        evs = (await _get(f"{api}/accounts/{address}/events?limit=5"))["events"]
        ops = []
        for e in evs:
            a = (e.get("actions") or [{}])[0].get("simple_preview") or {}
            when = datetime.fromtimestamp(e["timestamp"]).strftime("%d.%m %H:%M")
            ops.append(f"{when} {a.get('description') or a.get('name') or 'операция'}")
        if ops:
            lines.append("Последние операции:\n" + "\n".join(ops))
    except Exception as exc:
        logger.debug(f"ton events skip: {exc}")
    return "\n".join(lines) + "\n(только просмотр, переводов Мира не делает)"


async def overview(settings) -> str:
    """Рынок целиком для ответа Миры."""
    parts = []
    try:
        parts.append(" · ".join(f"{c['sym']} {_money(c['price'])} ({_pct(c['d24'])} за сутки" + (f", {_pct(c['d7'])} за неделю" if c["d7"] is not None else "") + ")"
                                for c in await coins(settings, ["BTC", "ETH", "TON", "SOL"])))
    except Exception as exc:
        parts.append(f"цены крипты не получила: {exc}")
    try:
        g = await crypto_global(settings)
        line = f"Капитализация крипты {_money(g['cap'])} ({_pct(g.get('d24'))})"
        if g.get("btc_dom"):
            line += f", доля биткоина {g['btc_dom']:.1f}%".replace(".", ",")
        if g.get("fng") is not None:
            line += f". Страх и жадность: {g['fng']}, {_FNG_RU.get(g['fng_label'].lower(), g['fng_label'])}"
        parts.append(line)
    except Exception as exc:
        logger.debug(f"overview global skip: {exc}")
    st = await stocks()
    if st:
        parts.append(" · ".join(_stock_line(*s) for s in st))
    parts.append("Источник: " + ("CoinMarketCap" if cmc_on(settings) else "CoinGecko") + " и Yahoo Finance, цены с задержкой до 15 минут.")
    return "\n".join(parts)


async def crypto_text(settings, symbols: list[str]) -> str:
    rows = await coins(settings, symbols)
    return "\n".join(f"{c['sym']}: {_money(c['price'])}, за сутки {_pct(c['d24'])}" + (f", за неделю {_pct(c['d7'])}" if c["d7"] is not None else "")
                     for c in rows) or "цены не получила"


async def morning(db, settings, client) -> tuple[list[str], str]:
    """Блок «Рынок» утренней сводки: строки цифр и новости одним текстом. Пустое — блок не показываем."""
    from core import connectors
    lines, text = [], ""
    if await connectors.enabled(db, settings, "crypto"):
        try:
            lines.append(" · ".join(f"{c['sym']} {_money(c['price'])} ({_pct(c['d24'])})" for c in await coins(settings, ["BTC", "ETH", "TON"])))
            g = await crypto_global(settings)
            if g.get("fng") is not None:
                lines.append(f"Страх и жадность: {g['fng']}, {_FNG_RU.get(g['fng_label'].lower(), g['fng_label'])}"
                             + f" · капитализация крипты {_money(g['cap'])} ({_pct(g.get('d24'))})")
        except Exception as exc:
            logger.warning(f"morning crypto skip: {exc}")
        st = {s[0]: s for s in await stocks()}
        idx = [_stock_line(*st[n]) for n in ("S&P 500", "Nasdaq", "Dow Jones") if n in st]
        rest = [_stock_line(*st[n]) for n in ("Золото", "Нефть WTI", "Доходность 10Y", "Доллар") if n in st]
        lines += [" · ".join(x) for x in (idx, rest) if x]
    if await connectors.enabled(db, settings, "finnews"):
        try:
            text = await news(db, settings, client)
        except Exception as exc:
            logger.warning(f"morning news skip: {exc}")
    return lines, text
