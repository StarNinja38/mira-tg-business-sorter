"""
v4.53 «Мира и соцсети», этап 1 (лёгкие платформы): YouTube + Twitch.
Сборщик снимает цифры раз в сутки (06:00) и при живом стриме каждые 5 мин → таблицы social_stats/social_items
в bot.db. Мира читает через инструмент social_stats; сводка показывает «📊 Соцсети вчера» (дельты).

YouTube — Data API v3 по API-КЛЮЧУ (публичные данные: подписчики, просмотры канала, статистика последних видео).
        Analytics API (CTR/удержание/время просмотра) требует OAuth владельца — этап 2.
Twitch  — Helix, app access token (client credentials): стримы (живые зрители → пик/среднее), VOD-просмотры, клипы;
        фолловеры требуют пользовательский токен (device-flow через /social auth twitch).
Ключи — только в .env (YOUTUBE_API_KEY, YOUTUBE_CHANNEL_ID, TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, TWITCH_LOGIN).
Без ключей модуль молчит (ничего не собирает, инструмент честно отвечает «не подключено»).
"""
import json
import time
from datetime import date, datetime, timedelta, timezone

import aiohttp
from loguru import logger

_YT = "https://www.googleapis.com/youtube/v3"
_TW = "https://api.twitch.tv/helix"
_TW_ID = "https://id.twitch.tv/oauth2"


# ── хранилище ───────────────────────────────────────────────────────────────
async def ensure_tables(db) -> None:
    await db.execute("CREATE TABLE IF NOT EXISTS social_stats (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, day TEXT, "
                     "platform TEXT, entity TEXT, metric TEXT, value REAL)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_social_stats ON social_stats(platform, entity, metric, day)")
    await db.execute("CREATE TABLE IF NOT EXISTS social_items (platform TEXT, item_id TEXT, title TEXT, published TEXT, url TEXT, "
                     "kind TEXT, PRIMARY KEY(platform, item_id))")
    await db.commit()


async def _put(db, platform: str, entity: str, metric: str, value, day: str | None = None) -> None:
    if value is None:
        return
    await db.execute("INSERT INTO social_stats(ts, day, platform, entity, metric, value) VALUES (?,?,?,?,?,?)",
                     (time.time(), day or date.today().isoformat(), platform, entity, metric, float(value)))


async def _item(db, platform: str, item_id: str, title: str, published: str, url: str, kind: str) -> None:
    await db.execute("INSERT OR REPLACE INTO social_items(platform, item_id, title, published, url, kind) VALUES (?,?,?,?,?,?)",
                     (platform, item_id, (title or "")[:200], (published or "")[:25], url, kind))


async def _get(url: str, params: dict | None = None, headers: dict | None = None, timeout: int = 20) -> dict:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
        async with s.get(url, params=params, headers=headers) as r:
            data = await r.json(content_type=None)
            if r.status >= 400:
                raise RuntimeError(f"HTTP {r.status}: {str(data)[:200]}")
            return data


# ── YouTube ─────────────────────────────────────────────────────────────────
def yt_enabled(settings) -> bool:
    return bool(getattr(settings, "youtube_api_key", None) and getattr(settings, "youtube_channel_id", None))


async def _yt_resolve_channel(key: str, ident: str) -> str:
    """UC… как есть; @handle → channels.list(forHandle)."""
    if ident.startswith("UC") and len(ident) >= 20:
        return ident
    d = await _get(f"{_YT}/channels", {"part": "id", "forHandle": ident.lstrip("@"), "key": key})
    items = d.get("items") or []
    if not items:
        raise RuntimeError(f"YouTube: канал {ident!r} не найден")
    return items[0]["id"]


async def collect_youtube(db, settings) -> dict:
    """Снимок канала + статистика последних 30 видео. Возвращает краткий dict для лога."""
    key = settings.youtube_api_key
    cid = await _yt_resolve_channel(key, settings.youtube_channel_id)
    ch = await _get(f"{_YT}/channels", {"part": "statistics,snippet,contentDetails", "id": cid, "key": key})
    it = (ch.get("items") or [None])[0]
    if not it:
        raise RuntimeError("YouTube: пустой ответ channels.list")
    st = it.get("statistics") or {}
    await ensure_tables(db)
    await _put(db, "youtube", "channel", "subscribers", st.get("subscriberCount"))
    await _put(db, "youtube", "channel", "views", st.get("viewCount"))
    await _put(db, "youtube", "channel", "videos", st.get("videoCount"))
    await _item(db, "youtube", "channel", (it.get("snippet") or {}).get("title") or "канал", "", f"https://www.youtube.com/channel/{cid}", "channel")
    uploads = ((it.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
    n_vid = 0
    if uploads:
        pl = await _get(f"{_YT}/playlistItems", {"part": "contentDetails,snippet", "playlistId": uploads, "maxResults": 30, "key": key})
        ids = [x["contentDetails"]["videoId"] for x in pl.get("items") or [] if x.get("contentDetails", {}).get("videoId")]
        if ids:
            vs = await _get(f"{_YT}/videos", {"part": "statistics,snippet,contentDetails", "id": ",".join(ids), "key": key})
            for v in vs.get("items") or []:
                vid = v["id"]; sn = v.get("snippet") or {}; vst = v.get("statistics") or {}
                dur = (v.get("contentDetails") or {}).get("duration") or ""
                kind = "short" if _iso_dur_sec(dur) <= 180 else "video"   # с 2024 шортсы до 3 минут
                await _item(db, "youtube", vid, sn.get("title"), sn.get("publishedAt"), f"https://youtu.be/{vid}", kind)
                await _put(db, "youtube", vid, "views", vst.get("viewCount"))
                await _put(db, "youtube", vid, "likes", vst.get("likeCount"))
                await _put(db, "youtube", vid, "comments", vst.get("commentCount"))
                n_vid += 1
    await db.commit()
    return {"subscribers": st.get("subscriberCount"), "views": st.get("viewCount"), "videos_snapshotted": n_vid}


async def twitch_followers(db, settings):
    """Фолловеры канала сейчас; None — нет входа владельца в Twitch."""
    ut = await _tw_user_token(db, settings)
    if not ut:
        return None
    bid = await _tw_broadcaster(db, settings, await _tw_app_token(db, settings))
    f = await _get(f"{_TW}/channels/followers", {"broadcaster_id": bid, "first": 1}, _tw_headers(settings, ut))
    return f.get("total")


def _iso_dur_sec(s: str) -> int:
    """PT1H2M3S → секунды."""
    import re
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 10 ** 6
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


# ── Twitch ──────────────────────────────────────────────────────────────────
def tw_enabled(settings) -> bool:
    return bool(getattr(settings, "twitch_client_id", None) and getattr(settings, "twitch_client_secret", None)
                and getattr(settings, "twitch_login", None))


async def _tw_app_token(db, settings) -> str:
    from db import get_setting, set_setting
    raw = await get_setting(db, "social:tw_app")
    try:
        d = json.loads(raw or "{}")
        if d.get("token") and d.get("exp", 0) > time.time() + 300:
            return d["token"]
    except Exception:
        pass
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        async with s.post(f"{_TW_ID}/token", data={"client_id": settings.twitch_client_id,
                                                    "client_secret": settings.twitch_client_secret,
                                                    "grant_type": "client_credentials"}) as r:
            d = await r.json(content_type=None)
    if not d.get("access_token"):
        raise RuntimeError(f"Twitch token: {str(d)[:200]}")
    await set_setting(db, "social:tw_app", json.dumps({"token": d["access_token"], "exp": time.time() + int(d.get("expires_in", 3600))}))
    return d["access_token"]


async def _tw_user_token(db, settings, key: str = "social:tw_user") -> str | None:
    """Пользовательский токен (после /social auth twitch): обновляем по refresh_token. key twitch:bot — аккаунт Миры (v4.93)."""
    from db import get_setting, set_setting
    raw = await get_setting(db, key)
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except Exception:
        return None
    if d.get("exp", 0) > time.time() + 300:
        return d.get("token")
    if not d.get("refresh"):
        return None
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        async with s.post(f"{_TW_ID}/token", data={"client_id": settings.twitch_client_id, "client_secret": settings.twitch_client_secret,
                                                    "grant_type": "refresh_token", "refresh_token": d["refresh"]}) as r:
            n = await r.json(content_type=None)
    if not n.get("access_token"):
        logger.warning(f"Twitch user token refresh failed: {str(n)[:160]}")
        return None
    await set_setting(db, key, json.dumps({"token": n["access_token"], "refresh": n.get("refresh_token") or d["refresh"],
                                                        "exp": time.time() + int(n.get("expires_in", 3600))}))
    return n["access_token"]


def _tw_headers(settings, token: str) -> dict:
    return {"Client-Id": settings.twitch_client_id, "Authorization": f"Bearer {token}"}


async def _tw_broadcaster(db, settings, token: str) -> str:
    from db import get_setting, set_setting
    bid = await get_setting(db, "social:tw_bid")
    if bid:
        return bid
    d = await _get(f"{_TW}/users", {"login": settings.twitch_login}, _tw_headers(settings, token))
    items = d.get("data") or []
    if not items:
        raise RuntimeError(f"Twitch: логин {settings.twitch_login!r} не найден")
    await set_setting(db, "social:tw_bid", items[0]["id"])
    return items[0]["id"]


async def twitch_profile(db, settings) -> dict:
    """v4.114: ник и аватар канала для картинки анонса; кэш на сутки."""
    from db import get_setting, set_setting
    raw = await get_setting(db, "social:tw_profile")
    if raw:
        p = json.loads(raw)
        if time.time() - p.get("ts", 0) < 86400:
            return p
    token = await _tw_app_token(db, settings)
    d = await _get(f"{_TW}/users", {"login": settings.twitch_login}, _tw_headers(settings, token))
    u = (d.get("data") or [{}])[0]
    p = {"nick": u.get("display_name") or settings.twitch_login, "avatar": u.get("profile_image_url") or "", "ts": time.time()}
    await set_setting(db, "social:tw_profile", json.dumps(p))
    return p


async def twitch_last_vod(db, settings, since: float = 0) -> str:
    """v4.114: ссылка на запись этого эфира для кнопки «Запись на Twitch». Запись старше начала эфира
    (VOD выключены или ещё не появился) — пусто, тогда кнопка ведёт на страницу записей канала."""
    token = await _tw_app_token(db, settings)
    bid = await _tw_broadcaster(db, settings, token)
    d = await _get(f"{_TW}/videos", {"user_id": bid, "type": "archive", "first": 1}, _tw_headers(settings, token))
    v = (d.get("data") or [{}])[0]
    try:
        created = datetime.fromisoformat(str(v.get("created_at")).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return ""
    return v.get("url") or "" if created >= since - 600 else ""


async def collect_twitch(db, settings, live_only: bool = False) -> dict:
    """live_only=True — только снимок живых зрителей (каждые 5 мин во время стрима). Полный: VOD, клипы, фолловеры."""
    token = await _tw_app_token(db, settings)
    h = _tw_headers(settings, token)
    bid = await _tw_broadcaster(db, settings, token)
    await ensure_tables(db)
    out = {}
    st = await _get(f"{_TW}/streams", {"user_id": bid}, h)
    live = (st.get("data") or [None])[0]
    if live:
        await _put(db, "twitch", f"stream:{live['id']}", "viewers", live.get("viewer_count"))
        await _item(db, "twitch", f"stream:{live['id']}", live.get("title"), live.get("started_at"),
                    f"https://twitch.tv/{settings.twitch_login}", "stream")
        out["live_viewers"] = live.get("viewer_count")
        out["stream_id"], out["title"] = live["id"], live.get("title") or ""
        out["game"], out["started_at"] = live.get("game_name") or "", live.get("started_at") or ""   # v4.114 для анонса
        out["thumb"] = (live.get("thumbnail_url") or "").replace("{width}", "1280").replace("{height}", "720")
    else:
        out["live"] = False
    if live_only:
        await db.commit()
        return out
    vids = await _get(f"{_TW}/videos", {"user_id": bid, "type": "archive", "first": 20}, h)
    for v in vids.get("data") or []:
        await _item(db, "twitch", f"vod:{v['id']}", v.get("title"), v.get("created_at"), v.get("url"), "vod")
        await _put(db, "twitch", f"vod:{v['id']}", "views", v.get("view_count"))
    since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    clips = await _get(f"{_TW}/clips", {"broadcaster_id": bid, "started_at": since, "first": 20}, h)
    for c in clips.get("data") or []:
        await _item(db, "twitch", f"clip:{c['id']}", c.get("title"), c.get("created_at"), c.get("url"), "clip")
        await _put(db, "twitch", f"clip:{c['id']}", "views", c.get("view_count"))
    out["vods"] = len(vids.get("data") or []); out["clips"] = len(clips.get("data") or [])
    ut = await _tw_user_token(db, settings)
    if ut:
        try:
            f = await _get(f"{_TW}/channels/followers", {"broadcaster_id": bid, "first": 1}, _tw_headers(settings, ut))
            await _put(db, "twitch", "channel", "followers", f.get("total"))
            out["followers"] = f.get("total")
        except Exception as exc:
            logger.warning(f"Twitch followers skip: {exc}")
    else:
        out["followers"] = "нужна авторизация: /social auth twitch"
    await db.commit()
    return out


async def twitch_device_start(settings, scopes: str = "moderator:read:followers channel:read:subscriptions") -> dict:
    """Device-flow: вернуть {device_code, user_code, verification_uri, interval}."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        async with s.post(f"{_TW_ID}/device", data={"client_id": settings.twitch_client_id,
                                                     "scopes": scopes}) as r:
            return await r.json(content_type=None)


async def twitch_device_poll(db, settings, device_code: str, key: str = "social:tw_user") -> bool:
    from db import set_setting
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        async with s.post(f"{_TW_ID}/token", data={"client_id": settings.twitch_client_id, "client_secret": settings.twitch_client_secret,
                                                    "device_code": device_code, "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}) as r:
            d = await r.json(content_type=None)
    if d.get("access_token"):
        await set_setting(db, key, json.dumps({"token": d["access_token"], "refresh": d.get("refresh_token"),
                                                            "exp": time.time() + int(d.get("expires_in", 3600))}))
        return True
    return False


# ── сбор по расписанию ──────────────────────────────────────────────────────
async def collect_all(db, settings, bot=None) -> dict:
    res = {}
    if yt_enabled(settings):
        try:
            res["youtube"] = await collect_youtube(db, settings)
        except Exception as exc:
            res["youtube"] = f"ошибка: {exc}"; logger.warning(f"social youtube: {exc}")
    if yta_enabled(settings):
        try:
            res["youtube_analytics"] = await collect_youtube_analytics(db, settings)
        except Exception as exc:
            res["youtube_analytics"] = f"ошибка: {exc}"; logger.warning(f"social yt analytics: {exc}")
    if bot is not None and getattr(settings, "post_channel", None):
        try:
            res["telegram"] = await collect_telegram(db, settings, bot)
        except Exception as exc:
            res["telegram"] = f"ошибка: {exc}"; logger.warning(f"social telegram: {exc}")
    if tw_enabled(settings):
        try:
            res["twitch"] = await collect_twitch(db, settings)
        except Exception as exc:
            res["twitch"] = f"ошибка: {exc}"; logger.warning(f"social twitch: {exc}")
    if res:
        logger.info(f"Social collect: {res}")
    return res


# ── чтение: сводка/инструмент Миры ──────────────────────────────────────────
async def _latest(db, platform: str, entity: str, metric: str, on_or_before: str | None = None):
    q = "SELECT value, day FROM social_stats WHERE platform=? AND entity=? AND metric=?"
    p = [platform, entity, metric]
    if on_or_before:
        q += " AND day<=?"; p.append(on_or_before)
    q += " ORDER BY ts DESC LIMIT 1"
    async with db.execute(q, p) as cur:
        r = await cur.fetchone()
    return (r[0], r[1]) if r else (None, None)


def _fmt(n) -> str:
    if n is None:
        return "—"
    n = int(n)
    return f"{n:,}".replace(",", " ")


def _delta(a, b) -> str:
    if a is None or b is None:
        return ""
    d = int(a) - int(b)
    return f" ({'+' if d >= 0 else ''}{_fmt(d)})" if d else " (без изменений)"


async def summary(db, platform: str = "all", days: int = 7) -> str:
    """Текстовая сводка для Миры/дайджеста: текущие значения, дельты за days, топ роликов/VOD по приросту."""
    await ensure_tables(db)
    since = (date.today() - timedelta(days=days)).isoformat()
    lines = []
    plats = ["youtube", "twitch", "telegram", "tiktok", "instagram"] if platform in ("all", "все", "") else [platform]
    for pl in plats:
        async with db.execute("SELECT COUNT(*) FROM social_stats WHERE platform=?", (pl,)) as cur:
            if (await cur.fetchone())[0] == 0:
                lines.append(f"{pl}: данных нет (не подключено или ещё не собирали)")
                continue
        if pl in ("tiktok", "instagram"):
            lines.append(await csv_summary(db, pl, days))
            continue
        if pl == "telegram":
            subs, _ = await _latest(db, pl, "channel", "subscribers"); subs0, _ = await _latest(db, pl, "channel", "subscribers", since)
            lines.append(f"Telegram-канал: подписчиков {_fmt(subs)}{_delta(subs, subs0)} за {days} дн.")
            continue
        if pl == "youtube":
            subs, _ = await _latest(db, pl, "channel", "subscribers"); subs0, _ = await _latest(db, pl, "channel", "subscribers", since)
            views, _ = await _latest(db, pl, "channel", "views"); views0, _ = await _latest(db, pl, "channel", "views", since)
            lines.append(f"YouTube: подписчиков {_fmt(subs)}{_delta(subs, subs0)}, просмотров канала {_fmt(views)}{_delta(views, views0)} за {days} дн.")
            _an = await analytics_summary(db, days)
            if _an:
                lines.append(_an)
            # v4.98: свежие ролики отдельно — у них ещё один замер, в топ по приросту они не попадают
            async with db.execute("SELECT item_id, title, kind, published FROM social_items WHERE platform='youtube' "
                                  "AND kind IN ('short', 'video') ORDER BY published DESC LIMIT 6") as cur:
                fresh = await cur.fetchall()
            if fresh:
                lines.append("  последние загрузки, новые сверху:")
                for vid, title, kind, pub in fresh:
                    v, _ = await _latest(db, pl, vid, "views"); v0, _ = await _latest(db, pl, vid, "views", since)
                    lk, _ = await _latest(db, pl, vid, "likes"); cm, _ = await _latest(db, pl, vid, "comments")
                    lines.append(f"   • [{kind}] {str(pub)[:10]} {(title or vid)[:50]} — {_fmt(v)} просм.{_delta(v, v0) if v0 is not None else ''}, "
                                 f"{_fmt(lk)} лайков, {_fmt(cm)} комм.")
        else:
            fol, _ = await _latest(db, pl, "channel", "followers"); fol0, _ = await _latest(db, pl, "channel", "followers", since)
            if fol is not None:
                lines.append(f"Twitch: фолловеров {_fmt(fol)}{_delta(fol, fol0)} за {days} дн.")
            else:
                lines.append("Twitch: фолловеры появятся после /social auth twitch")
            async with db.execute("SELECT entity, MAX(value), AVG(value), COUNT(*) FROM social_stats WHERE platform='twitch' AND metric='viewers' "
                                  "AND day>=? GROUP BY entity ORDER BY MIN(ts) DESC LIMIT 5", (since,)) as cur:
                streams = await cur.fetchall()
            for ent, mx, avg, n in streams:
                async with db.execute("SELECT title, published FROM social_items WHERE platform='twitch' AND item_id=?", (ent,)) as c2:
                    it = await c2.fetchone()
                lines.append(f"  стрим {str(it[1] if it else '')[:10]} «{(it[0] if it else '?')[:40]}»: пик {_fmt(mx)}, средн. {_fmt(avg)} зрителей ({n} замеров)")
        # топ роликов по приросту просмотров за период
        async with db.execute(
            "SELECT s.entity, i.title, i.kind, MAX(s.value) - MIN(s.value) AS growth, MAX(s.value) AS now_v "
            "FROM social_stats s JOIN social_items i ON i.platform=s.platform AND i.item_id=s.entity "
            "WHERE s.platform=? AND s.metric='views' AND s.entity!='channel' AND s.day>=? GROUP BY s.entity "
            "ORDER BY growth DESC LIMIT 5", (pl, since)) as cur:
            top = await cur.fetchall()
        if top and any((g or 0) > 0 for _, _, _, g, _ in top):
            lines.append(f"  топ по приросту просмотров за {days} дн.:")
            for ent, title, kind, growth, now_v in top:
                lines.append(f"   • [{kind}] {(title or ent)[:50]} — +{_fmt(growth)} (всего {_fmt(now_v)})")
        elif top:   # один снимок — прироста ещё нет, показываем текущие просмотры свежих роликов
            lines.append("  прирост появится после второго ежедневного снимка; сейчас (всего просмотров):")
            for ent, title, kind, growth, now_v in sorted(top, key=lambda r: -(r[4] or 0))[:5]:
                lines.append(f"   • [{kind}] {(title or ent)[:50]} — {_fmt(now_v)}")
    return "\n".join(lines) if lines else "статистика соцсетей не подключена"


async def digest_lines(db) -> list[str]:
    """Коротко для утренней сводки: дельты за сутки, только если есть данные за вчера и сегодня."""
    await ensure_tables(db)
    out = []
    y = (date.today() - timedelta(days=1)).isoformat()
    for pl, ent, met, label in (("youtube", "channel", "subscribers", "YouTube подписчики"),
                                ("youtube", "channel", "views", "YouTube просмотры"),
                                ("twitch", "channel", "followers", "Twitch фолловеры"),
                                ("telegram", "channel", "subscribers", "Telegram подписчики")):
        now, dnow = await _latest(db, pl, ent, met)
        prev, _ = await _latest(db, pl, ent, met, y)
        if now is not None and prev is not None and dnow != y:
            out.append(f"{label}: {_fmt(now)}{_delta(now, prev)}")
    async with db.execute("SELECT entity, MAX(value) FROM social_stats WHERE platform='twitch' AND metric='viewers' AND day=? GROUP BY entity", (y,)) as cur:
        for ent, mx in await cur.fetchall():
            out.append(f"Twitch стрим вчера: пик {_fmt(mx)} зрителей")
    v, _ = await _latest(db, "youtube", "analytics", "views", y)
    mins, _ = await _latest(db, "youtube", "analytics", "estimatedMinutesWatched", y)
    if v is not None:
        out.append(f"YouTube вчера: {_fmt(v)} просмотров, {_fmt(mins)} минут просмотра")
    return out


# ── Telegram-канал (этап 2): подписчики через бота-админа ───────────────────
async def collect_telegram(db, settings, bot) -> dict:
    """Подписчики канала (getChatMemberCount) — раз в сутки. Просмотры постов Bot API не отдаёт."""
    ch = getattr(settings, "post_channel", None)
    if not ch:
        return {}
    await ensure_tables(db)
    n = await bot.get_chat_member_count(ch)
    await _put(db, "telegram", "channel", "subscribers", n)
    await _item(db, "telegram", "channel", str(ch), "", f"https://t.me/{str(ch).lstrip('@')}", "channel")
    await db.commit()
    return {"subscribers": n}


# ── YouTube Analytics (этап 5): OAuth владельца → CTR/удержание/время просмотра ──
_G_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
_G_TOKEN = "https://oauth2.googleapis.com/token"
_YTA = "https://youtubeanalytics.googleapis.com/v2/reports"
_YT_SCOPES = "https://www.googleapis.com/auth/yt-analytics.readonly https://www.googleapis.com/auth/youtube.readonly"


def yta_enabled(settings) -> bool:
    return bool(getattr(settings, "google_oauth_client_id", None) and getattr(settings, "google_oauth_client_secret", None))


def youtube_auth_url(settings, scopes: str = _YT_SCOPES) -> str:
    from urllib.parse import urlencode
    return _G_AUTH + "?" + urlencode({"client_id": settings.google_oauth_client_id, "redirect_uri": "http://localhost",
                                       "response_type": "code", "scope": scopes, "access_type": "offline", "prompt": "consent"})


async def youtube_exchange_code(db, settings, code_or_url: str, key: str = "social:yt_user") -> bool:
    """Владелец после согласия попадает на http://localhost/?code=… (страница не откроется) и присылает URL/код."""
    from db import set_setting
    from urllib.parse import urlparse, parse_qs
    code = code_or_url.strip()
    if "code=" in code:
        code = parse_qs(urlparse(code).query).get("code", [code])[0]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        async with s.post(_G_TOKEN, data={"client_id": settings.google_oauth_client_id, "client_secret": settings.google_oauth_client_secret,
                                          "code": code, "grant_type": "authorization_code", "redirect_uri": "http://localhost"}) as r:
            d = await r.json(content_type=None)
    if not d.get("access_token"):
        logger.warning(f"YouTube OAuth exchange failed: {str(d)[:200]}")
        return False
    await set_setting(db, key, json.dumps({"token": d["access_token"], "refresh": d.get("refresh_token"),
                                                        "exp": time.time() + int(d.get("expires_in", 3600))}))
    return True


async def _yt_user_token(db, settings, key: str = "social:yt_user") -> str | None:
    """key youtube:bot — канал Миры для живого чата (v4.95)."""
    from db import get_setting, set_setting
    raw = await get_setting(db, key)
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except Exception:
        return None
    if d.get("exp", 0) > time.time() + 300:
        return d.get("token")
    if not d.get("refresh"):
        return None
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        async with s.post(_G_TOKEN, data={"client_id": settings.google_oauth_client_id, "client_secret": settings.google_oauth_client_secret,
                                          "refresh_token": d["refresh"], "grant_type": "refresh_token"}) as r:
            n = await r.json(content_type=None)
    if not n.get("access_token"):
        logger.warning(f"YouTube token refresh failed: {str(n)[:160]}")
        return None
    await set_setting(db, key, json.dumps({"token": n["access_token"], "refresh": d["refresh"],
                                                        "exp": time.time() + int(n.get("expires_in", 3600))}))
    return n["access_token"]


async def collect_youtube_analytics(db, settings) -> dict:
    """Ежедневная аналитика за вчера (views, estimatedMinutesWatched, averageViewDuration, subscribersGained/Lost,
    likes, comments, shares) + топ-10 видео за 28 дней по просмотрам с CTR-прокси (averageViewPercentage)."""
    tok = await _yt_user_token(db, settings)
    if not tok:
        return {"skip": "нет OAuth — /social auth youtube"}
    h = {"Authorization": f"Bearer {tok}"}
    y = (date.today() - timedelta(days=1)).isoformat()
    await ensure_tables(db)
    d = await _get(_YTA, {"ids": "channel==MINE", "startDate": y, "endDate": y,
                          "metrics": "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained,subscribersLost,likes,comments,shares"}, h)
    out = {}
    if d.get("rows"):
        names = [c["name"] for c in d.get("columnHeaders", [])]
        for name, val in zip(names, d["rows"][0]):
            await _put(db, "youtube", "analytics", name, val, day=y)
            out[name] = val
    since = (date.today() - timedelta(days=28)).isoformat()
    top = await _get(_YTA, {"ids": "channel==MINE", "startDate": since, "endDate": y, "dimensions": "video",
                            "metrics": "views,estimatedMinutesWatched,averageViewPercentage,likes", "sort": "-views", "maxResults": 10}, h)
    for row in top.get("rows") or []:
        vid, views, minutes, avp, likes = row[:5]
        await _put(db, "youtube", f"a28:{vid}", "views28", views, day=y)
        await _put(db, "youtube", f"a28:{vid}", "minutes28", minutes, day=y)
        await _put(db, "youtube", f"a28:{vid}", "avg_view_pct", avp, day=y)
    out["top28"] = len(top.get("rows") or [])
    for name, n in (await _yta_breakdowns(db, h, since, y, [r[0] for r in (top.get("rows") or [])[:3]])).items():
        out[name] = n
    await db.commit()
    return out


_TRAFFIC = {"YT_SEARCH": "поиск YouTube", "RELATED_VIDEO": "рекомендации к видео", "BROWSE": "главная и подписки", "SHORTS": "лента Shorts",
            "EXT_URL": "внешние сайты", "NO_LINK_OTHER": "прямые и неизвестные", "PLAYLIST": "плейлисты", "SUBSCRIBER": "подписки",
            "NOTIFICATION": "уведомления", "YT_CHANNEL": "страница канала", "YT_OTHER_PAGE": "другие страницы YouTube",
            "END_SCREEN": "конечные заставки", "HASHTAGS": "хэштеги", "SOUND_PAGE": "страница звука", "ANNOTATION": "подсказки",
            "CAMPAIGN_CARD": "карточки", "LIVE_REDIRECT": "переход на эфир", "PRODUCT_PAGE": "страница товара"}
_DEVICES = {"MOBILE": "телефон", "DESKTOP": "компьютер", "TV": "телевизор", "TABLET": "планшет", "GAME_CONSOLE": "приставка"}
_RET_POINTS = (0.25, 0.5, 0.75, 0.95)


async def _yta_breakdowns(db, h: dict, since: str, day: str, videos: list) -> dict:
    """v4.95: разрезы за 28 дней — источники трафика, возраст и пол, страны, устройства, удержание топ-видео.
    Каждый разрез отдельно: сбой одного не мешает остальным."""
    base = {"ids": "channel==MINE", "startDate": since, "endDate": day}
    plan = [("traffic", {"dimensions": "insightTrafficSourceType", "metrics": "views", "sort": "-views"}),
            ("demo", {"dimensions": "ageGroup,gender", "metrics": "viewerPercentage"}),
            ("geo", {"dimensions": "country", "metrics": "views", "sort": "-views", "maxResults": 10}),
            ("device", {"dimensions": "deviceType", "metrics": "views", "sort": "-views"})]
    out = {}
    for entity, q in plan:
        try:
            d = await _get(_YTA, {**base, **q}, h)
            rows = d.get("rows") or []
            for row in rows:
                await _put(db, "youtube", entity, ":".join(str(x) for x in row[:-1]), row[-1], day=day)
            out[entity] = len(rows)
        except Exception as exc:
            logger.warning(f"YouTube Analytics {entity}: {exc}")
    for vid in videos:
        try:
            d = await _get(_YTA, {**base, "dimensions": "elapsedVideoTimeRatio", "metrics": "audienceWatchRatio", "filters": f"video=={vid}"}, h)
            curve = [(float(r[0]), float(r[1])) for r in d.get("rows") or []]
            for point in _RET_POINTS:
                near = min(curve, key=lambda c: abs(c[0] - point)) if curve else None
                if near:
                    await _put(db, "youtube", f"ret:{vid}", f"at{int(point * 100)}", near[1], day=day)
            out["retention"] = out.get("retention", 0) + 1
        except Exception as exc:
            logger.warning(f"YouTube Analytics retention {vid}: {exc}")
    return out


async def _yt_cut(db, entity: str) -> list:
    """Строки разреза за последний день сбора: [(metric, value)], по убыванию."""
    async with db.execute("SELECT metric, MAX(value) FROM social_stats WHERE platform='youtube' AND entity=? AND day="
                          "(SELECT MAX(day) FROM social_stats WHERE platform='youtube' AND entity=?) GROUP BY metric ORDER BY 2 DESC", (entity, entity)) as cur:
        return await cur.fetchall()


async def breakdown_summary(db) -> str:
    """Откуда зрители и кто они, за 28 дней: для сводки Миры."""
    lines = []
    traffic = await _yt_cut(db, "traffic")
    total = sum(v for _, v in traffic) or 0
    if total:
        lines.append("  источники просмотров: " + ", ".join(f"{_TRAFFIC.get(k, k.lower())} {v / total:.0%}" for k, v in traffic[:5]))
    demo = await _yt_cut(db, "demo")
    if demo:
        g = {"male": "м", "female": "ж", "user_specified": "др"}
        lines.append("  аудитория: " + ", ".join(f"{k.split(':')[0].replace('age', '')} {g.get(k.split(':')[1], '')} {v:.0f}%" for k, v in demo[:4]))
    geo = await _yt_cut(db, "geo")
    gtotal = sum(v for _, v in geo) or 0
    if gtotal:
        lines.append("  страны (из топ-10): " + ", ".join(f"{k} {v / gtotal:.0%}" for k, v in geo[:5]))
    dev = await _yt_cut(db, "device")
    dtotal = sum(v for _, v in dev) or 0
    if dtotal:
        lines.append("  устройства: " + ", ".join(f"{_DEVICES.get(k, k.lower())} {v / dtotal:.0%}" for k, v in dev))
    async with db.execute("SELECT DISTINCT entity FROM social_stats WHERE platform='youtube' AND entity LIKE 'ret:%' AND day="
                          "(SELECT MAX(day) FROM social_stats WHERE platform='youtube' AND entity LIKE 'ret:%')") as cur:
        rets = [r[0] for r in await cur.fetchall()]
    for ent in rets:
        pts = dict(await _yt_cut(db, ent))
        async with db.execute("SELECT title FROM social_items WHERE platform='youtube' AND item_id=?", (ent[4:],)) as cur:
            row = await cur.fetchone()
        title = (row[0] if row else ent[4:])[:45]
        lines.append(f"  удержание «{title}»: " + ", ".join(f"{p}% ролика — {pts[f'at{p}'] * 100:.0f}% зрителей"
                                                         for p in (25, 50, 75, 95) if f"at{p}" in pts))
    return ("YouTube за 28 дней, кто и откуда смотрит:\n" + "\n".join(lines)) if lines else ""


async def analytics_summary(db, days: int = 7) -> str:
    """Сводка YouTube Analytics: суммы за period по дням + топ-видео за 28 дн. с удержанием."""
    await ensure_tables(db)
    since = (date.today() - timedelta(days=days)).isoformat()
    async with db.execute("SELECT metric, SUM(value), COUNT(DISTINCT day) FROM social_stats WHERE platform='youtube' AND entity='analytics' "
                          "AND day>=? GROUP BY metric", (since,)) as cur:
        rows = await cur.fetchall()
    if not rows:
        return ""
    m = {r[0]: (r[1], r[2]) for r in rows}
    days_n = max(v[1] for v in m.values())
    def g(k): return m.get(k, (0, 0))[0]
    lines = [f"YouTube Analytics за {days_n} дн.: просмотров {_fmt(g('views'))}, минут просмотра {_fmt(g('estimatedMinutesWatched'))}, "
             f"подписчиков +{_fmt(g('subscribersGained'))}/−{_fmt(g('subscribersLost'))}, лайков {_fmt(g('likes'))}, комментариев {_fmt(g('comments'))}"]
    if "averageViewPercentage" in m and days_n:
        lines.append(f"  среднее удержание ≈ {m['averageViewPercentage'][0] / days_n:.0f}%")
    async with db.execute("SELECT s.entity, MAX(s.value), i.title FROM social_stats s LEFT JOIN social_items i ON i.platform='youtube' AND i.item_id=substr(s.entity, 5) "
                          "WHERE s.platform='youtube' AND s.entity LIKE 'a28:%' AND s.metric='views28' AND s.day>=? GROUP BY s.entity ORDER BY 2 DESC LIMIT 5", (since,)) as cur:
        top = await cur.fetchall()
    if top:
        lines.append("  топ за 28 дн. по аналитике:")
        for ent, v, title in top:
            async with db.execute("SELECT MAX(value) FROM social_stats WHERE platform='youtube' AND entity=? AND metric='avg_view_pct'", (ent,)) as c2:
                avp = (await c2.fetchone())[0]
            lines.append(f"   • {(title or ent[4:])[:50]} — {_fmt(v)} просм., удержание {avp:.0f}%" if avp is not None else f"   • {(title or ent[4:])[:50]} — {_fmt(v)} просм.")
    extra = await breakdown_summary(db)
    if extra:
        lines.append(extra)
    return "\n".join(lines)


# ── Instagram / TikTok: выгрузки CSV (API аналитики закрыты) ─────────────────
_PLAT_HINTS = {"tiktok": "tiktok", "тикток": "tiktok", "instagram": "instagram", "insta": "instagram", "инст": "instagram", "ig_": "instagram", "meta": "instagram"}
_METRIC_MAP = {   # заголовки выгрузок → наши имена метрик
    "video views": "views", "views": "views", "просмотры": "views", "просмотры видео": "views",
    "profile views": "profile_views", "просмотры профиля": "profile_views",
    "likes": "likes", "лайки": "likes", "нравится": "likes", "comments": "comments", "комментарии": "comments",
    "shares": "shares", "репосты": "shares", "поделились": "shares", "reach": "reach", "охват": "reach",
    "impressions": "impressions", "показы": "impressions", "followers": "followers", "подписчики": "followers",
    "net followers": "followers_net", "new followers": "followers_new", "новые подписчики": "followers_new",
    "accounts reached": "reach", "accounts engaged": "engaged", "interactions": "interactions", "взаимодействия": "interactions",
    "saves": "saves", "сохранения": "saves", "total followers": "followers",
}


def detect_platform(filename: str, caption: str = "") -> str | None:
    low = f"{filename} {caption}".lower()
    for k, v in _PLAT_HINTS.items():
        if k in low:
            return v
    return None


def _parse_day(s: str):
    s = str(s or "").strip().strip('"')
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%b %d, %Y", "%d %b %Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19] if "T" in s else s, fmt).date()
        except Exception:
            continue
    return None


def _num(s):
    t = str(s or "").strip().replace("\u00a0", "").replace(" ", "").replace(",", ".")
    t = t.rstrip("%")
    try:
        return float(t)
    except Exception:
        return None


def parse_metrics_csv(raw: str) -> tuple[list[dict], list[str]]:
    """Общий разбор выгрузки «дата + числовые колонки» (TikTok Studio, Meta Business Suite, YouTube Studio).
    Возвращает (rows=[{day, metric, value}], unknown_headers). Разделитель — , или ; или таб."""
    import csv, io
    text = raw.lstrip("\ufeff")
    sample = text[:2000]
    delim = ";" if sample.count(";") > sample.count(",") else ("\t" if sample.count("\t") > sample.count(",") else ",")
    rd = csv.reader(io.StringIO(text), delimiter=delim)
    lines = [r for r in rd if any(c.strip() for c in r)]
    if len(lines) < 2:
        return [], []
    # заголовок — первая строка, где есть ≥2 непустых текстовых ячейки и следующая строка содержит дату
    hdr_i = 0
    for i in range(min(5, len(lines) - 1)):
        if _parse_day(lines[i + 1][0]) is not None:
            hdr_i = i; break
    headers = [h.strip().strip('"').lower() for h in lines[hdr_i]]
    rows, unknown = [], []
    mapped = {}
    for j, h in enumerate(headers[1:], start=1):
        m = _METRIC_MAP.get(h)
        if not m:
            for k, v in _METRIC_MAP.items():
                if k in h:
                    m = v; break
        if m:
            mapped[j] = m
        elif h:
            unknown.append(h)
    for r in lines[hdr_i + 1:]:
        d = _parse_day(r[0]) if r else None
        if not d:
            continue
        for j, m in mapped.items():
            if j < len(r):
                v = _num(r[j])
                if v is not None:
                    rows.append({"day": d.isoformat(), "metric": m, "value": v})
    return rows, unknown


async def import_csv(db, platform: str, raw: str, source_name: str = "") -> dict:
    """Записать метрики выгрузки в social_stats (по дню; повтор дня перезаписывается)."""
    rows, unknown = parse_metrics_csv(raw)
    if not rows:
        return {"rows": 0, "unknown": unknown}
    await ensure_tables(db)
    days = {r["day"] for r in rows}
    await db.execute(f"DELETE FROM social_stats WHERE platform=? AND entity='channel' AND day IN ({','.join('?' * len(days))})", (platform, *sorted(days)))
    for r in rows:
        await _put(db, platform, "channel", r["metric"], r["value"], day=r["day"])
    await _item(db, platform, "channel", source_name or platform, "", "", "channel")
    await db.commit()
    return {"rows": len(rows), "days": len(days), "from": min(days), "to": max(days),
            "metrics": sorted({r["metric"] for r in rows}), "unknown": unknown}


async def csv_summary(db, platform: str, days: int = 7) -> str:
    """Сводка по метрикам из выгрузок: сумма за период (потоковые метрики) / последнее значение (запасы)."""
    await ensure_tables(db)
    since = (date.today() - timedelta(days=days)).isoformat()
    async with db.execute("SELECT metric, SUM(value), MAX(day), COUNT(*) FROM social_stats WHERE platform=? AND entity='channel' AND day>=? GROUP BY metric", (platform, since)) as cur:
        rows = await cur.fetchall()
    if not rows:
        async with db.execute("SELECT MAX(day) FROM social_stats WHERE platform=?", (platform,)) as cur:
            last = (await cur.fetchone())[0]
        return f"{platform}: данных за {days} дн. нет" + (f" (последняя выгрузка {last})" if last else " — пришли CSV-выгрузку Мире")
    stock = {"followers", "profile_followers"}
    parts = []
    for metric, total, last_day, n in rows:
        if metric in stock:
            v, _ = await _latest(db, platform, "channel", metric)
            v0, _ = await _latest(db, platform, "channel", metric, since)
            parts.append(f"{metric} {_fmt(v)}{_delta(v, v0)}")
        else:
            parts.append(f"{metric} {_fmt(total)}")
    label = {"tiktok": "TikTok", "instagram": "Instagram"}.get(platform, platform)
    return f"{label} за {days} дн. (по выгрузке до {max(r[2] for r in rows)}): " + ", ".join(parts)
