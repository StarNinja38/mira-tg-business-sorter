"""
v4.73 Мини-приложение: страница и API на том же локальном порту, что и «Мира в чёлке».

nginx проксирует наружу только /webapp/, поэтому /mira/* остаётся за Bearer и SSH-туннелем.
Вход: подпись initData от Telegram (HMAC от токена бота) плюс id владельца. Секрета в странице
нет, а без подписи ни одна ручка данных не отвечает. Страница вне Telegram показывает примеры.
"""
import asyncio
import hashlib
import hmac
import json
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web
from loguru import logger

from core import autonomy, connectors, mira, richmsg, routines, skills
from core.mira_api import _OwnerMessage
from core.util import today_local

PAGE = Path(__file__).resolve().parent.parent / "webapp" / "index.html"
# webapp/index.html — фрагмент без заголовка документа, тот же файл публикуется артефактом
SHELL = ('<!doctype html><html lang="ru"><meta charset="utf-8">'
         '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
         '<style>:root{color-scheme:dark;padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}'
         'body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style>\n')
MAX_AGE = 24 * 3600
_ask_lock = asyncio.Lock()


def render_page() -> str:
    return SHELL + PAGE.read_text(encoding="utf-8")



def check_init_data(init_data: str, bot_token: str, owner_id: int, now: float | None = None) -> bool:
    """Подпись по правилам Telegram Mini Apps, свежесть и владелец. Чистая функция."""
    try:
        fields = dict(parse_qsl(init_data or "", keep_blank_values=True, strict_parsing=True))
        got = fields.pop("hash")
        check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        want = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(got, want):
            return False
        if (now or time.time()) - int(fields["auth_date"]) > MAX_AGE:
            return False
        return int(json.loads(fields["user"])["id"]) == owner_id
    except (KeyError, ValueError, TypeError):
        return False


def _due_time(due: str) -> str:
    due = str(due or "")
    return due[11:16] if len(due) >= 16 else ""


async def _day(db, settings, day: date) -> dict:
    from core import schedule
    timed, free = [], []
    async with db.execute("SELECT id, task_title, due_date, synced, user_id, is_commitment FROM task_sync "
                          "WHERE due_date LIKE ? AND (synced=0 OR closed_at LIKE ?)", (f"{day.isoformat()}%", f"{day.isoformat()}%")) as cur:
        tasks = await cur.fetchall()
    for t in tasks:
        item = {"id": t[0], "t": t[1], "task": True, "done": bool(t[3]), "who": t[4] if t[4] != settings.owner_user_id else None,
                "s": "Обещание" if t[5] else ""}
        when = _due_time(t[2])
        (timed if when else free).append({**item, "time": when, "kind": "Задача"} if when else item)
    for p in schedule.pairs_for(day):
        rng = schedule.pair_time(p) or ""
        timed.append({"id": f"p{rng}", "time": rng[:5], "kind": "Пара", "t": p.get("subject") or "Пара",
                      "s": ", ".join(x for x in [p.get("type"), p.get("room")] if x)})
    if settings.google_calendar_id:
        try:
            from core.calendar import list_events
            from core.digest import _day_start
            for e in await list_events(settings.google_service_account_path, settings.google_calendar_id,
                                       _day_start(day, settings), _day_start(day + timedelta(days=1), settings)):
                st = str(e.get("start") or "")
                if "T" in st:
                    timed.append({"id": f"c{e.get('id')}", "time": st[11:16], "kind": "Встреча", "t": str(e.get("summary") or "")[:80], "s": ""})
        except Exception as exc:
            logger.warning(f"webapp day calendar skip: {type(exc).__name__}: {exc}")
    if await connectors.enabled(db, settings, "todoist"):
        from core import todoist_read
        for t in await todoist_read.for_day(settings, day):
            when = t["due"][11:16] if len(t["due"]) >= 16 else ""
            item = {"id": f"t{t['id']}", "t": t["title"], "s": t["project"], "kind": "Todoist"}
            (timed if when else free).append({**item, "time": when} if when else item)
    timed.sort(key=lambda i: i["time"])
    pays = []
    if day == today_local():
        from core import subscriptions
        pays = [{"t": line} for line in (await subscriptions.digest_lines(db))[:4]]
    return {"date": day.isoformat(), "timed": timed, "free": free, "pays": pays}


def _name(row: dict) -> str:
    return row.get("name") or row.get("display_name") or row.get("username") or "Без имени"


async def _insights(db) -> list:
    from db import get_open_commitments, get_unanswered_questions
    out = []
    for w in await get_unanswered_questions(db, hours=24, limit=3):
        out.append({"lvl": "warn", "t": f"{w['who']} ждёт ответа", "s": f"{w['age_days']} дн. без ответа", "who": w.get("user_id")})
    async with db.execute("SELECT id, task_title, user_id FROM task_sync WHERE synced=0 AND is_commitment=1 ORDER BY created_at DESC LIMIT 2") as cur:
        for r in await cur.fetchall():
            out.append({"lvl": "bad", "t": r[1], "s": "Твоё обещание", "who": r[2]})
    if not out:
        out.append({"lvl": "good", "t": "Срочного нет", "s": "Обещания и ответы в порядке", "who": None})
    return out[:4]


async def _people(db, settings, query: str, flt: str) -> list:
    from db import get_unanswered_questions
    waiting = {w.get("user_id"): w for w in await get_unanswered_questions(db, hours=24, limit=30)}
    async with db.execute("SELECT user_id, COUNT(*) FROM task_sync WHERE synced=0 AND is_commitment=1 GROUP BY user_id") as cur:
        promised = {r[0]: r[1] for r in await cur.fetchall()}
    async with db.execute("SELECT user_id, username, name, display_name, relationship, summary FROM contact_profiles "
                          "WHERE user_id != ? AND COALESCE(relationship,'') != 'спамер' ORDER BY last_contact DESC LIMIT 200", (settings.owner_user_id,)) as cur:
        rows = await cur.fetchall()
    keys = ("user_id", "username", "name", "display_name", "relationship", "summary")
    out, q = [], query.strip().lower()
    for r in rows:
        p = dict(zip(keys, r))
        uid = p["user_id"]
        status = (f"Ждёт {waiting[uid]['age_days']} дн." if uid in waiting else "") or (f"Обещаний: {promised[uid]}" if uid in promised else "")
        if q and q not in " ".join(str(p[k] or "") for k in keys[1:]).lower():
            continue
        if (flt == "wait" and uid not in waiting) or (flt == "promise" and uid not in promised):
            continue
        out.append({"id": uid, "name": _name(p), "role": "" if p["relationship"] == "unknown" else (p["relationship"] or ""), "status": status})
    return out[:60]


_AVA_SIZE = 96
_ava_sem = asyncio.Semaphore(6)


def _thumb(raw: bytes) -> bytes:
    """Квадрат 96×96 JPEG: фото из Telegram бывают по 600 px, в списке хватает маленького."""
    from io import BytesIO
    from PIL import Image, ImageOps
    img = ImageOps.fit(Image.open(BytesIO(raw)).convert("RGB"), (_AVA_SIZE, _AVA_SIZE))
    out = BytesIO()
    img.save(out, "JPEG", quality=82)
    return out.getvalue()


async def _avatar(bot, db, settings, uid: int) -> str | None:
    """v4.90: аватар контакта как data URL (CSP страницы пускает только data:). Кэш на диске по photo_unique_id."""
    import base64
    async with db.execute("SELECT photo_file_id, photo_unique_id FROM contact_profiles WHERE user_id=?", (uid,)) as cur:
        row = await cur.fetchone()
    if not row or not row[0]:
        return None
    folder = Path(settings.db_path).parent / "avatars"
    path = folder / f"{uid}_{row[1] or 'x'}.jpg"
    if not path.exists():
        async with _ava_sem:
            try:
                tg_file = await bot.get_file(row[0])
                raw = (await bot.download_file(tg_file.file_path)).read()
                folder.mkdir(exist_ok=True)
                path.write_bytes(await asyncio.to_thread(_thumb, raw))
            except Exception as exc:
                logger.debug(f"avatar {uid} skip: {type(exc).__name__}: {exc}")
                return None
    return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode()


async def _person(db, uid: int) -> dict | None:
    from db import get_contact_profile
    p = await get_contact_profile(db, uid)
    if not p:
        return None
    p = dict(p)
    async with db.execute("SELECT id, task_title FROM task_sync WHERE user_id=? AND synced=0 AND is_commitment=1 ORDER BY created_at DESC LIMIT 10", (uid,)) as cur:
        promises = [{"id": r[0], "t": r[1]} for r in await cur.fetchall()]
    async with db.execute("SELECT task_title FROM task_sync WHERE user_id=? AND synced=0 AND is_commitment=0 ORDER BY created_at DESC LIMIT 6", (uid,)) as cur:
        deals = [r[0] for r in await cur.fetchall()]
    timeline = []
    try:
        for item in (json.loads(p.get("facts_json") or "{}").get("timeline") or [])[-8:]:
            timeline.append([str(item.get("date") or item.get("d") or ""), str(item.get("text") or item.get("event") or item.get("t") or "")])
    except (ValueError, AttributeError) as exc:
        logger.debug(f"webapp person timeline skip: {exc}")
    return {"id": uid, "name": _name(p), "role": p.get("relationship") or "", "summary": (p.get("summary") or "")[:500],
            "promises": promises, "deals": deals, "timeline": timeline[::-1]}


async def _routines(db) -> list:
    await routines._ensure(db)
    async with db.execute("SELECT id, kind, text, schedule FROM routines WHERE enabled=1 ORDER BY next_run") as cur:
        rows = await cur.fetchall()
    return [{"id": r[0], "kind": r[1], "text": r[2][:120], "when": routines.describe(json.loads(r[3]))} for r in rows if not r[2].startswith("skill:")]


async def _close_task(db, settings, sync_id: int, done: bool) -> bool:
    from core.actions import _delete_task_calendar_event, _reopen_todoist
    from core.sync import complete_todoist_task
    from db import get_sync_by_id, mark_sync_done_by_id, reopen_task_sync_by_id, set_setting
    row = await get_sync_by_id(db, sync_id)
    if not row or bool(row.get("synced")) == done:
        return bool(row)
    if settings.todoist_api_token and row.get("todoist_id"):
        await (complete_todoist_task if done else _reopen_todoist)(settings.todoist_api_token, row["todoist_id"])
    if done:
        await _delete_task_calendar_event(db, {"id": sync_id})
        await mark_sync_done_by_id(db, sync_id)
    else:
        await reopen_task_sync_by_id(db, sync_id)
    await set_setting(db, f"ownerclosed:{sync_id}", "webapp" if done else "")
    return True


async def _ask(bot, db, settings, client, text: str) -> None:
    """Фраза из приложения: ответ приходит в обычный чат с ботом."""
    msg = _OwnerMessage(bot, settings.owner_user_id)
    async with _ask_lock:
        try:
            await bot.send_message(settings.owner_user_id, f"▶️ {text}", parse_mode=None)
            ctx = await mira.assistant_context(db, settings, text)
            async with richmsg.thinking(bot, settings.owner_user_id, None, "🔥 Думаю", timer=True):
                reply, used, _ = await asyncio.wait_for(mira.think(client, settings, ctx, text, message=msg, bot=bot, db=db), timeout=240)
            await bot.send_message(settings.owner_user_id, reply[:4000], parse_mode=None)
            await mira.dialog_add(db, "owner", text)
            await mira.dialog_add(db, "mira", reply, used)
        except Exception as exc:
            logger.warning(f"webapp ask failed: {type(exc).__name__}: {exc}")
            await bot.send_message(settings.owner_user_id, "⚠️ Не вышло ответить, попробуй написать мне в чат.", parse_mode=None)


async def _audience(db) -> list:
    """v4.95: подписчики по площадкам из утреннего сбора social_stats и прирост за неделю."""
    out = []
    for platform, metric in (("telegram", "subscribers"), ("twitch", "followers"), ("youtube", "subscribers")):
        try:
            q = "SELECT value, ts FROM social_stats WHERE platform=? AND entity='channel' AND metric=? AND ts <= ? ORDER BY ts DESC LIMIT 1"
            async with db.execute(q, (platform, metric, 4e9)) as cur:
                last = await cur.fetchone()
            if not last:
                continue
            async with db.execute(q, (platform, metric, last[1] - 7 * 86400 + 3600)) as cur:
                week = await cur.fetchone()
        except Exception:
            return out
        out.append({"platform": platform, "value": int(last[0]), "delta": int(last[0] - week[0]) if week else None, "ts": last[1]})
    return out


async def _moderation(bot, db, settings) -> dict:
    """v4.94: экран «Модерация» — площадки, счётчики за сегодня, карточки без решения, чат Twitch, журнал, выключатели."""
    from core import autonomy, modlog, moderation, social, twitch_chat, youtube_chat
    from core.util import now_local
    from db import get_setting
    midnight = now_local().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    counts = await modlog.today(db, midnight)
    who = await twitch_chat.me(db)
    tw = {"ready": social.tw_enabled(settings), "connected": bool(who), "login": (who or {}).get("login"),
          "channel": settings.twitch_login, "listening": twitch_chat._state["listening"], "modes": None, "feed": twitch_chat.feed()}
    if who:
        try:
            tw["modes"] = await twitch_chat.modes(db, settings)
        except Exception as exc:
            tw["error"] = str(exc)[:160]
    ytme = await youtube_chat.me(db)
    yt = {"ready": youtube_chat.ready(settings), "connected": bool(ytme), "title": (ytme or {}).get("title"),
          "live": bool(youtube_chat._state["chat"]), "feed": youtube_chat.feed(), "modes": await youtube_chat.modes(db),
          "bans": youtube_chat.bans()}
    sums = [json.loads(r) for r in (await get_setting(db, "twitch:last_summary"), await get_setting(db, "youtube:last_summary")) if r]
    raw = json.dumps(max(sums, key=lambda x: x["ts"])) if sums else None
    try:
        group = await moderation._discussion_id(bot, db, settings)
    except Exception:
        group = None
    return {
        "audience": await _audience(db),
        "telegram": {"chat": bool(group), "on": await autonomy.enabled(db, "moderation")},
        "twitch": tw,
        "youtube": yt,
        "today": {"deleted": counts.get("delete", 0), "cards": counts.get("card", 0), "bans": counts.get("ban", 0) + counts.get("timeout", 0),
                  "replies": counts.get("reply", 0), "reply_cap": twitch_chat.REPLY_DAILY_CAP, "alerts": counts.get("alert", 0)},
        "pending": await moderation.pending(db),
        "log": await modlog.recent(db, 30),
        "summary": json.loads(raw) if raw else None,
        "switches": [s for s in await autonomy.state(db) if s.get("group") == "mod"],
    }


def setup(app, bot, db, settings, client) -> None:
    def api(fn):
        async def wrapped(request):
            if not check_init_data(request.headers.get("X-Init-Data", ""), settings.bot_token, settings.owner_user_id):
                return web.json_response({"error": "только для владельца"}, status=401)
            try:
                result = await fn(request)
            except (ValueError, KeyError, TypeError) as exc:
                return web.json_response({"error": str(exc)[:200]}, status=400)
            return web.json_response(result, headers={"Cache-Control": "no-store"})
        return wrapped

    async def page(request):
        return web.Response(text=render_page(), content_type="text/html", charset="utf-8", headers={
            "Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline' https://telegram.org; style-src 'unsafe-inline' https://fonts.googleapis.com; "
                                       "font-src https://fonts.gstatic.com; connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'"})

    async def bootstrap(request):
        from db import get_spending
        spending = await get_spending(db)
        return {"insights": await _insights(db), "skills": await skills.catalog(db, settings), "connectors": await connectors.state(db, settings),
                "routines": await _routines(db), "persona": await mira.persona(db), "spent": round(spending["month_usd"], 2),
                "day": await _day(db, settings, today_local()), "today": today_local().isoformat(), "timezone": settings.timezone,
                "model": settings.anthropic_model_smart, "auto": await autonomy.state(db), "memory": await autonomy.memory(db, settings)}

    async def day(request):
        return await _day(db, settings, date.fromisoformat(request.query["date"]))

    async def people(request):
        return await _people(db, settings, request.query.get("q", ""), request.query.get("filter", "all"))

    async def person(request):
        return await _person(db, int(request.query["id"])) or {"error": "нет такого контакта"}

    async def avatars(request):
        ids = [int(x) for x in request.query.get("ids", "").split(",") if x.strip().lstrip("-").isdigit()][:60]
        pics = await asyncio.gather(*(_avatar(bot, db, settings, uid) for uid in ids))
        return {"avatars": {str(uid): pic for uid, pic in zip(ids, pics) if pic}}

    async def task(request):
        body = await request.json()
        return {"ok": await _close_task(db, settings, int(body["id"]), bool(body["done"]))}

    async def skill(request):
        body = await request.json()
        sid = str(body["id"])
        note = await (skills.install(db, settings, sid, body.get("schedule")) if body.get("on") else skills.remove(db, sid))
        return {"note": note, "skills": await skills.catalog(db, settings)}

    async def connector(request):
        body = await request.json()
        ok = await connectors.toggle(db, settings, str(body["id"]), bool(body["on"]))
        return {"ok": ok, "connectors": await connectors.state(db, settings), "skills": await skills.catalog(db, settings)}

    async def routine_del(request):
        body = await request.json()
        await routines.remove(db, int(body["id"]))
        return {"routines": await _routines(db)}

    async def persona_set(request):
        from db import set_setting
        body = await request.json()
        data = {"tone": body.get("tone") if body.get("tone") in mira.TONES else "calm",
                "len": body.get("len") if body.get("len") in mira.LENGTHS else "short",
                "address": str(body.get("address") or "").strip()[:20]}
        await set_setting(db, "mira_persona", json.dumps(data, ensure_ascii=False))
        return await mira.persona(db)

    async def auto_set(request):
        body = await request.json()
        return {"auto": await autonomy.toggle(db, str(body.get("id")), bool(body.get("on")))}

    async def memory_forget(request):
        return {"memory": await autonomy.forget(db, settings, str((await request.json()).get("text") or "")[:220])}

    async def ask(request):
        text = str((await request.json()).get("text") or "").strip()[:1000]
        if not text:
            raise ValueError("пустой запрос")
        asyncio.create_task(_ask(bot, db, settings, client, text))
        return {"ok": True}

    _mk_cache: dict = {}

    async def market_get(request):
        """v4.112 экран «Рынок» в мини-приложении (вместо виджета, который Telegram с 13.11 не пускает).
        Те же данные, что у /market; кэш на период 10 минут, чтобы не дёргать котировки на каждое открытие."""
        from core.widgets import market
        p = request.query.get("p") or "1"
        if p not in ("1", "7", "30"):
            raise ValueError("период: 1, 7 или 30")
        hit = _mk_cache.get(p)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        state = await market.build_state(p)
        if state.get("r"):
            _mk_cache[p] = (time.time(), state)
        return state

    async def moderation_get(request):
        return await _moderation(bot, db, settings)

    async def mod_decide(request):
        from core import moderation
        body = await request.json()
        if body.get("action") not in ("del", "ban", "ok"):
            raise ValueError("нет такого действия")
        note = await moderation.decide(bot, db, str(body.get("token") or ""), body["action"])
        return {"note": note, **await _moderation(bot, db, settings)}

    async def twitch_act(request):
        """Действие в чате стрима. platform: twitch | youtube | both (только say: в оба чата сразу)."""
        from core import twitch_chat, youtube_chat
        body = await request.json()
        if body.get("action") not in ("say", "timeout", "ban", "unban", "mode"):
            raise ValueError("нет такого действия")
        where = body.get("platform") or "twitch"
        notes = []
        if where in ("twitch", "both"):
            notes.append("Twitch: " + await twitch_chat.tool(body, db, settings))
        if where == "youtube" or (where == "both" and body["action"] != "mode"):
            if where == "both" and not youtube_chat._state["chat"]:
                notes.append("YouTube: эфира нет")
            else:
                notes.append("YouTube: " + await youtube_chat.tool(body, db, settings))
        return {"note": "; ".join(notes), **await _moderation(bot, db, settings)}

    app.router.add_get("/webapp/", page)
    for path, fn in (("bootstrap", bootstrap), ("day", day), ("people", people), ("person", person), ("avatars", avatars), ("moderation", moderation_get), ("market", market_get)):
        app.router.add_get(f"/webapp/api/{path}", api(fn))
    for path, fn in (("task", task), ("skill", skill), ("connector", connector), ("routine/delete", routine_del), ("persona", persona_set), ("auto", auto_set), ("memory/forget", memory_forget), ("ask", ask), ("mod/decide", mod_decide), ("twitch", twitch_act)):
        app.router.add_post(f"/webapp/api/{path}", api(fn))
