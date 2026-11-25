"""
v4.73 Скиллы: сохранённые инструкции для Миры.

Скилл — текст: что сделать, что спросить, в каком виде выдать. Встроенные лежат в CATALOG,
свои владелец создаёт фразой в чате. По просьбе Мира читает инструкцию инструментом и следует ей,
по расписанию скилл идёт как поручение routines с текстом «skill:{id}», то есть только с чтением.
kind=system: уже работает в боте сам, в каталоге показан для полноты.
"""
import json
import re
import time

from core import connectors

SYSTEM = [
    {"id": "digest", "emoji": "📰", "name": "Утренний дайджест", "cat": "Продуктивность", "summary": "Задачи, пары, встречи и списания в 09:00.", "needs": []},
    {"id": "unanswered", "emoji": "💬", "name": "Кому не ответил", "cat": "Люди", "summary": "Кто ждёт ответа и кто молчит на твой вопрос.", "needs": ["telegram"]},
    {"id": "evening", "emoji": "🌙", "name": "Вечерний итог", "cat": "Продуктивность", "summary": "Что сделано и что на завтра.", "needs": []},
]
CATALOG = [
    {"id": "meeting", "emoji": "🤝", "name": "Подготовка к встрече", "cat": "Люди", "summary": "Обещания и договорённости на три строки.", "needs": ["telegram"],
     "prompt": "Подготовь владельца к встрече. Определи человека: из его просьбы или из ближайшей встречи сегодня. Прочитай полную память и поищи переписку за последний месяц. Выдай три строки: что обещал владелец, что обещали ему, что важно помнить в разговоре. Без вступления."},
    {"id": "booking", "emoji": "📌", "name": "Запись на встречу", "cat": "Люди", "summary": "Окна из календаря и черновик человеку.", "needs": ["telegram", "calendar"],
     "prompt": "Помоги назначить встречу. Определи человека и длительность из просьбы, если длительности нет, считай час. Инструментом find_slots найди три свободных окна на ближайшие дни. Инструментом draft_message подготовь человеку короткое сообщение с этими окнами: отправляет владелец своей кнопкой, сама никому не пиши. Когда владелец скажет, какое окно подошло, поставь задачу на это время через create_task. Запись работает только через Telegram, Instagram не подключён."},
    {"id": "promises", "emoji": "🧾", "name": "Мои обещания", "cat": "Люди", "summary": "Что ты обещал и кому, старые сверху.", "needs": ["telegram"],
     "prompt": "Собери открытые обещания владельца из картины мира. Сгруппируй по людям, старые и горящие сверху. Рядом с каждым укажи, сколько дней оно висит, если это видно. В конце назови одно обещание, с которого лучше начать."},
    {"id": "content_report", "emoji": "📈", "name": "Отчёт по контенту", "cat": "Контент", "summary": "Неделя на YouTube, Twitch и в Telegram.", "needs": ["youtube"],
     "prompt": "Сделай отчёт по контенту за неделю: статистика YouTube, Twitch и Telegram. Сравни с прошлой неделей, назови лучший и худший материал и почему он, по твоему мнению, сработал. Закончи одним советом на следующую неделю. Только цифры из инструментов."},
    {"id": "content_plan", "emoji": "🗓️", "name": "Контент-план", "cat": "Контент", "summary": "Идеи на две недели с контент-доски и радара.", "needs": ["vault", "reels"],
     "prompt": "Составь контент-план на две недели. Прочитай контент-доску и тренды Reels Radar, учти календарь и статистику. Не спрашивай про платформы и тематику: они есть в памяти владельца. Для каждого дня одна строка: дата, площадка, формат, тема, хук. Не больше трёх публикаций в неделю на площадку, если владелец не просил иначе."},
    {"id": "trends", "emoji": "📡", "name": "Тренды недели", "cat": "Контент", "summary": "Что заходит у других и что снять тебе.", "needs": ["reels"],
     "prompt": "Посмотри тренды Reels Radar и подборку владельца. Выбери три рилса, которые подходят его каналу, и для каждого напиши, что в нём цепляет и как повторить это по-своему. Ссылки обязательны."},
    {"id": "youtube_ideas", "emoji": "📺", "name": "Идеи для YouTube", "cat": "Контент", "summary": "Темы и заголовки под твой канал.", "needs": ["youtube"],
     "prompt": "Предложи идеи роликов для YouTube-канала владельца. Посмотри статистику YouTube и Twitch: что у него заходит лучше всего. Затем прочитай контент-доску и тренды Reels Radar, чтобы не повторять уже снятое. Дай пять идей, в каждой: рабочий заголовок, зацепка первых 30 секунд и почему это зайдёт именно у него, со ссылкой на его цифры. Цифры только из инструментов, если данных нет, так и скажи. В конце назови одну идею, с которой лучше начать."},
    {"id": "reel_adapt", "emoji": "🧐", "name": "Разбор рилса", "cat": "Контент", "summary": "Почему ролик зашёл и как снять у себя.", "needs": ["reels"],
     "prompt": "Разбери чужой рилс и адаптируй под владельца. Если ссылки нет, попроси её. Вызови инструмент reels с action=analyse и этой ссылкой. Коротко передай: чем цепляет первая секунда, как устроен ролик по секундам, куда ведёт зрителя. Затем дай три варианта, как снять похожее на его канале: что меняем под его тему, первая фраза, что показать в кадре. Чужой текст дословно не копируй. Причины успеха называй гипотезами. Обязательно дай ссылку на разбор с плеером из ответа инструмента."},
    {"id": "post", "emoji": "✏️", "name": "Пост для канала", "cat": "Контент", "summary": "Черновик поста в твоём стиле.", "needs": ["telegram"],
     "prompt": "Напиши черновик поста для Telegram-канала владельца на тему из его просьбы. Коротко, разговорно, без канцелярита и списков, один хук в первой строке. Публиковать нельзя: покажи текст и спроси, что поправить."},
    {"id": "ai_prompt", "emoji": "🪄", "name": "Промт для нейронки", "cat": "Контент", "summary": "Промт для картинки, видео, музыки, стикеров.", "needs": [],
     "prompt": "Владелец хочет получить что-то в нейросети. Сама ничего не генерируй: напиши готовый промт, который он вставит в нужный сервис. Определи тип (картинка, видео из фото, музыка, стикерпак, липсинк, персонаж, текст) и подходящий инструмент, если он не назван. Если не хватает одной важной детали, спроси её. Промт пиши на английском, если сервис так работает лучше: сюжет, стиль, свет, камера, формат кадра, чего избегать. Дай один основной вариант отдельным сообщением без лишних слов и один запасной, коротко поясни на русском."},
    {"id": "tribute_visual", "emoji": "📸", "name": "Визуал для поста", "cat": "Контент", "summary": "Идея картинки или видео и промт к посту.", "needs": [],
     "prompt": "Придумай визуал к посту из просьбы: одну идею, которая остановит ленту, и готовый промт для нейросети. Сама ничего не генерируй. Формат кадра подбери под площадку."},
    {"id": "mail_triage", "emoji": "📧", "name": "Разбор почты", "cat": "Продуктивность", "summary": "Что важно и на что ответить.", "needs": ["mail"],
     "prompt": "Прочитай последние письма. Отбрось рекламу и рассылки. Раздели остальное на три группы: нужно ответить, нужно оплатить или сделать, просто знать. Для каждого письма одна строка. Если важного нет, так и скажи."},
    {"id": "github_week", "emoji": "🐙", "name": "Неделя в GitHub", "cat": "Разработка", "summary": "Что изменилось в репозиториях.", "needs": ["github"],
     "prompt": "Посмотри репозитории владельца и что в них изменилось за неделю. Коротко по каждому активному: главные изменения, открытые pull request и issues. Отдельно назови то, что давно ждёт его внимания."},
    {"id": "rates", "emoji": "💱", "name": "Курс доллара и крипта", "cat": "Финансы", "summary": "Доллар, BTC и TON одним сообщением.", "needs": ["web", "crypto"],
     "prompt": "Возьми цены BTC и TON инструментом market, а курс доллара к рублю найди в интернете. Выдай по строке с числом и изменением за сутки. Только факты, никаких советов купить или продать."},
    {"id": "crypto_watch", "emoji": "📊", "name": "Рынок криптовалют", "cat": "Финансы", "summary": "Что с ценами и что случилось на рынке.", "needs": ["crypto", "web"],
     "prompt": "Возьми цены BTC, ETH, TON и SOL инструментом market, найди в интернете главное событие рынка за сутки. Коротко: цены, изменение, что произошло. Только факты, без советов и прогнозов."},
    {"id": "finance_news", "emoji": "📰", "name": "Новости финансов", "cat": "Финансы", "summary": "Главное на рынках за сутки.", "needs": ["web"],
     "prompt": "Найди главные финансовые новости за сутки: рубль, ставка ЦБ, крупные рынки, крипта. До пяти пунктов со ссылками. Только факты, без советов и прогнозов."},
    {"id": "market_research", "emoji": "🔬", "name": "Исследовать рынок", "cat": "Работа", "summary": "Короткий ресёрч темы со ссылками.", "needs": ["web"],
     "prompt": "Проведи короткий ресёрч темы из просьбы: кто игроки, сколько стоит, что люди хотят, где пустое место. Отдели проверенные факты от своих выводов, у фактов ставь ссылки. Не больше трёх поисков."},
    {"id": "fashion_trend", "emoji": "👟", "name": "Модный тренд дня", "cat": "На каждый день", "summary": "Что носят сейчас, коротко.", "needs": ["web"],
     "prompt": "Найди один актуальный тренд мужской моды и уличного стиля. Коротко: что это, где взять идею, как примерить на его стиль из памяти. Со ссылкой."},
    {"id": "news", "emoji": "🗞️", "name": "Новости по интересам", "cat": "Знания", "summary": "Главное в ИИ, стриминге и iOS.", "needs": ["web"],
     "prompt": "Найди в интернете главные новости за сутки по интересам владельца из памяти: ИИ, стриминг, разработка под iOS. Выбери не больше пяти, для каждой одна строка и ссылка."},
]
_BUILTIN = {s["id"]: s for s in CATALOG}
MAX_CUSTOM = 30


async def _ensure(db) -> None:
    await db.execute("""CREATE TABLE IF NOT EXISTS skills (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        emoji TEXT NOT NULL DEFAULT '✨',
        cat TEXT NOT NULL DEFAULT 'Свои',
        summary TEXT NOT NULL DEFAULT '',
        prompt TEXT NOT NULL DEFAULT '',
        needs TEXT NOT NULL DEFAULT '[]',
        custom INTEGER NOT NULL DEFAULT 0,
        installed INTEGER NOT NULL DEFAULT 1,
        routine_id INTEGER,
        last_run INTEGER)""")
    await db.commit()


def _slug(name: str) -> str:
    return "my_" + (re.sub(r"\W+", "_", name.lower()).strip("_")[:30] or str(int(time.time())))


def _find(sid: str, rows: dict) -> str:
    """Мира часто называет скилл по имени, а не по id."""
    key = (sid or "").strip().lower()
    for item in [*CATALOG, *rows.values()]:
        if key in (item["id"].lower(), item["name"].lower()):
            return item["id"]
    return sid


async def _rows(db) -> dict:
    await _ensure(db)
    async with db.execute("SELECT id, name, emoji, cat, summary, prompt, needs, custom, installed, routine_id, last_run FROM skills") as cur:
        keys = ("id", "name", "emoji", "cat", "summary", "prompt", "needs", "custom", "installed", "routine_id", "last_run")
        return {r[0]: dict(zip(keys, r)) for r in await cur.fetchall()}


async def _when(db, routine_id) -> str:
    if not routine_id:
        return ""
    from core import routines
    async with db.execute("SELECT schedule FROM routines WHERE id=? AND enabled=1", (routine_id,)) as cur:
        r = await cur.fetchone()
    return routines.describe(json.loads(r[0])) if r else ""


async def catalog(db, settings) -> list:
    """Для мини-приложения и инструмента: всё, что можно поставить, со статусом."""
    rows = await _rows(db)
    out = [{**s, "kind": "system", "installed": True, "missing": [], "when": "", "prompt": ""} for s in SYSTEM]
    custom = [{**r, "needs": json.loads(r["needs"])} for r in rows.values() if r["custom"]]
    for s in [*CATALOG, *custom]:
        row = rows.get(s["id"]) or {}
        missing = [n for n in s["needs"] if not await connectors.enabled(db, settings, n)]
        out.append({"id": s["id"], "emoji": s["emoji"], "name": s["name"], "cat": s["cat"], "summary": s["summary"],
                    "needs": s["needs"], "prompt": s["prompt"], "kind": "custom" if s.get("custom") else "builtin",
                    "installed": bool(row.get("installed")), "missing": missing,
                    "when": await _when(db, row.get("routine_id")), "last_run": row.get("last_run")})
    return out


async def installed_line(db) -> str:
    """Строка для картины мира, чтобы Мира узнавала просьбу и звала skill use."""
    rows = await _rows(db)
    installed = [f"{r['name']} [{r['id']}]" for r in rows.values() if r["installed"]]
    free = [f"{s['name']} [{s['id']}]" for s in CATALOG if not rows.get(s["id"], {}).get("installed")]
    return f"установлены: {', '.join(installed) or 'нет'}; можно поставить: {', '.join(free)}"


async def prompt_of(db, sid: str) -> str:
    rows = await _rows(db)
    sid = _find(sid, rows)
    row = rows.get(sid)
    if row and row["prompt"]:
        return row["prompt"]
    return (_BUILTIN.get(sid) or {}).get("prompt", "")


async def is_installed(db, sid: str) -> bool:
    return bool((await _rows(db)).get(sid, {}).get("installed"))


async def mark_run(db, sid: str) -> None:
    await db.execute("UPDATE skills SET last_run=? WHERE id=?", (int(time.time()), sid))
    await db.commit()


async def _drop_routine(db, routine_id) -> None:
    if routine_id:
        from core import routines
        await routines.remove(db, routine_id)


async def install(db, settings, sid: str, schedule: dict | None = None) -> str:
    """Поставить или обновить расписание. schedule — поля every/time/days… как у routines."""
    from core import routines
    rows = await _rows(db)
    sid = _find(sid, rows)
    base = _BUILTIN.get(sid) or rows.get(sid)
    if not base or not base.get("prompt"):
        return f"скилла {sid} нет"
    needs = base["needs"] if isinstance(base["needs"], list) else json.loads(base["needs"])
    missing = [n for n in needs if not await connectors.enabled(db, settings, n)]
    if missing:
        return f"сначала нужно подключить: {', '.join(missing)}"
    row = rows.get(sid) or {}
    await _drop_routine(db, row.get("routine_id"))
    routine_id, note = None, ""
    if schedule and schedule.get("every"):
        routine_id, note = await routines.add_row(db, {**schedule, "kind": "order", "text": f"skill:{sid}"})
    await db.execute(
        "INSERT INTO skills (id, name, emoji, cat, summary, needs, custom, installed, routine_id) VALUES (?,?,?,?,?,?,0,1,?) "
        "ON CONFLICT(id) DO UPDATE SET installed=1, routine_id=excluded.routine_id",
        (sid, base["name"], base["emoji"], base["cat"], base["summary"], json.dumps(needs), routine_id))
    await db.commit()
    return f"поставила {base['name']}" + (f": {note}" if note else "")


async def remove(db, sid: str) -> str:
    rows = await _rows(db)
    sid = _find(sid, rows)
    row = rows.get(sid)
    if not row:
        return f"скилл {sid} не стоит"
    await _drop_routine(db, row["routine_id"])
    if row["custom"]:
        await db.execute("DELETE FROM skills WHERE id=?", (sid,))
    else:
        await db.execute("UPDATE skills SET installed=0, routine_id=NULL WHERE id=?", (sid,))
    await db.commit()
    return f"убрала {row['name']}"


async def create(db, settings, args: dict) -> str:
    """Свой скилл из разговора: name, prompt, summary, emoji, needs, расписание."""
    name, prompt = (args.get("name") or "").strip()[:60], (args.get("prompt") or "").strip()[:2000]
    if not name or not prompt:
        raise ValueError("не хватает названия или инструкции")
    rows = await _rows(db)
    if sum(1 for r in rows.values() if r["custom"]) >= MAX_CUSTOM:
        raise ValueError(f"уже {MAX_CUSTOM} своих скиллов, сначала убери лишние")
    needs = [n for n in (args.get("needs") or []) if n in {c["id"] for c in connectors.CONNECTORS}]
    sid = _slug(name)
    await db.execute(
        "INSERT OR REPLACE INTO skills (id, name, emoji, cat, summary, prompt, needs, custom, installed) VALUES (?,?,?,?,?,?,?,1,1)",
        (sid, name, (args.get("emoji") or "✨")[:8], "Свои", (args.get("summary") or "").strip()[:120], prompt, json.dumps(needs)))
    await db.commit()
    return await install(db, settings, sid, args if args.get("every") else None)
