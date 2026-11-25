# -*- coding: utf-8 -*-
"""v4.106: виджет «Мой день» (Bot API 10.4, HTML-виджеты в сообщении).

Одно сообщение в личке с Мирой: задачи на сегодня и просроченные. Нажатие на задачу закрывает
её (повторное — возвращает), «→» переносит на завтра. Состояние виджета (chat_data, до 1 КБ)
пишет только бот: после нажатия, раз в 2 минуты из цикла дайджеста и при каждом открытии чата
(виджет сам просит «s» в фоне). Код виджета неизменен с момента отправки, меняются только данные.

aiogram 3.31 знает Bot API до 10.3: методы зовём напрямую (как richmsg), а обновление widget_query
ловим outer-middleware до того, как aiogram его отбросит как неизвестное.
"""
import asyncio
import hashlib
import json
import zlib

import aiohttp
from loguru import logger

_API = "https://api.telegram.org/bot{token}/{method}"
_KEY = "daywidget:cur"      # {"c": chat, "m": message_id, "d": "YYYY-MM-DD", "ids": [...], "h": hash, "r": rev}
_ON_KEY = "daywidget:on"    # "1" после первой удачной отправки → утром виджет приходит сам
_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
           "сентября", "октября", "ноября", "декабря")
_LIMIT = 1000               # chat_data ≤ 1024 байт, с запасом

# Профиль 1: без форм, прокрутки, сети и внешних шрифтов. Текст задач — только через textContent.
HTML = """<style>
body{margin:var(--tg-text-padding,8px 12px);font:var(--tg-text-font-size,15px)/1.3 system-ui;color:var(--tg-theme-text-color)}
.h{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.h b{font-size:1.1em}
.c,#n{color:var(--tg-theme-hint-color);font-size:.82em;font-variant-numeric:tabular-nums}
.bar{height:4px;border-radius:2px;background:var(--tg-theme-secondary-bg-color);margin:6px 0 2px;overflow:hidden}
.bar i{display:block;height:100%;width:0;background:var(--tg-theme-button-color);transition:width .3s}
.r{display:flex;align-items:center;gap:10px;padding:8px 0;border-top:1px solid var(--tg-theme-section-separator-color,rgba(128,128,128,.25))}
.r:first-child{border-top:0}
.k{flex:none;width:20px;height:20px;border-radius:50%;border:2px solid var(--tg-theme-hint-color);box-sizing:border-box;color:#fff;font-size:12px;line-height:16px;text-align:center}
.x .k{background:var(--tg-theme-button-color);border-color:var(--tg-theme-button-color)}
.t{flex:1;min-width:0}
.t div{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.x .t div{text-decoration:line-through;color:var(--tg-theme-hint-color)}
.o .c{color:var(--tg-theme-destructive-text-color,#e53935)}
.w{opacity:.45}
button{flex:none;padding:4px 10px;min-height:0;height:auto;font-size:.85em;background:var(--tg-theme-secondary-bg-color);color:var(--tg-theme-link-color)}
#e{text-align:center;padding:12px 0;color:var(--tg-theme-hint-color)}
#n{min-height:1.2em;padding-top:2px}
</style>
<div class="h"><b>Мой день</b><span class="c" id="d"></span></div>
<div class="bar"><i id="p"></i></div>
<div id="l"></div><div id="n"></div>
<script>
const W=window.TelegramWidget,$=i=>document.getElementById(i);
let S={t:[]},T=0;
function load(){try{const s=JSON.parse(W.chatData);if(s)S=s}catch(e){}}
function note(s){$('n').textContent=s||'';clearTimeout(T);if(s)T=setTimeout(()=>$('n').textContent='',4000)}
function ask(q,row){
 if(row)row.classList.add('w');
 W.request(q).then(a=>{note(a.result);W.HapticFeedback&&W.HapticFeedback.notificationOccurred('success')},
  e=>{if(row)row.classList.remove('w');note(e.code=='RATE_LIMITED'?'Слишком часто, подожди секунду':'Не вышло: '+e.code)});
}
function draw(){
 const t=S.t||[],done=t.filter(x=>x[2]==1).length,all=t.length+(S.m||0),l=$('l');
 $('d').textContent=(S.d||'')+(all?' · '+done+' из '+all:'');
 $('p').style.width=all?(done*100/all)+'%':'0';
 l.textContent='';
 if(!t.length){const e=document.createElement('div');e.id='e';e.textContent=S.d?'На сегодня задач нет':'Загружаю…';l.appendChild(e)}
 for(const x of t){
  const r=document.createElement('div'),k=document.createElement('span'),b=document.createElement('div'),n=document.createElement('div'),m=document.createElement('div');
  r.className='r'+(x[2]==1?' x':x[2]==2?' o':'');k.className='k';k.textContent=x[2]==1?'✓':'';
  b.className='t';n.textContent=x[1];m.className='c';m.textContent=x[3]||'';b.appendChild(n);if(x[3])b.appendChild(m);
  r.appendChild(k);r.appendChild(b);
  r.onclick=()=>ask((x[2]==1?'u:':'d:')+x[0],r);
  if(x[2]!=1){const btn=document.createElement('button');btn.textContent='→';btn.title='На завтра';
   btn.onclick=ev=>{ev.stopPropagation();ask('t:'+x[0],r)};r.appendChild(btn)}
  l.appendChild(r);
 }
 if(S.m){const e=document.createElement('div');e.className='c';e.textContent='и ещё '+S.m+' — в мини-приложении';l.appendChild(e)}
}
if(W){load();draw();W.onEvent('chatDataChanged',()=>{load();draw()});
 W.onEvent('activated',()=>W.request('s').catch(()=>{}));
 W.request('s').catch(()=>{});W.ready()}
</script>"""


def html_sizes() -> tuple[int, int]:
    raw = HTML.encode("utf-8")
    return len(raw), len(zlib.compress(raw, 9))


def _date_label(d) -> str:
    return f"{d.day} {_MONTHS[d.month - 1]}"


def _meta(t: dict, overdue: bool) -> str:
    due = str(t.get("due_date") or "")
    parts = []
    if overdue:
        try:
            parts.append(f"просрочено, {due[8:10]}.{due[5:7]}")
        except Exception:
            parts.append("просрочено")
    elif len(due) >= 16 and due[10] in "T ":
        parts.append(due[11:16])
    who = str(t.get("from") or "")
    if who and who != "?" and who != "@me":
        parts.append(who[:16])
    return " · ".join(parts)


async def text(db) -> str:
    """v4.110: «Мой день» обычным текстом — когда Telegram не пускает виджеты бота."""
    state, _ids = await build_state(db)
    rows = state.get("t") or []
    done = sum(1 for r in rows if r[2] == 1)
    lines = [f"📋 Мой день — {done} из {len(rows)}" if rows else "📋 Мой день — на сегодня задач нет"]
    for _id, title, st, meta in rows:
        lines.append(f"{'✅' if st == 1 else '🔴' if st == 2 else '⬜'} {title}" + (f" · {meta}" if meta else ""))
    if state.get("m"):
        lines.append(f"и ещё {state['m']}")
    return "\n".join(lines)


async def build_state(db, prev_ids: list | None = None) -> tuple[dict, list]:
    """Состояние на сегодня: (state для chat_data, ids всех показанных задач по порядку)."""
    from db import get_open_tasks_due, get_sync_by_id
    from core.digest import _split_due_buckets
    from core.util import today_local
    today = today_local()
    due_today, _tmr, overdue = _split_due_buckets(await get_open_tasks_due(db), today)
    due_today.sort(key=lambda t: str(t.get("due_date") or ""))
    open_rows = [(t, 2) for t in overdue] + [(t, 0) for t in due_today]
    open_ids = {t["id"] for t, _ in open_rows}

    # v4.106.2: закрытые сегодня (где угодно: виджет, Todoist, Мира) — из базы, а не только из прошлого
    # виджета, иначе свежий /day показывал «0 из 2» при двух уже закрытых
    closed_today = []
    try:
        from datetime import datetime, time as _t, timezone as _tzu
        from core.util import _tz
        start = datetime.combine(today, _t(0), tzinfo=_tz()).astimezone(_tzu.utc).strftime("%Y-%m-%d %H:%M:%S")
        async with db.execute(
            "SELECT id FROM task_sync WHERE synced = 1 AND closed_at IS NOT NULL "
            "AND julianday(closed_at) >= julianday(?) AND due_date IS NOT NULL AND due_date != '' "
            "AND substr(due_date, 1, 10) <= ? ORDER BY julianday(closed_at)",
            (start, today.isoformat())) as cur:
            closed_today = [r[0] for r in await cur.fetchall()]
    except Exception as exc:
        logger.debug(f"day widget closed-today skip: {exc}")
    done_rows, seen = [], set()
    for sid in list(prev_ids or []) + closed_today:   # закрытые сегодня остаются в списке зачёркнутыми
        if sid in open_ids or sid in seen:
            continue
        seen.add(sid)
        row = await get_sync_by_id(db, sid)
        if row and row.get("synced"):
            done_rows.append(({"id": sid, "title": row.get("title") or "", "due_date": row.get("due_date"),
                               "from": ""}, 1))
    rows = open_rows + done_rows
    ids = [t["id"] for t, _ in rows]

    def pack(title_len: int, items):
        return [[t["id"], (t.get("title") or "")[:title_len], st, _meta(t, st == 2) if st != 1 else ""]
                for t, st in items]

    state = {"d": _date_label(today), "t": [], "m": 0}
    keep = list(rows)
    title_len = 48
    while True:
        state["t"] = pack(title_len, keep)
        state["m"] = len(rows) - len(keep)
        if len(json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= _LIMIT - 20:
            break
        if title_len > 24:
            title_len -= 6
        elif any(st == 1 for _, st in keep):   # сначала убираем зачёркнутые, потом хвост
            for i in range(len(keep) - 1, -1, -1):
                if keep[i][1] == 1:
                    keep.pop(i); break
        elif keep:
            keep.pop()
        else:
            break
    return state, ids


def _dump(state: dict, rev: int) -> str:
    s = dict(state, r=rev)
    return json.dumps(s, ensure_ascii=False, separators=(",", ":"))


def _hash(state: dict) -> str:
    return hashlib.sha1(json.dumps(state, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]


async def _call(token: str, method: str, payload: dict) -> dict:
    from core import widgets   # v4.107: один HTTP-путь для всех виджетов
    return await widgets.call(token, method, payload)


async def _get_cur(db) -> dict:
    from db import get_setting
    try:
        return json.loads(await get_setting(db, _KEY) or "{}")
    except Exception:
        return {}


async def _set_cur(db, cur: dict) -> None:
    from db import set_setting
    await set_setting(db, _KEY, json.dumps(cur))


async def send(db, settings, chat_id: int, thread_id=None) -> tuple[bool, str]:
    """Отправить новый виджет «Мой день». (успех, описание ошибки)."""
    from core.util import today_local
    from db import set_setting
    state, ids = await build_state(db)
    payload = {"chat_id": chat_id, "html": HTML, "chat_data": _dump(state, 1),
               "allow_queries": True, "disable_notification": True}
    if thread_id:
        payload["message_thread_id"] = thread_id
    data = await _call(settings.bot_token, "sendWidget", payload)
    if not data.get("ok") and thread_id:   # тема удалена или не поддержана — без темы
        payload.pop("message_thread_id", None)
        data = await _call(settings.bot_token, "sendWidget", payload)
    if not data.get("ok"):
        desc = str(data.get("description") or "нет ответа")
        logger.warning(f"sendWidget failed: {desc}")
        return False, desc
    mid = (data.get("result") or {}).get("message_id")
    await _set_cur(db, {"c": chat_id, "m": mid, "d": today_local().isoformat(), "ids": ids,
                        "h": _hash(state), "r": 1})
    await set_setting(db, _ON_KEY, "1")
    logger.info(f"Day widget sent (msg {mid}, {len(state['t'])} задач)")
    return True, ""


async def refresh(db, settings, force: bool = False) -> bool:
    """Обновить данные текущего виджета, если что-то поменялось. True = было изменение."""
    from core.util import today_local
    cur = await _get_cur(db)
    if not cur.get("m") or cur.get("d") != today_local().isoformat():
        return False
    state, ids = await build_state(db, cur.get("ids"))
    h = _hash(state)
    if h == cur.get("h") and not force:
        return False
    rev = int(cur.get("r") or 1) + 1
    data = await _call(settings.bot_token, "editMessageWidgetData",
                       {"chat_id": cur["c"], "message_id": cur["m"], "chat_data": _dump(state, rev)})
    if not data.get("ok"):
        desc = str(data.get("description") or "")
        if "not modified" not in desc.lower():
            logger.info(f"editMessageWidgetData failed: {desc}")
            if "not found" in desc.lower():   # сообщение удалили — больше не трогаем
                cur["m"] = None
                await _set_cur(db, cur)
            return False
    cur.update(ids=ids, h=h, r=rev)
    await _set_cur(db, cur)
    return True


async def morning(db, settings, owner_id: int, thread_id=None) -> None:
    """Из утренней сводки: шлём виджет, если он хоть раз удачно отправлялся (иначе молчим)."""
    from db import get_setting
    try:
        if (await get_setting(db, _ON_KEY)) != "1":
            return
        await send(db, settings, owner_id, thread_id)
    except Exception as exc:
        logger.info(f"day widget morning skip: {exc}")


# ── нажатия ──────────────────────────────────────────────────────────────────

async def _answer(settings, qid: str, text: str) -> None:
    await _call(settings.bot_token, "answerWidgetQuery", {"widget_query_id": qid, "data": text[:300]})


async def _bg(coro, what: str) -> None:
    try:
        await coro
    except Exception as exc:
        logger.warning(f"day widget {what}: {exc}")


async def _close(db, settings, sid: int) -> str:
    from db import get_sync_by_id, mark_sync_done_by_id, set_setting
    row = await get_sync_by_id(db, sid)
    if not row:
        return "Задача не найдена"
    if row.get("synced"):
        return "Уже закрыта"
    await mark_sync_done_by_id(db, sid)   # локально сразу, как кнопка ✅ (cb_done)
    try:
        await set_setting(db, f"ownerclosed:{sid}", "widget")
    except Exception:
        pass
    if settings.todoist_api_token and row.get("todoist_id"):
        from handlers.callbacks import _todoist_complete

        async def _td():
            if not await _todoist_complete(settings.todoist_api_token, row["todoist_id"]):
                logger.warning(f"day widget: Todoist не закрыл задачу {sid}, доберёт sync-петля")
        asyncio.create_task(_bg(_td(), "todoist close"))
    logger.info(f"Task {sid} closed via day widget")
    return "Готово ✓"


async def _reopen(db, settings, sid: int) -> str:
    from db import get_sync_by_id, reopen_task_sync_by_id
    row = await get_sync_by_id(db, sid)
    if not row:
        return "Задача не найдена"
    if not row.get("synced"):
        return "Задача и так открыта"
    await reopen_task_sync_by_id(db, sid)
    if settings.todoist_api_token and row.get("todoist_id"):
        from core.actions import _reopen_todoist
        asyncio.create_task(_bg(_reopen_todoist(settings.todoist_api_token, row["todoist_id"]), "todoist reopen"))
    logger.info(f"Task {sid} reopened via day widget")
    return "Вернула в работу"


async def _tomorrow(db, settings, sid: int) -> str:
    from datetime import timedelta
    from core.util import today_local
    from handlers.owner import _apply_reschedule
    from db import bump_reschedule
    applied = await _apply_reschedule(db, settings, sid, (today_local() + timedelta(days=1)).isoformat())
    if not applied:
        return "Задача не найдена или уже закрыта"
    n = await bump_reschedule(db, sid)
    logger.info(f"Task {sid} → tomorrow via day widget (reschedules={n})")
    return f"Перенесла на завтра. Это уже {n}-й перенос" if n >= 3 else "Перенесла на завтра"


async def handle_query(db, settings, wq: dict) -> None:
    from core import owners
    qid = str(wq.get("id") or "")
    uid = int((wq.get("from") or {}).get("id") or 0)
    data = str(wq.get("data") or "")
    if not qid:
        return
    if not owners.is_owner(settings.owner_user_id, uid):
        await _answer(settings, qid, "Недоступно")
        return
    cur = await _get_cur(db)
    chat_id = (wq.get("chat") or {}).get("id")
    if cur.get("m") and (chat_id != cur.get("c") or wq.get("message_id") != cur.get("m")):
        # старый виджет (вчерашний или прошлый /day): действия разрешены, но данные не трогаем
        if data == "s":
            await _answer(settings, qid, "")
            return
    act, _, arg = data.partition(":")
    if act == "s":
        await refresh(db, settings)
        await _answer(settings, qid, "")
        return
    try:
        sid = int(arg)
    except ValueError:
        await _answer(settings, qid, "Не поняла нажатие")
        return
    fn = {"d": _close, "u": _reopen, "t": _tomorrow}.get(act)
    if not fn:
        await _answer(settings, qid, "Не поняла нажатие")
        return
    job = asyncio.ensure_future(fn(db, settings, sid))
    try:   # ответить надо за 15 с, перенос с календарём бывает дольше — тогда досчитываем в фоне
        text = await asyncio.wait_for(asyncio.shield(job), timeout=10)
    except asyncio.TimeoutError:
        text = "Делаю, обновлю через пару секунд"

        async def _later():
            await job
            await refresh(db, settings)
        asyncio.create_task(_bg(_later(), "slow action"))
    except Exception as exc:
        logger.warning(f"day widget action {data}: {exc}")
        text = "Не вышло, попробуй ещё раз"
    await _answer(settings, qid, text)
    await refresh(db, settings)


async def middleware(handler, event, data):
    """Совместимость v4.106: маршрутизация нажатий теперь в core.widgets."""
    from core import widgets
    return await widgets.middleware(handler, event, data)
