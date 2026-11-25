# -*- coding: utf-8 -*-
"""v4.107 виджет «Черновик ответа»: текст черновика с кнопками «Отправить», «Короче», «Мягче»,
«Скопировать», «Отвечу сам», «✕». Отправка контакту — только по нажатию владельца, как у старой карточки.

«Скопировать» работает внутри виджета без бота. «Короче»/«Мягче» переписывают черновик моделью
и обновляют ту же карточку. Не влез в 1 КБ или виджет не отправился — drafts.offer_draft шлёт старую карточку.
"""
from loguru import logger

from core import widgets as W

HTML = W.page(
    ".q{color:var(--tg-theme-hint-color);font-size:.85em;margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}"
    ".x{margin:8px 0;padding:8px 10px;border-radius:10px;background:var(--tg-theme-secondary-bg-color);white-space:pre-wrap}"
    ".b{display:flex;flex-wrap:wrap;gap:6px}.b button{flex:1 1 auto}"
    ".b .p{background:var(--tg-theme-button-color);color:var(--tg-theme-button-text-color,#fff)}"
    ".st{font-weight:600}.w{opacity:.45}",
    '<div class="h"><b>Черновик ответа</b><span class="c" id="u"></span></div>'
    '<div class="q" id="q"></div><div class="x" id="x"></div><div id="b"></div>',
    "const ST={s:'Отправлено',n:'Не отправляю',t:'Жду твой ответ — запомню стиль',x:'Черновик устарел, ответь сам',"
    "c:'Telegram не даёт боту написать этому контакту — скопируй и отправь сам'};"
    "function btn(t,c,f){const b=el('button',c,t);b.onclick=f;return b}"
    "function draw(){$('u').textContent=S.u||'';$('q').textContent=S.i?'на «'+S.i+'»':'';$('x').textContent=S.x||'';"
    "const b=$('b');b.textContent='';b.className='b';"
    "const cp=btn('Скопировать','',()=>{W.copyText(S.x||'');note('Скопировала')});"
    "if(S.st){b.appendChild(el('div','st',ST[S.st]||''));if(S.st=='c'||S.st=='x')b.appendChild(cp);return}"
    "b.appendChild(btn('Отправить','p',e=>ask('df:o:'+S.k,b)));b.appendChild(cp);"
    "b.appendChild(btn('Короче','',()=>ask('df:k:'+S.k,$('x'))));b.appendChild(btn('Мягче','',()=>ask('df:m:'+S.k,$('x'))));"
    "b.appendChild(btn('Отвечу сам','',()=>ask('df:t:'+S.k,b)));b.appendChild(btn('✕','',()=>ask('df:n:'+S.k,b)))}"
    "if(W){load();draw();W.onEvent('chatDataChanged',()=>{load();draw()});W.ready()}",
)


def _state(item: dict, token: str, status: str = "") -> dict | None:
    state = {"u": "@" + str(item.get("username") or "")[:32], "i": "", "x": item.get("text") or "",
             "k": token, "st": status}
    for cut in (120, 80, 40, 0):
        state["i"] = (item.get("incoming") or "").replace("\n", " ")[:cut]
        if W.nbytes(state) <= W.LIMIT:
            return state
    return None   # сам черновик не влезает — показывать урезанным нельзя, отправится ведь полный


async def offer(owner_id: int, token: str) -> bool:
    from config import settings
    from core import drafts
    from core.topics import thread_id
    item = drafts._pending.get(token)
    state = _state(item, token) if item else None
    if not state:
        return False
    ok, mid, _err = await W.send(settings, owner_id, HTML, state, thread_id("drafts"))
    if ok:
        item["w"] = (owner_id, mid)
    return ok


async def _set(ctx, item: dict | None, token: str, status: str) -> None:
    item = item or {}
    if not ctx.get("message_id"):
        return
    st = _state(item, token, status) if item.get("text") else {"u": "", "i": "", "x": "", "k": token, "st": status}
    if st:
        await W.edit(ctx["settings"], ctx["chat_id"], ctx["message_id"], st)


async def on_query(ctx: dict, act: str, token: str) -> str:
    from core import drafts
    settings = ctx["settings"]
    item = dict(drafts._pending.get(token) or {})
    if not item and act in ("o", "k", "m", "t"):
        await _set(ctx, None, token, "x")
        return "Черновик устарел (бот перезапускался)"
    if act == "o":
        ok, uname, err = await drafts.send_draft(ctx["bot"], token)
        if ok:
            await _set(ctx, item, token, "s")
            return f"Отправила @{uname}"
        if "BUSINESS_PEER_USAGE_MISSING" in err:
            await _set(ctx, item, token, "c")
            return "Telegram запрещает — скопируй"
        if err == "expired":
            await _set(ctx, item, token, "x")
            return "Черновик устарел"
        return f"Не отправилось: {err[:120]}"
    if act in ("k", "m"):
        async def _do():
            new = await drafts.rewrite(ctx["client"], settings.anthropic_model_smart, token, act)
            if not new:
                return "Не получилось переписать"
            st = _state(drafts._pending.get(token) or {}, token)
            if not st:   # новый вариант не влез в карточку — откатываем
                drafts._pending[token]["text"] = item.get("text")
                return "Вышло слишком длинно, оставила как было"
            await W.edit(settings, ctx["chat_id"], ctx["message_id"], st)
            return "Переписала"
        return await W.within(_do(), "Переписываю, обновлю карточку")   # _do сам обновит карточку
    if act == "t":
        drafts.arm_teach_from_token(token, settings.owner_user_id)
        await _set(ctx, item, token, "t")
        return "Ок, ответь сам"
    if act == "n":
        drafts.drop_draft(token)
        await _set(ctx, item, token, "n")
        return "Не отправляю"
    return ""


W.register("df", on_query)
logger.debug("drafts widget registered")
