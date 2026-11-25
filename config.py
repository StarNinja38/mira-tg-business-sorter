from typing import Literal, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    bot_token: str
    owner_user_id: int
    # Персона бота (v4.16): имя ассистента, женский род, «я»-речь в сообщениях владельцу.
    owner_address: str = "босс"   # v4.56: как Мира обращается к владельцу («босс», не «Миш») — просьба владельца 09.09
    persona_name: str = "Mira"
    # v4.20 чеклист-дайджест: куда слать нативный Telegram-чеклист плана дня. Ограничение
    # Bot API: только диалог business-аккаунта с ДРУГИМ пользователем, у которого недавняя
    # активность (BUSINESS_PEER_USAGE_MISSING иначе; шлём best-effort со скипом). По умолчанию —
    # диалог со вторым аккаунтом владельца @starninjaMAN («личная доска»). 0 = выключено.
    checklist_chat_id: int = 0
    # v4.23: час вечернего закрытия дня («итог: закрыто/висит» с кнопками). 0 = выключено.
    evening_wrap_hour: int = 21
    # v4.26: «переговоры о встрече» — контакт спросил «когда встретимся?» → слоты-кнопки
    # владельцу, по тапу Мира пишет контакту от его имени. Автоотправки нет.
    meeting_negotiate: bool = True
    # v4.28 почта: до 3+ ящиков Gmail через IMAP App-пароли. Формат:
    # EMAIL_ACCOUNTS="user1@gmail.com:app-pass-1, user2@gmail.com:app-pass-2"
    # Пусто = выключено. important_only: слать только помеченные Gmail как важные (\Important).
    email_accounts: str = ""
    email_imap_host: str = "imap.gmail.com"
    email_important_only: bool = True
    email_poll_min: int = 5
    # v4.29 погода (Open-Meteo, без ключей): координаты Москвы; weather_lat=0 → выключено.
    weather_lat: float = 55.7558
    weather_lon: float = 37.6173
    # Имя владельца (как его зовут контакты). Бот использует его в памяти контактов,
    # чтобы НЕ принять обращение контакта к владельцу («Миш, привет») за имя самого контакта.
    owner_name: str = "Миша (Михаил)"
    owner_username: str = "StarNinja38"   # реальный TG-@username владельца (для Notion вместо «me»)
    mode: Literal["observe", "active"] = "observe"
    log_level: str = "INFO"
    db_path: str = "./data/bot.db"

    anthropic_api_key: str
    anthropic_model: str = "claude-haiku-5-5"   # v4.63 (08.10.2026): было claude-haiku-4-5
    # "Smart" model for understanding-critical steps (task extraction, profile memory).
    # Classification stays on the cheap model; this lifts comprehension where it matters.
    anthropic_model_smart: str = "claude-sonnet-5-5"   # v4.61 (30.09.2026): было claude-sonnet-4-5; откат — ANTHROPIC_MODEL_SMART в .env
    # v4.61: effort для Claude 5 в фоновых вызовах (low | medium | high); Мира задаёт своё
    smart_effort: str = "low"
    mira_effort: str = "low"   # v4.61: effort Миры на Claude 5 (адаптивное мышление)

    # Perplexity (sonar) — веб-пробив /whois: ДОПОЛНЕНИЕ к внутренней памяти, только ручной вызов
    perplexity_api_key: Optional[str] = None


    # OpenRouter — ФОЛБЭК для LLM (core/llm): включается только при сбое прямого Anthropic.
    # Hot-path остаётся на прямом Anthropic (кэш = экономия). Без ключа фолбэк выключен.
    openrouter_api_key: Optional[str] = None
    # Diverse-модель (другая лаборатория) для «второго мнения» на ПРОПУЩЕННЫЕ задачи (v4.4):
    # когда Claude извлёк 0, а тред пахнет расписанием — этот проход ловит пропуск. Только
    # предложением-кнопкой, не авто-создание. Через OpenRouter (тем же ключом).
    openrouter_second_model: str = "google/gemini-2.5-flash"

    todoist_api_token: Optional[str] = None
    todoist_project_id: Optional[str] = None
    notion_token: Optional[str] = None
    notion_database_id: Optional[str] = None
    anthropic_monthly_budget: float = 5.0
    timezone: str = "Europe/Moscow"
    notion_contacts_id: Optional[str] = None
    notion_archive_id: Optional[str] = None
    business_reply_template: str = "Спасибо за обращение! Рассмотрю и отвечу в течение 24 часов."
    enable_draft_replies: bool = True   # бот драфтит ответ владельцу на одобрение (отправка только по кнопке)
    daily_photo_limit: int = 30
    google_calendar_id: Optional[str] = None
    google_service_account_path: str = "./gcal-service.json"
    # Рабочие часы для поиска свободных слотов («найди время»)
    work_start_hour: int = 9
    work_end_hour: int = 21
    # За сколько минут до события календаря присылать владельцу бриф-подготовку (0 = выкл)
    meeting_prep_min: int = 30
    todoist_section_business: Optional[str] = None
    todoist_section_meeting: Optional[str] = None
    todoist_section_unclear: Optional[str] = None
    todoist_section_friend: Optional[str] = None
    todoist_section_commitment: Optional[str] = None  # секция «🤝 Обещания» (is_commitment, приоритет)

    # Privacy toggles (default off = current behaviour)
    exclude_friend_from_cloud: bool = False  # FRIEND-текст не уходит в Notion/Obsidian
    redact_pii: bool = False                 # маскировать email/телефоны/карты перед LLM

    # Генератор постов в канал (/post): целевой канал (бот должен быть админом с правом публикации).
    post_channel: str = "@St4rNinja38"
    # Эффект Bot API на КАЖДОМ ответе бота владельцу в личку (Bot API 7.3, только приватный чат).
    # 🔥=5104841245755180586 👍=5107584321108051014 ❤=5159385139981059251 🎉=5046509860389126442. "" = выкл.
    message_effect_id: str = "5104841245755180586"

    # Semantic retrieval (RAG) — off by default. Needs: pip install fastembed numpy.
    # When on, bot recalls the most relevant past messages by meaning, not just recency.
    enable_rag: bool = False
    # v4.44 «Мира-чат 2.0» фаза 1: единый контекст + Sonnet с инструментами (core/mira.py). False = старый роутер.
    mira_v2: bool = False
    mira_api_token: str = ""   # v4.62: вход «Мира в чёлке» (Mac через SSH-туннель); пусто = выключен
    mira_api_port: int = 8787
    webapp_url: str = ""       # v4.73: адрес мини-приложения; пусто = кнопки в меню нет
    github_token: str = ""     # v4.73: подключения GitHub и Vault
    github_owner: str = "StarNinja38"
    vault_repo: str = ""       # owner/repo
    web_search_daily: int = 20
    contact_memory_slice: bool = False  # v4.65: в обновление памяти — выборка по смыслу, не вся память (CONTACT_MEMORY_SLICE=1)
    whisper_model: str = "small"   # v4.50: base давал мусор по-русски; small ~0.5 ГБ int8
    # v4.53 «Мира и соцсети» (этап 1: YouTube + Twitch). Пусто = модуль молчит.
    youtube_api_key: Optional[str] = None
    youtube_channel_id: Optional[str] = None      # UC… или @handle
    twitch_client_id: Optional[str] = None
    twitch_client_secret: Optional[str] = None
    twitch_login: Optional[str] = None
    google_oauth_client_id: Optional[str] = None      # OAuth-клиент «Desktop» для YouTube Analytics (этап 5)
    google_oauth_client_secret: Optional[str] = None
    vidiq_api_key: Optional[str] = None             # v4.97 MCP vidIQ, ключ из app.vidiq.com → MCP
    coinmarketcap_api_key: Optional[str] = None     # v4.100 CoinMarketCap API платный (от $29/мес); без ключа цены из CoinGecko


settings = Settings()
