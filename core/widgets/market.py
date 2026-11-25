# -*- coding: utf-8 -*-
"""v4.107 виджет «Рынок»: биткоин, эфир, TON, S&P 500, Nasdaq, золото, нефть и доллар — цена, изменение
за период и маленький график. Переключатель 1Д / 7Д / 1М. Только цифры: советов и сделок нет.

Источники те же, что у блока «Рынок» в сводке, без ключей: CoinGecko (market_chart) и Yahoo Finance (chart).
Данные обновляются при открытии чата, но не чаще раза в 10 минут; переключение периода — сразу.
"""
import asyncio
import time

from loguru import logger

from core import widgets as W

_KEY = "mk:cur"
_TTL = 600
_COINS = [("BTC", "bitcoin"), ("ETH", "ethereum"), ("TON", "the-open-network")]
_STOCKS = [("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq"), ("GC=F", "Золото"), ("CL=F", "Нефть"), ("RUB=X", "Доллар")]
_YAHOO_RANGE = {"1": ("1d", "15m"), "7": ("5d", "1h"), "30": ("1mo", "1d")}
_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"

HTML = W.page(
    ".p{display:flex;gap:6px;margin:6px 0}.p button{flex:1}.p .on{background:var(--tg-theme-button-color);"
    "color:var(--tg-theme-button-text-color,#fff)}"
    ".r{display:grid;grid-template-columns:1fr 72px auto;gap:8px;align-items:center;padding:5px 0;"
    "border-top:1px solid var(--tg-theme-section-separator-color,rgba(128,128,128,.25))}"
    ".v{text-align:right;font-variant-numeric:tabular-nums}.v small{display:block}"
    ".up{color:#2fb35e}.dn{color:var(--tg-theme-destructive-text-color,#e53935)}.w{opacity:.45}",
    '<div class="h"><b>Рынок</b><span class="c" id="at"></span></div><div class="p" id="p"></div><div id="l"></div>',
    "const B='" + _B36 + "';"
    "function sp(s,up){if(!s)return'';let p=[];for(let i=0;i<s.length;i++)p.push((i*70/(s.length-1||1)).toFixed(1)+','+(20-B.indexOf(s[i])*18/35).toFixed(1));"
    "return '<svg width=\"72\" height=\"22\" viewBox=\"0 0 72 22\"><polyline fill=\"none\" stroke-width=\"1.6\" stroke=\"'+(up?'#2fb35e':'#e53935')+'\" points=\"'+p.join(' ')+'\"/></svg>'}"
    "function draw(){const p=$('p'),l=$('l');p.textContent='';l.textContent='';$('at').textContent=S.u||'';"
    "for(const[k,t]of[['1','1Д'],['7','7Д'],['30','1М']]){const b=el('button',S.p==k?'on':'',t);b.onclick=()=>ask('mk:p:'+k,l);p.appendChild(b)}"
    "if(!S.r){l.appendChild(el('div','c','Загружаю…'));return}"
    "for(const x of S.r){const r=el('div','r'),g=el('span'),v=el('div','v',x[1]),c=el('small',x[2]>=0?'up':'dn',(x[2]>=0?'+':'−')+Math.abs(x[2]).toFixed(1).replace('.',',')+'%');"
    "g.innerHTML=sp(x[3],x[2]>=0);v.appendChild(c);r.appendChild(el('span','',x[0]));r.appendChild(g);r.appendChild(v);l.appendChild(r)}}"
    "if(W){load();draw();W.onEvent('chatDataChanged',()=>{load();draw()});"
    "W.onEvent('activated',()=>W.request('mk:s').catch(()=>{}));W.request('mk:s').catch(()=>{});W.ready()}",
)


def _spark(values: list[float], n: int = 20) -> str:
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return ""
    if len(vals) > n:
        step = (len(vals) - 1) / (n - 1)
        vals = [vals[round(i * step)] for i in range(n)]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    return "".join(_B36[round((v - lo) / span * 35)] for v in vals)


def _fmt(name: str, price: float) -> str:
    if name == "Доллар":
        return f"{price:.2f} ₽".replace(".", ",")
    if name in ("S&P 500", "Nasdaq"):
        return f"{price:,.0f}".replace(",", " ")
    if price >= 1000:
        return f"${price:,.0f}".replace(",", " ")
    if price >= 10:
        return f"${price:,.1f}".replace(",", " ").replace(".", ",")
    return f"${price:.2f}".replace(".", ",")


async def _coin(gid: str, days: str) -> list[float]:
    from core.market import _get
    data = await _get(f"https://api.coingecko.com/api/v3/coins/{gid}/market_chart?vs_currency=usd&days={days}")
    return [p[1] for p in data.get("prices") or []]


async def _stock(sym: str, period: str) -> list[float]:
    from urllib.parse import quote
    from core.market import _get
    rng, iv = _YAHOO_RANGE[period]
    res = (await _get(f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(sym)}?range={rng}&interval={iv}"))["chart"]["result"][0]
    closes = [c for c in (res["indicators"]["quote"][0].get("close") or []) if c]
    price = (res.get("meta") or {}).get("regularMarketPrice")
    if price and closes and abs(closes[-1] - price) / price > 1e-6:
        closes.append(price)
    return closes


async def build_state(period: str) -> dict:
    from core.util import now_local
    jobs = [_coin(gid, period) for _, gid in _COINS] + [_stock(sym, period) for sym, _ in _STOCKS]
    names = [s for s, _ in _COINS] + [n for _, n in _STOCKS]
    rows = []
    for name, res in zip(names, await asyncio.gather(*jobs, return_exceptions=True)):
        if isinstance(res, Exception) or len(res) < 2:
            logger.debug(f"market widget {name} skip: {res if isinstance(res, Exception) else 'мало точек'}")
            continue
        pct = (res[-1] / res[0] - 1) * 100 if res[0] else 0.0
        rows.append([name, _fmt(name, res[-1]), round(pct, 1), _spark(res)])
    return {"p": period, "r": rows, "u": now_local().strftime("%H:%M")}


async def text(period: str = "1") -> str:
    """v4.110: рынок текстом — когда Telegram не пускает виджеты бота."""
    state = await build_state(period)
    if not state["r"]:
        return "Котировки не ответили, попробуй позже"
    head = {"1": "за день", "7": "за неделю", "30": "за месяц"}.get(period, "")
    lines = [f"📈 Рынок {head}, {state['u']}"]
    for name, price, pct, _sp in state["r"]:
        sign = "+" if pct >= 0 else "−"
        lines.append(f"{'🟢' if pct >= 0 else '🔴'} {name}: {price}  {sign}{abs(pct):.1f}%".replace(".", ","))
    return "\n".join(lines)


async def send(db, settings, chat_id: int, thread_id=None, period: str = "1") -> tuple[bool, str]:
    from db import set_setting
    state = await build_state(period)
    if not state["r"]:
        return False, "источники котировок не ответили"
    ok, mid, err = await W.send(settings, chat_id, HTML, state, thread_id)
    if ok:
        await W.set_json(db, f"{_KEY}:{mid}", {"p": period, "t": time.time()})
        try:   # держим данные последних 10 карточек
            await db.execute("DELETE FROM settings_kv WHERE key LIKE 'mk:cur:%' AND rowid NOT IN "
                             "(SELECT rowid FROM settings_kv WHERE key LIKE 'mk:cur:%' ORDER BY rowid DESC LIMIT 10)")
            await db.commit()
        except Exception:
            pass
        await set_setting(db, "mk:on", "1")
        logger.info(f"Market widget sent (msg {mid}, {len(state['r'])} строк)")
    return ok, err


async def morning(db, settings, owner_id: int, thread_id=None) -> None:
    from core import connectors
    from db import get_setting
    try:
        if (await get_setting(db, "mk:on")) != "1" or not await connectors.enabled(db, settings, "crypto"):
            return
        await send(db, settings, owner_id, thread_id)
    except Exception as exc:
        logger.info(f"market widget morning skip: {exc}")


async def on_query(ctx: dict, act: str, arg: str) -> str:
    db, settings = ctx["db"], ctx["settings"]
    cur = await W.get_json(db, f"{_KEY}:{ctx.get('message_id')}")   # период и время данных этой карточки
    period = cur.get("p") or "1"
    if act == "s":
        if time.time() - float(cur.get("t") or 0) < _TTL:
            return ""
    elif act == "p" and arg in _YAHOO_RANGE:
        period = arg
    else:
        return ""

    async def _do():
        state = await build_state(period)
        if not state["r"]:
            return "Котировки не ответили, попробуй позже"
        await W.edit(settings, ctx["chat_id"], ctx["message_id"], state)
        await W.set_json(db, f"{_KEY}:{ctx.get('message_id')}", {"p": period, "t": time.time()})
        return "" if act == "s" else {"1": "За день", "7": "За неделю", "30": "За месяц"}[period]
    return await W.within(_do(), "Загружаю котировки")


W.register("mk", on_query)
