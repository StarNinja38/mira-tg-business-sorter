# -*- coding: utf-8 -*-
"""v4.107 виджет «Привычки»: неделя кружками, нажатие отмечает день или снимает отметку, серия дней подряд.

Команда /habits. Данные — core.habits (те же, что у Миры: «добавь привычку…», «отметь…»).
Обновляется после нажатия и раз в 2 минуты из цикла дайджеста (отметки через Миру тоже доезжают).
"""
from datetime import date, timedelta

from loguru import logger

from core import widgets as W

_KEY = "hb:cur"
_DAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")

HTML = W.page(
    ".g{display:grid;grid-template-columns:1fr repeat(7,26px);gap:6px 4px;align-items:center;margin-top:8px}"
    ".g .c{text-align:center}.nm{min-width:0}.nm div{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}"
    ".nm small{display:block;color:var(--tg-theme-hint-color);font-size:.75em}"
    ".d{width:22px;height:22px;border-radius:50%;border:2px solid var(--tg-theme-secondary-bg-color);"
    "box-sizing:border-box;margin:auto}"
    ".d.on{background:var(--tg-theme-button-color);border-color:var(--tg-theme-button-color)}"
    ".d.t{border-color:var(--tg-theme-button-color)}.w{opacity:.45}"
    "#e{color:var(--tg-theme-hint-color);padding:10px 0}",
    '<div class="h"><b>Привычки</b><span class="c" id="s"></span></div><div id="l"></div>',
    "function draw(){const h=S.h||[],w=S.w||[],l=$('l');l.textContent='';"
    "$('s').textContent=h.length?'сегодня '+h.filter(x=>x[3][6]=='1').length+' из '+h.length:'';"
    "if(!h.length){l.appendChild(el('div','',S.w?'Привычек пока нет. Скажи Мире: «добавь привычку зарядка»':'Загружаю…')).id='e';return}"
    "const g=el('div','g');g.appendChild(el('span'));for(const d of w)g.appendChild(el('span','c',d));"
    "for(const x of h){const n=el('div','nm');n.appendChild(el('div','',x[1]));if(x[2])n.appendChild(el('small','','🔥 '+x[2]+' подряд'));g.appendChild(n);"
    "for(let i=0;i<7;i++){const d=el('div','d'+(x[3][i]=='1'?' on':'')+(i==6?' t':''));"
    "d.onclick=()=>ask('hb:x:'+x[0]+':'+(S.k||[])[i],d);g.appendChild(d)}}l.appendChild(g)}"
    "if(W){load();draw();W.onEvent('chatDataChanged',()=>{load();draw()});W.ready()}",
)


async def build_state(db, today: date) -> dict:
    from core import habits
    rows = await habits.week(db, today)
    keys = [(today - timedelta(days=6 - i)) for i in range(7)]
    state = {"w": [_DAYS[d.weekday()] for d in keys], "k": [d.strftime("%m%d") for d in keys], "h": []}
    for cut in (24, 18, 12, 8):
        state["h"] = [[hid, name[:cut], st, line] for hid, name, st, line in rows]
        if W.nbytes(state) <= W.LIMIT:
            break
    return state


async def text(db) -> str:
    """v4.110: привычки за неделю текстом — когда Telegram не пускает виджеты бота."""
    from core import habits
    from core.util import today_local
    today = today_local()
    rows = await habits.week(db, today)
    if not rows:
        return "Привычек пока нет. Скажи Мире: «добавь привычку зарядка»"
    days = " ".join(_DAYS[(today - timedelta(days=6 - i)).weekday()] for i in range(7))
    lines = [f"🔁 Привычки за неделю ({days})"]
    for _id, name, streak, line in rows:
        dots = "".join("●" if c == "1" else "○" for c in line)
        lines.append(f"{dots}  {name}" + (f" · 🔥 {streak} подряд" if streak else ""))
    lines.append("Отметить: скажи Мире «отметь зарядку»")
    return "\n".join(lines)


async def send(db, settings, chat_id: int, thread_id=None) -> tuple[bool, str]:
    from core.util import today_local
    from db import set_setting
    state = await build_state(db, today_local())
    ok, mid, err = await W.send(settings, chat_id, HTML, state, thread_id)
    if ok:
        await W.set_json(db, _KEY, {"c": chat_id, "m": mid, "s": W.dump(state)})
        await set_setting(db, "hb:on", "1")
        logger.info(f"Habits widget sent (msg {mid}, {len(state['h'])} привычек)")
    return ok, err


async def refresh(db, settings) -> bool:
    from core.util import today_local
    cur = await W.get_json(db, _KEY)
    if not cur.get("m"):
        return False
    state = await build_state(db, today_local())
    dumped = W.dump(state)
    if dumped == cur.get("s"):
        return False
    ok, desc = await W.edit(settings, cur["c"], cur["m"], state)
    if not ok:
        logger.info(f"habits widget edit failed: {desc}")
        if "not found" in desc.lower():
            cur["m"] = None
            await W.set_json(db, _KEY, cur)
        return False
    cur["s"] = dumped
    await W.set_json(db, _KEY, cur)
    return True


async def morning(db, settings, owner_id: int, thread_id=None) -> None:
    """Утром под сводкой — если привычки есть и виджет хоть раз отправлялся."""
    from core import habits
    from core.util import today_local
    from db import get_setting
    try:
        if (await get_setting(db, "hb:on")) != "1" or not await habits.week(db, today_local()):
            return
        await send(db, settings, owner_id, thread_id)
    except Exception as exc:
        logger.info(f"habits widget morning skip: {exc}")


async def on_query(ctx: dict, act: str, arg: str) -> str:
    from core import habits
    from core.util import today_local
    db, settings = ctx["db"], ctx["settings"]
    if act != "x":
        return ""
    hid, _, mmdd = arg.partition(":")
    today = today_local()
    day = None
    for i in range(7):   # «ММДД» → дата в пределах последней недели (будущее не отмечаем)
        d = today - timedelta(days=i)
        if d.strftime("%m%d") == mmdd:
            day = d
            break
    if day is None:
        return "Этот день уже вне недели"
    res = await habits.toggle(db, int(hid), day.isoformat())
    if res is None:
        return "Привычки уже нет"
    name, on = res
    await refresh(db, settings)
    when = "сегодня" if day == today else day.strftime("%d.%m")
    return f"{name}: {when} отмечено" if on else f"{name}: отметку за {when} сняла"


W.register("hb", on_query)
