"""
v4.78 Живая контент-доска: 04-content/Контент-доска.md двигается сама.

Twitch закончился → Мира спрашивает, какая это была карточка → «🔴 Отстримлено».
Через CLIPS_AFTER_DAYS дней спрашивает про клипы → «✅ Опубликовано».
Формат карточек и дат тот же, что у кнопок @vaultsn38bot, файл один, запись через GitHub Contents API.
Ключ карточки — хеш её названия без служебных дат, поэтому работают и карточки без файла сценария.
"""
import base64
import hashlib
import json
import re
import time
from datetime import date
from urllib.parse import quote

import aiohttp
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger

BOARD_PATH = "04-content/Контент-доска.md"
LANES = {"w": "## 🎯 Стрим недели", "n": "## ✍️ Сценарий", "s": "## 🔴 Отстримлено", "p": "## ✅ Опубликовано"}
MARK = {"s": "🔴", "p": "✅"}
ENDED_AFTER = 15 * 60       # снимков зрителей нет 15 минут — стрим кончился
LOOK_BACK = 6 * 3600
CLIPS_AFTER_DAYS = 2
MAX_CLIP_ASKS = 3
EVERY_MIN = 5
ASKED = "board:asked"       # Мира спросила про карточку: владелец может ответить ей словами
ASKED_FOR = 12 * 3600
_TIMEOUT = aiohttp.ClientTimeout(total=25)
_DATES = re.compile(r" · (📅|🔴|✅) \d{4}-\d{2}-\d{2}")


def card_title(line: str) -> str:
    text = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", line[6:])
    text = re.sub(r"\[\[[^\]]*\]\]", "", text)
    return _DATES.sub("", text).split(" · ")[0].strip()


def card_key(line: str) -> str:
    return hashlib.sha1(card_title(line).encode()).hexdigest()[:8]


def lane_cards(text: str, lane: str) -> list:
    out, inside = [], False
    for row in text.split("\n"):
        if row.startswith("## "):
            inside = row.startswith(LANES[lane])
        elif inside and row.startswith("- ["):
            out.append(row)
    return out


def move(text: str, key: str, lane: str, today: date):
    """Перенести карточку. → (новый текст, название) или (None, ""), если карточки нет. Чистая функция."""
    lines = text.split("\n")
    index = next((i for i, row in enumerate(lines) if row.startswith("- [") and card_key(row) == key), None)
    target = next((i for i, row in enumerate(lines) if row.startswith(LANES[lane])), None)
    if index is None or target is None:
        return None, ""
    card = _DATES.sub("", lines.pop(index))
    title = card_title(card)
    if index < target:
        target -= 1
    if lane in MARK:
        card += f" · {MARK[lane]} {today.isoformat()}"
    at = target + 2 if target + 1 < len(lines) and not lines[target + 1].strip() else target + 1
    lines.insert(at, card)
    return "\n".join(lines), title


def _words(text: str) -> set:
    return set(re.findall(r"\w{3,}", text.lower()))


def find(text: str, name: str):
    """Карточка по словам владельца: совпала хотя бы половина слов → ключ лучшей, иначе None."""
    asked = _words(name)
    best = (0.0, None)
    for row in text.split("\n"):
        if row.startswith("- [") and asked:
            score = len(asked & _words(card_title(row))) / len(asked)
            if score >= 0.5 and score > best[0]:
                best = (score, card_key(row))
    return best[1]


def add(text: str, title: str, lane: str, today: date):
    """Новая карточка в начало колонки. → (новый текст, название) или (None, ""). Чистая функция."""
    lines = text.split("\n")
    target = next((i for i, row in enumerate(lines) if row.startswith(LANES[lane])), None)
    title = " ".join(title.replace(" · ", " ").split())[:120]
    if target is None or not title:
        return None, ""
    card = f"- [ ] {title}" + (f" · {MARK[lane]} {today.isoformat()}" if lane in MARK else "")
    at = target + 2 if target + 1 < len(lines) and not lines[target + 1].strip() else target + 1
    lines.insert(at, card)
    return "\n".join(lines), card_title(card)


def _headers(settings) -> dict:
    return {"Authorization": f"Bearer {settings.github_token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "mira-bot"}


def _url(settings) -> str:
    return f"https://api.github.com/repos/{settings.vault_repo}/contents/{quote(BOARD_PATH)}"


async def read_board(session, settings) -> tuple:
    async with session.get(_url(settings), headers=_headers(settings)) as r:
        r.raise_for_status()
        data = await r.json(content_type=None)
    return data["sha"], base64.b64decode(data["content"]).decode()


async def _write(settings, lane: str, change) -> str:
    """Правка доски с записью в репозиторий; 409 — файл изменился рядом, повторяем один раз. → название или ''."""
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        for attempt in range(2):
            sha, text = await read_board(session, settings)
            new, title = change(text)
            if new is None:
                return ""
            body = {"message": f"board: {LANES[lane][3:]} ← {title[:60]}", "sha": sha,
                    "content": base64.b64encode(new.encode()).decode()}
            async with session.put(_url(settings), headers=_headers(settings), json=body) as r:
                if r.status == 409 and not attempt:
                    continue
                r.raise_for_status()
                return title
    return ""


async def move_card(settings, key: str, lane: str, today: date) -> str:
    return await _write(settings, lane, lambda text: move(text, key, lane, today))


async def mark(db, settings, name: str, lane: str, today: date, now_ts: float) -> str:
    """Мира: владелец назвал карточку словами. «s» — в Отстримлено, а нет на доске — новая карточка; «p» — в Опубликовано."""
    from db import set_setting

    def change(text):
        key = find(text, name)
        if key:
            return move(text, key, lane, today)
        return add(text, name, lane, today) if lane == "s" else (None, "")

    title = await _write(settings, lane, change)
    if not title:
        return "такой карточки на доске нет"
    key = hashlib.sha1(title.encode()).hexdigest()[:8]
    if lane == "s":
        await plan_clips(db, key, title, now_ts)
        await set_setting(db, ASKED, "")
    else:
        await set_setting(db, f"boardclips:{key}", "")
    return f"на доске: «{title}» → {LANES[lane][3:]}"


async def asked(db, now_ts: float) -> str:
    """Название стрима, про который Мира недавно спросила и ответа ещё нет."""
    from db import get_setting
    raw = await get_setting(db, ASKED) or ""
    data = json.loads(raw) if raw else {}
    return data.get("title", "") if now_ts - data.get("ts", 0) < ASKED_FOR else ""


async def _ended_stream(db, now_ts: float):
    """Последний закончившийся стрим: (entity, название) или None."""
    async with db.execute("SELECT entity, MAX(ts) FROM social_stats WHERE platform='twitch' AND metric='viewers' "
                          "AND entity LIKE 'stream:%' AND ts>? GROUP BY entity ORDER BY MAX(ts) DESC LIMIT 1", (now_ts - LOOK_BACK,)) as cur:
        row = await cur.fetchone()
    if not row or now_ts - row[1] < ENDED_AFTER:
        return None
    async with db.execute("SELECT title FROM social_items WHERE platform='twitch' AND item_id=?", (row[0],)) as cur:
        item = await cur.fetchone()
    return row[0], (item[0] if item else "") or "без названия"


async def _ask_stream(bot, db, settings, now_ts: float) -> None:
    from core.topics import send_owner
    from db import get_digest_state, set_digest_state
    ended = await _ended_stream(db, now_ts)
    if not ended or await get_digest_state(db, f"boardask:{ended[0]}"):
        return
    await set_digest_state(db, f"boardask:{ended[0]}", "sent")
    kb = await board_keyboard(db, settings, ended[1], now_ts)
    await send_owner(bot, settings.owner_user_id, "digest", f"🎬 Стрим закончился: {ended[1][:120]}\nКакая это была карточка?",
                     reply_markup=kb, parse_mode=None)


async def board_keyboard(db, settings, title: str, now_ts: float) -> InlineKeyboardMarkup:
    """Кнопки «какая это была карточка» + запомнить, что спросили (Мира поймёт ответ словами)."""
    from db import set_setting
    await set_setting(db, ASKED, json.dumps({"title": title, "ts": now_ts}, ensure_ascii=False))
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        _, text = await read_board(session, settings)
    cards = (lane_cards(text, "w") + lane_cards(text, "n"))[:4]
    rows = [[InlineKeyboardButton(text=card_title(c)[:50], callback_data=f"cbs:{card_key(c)}")] for c in cards]
    rows.append([InlineKeyboardButton(text="Другое, не с доски", callback_data="cbs:none")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def plan_clips(db, key: str, title: str, now_ts: float) -> None:
    from db import set_setting
    await set_setting(db, f"boardclips:{key}", json.dumps({"title": title, "at": now_ts + CLIPS_AFTER_DAYS * 86400, "asks": 0}, ensure_ascii=False))


async def _ask_clips(bot, db, settings, now_ts: float) -> None:
    from core.topics import send_owner
    from db import set_setting
    async with db.execute("SELECT key, value FROM settings_kv WHERE key LIKE 'boardclips:%' AND value != ''") as cur:
        rows = await cur.fetchall()
    for name, raw in rows:
        plan = json.loads(raw)
        if plan["at"] > now_ts:
            continue
        key = name.split(":", 1)[1]
        if plan["asks"] >= MAX_CLIP_ASKS:
            await set_setting(db, name, "")
            continue
        plan.update(at=now_ts + CLIPS_AFTER_DAYS * 86400, asks=plan["asks"] + 1)
        await set_setting(db, name, json.dumps(plan, ensure_ascii=False))
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Вышли", callback_data=f"cbp:{key}"),
                                                    InlineKeyboardButton(text="Ещё нет", callback_data="cbp:later")]])
        await send_owner(bot, settings.owner_user_id, "digest", f"✂️ Клипы со стрима вышли? {plan['title'][:120]}", reply_markup=kb, parse_mode=None)


async def tick(bot, db, settings, now) -> None:
    if now.minute % EVERY_MIN or not (settings.github_token and settings.vault_repo):
        return
    ts = now.timestamp()
    from core import autonomy
    try:
        if not await autonomy.enabled(db, "streamreport"):   # v4.99: иначе вопрос едет в итоге стрима
            await _ask_stream(bot, db, settings, ts)
    except Exception as exc:
        logger.warning(f"board ask failed: {type(exc).__name__}: {exc}")
    await _ask_clips(bot, db, settings, ts)
