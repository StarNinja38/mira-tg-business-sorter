import aiosqlite
from core.redact import mask_cards as _mask_cards   # v4.106: номера карт не храним
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from loguru import logger


def _utc_cutoff(kind: str = "day") -> str:
    """Start of the current LOCAL day/month expressed as a UTC 'YYYY-MM-DD HH:MM:SS'
    string matching SQLite CURRENT_TIMESTAMP. Fixes ±TZ skew in 'today'/'month' windows
    (created_at is stored UTC; comparing to a local date string was off by the TZ offset)."""
    from zoneinfo import ZoneInfo
    from config import settings as _s
    now_local = datetime.now(ZoneInfo(_s.timezone))
    start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "month":
        start = start.replace(day=1)
    return start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ── LLM pricing ($/MTok input, $/MTok output) ─────────────────────────────────
# Семейства матчатся по подстроке имени модели. Неизвестная → как Sonnet (консервативно).
_PRICING = {
    # v4.61: поколение Claude 5 — раньше семейств (матч по первой подстроке в порядке словаря)
    "haiku-5": (0.10, 0.50),     # v4.63: Haiku 5.5 — цена для промптов до 100k токенов (у нас все такие)
    "sonnet-5": (2.00, 10.00),   # Sonnet 5 / 5.5
    "opus-5-5": (4.00, 20.00),
    "opus-5": (5.00, 25.00),
    "haiku": (1.00, 5.00),   # v4.36: Haiku 4.5 = 1.00/5.00 (было занижено на 20%)
    "sonnet": (3.00, 15.00),
    "opus": (15.00, 75.00),
}


# v4.63: чтение из кэша — доля от цены input. По умолчанию 0.1; у Sonnet 5.5 и Opus 5.5 — 0.05 (прайс 08.10.2026)
_CACHE_READ_MULT = {"sonnet-5-5": 0.05, "opus-5-5": 0.05}


def _cache_read_mult(model: str) -> float:
    m = (model or "").lower()
    return next((v for k, v in _CACHE_READ_MULT.items() if k in m), 0.1)


def _model_family(model: str) -> str:
    m = (model or "").lower()
    for fam in _PRICING:
        if fam in m:
            return fam
    return "sonnet"


def model_cost(model: str, inp: int, out: int, cache_write: int = 0, cache_read: int = 0) -> float:
    """USD за вызов модели по её тарифу (по семейству имени).
    Prompt caching: запись = 2× input-тарифа (наш единственный кэш — extract с ttl=1h;
    у 5-мин было бы 1.25×), чтение = 0.1×."""
    ri, ro = _PRICING[_model_family(model)]
    return ((inp or 0) * ri + (out or 0) * ro
            + (cache_write or 0) * ri * 2.0 + (cache_read or 0) * ri * _cache_read_mult(model)) / 1_000_000


async def init_db(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        # WAL mode — concurrent reads + writes, fewer "database is locked"
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS whitelist (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                reason TEXT
            );

            CREATE TABLE IF NOT EXISTS message_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                message_text TEXT,
                category TEXT,
                rule_matched TEXT,
                action_taken TEXT,
                llm_latency_ms INTEGER,
                llm_tokens_input INTEGER,
                llm_tokens_output INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS conversation_state (
                user_id INTEGER PRIMARY KEY,
                message_count INTEGER DEFAULT 0,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS settings_kv (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                direction TEXT NOT NULL,  -- 'in' | 'out'
                text TEXT,
                media_type TEXT,          -- 'photo' | 'voice' | 'document' | NULL
                category TEXT,            -- BUSINESS | UNCLEAR | FRIEND | SPAM | NULL
                followup_done INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS notification_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                type TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS digest_state (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS saved_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                url TEXT,
                context TEXT,
                category TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                text TEXT,
                correct_category TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS draft_style (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                incoming TEXT,
                reply TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS task_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                verdict TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS command_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                command TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS processed_updates (
                key TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS processed_media (
                file_unique_id TEXT PRIMARY KEY,
                user_id INTEGER,
                media_type TEXT,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS message_vectors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                text TEXT,
                vector BLOB,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS pending_analysis (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                category TEXT,
                last_activity TIMESTAMP,
                dirty INTEGER DEFAULT 1
            );

            CREATE INDEX IF NOT EXISTS idx_links_created ON saved_links(created_at);

            CREATE TABLE IF NOT EXISTS contact_profiles (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                display_name TEXT,
                relationship TEXT DEFAULT 'unknown',
                topics TEXT DEFAULT '[]',
                intent TEXT DEFAULT '',
                summary TEXT DEFAULT '',
                name TEXT,
                message_count INTEGER DEFAULT 0,
                first_contact TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_contact TIMESTAMP,
                notion_contact_id TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_notif_user ON notification_log(user_id, type, created_at);

            CREATE TABLE IF NOT EXISTS task_sync (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                todoist_task_id TEXT UNIQUE,
                notion_page_id TEXT,
                user_id INTEGER DEFAULT 0,
                task_title TEXT DEFAULT '',
                synced INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS llm_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model TEXT,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cache_creation_tokens INTEGER DEFAULT 0,
                cache_read_tokens INTEGER DEFAULT 0,
                kind TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_log_created ON message_log(created_at);
            CREATE INDEX IF NOT EXISTS idx_log_category ON message_log(category);
            CREATE INDEX IF NOT EXISTS idx_task_notion ON task_sync(notion_page_id);
            CREATE INDEX IF NOT EXISTS idx_task_user ON task_sync(user_id, synced);
            CREATE INDEX IF NOT EXISTS idx_usage_created ON llm_usage(created_at);
        """)
        # Migrations for existing DBs — add columns if missing
        for table, col, decl in [
            ("conversations", "category", "TEXT"),
            ("conversations", "followup_done", "INTEGER DEFAULT 0"),
            ("task_sync", "closed_at", "TIMESTAMP"),
            ("task_sync", "topic", "TEXT DEFAULT ''"),          # task linking by theme
            ("task_sync", "notion_status", "TEXT DEFAULT 'Новая'"),  # local guard: текущий Статус в Notion (Новая/В работе/Готово)
            ("task_sync", "calendar_event_id", "TEXT"),              # id события Google Calendar (чтобы обновлять/удалять)
            ("task_sync", "due_date", "TEXT"),                       # дедлайн (локально, для пультов/дайджеста); NULL если не задан
            ("task_sync", "is_commitment", "INTEGER DEFAULT 0"),     # задача = обещание владельца человеку → «Ты обещал» (живой, закрываемый)
            ("llm_usage", "cache_creation_tokens", "INTEGER DEFAULT 0"),  # prompt caching: запись в кэш (×1.25 input-тарифа)
            ("llm_usage", "cache_read_tokens", "INTEGER DEFAULT 0"),      # prompt caching: чтение из кэша (×0.1 input-тарифа)
            ("contact_profiles", "commitments", "TEXT DEFAULT ''"),  # memory ledger: open promises
            ("contact_profiles", "facts", "TEXT DEFAULT ''"),        # memory ledger: stable facts
            ("contact_profiles", "src_account", "INTEGER"),          # на какой аккаунт-владелец писал контакт (мультиаккаунт v3.4)
            ("contact_profiles", "photo_file_id", "TEXT"),           # фото в памяти: Telegram file_id (аватар/подтверждённое фото)
            ("contact_profiles", "photo_asked_at", "TIMESTAMP"),     # когда последний раз спрашивали про фото (троттл)
            ("contact_profiles", "photo_unique_id", "TEXT"),         # file_unique_id текущего аватара (для детекта смены)
            ("contact_profiles", "photo_src", "TEXT"),               # источник фото: avatar|manual|confirmed (refresh трогает только avatar)
            ("contact_profiles", "contact_type", "TEXT"),            # ТИП — широкая эмёрджентная группа (бот сам, ≥2 чел.), отдельно от Роли (relationship=узкая роль)
            ("contact_profiles", "facts_json", "TEXT"),              # структурная память v3.23: {sections{8}, timeline[], style} — facts = плоское зеркало
            ("task_sync", "reschedule_count", "INTEGER DEFAULT 0"),  # v4.23 «→ завтра»: счётчик переносов (на проде добавлен вручную; здесь — для свежих БД)
        ]:
            cols = [r[1] for r in await (await db.execute(f"PRAGMA table_info({table})")).fetchall()]
            if col not in cols:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        # Dedupe saved_links then enforce a UNIQUE (user_id, url) — atomic dedup, no race
        try:
            await db.execute(
                "DELETE FROM saved_links WHERE id NOT IN "
                "(SELECT MIN(id) FROM saved_links GROUP BY user_id, url)"
            )
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_links_unique ON saved_links(user_id, url)"
            )
        except Exception as exc:
            logger.warning(f"saved_links unique index migration skipped: {exc}")
        # task_sync: партиальный UNIQUE на notion_page_id (NULL допускает множество строк) —
        # backstop от дубля задачи при partial-создании (Todoist упал/таймаут, Notion есть).
        # На проде дублей нет (проверено). Оборачиваем — чтобы старт не падал, если где-то есть.
        try:
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_task_notion_uniq "
                "ON task_sync(notion_page_id) WHERE notion_page_id IS NOT NULL"
            )
        except Exception as exc:
            logger.warning(f"task_sync notion unique index migration skipped: {exc}")
        await db.commit()
    logger.info(f"Database initialized at {db_path}")


# ── whitelist ────────────────────────────────────────────────────────────────

async def is_whitelisted(db: aiosqlite.Connection, user_id: int) -> bool:
    async with db.execute(
        "SELECT 1 FROM whitelist WHERE user_id = ?", (user_id,)
    ) as cur:
        return await cur.fetchone() is not None


async def add_to_whitelist(
    db: aiosqlite.Connection,
    user_id: int,
    username: Optional[str],
    reason: str,
) -> None:
    await db.execute(
        "INSERT OR REPLACE INTO whitelist (user_id, username, reason) VALUES (?, ?, ?)",
        (user_id, username, reason),
    )
    await db.commit()


async def remove_from_whitelist(db: aiosqlite.Connection, user_id: int) -> bool:
    async with db.execute(
        "DELETE FROM whitelist WHERE user_id = ?", (user_id,)
    ) as cur:
        await db.commit()
        return cur.rowcount > 0


async def get_whitelist(db: aiosqlite.Connection) -> list[dict]:
    async with db.execute(
        "SELECT user_id, username, added_at, reason FROM whitelist ORDER BY added_at DESC LIMIT 50"
    ) as cur:
        rows = await cur.fetchall()
    return [
        {"user_id": r[0], "username": r[1], "added_at": r[2], "reason": r[3]}
        for r in rows
    ]


# ── conversation state ────────────────────────────────────────────────────────

async def get_message_count(db: aiosqlite.Connection, user_id: int) -> int:
    async with db.execute(
        "SELECT message_count FROM conversation_state WHERE user_id = ?", (user_id,)
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0


async def increment_message_count(db: aiosqlite.Connection, user_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """
        INSERT INTO conversation_state (user_id, message_count, last_seen)
        VALUES (?, 1, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            message_count = message_count + 1,
            last_seen = excluded.last_seen
        """,
        (user_id, now),
    )
    await db.commit()


# ── message log ───────────────────────────────────────────────────────────────

async def log_message(
    db: aiosqlite.Connection,
    user_id: int,
    username: Optional[str],
    message_text: Optional[str],
    category: str,
    rule_matched: Optional[str],
    action_taken: str,
    llm_latency_ms: Optional[int] = None,
    llm_tokens_input: Optional[int] = None,
    llm_tokens_output: Optional[int] = None,
) -> None:
    await db.execute(
        """
        INSERT INTO message_log
            (user_id, username, message_text, category, rule_matched, action_taken,
             llm_latency_ms, llm_tokens_input, llm_tokens_output)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, username, _mask_cards(message_text), category, rule_matched, action_taken,
         llm_latency_ms, llm_tokens_input, llm_tokens_output),
    )
    await db.commit()


async def get_recent_messages(db: aiosqlite.Connection, limit: int = 10) -> list[dict]:
    async with db.execute(
        """SELECT user_id, username, category, rule_matched, created_at
           FROM message_log ORDER BY created_at DESC LIMIT ?""",
        (limit,),
    ) as cur:
        rows = await cur.fetchall()
    return [
        {"user_id": r[0], "username": r[1], "category": r[2], "rule_matched": r[3], "created_at": r[4]}
        for r in rows
    ]


# ── stats ─────────────────────────────────────────────────────────────────────

async def _spend_since(db: aiosqlite.Connection, since_iso: str) -> float:
    """USD на Anthropic с момента since_iso = классификатор (Haiku, message_log) +
    все остальные вызовы (llm_usage) по тарифу своей модели. Единый источник правды."""
    async with db.execute(
        "SELECT SUM(llm_tokens_input), SUM(llm_tokens_output) FROM message_log WHERE created_at >= ?",
        (since_iso,),
    ) as cur:
        r = await cur.fetchone()
    total = model_cost("haiku", (r[0] or 0) if r else 0, (r[1] or 0) if r else 0)
    async with db.execute(
        "SELECT model, SUM(input_tokens), SUM(output_tokens), "
        "SUM(cache_creation_tokens), SUM(cache_read_tokens) FROM llm_usage "
        "WHERE created_at >= ? GROUP BY model",
        (since_iso,),
    ) as cur:
        for model, si, so, cw, cr in await cur.fetchall():
            total += model_cost(model, si or 0, so or 0, cw or 0, cr or 0)
    return total


async def get_spending(db: aiosqlite.Connection) -> dict:
    """Сегодняшний и месячный расход на Anthropic (все модели, правильный прайсинг)."""
    return {
        "today_usd": await _spend_since(db, _utc_cutoff("day")),
        "month_usd": await _spend_since(db, _utc_cutoff("month")),
    }


async def get_usage_breakdown(db: aiosqlite.Connection, since_iso: str) -> dict:
    """Разбивка расхода по семействам моделей с since_iso: {family: {in, out, cost}}.
    Классификатор (Haiku) берётся из message_log, остальное — из llm_usage."""
    fams: dict = {}
    async with db.execute(
        "SELECT SUM(llm_tokens_input), SUM(llm_tokens_output) FROM message_log WHERE created_at >= ?",
        (since_iso,),
    ) as cur:
        r = await cur.fetchone()
    if r and (r[0] or r[1]):
        fams["haiku"] = [r[0] or 0, r[1] or 0, 0, 0]
    async with db.execute(
        "SELECT model, SUM(input_tokens), SUM(output_tokens), "
        "SUM(cache_creation_tokens), SUM(cache_read_tokens) FROM llm_usage "
        "WHERE created_at >= ? GROUP BY model",
        (since_iso,),
    ) as cur:
        for model, si, so, cw, cr in await cur.fetchall():
            acc = fams.setdefault(_model_family(model), [0, 0, 0, 0])
            acc[0] += si or 0
            acc[1] += so or 0
            acc[2] += cw or 0
            acc[3] += cr or 0
    # "in" в отображении = весь промпт (uncached + cache-write + cache-read); cost — по реальным тарифам
    return {fam: {"in": i + cw + cr, "out": o, "cost": model_cost(fam, i, o, cw, cr)}
            for fam, (i, o, cw, cr) in fams.items()}


async def get_spend_breakdown_month(db: aiosqlite.Connection) -> dict:
    """Разбивка расхода по моделям за текущий (локальный) месяц."""
    return await get_usage_breakdown(db, _utc_cutoff("month"))


async def save_correction(db: aiosqlite.Connection, user_id: int, username: str, text: str, category: str) -> None:
    await db.execute(
        "INSERT INTO corrections (user_id, username, text, correct_category) VALUES (?, ?, ?, ?)",
        (user_id, username, (text or "")[:300], category),
    )
    await db.commit()


async def get_recent_corrections(db: aiosqlite.Connection, limit: int = 15) -> list[dict]:
    async with db.execute(
        "SELECT text, correct_category FROM corrections WHERE text IS NOT NULL AND text != '' ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ) as cur:
        rows = await cur.fetchall()
    return [{"text": r[0], "correct_category": r[1]} for r in rows]


async def save_draft_example(db: aiosqlite.Connection, user_id, incoming: str, reply: str) -> None:
    """Запомнить пример стиля владельца: на какое входящее как он ответил.
    Питает few-shot в генерации черновиков (бот учится твоей манере)."""
    if not (reply or "").strip():
        return
    await db.execute(
        "INSERT INTO draft_style (user_id, incoming, reply) VALUES (?, ?, ?)",
        (user_id, (incoming or "")[:400], (reply or "")[:400]),
    )
    await db.commit()


async def get_draft_examples(db: aiosqlite.Connection, limit: int = 5) -> list[dict]:
    """Свежие примеры стиля владельца (глобально, новейшие) для подражания в черновиках."""
    async with db.execute(
        "SELECT incoming, reply FROM draft_style WHERE reply IS NOT NULL AND reply != '' "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ) as cur:
        rows = await cur.fetchall()
    return [{"incoming": r[0], "reply": r[1]} for r in rows]


async def is_update_processed(db: aiosqlite.Connection, key: str) -> bool:
    """Dedup Telegram updates: True if already seen. Fail-OPEN: on any non-UNIQUE error
    (e.g. 'database is locked') return False so the message is still processed, never lost."""
    try:
        await db.execute("INSERT INTO processed_updates (key) VALUES (?)", (key,))
        await db.commit()
        return False
    except aiosqlite.IntegrityError:
        return True  # UNIQUE violation → genuinely already processed
    except Exception as exc:
        logger.warning(f"is_update_processed non-unique error (fail-open): {exc}")
        return False  # do NOT drop the message on transient DB errors


async def get_spend_since(db: aiosqlite.Connection, since_iso: str) -> float:
    """USD на Anthropic с заданной даты — все модели по своим тарифам (для оценки кредитов)."""
    return await _spend_since(db, since_iso)


async def get_credits_estimate(db: aiosqlite.Connection) -> Optional[dict]:
    """Estimate remaining Anthropic credits from a manually-set anchor minus spend since."""
    balance = await get_setting(db, "credits_balance")
    anchor_date = await get_setting(db, "credits_date")
    if balance is None or anchor_date is None:
        return None
    try:
        base = float(balance)
    except Exception:
        return None
    spent = await get_spend_since(db, anchor_date)
    return {
        "base": base,
        "anchor_date": anchor_date,
        "spent_since": spent,
        "remaining": max(0.0, base - spent),
    }


async def credits_short(db: aiosqlite.Connection) -> str:
    """Единая короткая строка денег для дайджеста/футеров: остаток кредитов Anthropic
    (то, что владелец задаёт через /credits). '' если баланс не задан — тогда показывать нечего."""
    est = await get_credits_estimate(db)
    return f"🪙 ≈ ${est['remaining']:.2f} кредитов" if est else ""


async def is_over_budget(db: aiosqlite.Connection, monthly_budget: float) -> bool:
    """True if this month's Anthropic spend has reached the budget ceiling."""
    if not monthly_budget or monthly_budget <= 0:
        return False
    spending = await get_spending(db)
    return spending["month_usd"] >= monthly_budget


async def get_stats(db: aiosqlite.Connection, days: int = 7) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    async with db.execute(
        # fix D2: datetime()-обёртка (created_at пробел-формат vs since isoformat) — иначе счётчик
        # недоучитывал строки на границе окна.
        "SELECT category, COUNT(*) FROM message_log WHERE datetime(created_at) >= datetime(?) GROUP BY category",
        (since,),
    ) as cur:
        counts = {row[0]: row[1] for row in await cur.fetchall()}

    async with db.execute(
        """SELECT AVG(llm_latency_ms), SUM(llm_tokens_input), SUM(llm_tokens_output)
           FROM message_log WHERE datetime(created_at) >= datetime(?) AND rule_matched = 'llm'""",
        (since,),
    ) as cur:
        llm_row = await cur.fetchone()

    avg_latency = int(llm_row[0]) if llm_row and llm_row[0] else 0
    total_input = llm_row[1] if llm_row and llm_row[1] else 0
    total_output = llm_row[2] if llm_row and llm_row[2] else 0

    async with db.execute(
        "SELECT message_text FROM message_log WHERE category = 'SPAM' AND created_at >= ?",
        (since,),
    ) as cur:
        spam_texts = [row[0] for row in await cur.fetchall() if row[0]]

    return {
        "counts": counts,
        "spam_texts": spam_texts,
        "days": days,
        "avg_llm_latency_ms": avg_latency,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
    }


# ── mode (persisted) ──────────────────────────────────────────────────────────

async def get_mode(db: aiosqlite.Connection, default: str = "observe") -> str:
    async with db.execute(
        "SELECT value FROM settings_kv WHERE key = 'mode'"
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else default


# ── conversation vault ────────────────────────────────────────────────────────

async def vault_add(
    db: aiosqlite.Connection,
    user_id: int,
    username: Optional[str],
    direction: str,
    text: Optional[str],
    media_type: Optional[str] = None,
    category: Optional[str] = None,
) -> None:
    await db.execute(
        """INSERT INTO conversations (user_id, username, direction, text, media_type, category)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (user_id, username, direction, _mask_cards(text), media_type, category),   # v4.106
    )
    await db.commit()


async def get_contact_by_username(db: aiosqlite.Connection, username: str) -> Optional[dict]:
    uname = username.lstrip("@")
    async with db.execute(
        "SELECT * FROM contact_profiles WHERE username = ? COLLATE NOCASE", (uname,)
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def search_conversations(db: aiosqlite.Connection, query: str, limit: int = 30) -> list[dict]:
    async with db.execute(
        """SELECT username, user_id, direction, text, created_at FROM conversations
           WHERE text LIKE ? ORDER BY created_at DESC LIMIT ?""",
        (f"%{query}%", limit),
    ) as cur:
        rows = await cur.fetchall()
    return [{"username": r[0], "user_id": r[1], "direction": r[2], "text": r[3], "created_at": r[4]} for r in rows]


async def get_unanswered_business(db: aiosqlite.Connection, days: int = 2) -> list[dict]:
    """Contacts whose last incoming BUSINESS message has no later 'out' reply, older than N days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    # Last incoming BUSINESS per user, not yet followed up
    async with db.execute(
        """SELECT c.user_id, c.username, MAX(c.created_at) as last_in
           FROM conversations c
           WHERE c.direction = 'in' AND c.category = 'BUSINESS' AND c.followup_done = 0
           GROUP BY c.user_id
           HAVING datetime(last_in) < datetime(?)""",   # fix D3: datetime()-обёртка (format mismatch)
        (cutoff,),
    ) as cur:
        candidates = await cur.fetchall()

    result = []
    for user_id, username, last_in in candidates:
        # Any outgoing reply after the last incoming BUSINESS?
        async with db.execute(
            "SELECT 1 FROM conversations WHERE user_id = ? AND direction = 'out' AND created_at > ? LIMIT 1",
            (user_id, last_in),
        ) as c2:
            replied = await c2.fetchone() is not None
        if not replied:
            result.append({"user_id": user_id, "username": username, "last_in": last_in})
    return result


async def mark_followup_done(db: aiosqlite.Connection, user_id: int) -> None:
    await db.execute(
        "UPDATE conversations SET followup_done = 1 WHERE user_id = ? AND category = 'BUSINESS'",
        (user_id,),
    )
    await db.commit()


async def vault_get_context(db: aiosqlite.Connection, user_id: int, limit: int = 10,
                            since_hours: int | None = None, until: str | None = None) -> str:
    """Return last N messages formatted as conversation context.
    since_hours: если задано — только сообщения свежее этого окна (чтобы извлечение задач
    не цеплялось за старый, уже закрытый контекст).
    until: контекст, каким он был на этот момент (eval на реальных переписках)."""
    q = "SELECT direction, username, text, media_type, created_at FROM conversations WHERE user_id = ?"
    params = [user_id]
    if since_hours:
        q += " AND created_at > datetime(?, ?)"
        params += [until or "now", f"-{int(since_hours)} hours"]
    if until:
        q += " AND created_at <= ?"
        params.append(until)
    q += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    async with db.execute(q, params) as cur:
        rows = await cur.fetchall()
    if not rows:
        return ""
    lines = []
    # Дату сообщения ставим префиксом — чтобы относительные слова («завтра», «сегодня»)
    # в СТАРЫХ сообщениях разрешались от ДНЯ ИХ ОТПРАВКИ, а не от дня разбора (баг: «завтра»
    # из сообщения 13-го при settle 14-го уезжало на 15-е).
    for direction, username, text, media_type, created_at in reversed(rows):
        speaker = "Ты" if direction == "out" else f"@{username or 'unknown'}"
        content = text or f"[{media_type or 'медиа'}]"
        day = str(created_at)[:10] if created_at else "?"
        lines.append(f"[{day}] {speaker}: {content}")
    return "\n".join(lines)


async def get_distinct_roles(db: aiosqlite.Connection, limit: int = 40) -> list[str]:
    """Уникальные роли контактов (для группировки и подсказки enrich — чтобы людей объединять
    в общие роли, а не плодить синонимы). Без служебных ('', 'unknown', 'владелец')."""
    async with db.execute(
        "SELECT relationship, COUNT(*) c FROM contact_profiles "
        "WHERE relationship IS NOT NULL AND trim(lower(relationship)) NOT IN ('', 'unknown', 'владелец') "
        "GROUP BY lower(trim(relationship)) ORDER BY c DESC LIMIT ?", (limit,)
    ) as cur:
        return [r[0] for r in await cur.fetchall()]


async def get_distinct_types(db: aiosqlite.Connection, limit: int = 30) -> list[str]:
    """Уникальные ТИПЫ (широкие группы, бот сам, ≥2 чел.) — подсказка enrich, чтобы объединять
    людей в существующие типы, а не плодить. Без служебных."""
    async with db.execute(
        "SELECT contact_type, COUNT(*) c FROM contact_profiles "
        "WHERE contact_type IS NOT NULL AND trim(lower(contact_type)) NOT IN ('', 'unknown', 'владелец') "
        "GROUP BY lower(trim(contact_type)) ORDER BY c DESC LIMIT ?", (limit,)
    ) as cur:
        return [r[0] for r in await cur.fetchall()]


async def get_contacts_by_role(db: aiosqlite.Connection) -> dict:
    """Контакты, сгруппированные по роли (relationship): {role: [имя/@username, ...]}.
    Для /roles и группировки. Без служебных ('', unknown)."""
    async with db.execute(
        "SELECT relationship, username, name FROM contact_profiles "
        "WHERE trim(lower(coalesce(relationship,''))) NOT IN ('', 'unknown') "
        "ORDER BY relationship, COALESCE(message_count,0) DESC"
    ) as cur:
        rows = await cur.fetchall()
    out: dict = {}
    for rel, uname, name in rows:
        label = name or (f"@{uname}" if uname else "?")
        out.setdefault(rel, []).append(label)
    return out


async def can_notify(db: aiosqlite.Connection, user_id: int, ntype: str, cooldown_minutes: int = 30) -> bool:
    """Returns True if we haven't sent this notification type recently."""
    since = (datetime.now(timezone.utc) - timedelta(minutes=cooldown_minutes)).isoformat()
    async with db.execute(
        "SELECT 1 FROM notification_log WHERE user_id = ? AND type = ? AND datetime(created_at) >= datetime(?)",
        (user_id, ntype, since),
    ) as cur:
        return await cur.fetchone() is None


async def log_notification(db: aiosqlite.Connection, user_id: int, ntype: str) -> None:
    await db.execute(
        "INSERT INTO notification_log (user_id, type) VALUES (?, ?)", (user_id, ntype)
    )
    await db.commit()


# ── contact profiles (vault memory) ──────────────────────────────────────────

async def get_contact_profile(db: aiosqlite.Connection, user_id: int) -> Optional[dict]:
    async with db.execute(
        "SELECT * FROM contact_profiles WHERE user_id = ?", (user_id,)
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    return dict(row)


async def get_profiles_missing_notion(db: aiosqlite.Connection, limit: int = 20) -> list[dict]:
    """Reconciliation: profiles that have a summary but no notion_contact_id yet.
    Used by the sync loop to retry contact creation (fixes fire-and-forget enrich)."""
    async with db.execute(
        "SELECT * FROM contact_profiles "
        "WHERE (notion_contact_id IS NULL OR notion_contact_id = '') "
        "AND summary IS NOT NULL AND summary != '' "
        "ORDER BY last_contact DESC LIMIT ?",
        (limit,),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def set_contact_photo(db: aiosqlite.Connection, user_id: int, file_id: str,
                            unique_id: str | None = None, src: str = "avatar") -> None:
    """Сохранить фото памяти контакта. unique_id — для детекта смены аватара;
    src: avatar (авто, обновляется) | manual (/setphoto) | confirmed (подтверждённое присланное) — последние два НЕ перетираются авто-рефрешем."""
    await db.execute(
        "UPDATE contact_profiles SET photo_file_id = ?, photo_unique_id = ?, photo_src = ? WHERE user_id = ?",
        (file_id, unique_id, src, user_id),
    )
    await db.commit()


async def mark_photo_asked(db: aiosqlite.Connection, user_id: int) -> None:
    """Отметить, что про фото этого контакта только что спрашивали (троттл вопроса)."""
    await db.execute(
        "UPDATE contact_profiles SET photo_asked_at = ? WHERE user_id = ?",
        (datetime.now(timezone.utc).isoformat(), user_id),
    )
    await db.commit()


async def get_owner_outgoing(db: aiosqlite.Connection, limit: int = 80) -> list[dict]:
    """Недавние ИСХОДЯЩИЕ сообщения владельца (direction='out') по всем чатам —
    материал для память о себе владельца. С username контакта-адресата для контекста."""
    cur = await db.execute(
        "SELECT username, text, created_at FROM conversations "
        "WHERE direction='out' AND text IS NOT NULL AND TRIM(text) != '' "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def upsert_contact_profile(
    db: aiosqlite.Connection,
    user_id: int,
    username: str,
    display_name: str = "",
    increment: bool = True,
    **kwargs,
) -> None:
    """increment=True: count this as a new message (+1). increment=False: only update
    profile fields (used by enrich_profile so the counter isn't double-bumped)."""
    now = datetime.now(timezone.utc).isoformat()
    existing = await get_contact_profile(db, user_id)
    if not existing:
        await db.execute(
            """INSERT INTO contact_profiles
               (user_id, username, display_name, message_count, last_contact, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, username, display_name, 1 if increment else 0, now, now),
        )
    elif increment:
        await db.execute(
            "UPDATE contact_profiles SET message_count = message_count + 1, last_contact = ?, username = ?, updated_at = ? WHERE user_id = ?",
            (now, username, now, user_id),
        )
    else:
        await db.execute(
            "UPDATE contact_profiles SET last_contact = ?, username = ?, updated_at = ? WHERE user_id = ?",
            (now, username, now, user_id),
        )
    for key, value in kwargs.items():
        await db.execute(
            f"UPDATE contact_profiles SET {key} = ? WHERE user_id = ?", (value, user_id)
        )
    await db.commit()


async def get_digest_state(db: aiosqlite.Connection, key: str) -> Optional[str]:
    async with db.execute("SELECT value FROM digest_state WHERE key = ?", (key,)) as cur:
        row = await cur.fetchone()
    return row[0] if row else None


async def set_digest_state(db: aiosqlite.Connection, key: str, value: str) -> None:
    await db.execute(
        "INSERT OR REPLACE INTO digest_state (key, value) VALUES (?, ?)", (key, value)
    )
    await db.commit()


async def save_message_context(db: aiosqlite.Connection, user_id: int, text: str) -> None:
    """Keep last 5 messages per user for context."""
    await db.execute(
        "INSERT OR IGNORE INTO conversation_state (user_id, message_count) VALUES (?, 0)",
        (user_id,),
    )
    # Store last messages as JSON in last_seen field — reuse existing table
    async with db.execute(
        "SELECT message_count FROM conversation_state WHERE user_id = ?", (user_id,)
    ) as cur:
        row = await cur.fetchone()
    count = row[0] if row else 0

    # Store context in message_log, we'll retrieve last N
    await db.commit()


async def get_recent_context(db: aiosqlite.Connection, user_id: int, limit: int = 4) -> str:
    """Get last N message texts from a user, combined as context."""
    async with db.execute(
        """SELECT message_text FROM message_log
           WHERE user_id = ? AND message_text IS NOT NULL
           ORDER BY created_at DESC LIMIT ?""",
        (user_id, limit),
    ) as cur:
        rows = await cur.fetchall()
    # Reverse so oldest first
    texts = [r[0] for r in reversed(rows)]
    return " | ".join(texts) if texts else ""


async def save_task_sync(
    db: aiosqlite.Connection,
    todoist_id: str,
    notion_page_id: str,
    user_id: int = 0,
    title: str = "",
    topic: str = "",
    due_date: Optional[str] = None,
    is_commitment: int = 0,
) -> Optional[int]:
    cur = await db.execute(
        "INSERT OR IGNORE INTO task_sync (todoist_task_id, notion_page_id, user_id, task_title, topic, due_date, is_commitment) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (todoist_id, notion_page_id, user_id, title, topic or "", due_date, int(is_commitment)),
    )
    await db.commit()
    # If IGNORE skipped (already exists), fetch the existing row id so callers get a valid sync_id
    if cur.lastrowid:
        return cur.lastrowid
    async with db.execute("SELECT id FROM task_sync WHERE todoist_task_id = ?", (todoist_id,)) as c:
        row = await c.fetchone()
    return row[0] if row else None


def _normalize_title(title: str) -> str:
    """Normalize for dedup: lowercase, strip punctuation/emoji, collapse spaces."""
    import re as _re
    t = title.lower().strip()
    t = _re.sub(r"[^\wа-яё ]", "", t)  # keep letters/digits/spaces
    t = _re.sub(r"\s+", " ", t).strip()
    return t


# Stop-words ignored when comparing task titles for fuzzy dedup
_TITLE_STOP = {
    "и", "в", "на", "с", "со", "о", "об", "по", "для", "к", "у", "из", "что",
    "как", "это", "чтобы", "нужно", "надо", "помочь", "сделать", "ответить",
    "уточнить", "по поводу", "насчет", "насчёт", "the", "a", "to", "for", "of",
}


def _title_tokens(title: str) -> set:
    # crude stemming: 5-char prefix collapses Russian inflections
    # ('курсовую'/'курсовой' → 'курсо', 'работу'/'работы' → 'работ')
    return {w[:5] for w in _normalize_title(title).split() if w and w not in _TITLE_STOP}


def _titles_similar(a: str, b: str, threshold: float = 0.6) -> bool:
    """Fuzzy match: exact normalized OR high token-overlap (Jaccard).
    Catches LLM rephrasings ('помочь с PDF курсовой' ≈ 'отредактировать PDF курсовой')."""
    na, nb = _normalize_title(a), _normalize_title(b)
    if na and na == nb:
        return True
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return False
    inter = len(ta & tb)
    union = len(ta | tb)
    jaccard = inter / union if union else 0.0
    # Containment branch only when there are ≥2 content tokens on each side AND ≥2 shared —
    # otherwise terse titles ('Созвон' vs 'Созвониться') with one shared token wrongly collapse.
    containment = inter / min(len(ta), len(tb))
    containment_ok = (min(len(ta), len(tb)) >= 2 and inter >= 2 and containment >= 0.8)
    return jaccard >= threshold or containment_ok


async def task_exists_recently(db: aiosqlite.Connection, user_id: int, title: str,
                               hours: int = 24, due_date: Optional[str] = None) -> bool:
    """Dedup: True if a similar task from same user exists recently (fuzzy, catches rephrasings).
    Учёт дня: если у кандидата и у похожей задачи заданы РАЗНЫЕ календарные дни — это РАЗНЫЕ
    задачи (напр. два урока на 16-е и 19-е), НЕ дубль. Совпадение дня / отсутствие дат → дубль."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    # NB: created_at хранится как CURRENT_TIMESTAMP («2026-06-07 19:52:33», пробел),
    # а since — isoformat («…T…+00:00»). Прямое строковое сравнение ломается (пробел < 'T'),
    # из-за чего задача того же дня выпадала из окна → дубль проскакивал. datetime() нормализует оба.
    cand_day = str(due_date)[:10] if due_date else None
    async with db.execute(
        "SELECT task_title, due_date FROM task_sync WHERE user_id = ? AND datetime(created_at) >= datetime(?)",
        (user_id, since),
    ) as cur:
        rows = await cur.fetchall()
    for existing, ex_due in rows:
        if not _titles_similar(existing or "", title):
            continue
        ex_day = str(ex_due)[:10] if ex_due else None
        if cand_day and ex_day and cand_day != ex_day:
            continue  # та же тема, но другой день → отдельная задача, не дубль
        if cand_day and not ex_day:
            continue  # кандидат датирован, похожая — бездатная заготовка (❓ без времени) →
            #           это КОНКРЕТИЗАЦИЯ, а не дубль (иначе два урока «Ср/Пт» гасились о ❓-заготовку)
        return True
    return False


async def title_tracked_in_sync(db: aiosqlite.Connection, title: str) -> bool:
    """Dedup for manual Notion import: True if any tracked task (any user, incl. open)
    already has this normalized title. Prevents re-importing the same task to Todoist."""
    norm = _normalize_title(title)
    if not norm:
        return False
    async with db.execute("SELECT task_title FROM task_sync") as cur:
        rows = await cur.fetchall()
    for (existing,) in rows:
        if _normalize_title(existing or "") == norm:
            return True
    return False


async def archive_and_delete_old_conversations(db: aiosqlite.Connection, months: int, archive_dir: str) -> dict:
    """Archive conversations older than N months to a file, THEN delete them.
    Keeps full deleted-context retrievable; contact_profiles (bot memory) are NOT touched."""
    import json as _json
    from pathlib import Path as _Path
    cutoff = (datetime.now(timezone.utc) - timedelta(days=months * 30)).isoformat()

    async with db.execute(
        # fix #7: datetime()-обёртка — created_at хранится в формате CURRENT_TIMESTAMP (пробел, без tz),
        # а cutoff = .isoformat() ('T', +00:00); сырое строковое сравнение резало лишние ~сутки на границе.
        "SELECT user_id, username, direction, text, media_type, category, created_at "
        "FROM conversations WHERE datetime(created_at) < datetime(?) ORDER BY user_id, created_at",
        (cutoff,),
    ) as cur:
        rows = await cur.fetchall()

    if not rows:
        return {"count": 0, "contacts": 0, "body": "", "period": f"до {cutoff[:10]}", "date": datetime.now(timezone.utc).strftime("%Y-%m-%d")}

    _Path(archive_dir).mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # JSONL — полный машинный архив
    jsonl = _Path(archive_dir) / f"conversations-archived-{stamp}.jsonl"
    with open(jsonl, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(_json.dumps({
                "user_id": r[0], "username": r[1], "direction": r[2],
                "text": r[3], "media_type": r[4], "category": r[5], "created_at": r[6],
            }, ensure_ascii=False) + "\n")
    # Markdown — человекочитаемый, сгруппирован по контактам
    md = _Path(archive_dir) / f"archive-{stamp}.md"
    with open(md, "a", encoding="utf-8") as f:
        f.write(f"\n# Архив переписки (старше {months} мес) — выгружено {stamp}\n\n")
        cur_user = None
        for r in rows:
            uname = r[1] or f"id{r[0]}"
            if uname != cur_user:
                f.write(f"\n## @{uname}\n\n")
                cur_user = uname
            who = "Ты" if r[2] == "out" else f"@{uname}"
            content = r[3] or f"[{r[4] or 'медиа'}]"
            f.write(f"- `{(r[6] or '')[:16]}` **{who}:** {content}\n")

    # Build a capped markdown body + contacts set for Notion push
    contacts = sorted({(r[1] or f"id{r[0]}") for r in rows})
    body_lines = []
    cur_user = None
    for r in rows:
        uname = r[1] or f"id{r[0]}"
        if uname != cur_user:
            body_lines.append(f"\n@{uname}:")
            cur_user = uname
        who = "Ты" if r[2] == "out" else uname
        content = r[3] or f"[{r[4] or 'медиа'}]"
        body_lines.append(f"{(r[6] or '')[:16]} {who}: {content}")

    await db.execute("DELETE FROM conversations WHERE datetime(created_at) < datetime(?)", (cutoff,))
    await db.commit()
    return {
        "count": len(rows),
        "contacts": len(contacts),
        "body": "\n".join(body_lines),
        "period": f"до {cutoff[:10]}",
        "date": stamp,
    }


async def delete_old_conversations(db: aiosqlite.Connection, months: int = 6) -> int:
    """Deprecated direct delete (kept for compatibility). Prefer archive_and_delete_old_conversations."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=months * 30)).isoformat()
    async with db.execute(
        "DELETE FROM conversations WHERE datetime(created_at) < datetime(?)", (cutoff,)
    ) as cur:
        await db.commit()
        return cur.rowcount


async def save_links(
    db: aiosqlite.Connection,
    user_id: int,
    username: Optional[str],
    urls: list[str],
    context: Optional[str],
    category: Optional[str],
) -> None:
    for url in urls:
        # Atomic dedup via UNIQUE(user_id, url) — no SELECT race
        await db.execute(
            "INSERT OR IGNORE INTO saved_links (user_id, username, url, context, category) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, url, (context or "")[:500], category),
        )
    await db.commit()


# ── media dedup (identical photos/voice/docs by Telegram file_unique_id) ────────

async def store_message_vector(db: aiosqlite.Connection, user_id: int, text: str, vector: bytes) -> None:
    await db.execute(
        "INSERT INTO message_vectors (user_id, text, vector) VALUES (?, ?, ?)",
        (user_id, text, vector),
    )
    await db.commit()


async def get_user_vectors(db: aiosqlite.Connection, user_id: int, limit: int = 2000) -> list:
    async with db.execute(
        "SELECT text, vector FROM message_vectors WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ) as cur:
        return await cur.fetchall()


async def get_open_commitments(db: aiosqlite.Connection, limit: int = 12) -> list[dict]:
    """«Ты обещал» = ОТКРЫТЫЕ задачи-обещания (is_commitment=1) — живые и закрываемые (бот/Todoist),
    а не саможивущий текст профиля (тот виснет на офлайн-выполненном). Ключ `commitments` = тайтл задачи
    (совместимость с дайджестом/радаром); `id`/`todoist_id` — для кнопок закрытия."""
    async with db.execute(
        "SELECT COALESCE(p.name,''), COALESCE(p.username,''), t.task_title, t.id, t.todoist_task_id "
        "FROM task_sync t LEFT JOIN contact_profiles p ON p.user_id = t.user_id "
        "WHERE t.synced = 0 AND t.is_commitment = 1 AND COALESCE(p.relationship,'') != 'спамер' "
        "ORDER BY t.created_at DESC LIMIT ?",
        (limit,),
    ) as cur:
        rows = await cur.fetchall()
    return [{"name": r[0], "username": r[1], "commitments": r[2], "id": r[3], "todoist_id": r[4]} for r in rows]


async def set_contact_src_account(db: aiosqlite.Connection, user_id: int, account_id) -> None:
    """Запомнить, на какой аккаунт-владелец писал контакт (для метки источника в уведомлениях)."""
    try:
        await db.execute("UPDATE contact_profiles SET src_account = ? WHERE user_id = ?", (account_id, user_id))
        await db.commit()
    except Exception:
        pass


async def get_contact_src_account(db: aiosqlite.Connection, user_id: int):
    async with db.execute("SELECT src_account FROM contact_profiles WHERE user_id = ?", (user_id,)) as cur:
        row = await cur.fetchone()
    return row[0] if row and row[0] else None


async def get_contact_rule(db: aiosqlite.Connection, user_id: int) -> Optional[str]:
    """Правило по контакту: 'vip' | 'task' | 'never' | 'mute' | None. Хранится в settings_kv."""
    return await get_setting(db, f"rule_{user_id}")


async def set_contact_rule(db: aiosqlite.Connection, user_id: int, rule: Optional[str]) -> None:
    if rule:
        await set_setting(db, f"rule_{user_id}", rule)
    else:
        await db.execute("DELETE FROM settings_kv WHERE key = ?", (f"rule_{user_id}",))
        await db.commit()


async def get_cold_contacts(db: aiosqlite.Connection, days: int = 7, limit: int = 10) -> list[dict]:
    """Важные контакты без активности N+ дней — для радара отношений. Через datetime() (формат-устойчиво)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    async with db.execute(
        "SELECT COALESCE(name, COALESCE(display_name,'')), COALESCE(username,''), "
        "COALESCE(relationship,''), last_contact, COALESCE(message_count,0) "
        "FROM contact_profiles WHERE last_contact IS NOT NULL "
        "AND datetime(last_contact) < datetime(?) "
        "AND relationship NOT IN ('спамер', 'unknown', '') AND COALESCE(message_count,0) >= 3 "
        "ORDER BY datetime(last_contact) ASC LIMIT ?",
        (cutoff, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [{"name": r[0], "username": r[1], "relationship": r[2],
             "last_contact": r[3], "msgs": r[4]} for r in rows]


async def get_all_vectors_with_names(db: aiosqlite.Connection, limit: int = 6000) -> list:
    """Все векторы сообщений + имя/username контакта — для глобального RAG Q&A («спроси память»)."""
    async with db.execute(
        "SELECT mv.text, mv.vector, COALESCE(cp.name, ''), COALESCE(cp.username, '') "
        "FROM message_vectors mv LEFT JOIN contact_profiles cp ON cp.user_id = mv.user_id "
        "ORDER BY mv.id DESC LIMIT ?",
        (limit,),
    ) as cur:
        return await cur.fetchall()


async def get_processed_media(db: aiosqlite.Connection, file_unique_id: str):
    """Return prior record for this exact file (same file_unique_id), or None.
    Lets the bot skip re-analyzing/re-forwarding identical media."""
    if not file_unique_id:
        return None
    async with db.execute(
        "SELECT description, media_type, user_id FROM processed_media WHERE file_unique_id = ?",
        (file_unique_id,),
    ) as cur:
        row = await cur.fetchone()
    return {"description": row[0], "media_type": row[1], "user_id": row[2]} if row else None


async def mark_processed_media(db: aiosqlite.Connection, file_unique_id: str,
                               user_id: int, media_type: str, description: str = "") -> None:
    if not file_unique_id:
        return
    await db.execute(
        "INSERT OR IGNORE INTO processed_media (file_unique_id, user_id, media_type, description) "
        "VALUES (?, ?, ?, ?)",
        (file_unique_id, user_id, media_type, (description or "")[:1000]),
    )
    await db.commit()


async def get_recent_links(db: aiosqlite.Connection, limit: int = 20, username: Optional[str] = None) -> list[dict]:
    if username:
        q = "SELECT username, url, category, created_at FROM saved_links WHERE username = ? COLLATE NOCASE ORDER BY created_at DESC LIMIT ?"
        params = (username.lstrip("@"), limit)
    else:
        q = "SELECT username, url, category, created_at FROM saved_links ORDER BY created_at DESC LIMIT ?"
        params = (limit,)
    async with db.execute(q, params) as cur:
        rows = await cur.fetchall()
    return [{"username": r[0], "url": r[1], "category": r[2], "created_at": r[3]} for r in rows]


async def get_setting(db: aiosqlite.Connection, key: str) -> Optional[str]:
    async with db.execute("SELECT value FROM settings_kv WHERE key = ?", (key,)) as cur:
        row = await cur.fetchone()
    return row[0] if row else None


async def set_setting(db: aiosqlite.Connection, key: str, value: str) -> None:
    await db.execute(
        "INSERT OR REPLACE INTO settings_kv (key, value) VALUES (?, ?)", (key, value)
    )
    await db.commit()


async def claim_once(db: aiosqlite.Connection, key: str) -> bool:
    """Атомарный однократный захват: True только ПЕРВОМУ вызвавшему. Гонка-безопасно через
    INSERT OR IGNORE по PRIMARY KEY (в отличие от read-then-write get/set). fix #4."""
    cur = await db.execute("INSERT OR IGNORE INTO settings_kv (key, value) VALUES (?, '1')", (key,))
    await db.commit()
    return cur.rowcount == 1


async def notion_page_tracked(db: aiosqlite.Connection, notion_page_id: str) -> bool:
    async with db.execute(
        "SELECT 1 FROM task_sync WHERE notion_page_id = ?", (notion_page_id,)
    ) as cur:
        return await cur.fetchone() is not None


async def get_sync_by_id(db: aiosqlite.Connection, sync_id: int) -> Optional[dict]:
    async with db.execute(
        "SELECT todoist_task_id, notion_page_id, task_title, synced, created_at, "
        "calendar_event_id, due_date FROM task_sync WHERE id = ?",
        (sync_id,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    return {"todoist_id": row[0], "notion_page_id": row[1], "title": row[2],
            "synced": row[3], "created_at": row[4], "id": sync_id,
            "calendar_event_id": row[5], "due_date": row[6]}


async def save_task_feedback(db: aiosqlite.Connection, title: str, verdict: str) -> None:
    """Обратная связь по задаче: verdict='rejected' — владелец быстро удалил (бот ошибся,
    это НЕ задача). Питает анти-примеры в извлечении (зеркало draft_style для пере-создания)."""
    if not (title or "").strip():
        return
    await db.execute(
        "INSERT INTO task_feedback (title, verdict) VALUES (?, ?)",
        ((title or "")[:200], verdict))
    await db.commit()


async def log_command(db: aiosqlite.Connection, command: str) -> None:
    """v4.71: команда владельца, только название (см. core/command_log.py)."""
    await db.execute("INSERT INTO command_log (command) VALUES (?)", (command,))
    await db.commit()


async def get_rejected_titles(db: aiosqlite.Connection, days: int = 7, limit: int = 8) -> list[str]:
    """Недавно отклонённые (быстро удалённые) названия задач — анти-примеры для извлечения."""
    async with db.execute(
        "SELECT DISTINCT title FROM task_feedback WHERE verdict='rejected' "
        "AND created_at > datetime('now', ?) ORDER BY created_at DESC LIMIT ?",
        (f"-{days} days", limit)) as cur:
        rows = await cur.fetchall()
    return [r[0] for r in rows if r[0]]


async def count_photos_today(db: aiosqlite.Connection) -> int:
    today = _utc_cutoff("day")
    async with db.execute(
        "SELECT COUNT(*) FROM conversations WHERE media_type = 'photo' AND created_at >= ?",
        (today,),
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0


async def save_notion_contact_id(db: aiosqlite.Connection, user_id: int, notion_id: str) -> None:
    await db.execute(
        "UPDATE contact_profiles SET notion_contact_id = ? WHERE user_id = ?",
        (notion_id, user_id),
    )
    await db.commit()


async def get_pending_syncs(db: aiosqlite.Connection) -> list[dict]:
    async with db.execute(
        "SELECT id, todoist_task_id, notion_page_id FROM task_sync WHERE synced = 0"
    ) as cur:
        rows = await cur.fetchall()
    return [{"id": r[0], "todoist_id": r[1], "notion_page_id": r[2]} for r in rows]


async def get_open_tasks_for_user(db: aiosqlite.Connection, user_id: int) -> list[dict]:
    async with db.execute(
        "SELECT id, todoist_task_id, notion_page_id, task_title, topic, calendar_event_id, due_date "
        "FROM task_sync WHERE user_id = ? AND synced = 0",
        (user_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [{"id": r[0], "todoist_id": r[1], "notion_page_id": r[2], "title": r[3],
             "topic": r[4] or "", "calendar_event_id": r[5], "due_date": r[6]} for r in rows]


async def get_all_open_timed_tasks(db: aiosqlite.Connection) -> list[dict]:
    """Открытые задачи с КОНКРЕТНЫМ временем (due содержит 'T') — для гибрид-закрытия по времени."""
    async with db.execute(
        "SELECT id, todoist_task_id, notion_page_id, task_title, user_id, calendar_event_id, due_date "
        "FROM task_sync WHERE synced = 0 AND due_date LIKE '%T%'"
    ) as cur:
        rows = await cur.fetchall()
    return [{"id": r[0], "todoist_id": r[1], "notion_page_id": r[2], "title": r[3],
             "user_id": r[4], "calendar_event_id": r[5], "due_date": r[6]} for r in rows]


async def get_dominant_category(db: aiosqlite.Connection, user_id: int,
                                days: int = 30, min_msgs: int = 30, share: float = 0.9) -> Optional[str]:
    """Устоявшаяся категория контакта: ≥min_msgs классифицированных за days и ≥share одной категории.
    P1.3 плана 07/2026: короткие реплики от 4000×FRIEND-контакта шли в UNCLEAR (LLM игнорировал роль)."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    async with db.execute(
        "SELECT category, COUNT(*) FROM message_log "
        "WHERE user_id = ? AND datetime(created_at) >= datetime(?) AND category IS NOT NULL "
        "GROUP BY category", (user_id, since),
    ) as cur:
        rows = await cur.fetchall()
    total = sum(r[1] for r in rows)
    if total < min_msgs:
        return None
    top = max(rows, key=lambda r: r[1])
    return top[0] if top[1] / total >= share else None


async def get_open_tasks_due(db: aiosqlite.Connection) -> list[dict]:
    """Все открытые задачи с дедлайном (+username контакта) — для дайджеста/напоминаний.
    Замена мёртвых Notion-запросов после ретайра (bot.db = источник истины)."""
    async with db.execute(
        "SELECT t.id, t.task_title, t.due_date, t.user_id, c.username, c.name "
        "FROM task_sync t LEFT JOIN contact_profiles c ON c.user_id = t.user_id "
        "WHERE t.synced = 0 AND t.due_date IS NOT NULL AND t.due_date != '' "
        "ORDER BY t.due_date"
    ) as cur:
        rows = await cur.fetchall()
    return [{"id": r[0], "title": r[1], "due_date": r[2], "user_id": r[3],
             "from": (r[5] or (f"@{r[4]}" if r[4] else "?"))} for r in rows]


async def set_calendar_event_id(db: aiosqlite.Connection, sync_id: int, event_id: str) -> None:
    await db.execute("UPDATE task_sync SET calendar_event_id = ? WHERE id = ?", (event_id, sync_id))
    await db.commit()


async def set_task_title(db: aiosqlite.Connection, sync_id: int, title: str) -> None:
    """v4.35: переименовать задачу (суть изменилась по переписке — сумма/условия)."""
    await db.execute("UPDATE task_sync SET task_title = ? WHERE id = ?", (title[:300], sync_id))
    await db.commit()


async def set_task_due(db: aiosqlite.Connection, sync_id: int, due: Optional[str]) -> None:
    """Сохранить дедлайн задачи локально (для пультов/дайджеста). Зовётся при создании и переносе."""
    if not sync_id:
        return
    await db.execute("UPDATE task_sync SET due_date = ? WHERE id = ?", (due, sync_id))
    await db.commit()


async def bump_reschedule(db: aiosqlite.Connection, sync_id: int) -> int:
    """v4.24-бонус «хронический перенос»: счётчик переносов задачи. Возвращает новое значение
    (0 при сбое/отсутствии колонки — фича мягкая)."""
    try:
        await db.execute(
            "UPDATE task_sync SET reschedule_count = COALESCE(reschedule_count,0)+1 WHERE id=?",
            (sync_id,))
        await db.commit()
        async with db.execute("SELECT reschedule_count FROM task_sync WHERE id=?", (sync_id,)) as cur:
            r = await cur.fetchone()
        return int(r[0] or 0) if r else 0
    except Exception:
        return 0


async def get_unanswered_questions(db: aiosqlite.Connection, hours: int = 24, limit: int = 5) -> list[dict]:
    """v4.22 awaiting-reply: диалоги, где ПОСЛЕДНЕЕ сообщение — входящий вопрос («?»),
    висящий без ответа владельца ≥ hours. Чистый SQL, без LLM; гейт msg_count≥5."""
    q = ("SELECT c.user_id, p.username, p.name, c.text, c.created_at "
         "FROM conversations c "
         "JOIN (SELECT user_id, MAX(id) AS mid FROM conversations GROUP BY user_id) l ON l.mid = c.id "
         "LEFT JOIN contact_profiles p ON p.user_id = c.user_id "
         "WHERE c.direction='in' AND instr(c.text,'?')>0 "
         "AND datetime(c.created_at) <= datetime('now', ?) "
         "AND COALESCE(p.message_count,0) >= 5 "
         "ORDER BY c.created_at ASC LIMIT ?")
    out = []
    async with db.execute(q, (f"-{hours} hours", limit)) as cur:
        for uid, un, nm, txt, ca in await cur.fetchall():
            try:
                t0 = datetime.fromisoformat(str(ca).replace(" ", "T"))
                if t0.tzinfo is None:
                    t0 = t0.replace(tzinfo=timezone.utc)
                age_d = max(1, int((datetime.now(timezone.utc) - t0).total_seconds() // 86400))
            except Exception:
                age_d = 1
            out.append({"user_id": uid, "username": un, "who": nm or (f"@{un}" if un else f"id{uid}"),
                        "text": (txt or "").strip(), "age_days": age_d})
    return out


async def get_unanswered_outgoing(db: aiosqlite.Connection, hours: int = 48, limit: int = 5,
                                  max_days: int = 14) -> list[dict]:
    """v4.30 «тебе не отвечают»: зеркало get_unanswered_questions — диалоги, где ПОСЛЕДНЕЕ
    сообщение — ТВОЙ исходящий вопрос («?»), висящий без ответа контакта ≥ hours.
    Чистый SQL, без LLM; тот же гейт msg_count≥5. v4.34.1: не старше max_days — висяк на
    69 дней уже не «молчит», а мёртв, и Telegram всё равно не даст боту туда написать."""
    q = ("SELECT c.user_id, p.username, p.name, c.text, c.created_at "
         "FROM conversations c "
         "JOIN (SELECT user_id, MAX(id) AS mid FROM conversations GROUP BY user_id) l ON l.mid = c.id "
         "LEFT JOIN contact_profiles p ON p.user_id = c.user_id "
         "WHERE c.direction='out' AND instr(c.text,'?')>0 "
         "AND datetime(c.created_at) <= datetime('now', ?) "
         "AND datetime(c.created_at) >= datetime('now', ?) "
         "AND COALESCE(p.message_count,0) >= 5 "
         "ORDER BY c.created_at DESC LIMIT ?")
    out = []
    async with db.execute(q, (f"-{hours} hours", f"-{max_days} days", limit)) as cur:
        for uid, un, nm, txt, ca in await cur.fetchall():
            try:
                t0 = datetime.fromisoformat(str(ca).replace(" ", "T"))
                if t0.tzinfo is None:
                    t0 = t0.replace(tzinfo=timezone.utc)
                age_d = max(1, int((datetime.now(timezone.utc) - t0).total_seconds() // 86400))
            except Exception:
                age_d = 1
            out.append({"user_id": uid, "username": un,
                        "who": nm or (f"@{un}" if un else f"id{uid}"),
                        "text": (txt or "").strip(), "age_days": age_d})
    return out


async def get_user_by_calendar_event(db: aiosqlite.Connection, event_id: str) -> Optional[int]:
    """user_id задачи, привязанной к событию календаря (для брифа-подготовки). None если не наша."""
    if not event_id:
        return None
    async with db.execute(
        "SELECT user_id FROM task_sync WHERE calendar_event_id = ? AND user_id IS NOT NULL LIMIT 1",
        (event_id,),
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row and row[0] else None


async def task_closed_by_calendar_event(db: aiosqlite.Connection, event_id: str) -> bool:
    """True, если задача по этому событию календаря ЗАКРЫТА (чтобы не напоминать о закрытой/отменённой
    встрече). ORDER BY synced ASC → если есть открытая (0) строка, вернём её → не закрыто."""
    if not event_id:
        return False
    async with db.execute(
        "SELECT synced FROM task_sync WHERE calendar_event_id = ? ORDER BY synced ASC LIMIT 1",
        (event_id,),
    ) as cur:
        row = await cur.fetchone()
    return bool(row and row[0] == 1)


async def get_named_profiles(db: aiosqlite.Connection, limit: int = 300) -> list[dict]:
    """Контакты с непустым именем — для фолбэк-сопоставления имени из названия события."""
    async with db.execute(
        "SELECT user_id, COALESCE(name,''), COALESCE(username,'') FROM contact_profiles "
        "WHERE name IS NOT NULL AND trim(name) != '' AND relationship != 'спамер' "
        "ORDER BY last_contact DESC LIMIT ?",
        (limit,),
    ) as cur:
        rows = await cur.fetchall()
    return [{"user_id": r[0], "name": r[1], "username": r[2]} for r in rows]


async def get_partial_open_tasks(db: aiosqlite.Connection) -> list[dict]:
    """Открытые задачи, где есть только ОДНА сторона (Todoist xor Notion) — кандидаты на реконсиляцию."""
    async with db.execute(
        "SELECT t.id, t.todoist_task_id, t.notion_page_id, t.task_title, t.user_id, "
        "COALESCE(p.username,'') FROM task_sync t LEFT JOIN contact_profiles p ON p.user_id=t.user_id "
        "WHERE t.synced=0 AND ((t.todoist_task_id IS NULL) <> (t.notion_page_id IS NULL))"
    ) as cur:
        rows = await cur.fetchall()
    return [{"id": r[0], "todoist_id": r[1], "notion_page_id": r[2], "title": r[3],
             "user_id": r[4], "username": r[5]} for r in rows]


async def set_task_side_id(db: aiosqlite.Connection, sync_id: int, todoist_id=None, notion_id=None) -> None:
    if todoist_id:
        await db.execute("UPDATE task_sync SET todoist_task_id = ? WHERE id = ?", (todoist_id, sync_id))
    if notion_id:
        await db.execute("UPDATE task_sync SET notion_page_id = ? WHERE id = ?", (notion_id, sync_id))
    await db.commit()


def without_done(commitments: str, titles: list) -> str:
    """Чистая: текст обещаний без пунктов, совпавших с закрытыми задачами. Заголовок задачи бывает обрезан, поэтому сравниваем и вхождение."""
    import re as _re

    def done(item: str) -> bool:
        n = _normalize_title(item)
        for title in titles:
            t = _normalize_title(title or "")
            if n == t or (len(n) > 8 and len(t) > 8 and (n in t or t in n)) or _titles_similar(item, title or ""):
                return True
        return False

    items = [c.strip() for c in _re.split(r"[;\n]+", commitments or "") if c.strip()]
    kept = [c for c in items if not done(c)]
    return commitments if len(kept) == len(items) else "; ".join(kept)


async def _prune_commitment(db: aiosqlite.Connection, where: str, value) -> None:
    """Задача закрыта: снять её обещание из памяти. Сбой здесь не должен мешать закрытию."""
    try:
        async with db.execute(f"SELECT t.user_id, t.task_title, p.commitments FROM task_sync t JOIN contact_profiles p ON p.user_id = t.user_id WHERE t.{where} = ?", (value,)) as cur:
            row = await cur.fetchone()
        if not row or not row[2]:
            return
        pruned = without_done(row[2], [row[1]])
        if pruned != row[2]:
            await db.execute("UPDATE contact_profiles SET commitments = ? WHERE user_id = ?", (pruned, row[0]))
            logger.info(f"Commitment cleared for {row[0]}: {row[1][:60]}")
    except Exception as exc:
        logger.warning(f"prune commitment skip: {type(exc).__name__}: {exc}")


async def mark_sync_done_by_id(db: aiosqlite.Connection, sync_id: int) -> None:
    """Close a task by task_sync row id (works even when todoist_id is NULL, e.g. partials)."""
    now = datetime.now(timezone.utc).isoformat()
    await _prune_commitment(db, "id", sync_id)
    await db.execute("UPDATE task_sync SET synced = 1, closed_at = ?, notion_status = 'Готово' WHERE id = ?", (now, sync_id))
    await db.commit()


async def mark_pending_analysis(db: aiosqlite.Connection, user_id: int, username: str, category: str) -> None:
    """Flag a conversation as needing task analysis once it settles."""
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT INTO pending_analysis (user_id, username, category, last_activity, dirty)
           VALUES (?, ?, ?, ?, 1)
           ON CONFLICT(user_id) DO UPDATE SET
               username=excluded.username, category=excluded.category,
               last_activity=excluded.last_activity, dirty=1""",
        (user_id, username, category, now),
    )
    await db.commit()


async def get_settled_pending(db: aiosqlite.Connection, settle_minutes: int = 10) -> list[dict]:
    """Conversations that went quiet for N minutes and still need analysis."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=settle_minutes)).isoformat()
    async with db.execute(
        "SELECT user_id, username, category, last_activity FROM pending_analysis WHERE dirty = 1 AND last_activity < ?",
        (cutoff,),
    ) as cur:
        rows = await cur.fetchall()
    return [{"user_id": r[0], "username": r[1], "category": r[2], "last_activity": r[3]} for r in rows]


async def clear_pending_analysis(db: aiosqlite.Connection, user_id: int,
                                 last_activity: Optional[str] = None) -> None:
    """Снять флаг dirty после успешного разбора. Если передан last_activity (снимок на входе
    settle), сбрасываем ТОЛЬКО если активность не менялась — иначе сообщение, пришедшее во время
    долгого LLM-прогона, не затрётся и будет разобрано следующим проходом (без тихой потери)."""
    if last_activity is not None:
        await db.execute(
            "UPDATE pending_analysis SET dirty = 0 WHERE user_id = ? AND last_activity = ?",
            (user_id, last_activity))
    else:
        await db.execute("UPDATE pending_analysis SET dirty = 0 WHERE user_id = ?", (user_id,))
    await db.commit()


async def get_recent_category(db: aiosqlite.Connection, user_id: int, hours: int = 3) -> Optional[str]:
    """Самая свежая категория сообщений этого пользователя за окно N часов.
    Нужна для удешевления: в идущем FRIEND-треде короткие реплики не гоняем через LLM."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    async with db.execute(
        "SELECT category FROM message_log WHERE user_id = ? AND datetime(created_at) >= datetime(?) "
        "ORDER BY created_at DESC LIMIT 1",
        (user_id, since),
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else None


async def has_recent_open_task(db: aiosqlite.Connection, user_id: int, hours: int = 12) -> bool:
    """True if user already has an OPEN task created within N hours — same conversation thread."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    async with db.execute(
        "SELECT 1 FROM task_sync WHERE user_id = ? AND synced = 0 AND datetime(created_at) >= datetime(?) LIMIT 1",
        (user_id, since),
    ) as cur:
        return await cur.fetchone() is not None


async def mark_sync_done(db: aiosqlite.Connection, todoist_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await _prune_commitment(db, "todoist_task_id", todoist_id)
    await db.execute(
        "UPDATE task_sync SET synced = 1, closed_at = ? WHERE todoist_task_id = ?", (now, todoist_id)
    )
    await db.commit()


async def get_recently_closed_tasks(db: aiosqlite.Connection, user_id: int, days: int = 7, hours: int = None) -> list[dict]:
    """Tasks closed recently — candidates to re-open if changes are requested.
    Use `hours` for a tight window (reopen-просьбы приходят сразу, не через дни)."""
    delta = timedelta(hours=hours) if hours is not None else timedelta(days=days)
    cutoff = (datetime.now(timezone.utc) - delta).isoformat()
    async with db.execute(
        "SELECT id, todoist_task_id, notion_page_id, task_title FROM task_sync "
        "WHERE user_id = ? AND synced = 1 AND closed_at >= ?",
        (user_id, cutoff),
    ) as cur:
        rows = await cur.fetchall()
    return [{"id": r[0], "todoist_id": r[1], "notion_page_id": r[2], "title": r[3]} for r in rows]


async def reopen_task_sync(db: aiosqlite.Connection, todoist_id: str) -> None:
    await db.execute(
        "UPDATE task_sync SET synced = 0, closed_at = NULL WHERE todoist_task_id = ?", (todoist_id,)
    )
    await db.commit()


async def reopen_task_sync_by_id(db: aiosqlite.Connection, sync_id: int) -> None:
    """Reopen by row id — works for partial tasks whose todoist_task_id is NULL."""
    await db.execute(
        "UPDATE task_sync SET synced = 0, closed_at = NULL, notion_status = 'В работе' WHERE id = ?", (sync_id,)
    )
    await db.commit()


async def get_open_tasks_with_status(db: aiosqlite.Connection, user_id: int) -> list[dict]:
    """Открытые задачи пользователя + локальный guard notion_status (для перехода Новая→В работе)."""
    async with db.execute(
        "SELECT id, notion_page_id, COALESCE(notion_status, 'Новая') FROM task_sync WHERE user_id = ? AND synced = 0",
        (user_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [{"id": r[0], "notion_page_id": r[1], "notion_status": r[2]} for r in rows]


async def set_task_notion_status(db: aiosqlite.Connection, sync_id: int, status: str) -> None:
    """Запомнить текущий Статус задачи в Notion (локальный guard — чтобы не переписывать каждый раз)."""
    await db.execute("UPDATE task_sync SET notion_status = ? WHERE id = ?", (status, sync_id))
    await db.commit()


async def set_mode(db: aiosqlite.Connection, mode: str) -> None:
    await db.execute(
        "INSERT OR REPLACE INTO settings_kv (key, value) VALUES ('mode', ?)", (mode,)
    )
    await db.commit()
