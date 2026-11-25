# -*- coding: utf-8 -*-
"""v4.107 виджет «Встреча»: за полчаса до события под брифом приходит карточка с обратным отсчётом
(тикает внутри виджета, без бота), временем и кнопками «+15 мин» и «+1 ч». Сдвиг двигает событие
в календаре целиком (длительность та же) и срок привязанной задачи в Todoist и в базе.
"""
from datetime import datetime

from loguru import logger

from core import widgets as W

HTML = W.page(
    ".big{font-size:2em;font-weight:600;font-variant-numeric:tabular-nums;margin:6px 0 2px}"
    ".t{font-weight:600}.b{display:flex;gap:6px;margin-top:8px}.b button{flex:1}.w{opacity:.45}",
    '<div class="h"><b>Встреча</b><span class="c" id="at"></span></div>'
    '<div class="t" id="t"></div><div class="c" id="who"></div><div class="big" id="cd"></div>'
    '<div class="b" id="b"></div>',
    "function pad(n){return n<10?'0'+n:''+n}"
    "function tick(){const s=Math.round((S.u||0)-Date.now()/1000),c=$('cd');"
    "if(!S.u){c.textContent='';return}if(s<=0){c.textContent=s>-3600?'идёт':'прошла';return}"
    "const h=Math.floor(s/3600),m=Math.floor(s%3600/60);c.textContent='через '+(h?h+' ч '+pad(m)+' мин':m+':'+pad(s%60))}"
    "function draw(){$('t').textContent=S.t||'';$('who').textContent=S.w||'';"
    "const d=new Date((S.u||0)*1000);$('at').textContent=S.u?pad(d.getHours())+':'+pad(d.getMinutes()):'';"
    "const b=$('b');b.textContent='';if(S.u&&S.u>Date.now()/1000-600)"
    "for(const[m,l]of[[15,'+15 мин'],[60,'+1 ч']]){const x=el('button','',l);x.onclick=()=>ask('mt:m:'+m,b);b.appendChild(x)}tick()}"
    "if(W){load();draw();setInterval(tick,1000);W.onEvent('chatDataChanged',()=>{load();draw()});W.ready()}",
)


def _key(mid) -> str:
    return f"mt:msg:{mid}"


def _text(state: dict) -> str:
    """v4.110: запасной вид карточки встречи обычным сообщением (Telegram не пускает виджеты)."""
    import time
    from zoneinfo import ZoneInfo
    from config import settings as _s
    at = datetime.fromtimestamp(int(state.get("u") or 0), ZoneInfo(_s.timezone))
    left = int((state.get("u") or 0) - time.time()) // 60
    when = f"через {left // 60} ч {left % 60:02d} мин" if left >= 60 else f"через {left} мин" if left > 0 else "уже началась"
    who = f"\n{state['w']}" if state.get("w") else ""
    return f"⏰ {state.get('t') or 'Встреча'}{who}\nв {at:%H:%M}, {when}"


def _kb(mid: int) -> dict:
    return {"inline_keyboard": [[{"text": "+15 мин", "callback_data": f"mtb:15:{mid}"},
                                 {"text": "+1 ч", "callback_data": f"mtb:60:{mid}"}]]}


async def _send_text(settings, owner_id: int, state: dict, thread) -> int:
    """Текстовая карточка: сначала без кнопок (нужен id сообщения для кнопок), потом кнопки с этим id."""
    payload = {"chat_id": owner_id, "text": _text(state), "disable_notification": True}
    data = await W.call(settings.bot_token, "sendMessage", dict(payload, message_thread_id=thread) if thread else payload)
    if not data.get("ok") and thread:
        data = await W.call(settings.bot_token, "sendMessage", payload)
    mid = int((data.get("result") or {}).get("message_id") or 0)
    if mid:
        await W.call(settings.bot_token, "editMessageReplyMarkup",
                     {"chat_id": owner_id, "message_id": mid, "reply_markup": _kb(mid)})
    return mid


async def offer(db, settings, owner_id: int, event_id: str, summary: str, start_dt: datetime, who: str = "") -> bool:
    from core.topics import thread_id
    state = {"t": (summary or "")[:120], "u": int(start_dt.timestamp()), "w": (who or "")[:60]}
    ok, mid, err = await W.send(settings, owner_id, HTML, state, thread_id("meetings"))
    if not ok and W.refused(err):   # v4.110: виджеты запрещены — та же карточка текстом с кнопками
        try:
            mid = await _send_text(settings, owner_id, state, thread_id("meetings"))
            ok = bool(mid)
        except Exception as exc:
            logger.warning(f"meeting text card failed: {type(exc).__name__}: {exc}")
    if ok:
        await W.set_json(db, _key(mid), {"e": event_id, "s": state})
        try:   # не копим привязки — последние 20 карточек встреч
            await db.execute("DELETE FROM settings_kv WHERE key LIKE 'mt:msg:%' AND rowid NOT IN "
                             "(SELECT rowid FROM settings_kv WHERE key LIKE 'mt:msg:%' ORDER BY rowid DESC LIMIT 20)")
            await db.commit()
        except Exception:
            pass
    return ok


async def _shift_task(db, settings, event_id: str, new_start: datetime) -> None:
    """Привязанная открытая задача: срок в Todoist и в базе вслед за событием (календарь уже сдвинут)."""
    from core.actions import update_todoist_task, _normalize_due
    from db import set_task_due
    async with db.execute("SELECT id, todoist_task_id FROM task_sync WHERE calendar_event_id = ? AND synced = 0",
                          (event_id,)) as cur:
        rows = await cur.fetchall()
    due = _normalize_due(new_start.strftime("%Y-%m-%dT%H:%M")) or new_start.strftime("%Y-%m-%dT%H:%M:00")
    for sid, td in rows:
        await set_task_due(db, sid, due)
        if td and settings.todoist_api_token:
            await update_todoist_task(settings.todoist_api_token, td, due)


async def on_query(ctx: dict, act: str, arg: str) -> str:
    from zoneinfo import ZoneInfo
    db, settings = ctx["db"], ctx["settings"]
    if act != "m" or arg not in ("15", "60"):
        return ""
    cur = await W.get_json(db, _key(ctx.get("message_id")))
    if not cur.get("e"):
        return "Не нашла встречу"

    async def _do():
        res = await shift(db, settings, ctx["message_id"], int(arg))
        if isinstance(res, str):
            return res
        await W.edit(settings, ctx["chat_id"], ctx["message_id"], res)
        return f"Сдвинула на {datetime.fromtimestamp(res['u'], ZoneInfo(settings.timezone)):%H:%M}"
    return await W.within(_do(), "Сдвигаю, обновлю карточку")


async def shift(db, settings, mid, minutes: int):
    """Сдвинуть встречу карточки mid: событие целиком и привязанную задачу. Новое состояние или текст ошибки."""
    from zoneinfo import ZoneInfo
    from core.calendar import shift_event, _parse_rfc3339
    cur = await W.get_json(db, _key(mid))
    if not cur.get("e"):
        return "Не нашла встречу"
    new = await shift_event(settings.google_service_account_path, settings.google_calendar_id, cur["e"], int(minutes))
    if not new:
        return "Календарь не ответил, сдвинь сам"
    start = _parse_rfc3339(new).astimezone(ZoneInfo(settings.timezone))
    state = dict(cur.get("s") or {}, u=int(start.timestamp()))
    cur["s"] = state
    await W.set_json(db, _key(mid), cur)
    try:
        await _shift_task(db, settings, cur["e"], start.replace(tzinfo=None))
    except Exception as exc:
        logger.warning(f"meeting task shift: {exc}")
    return state


async def on_button(db, settings, mid: int, minutes: int) -> tuple[str, str | None]:
    """v4.110: кнопка текстовой карточки встречи → (ответ на нажатие, новый текст карточки или None)."""
    res = await shift(db, settings, mid, minutes)
    if isinstance(res, str):
        return res, None
    from zoneinfo import ZoneInfo
    return f"Сдвинула на {datetime.fromtimestamp(res['u'], ZoneInfo(settings.timezone)):%H:%M}", _text(res)


W.register("mt", on_query)
