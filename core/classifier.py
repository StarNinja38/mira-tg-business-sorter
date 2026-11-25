import asyncio
import json
from core import llm
import re
from dataclasses import dataclass
from typing import Optional

from anthropic import AsyncAnthropic
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential
from core.llm import text_of as _llm_text   # v4.61: читать ответ по типу блока (Claude 5 может начать с thinking)

CLASSIFY_SYSTEM = """Ты классифицируешь входящие сообщения в личку Telegram у стримера и контент-мейкера.

Категории:
- SPAM: рекламные предложения, крипта, инвестиции, "уникальные возможности", массовые рассылки, фишинг, попытки развода, мошенничество
- BUSINESS: реальные деловые запросы — коллабы, спонсорство, интервью, предложения работы по контенту, вопросы по стримам/видео от потенциальных партнёров
- FRIEND: дружеское общение, шутки, личное, кто-то знакомый пишет по личному поводу
- UNCLEAR: не подходит ни под одну категорию однозначно, нужен ручной взгляд

Отвечай строго JSON-объектом без markdown-обёрток и без пояснений вокруг."""

CLASSIFY_USER = """Сообщение от @{username} (это сообщение №{msg_number} от этого юзера за всё время).

Текст:
---
{text}
---

Верни JSON с полями:
- category: одна из SPAM, BUSINESS, FRIEND, UNCLEAR
- confidence: число 0.0-1.0
- summary: одно предложение на русском о сути сообщения (для BUSINESS — конкретика, кто и что предлагает)
- reasoning: коротко почему так классифицировал"""

SPAM_KEYWORDS = frozenset({
    "крипт", "usdt", "bitcoin", "btc", "заработ", "инвестиц", "доход",
    "passive income", "trading", "арбитраж", "p2p", "майнинг", "форекс",
})

SPAM_PHRASES = [
    r"представител[ья] компании",
    r"выгодное предложение",
    r"уникальная возможность",
    r"сотрудничество за процент",
    r"работа на дому",
    r"пассивный доход",
    r"финансовая свобода",
    r"быстрый заработок",
]

GREETING_WORDS = ("привет", "хай", "hi", "hello", "здарова", "йо", "ку")

# Casual short phrases that are clearly not business — skip LLM entirely
CASUAL_PATTERNS = re.compile(
    r"^(ты где|где ты|ты как|как ты|что делаешь|чем занят|ты дома|едешь|выехал|скоро|"
    r"выходи|выходишь|выхожу|иди|иду|иду уже|жди меня|жди|ждём|ждёшь|"
    r"приходи|приду|пришёл|пришла|уже иду|уже еду|уже вышел|уже вышла|"
    r"открой|открывай|открываю|зайди|зайду|захожу|"
    r"ок|окей|okay|ok|хорошо|понял|понятно|угу|ага|да|нет|не|ладно|давай|"
    r"спасибо|спс|thx|thanks|пасиб|"
    r"удачи|пока|до свидания|bye|good luck|увидимся|увидимся скоро|"
    r"лол|лмао|ха|хаха|😂|👍|👎|❤️|🔥|💯|✅|"
    r"жду|жди|скоро буду|еду|едем|едешь|"
    r"ладно|договорились|договорились|окей договорились|"
    r"норм|нормально|всё норм|всё хорошо|хорошо спасибо)[?!.\s]*$",
    re.IGNORECASE,
)

URL_RE = re.compile(r"https?://\S+|t\.me/\S+|bit\.ly/\S+|www\.\S+", re.IGNORECASE)
INVITE_RE = re.compile(r"t\.me/\+|t\.me/joinchat", re.IGNORECASE)
FLAG_EMOJI_RE = re.compile(r"^[\U0001F1E0-\U0001F1FF\d\s]+$")
_SPAM_PHRASE_RE = [re.compile(p, re.IGNORECASE) for p in SPAM_PHRASES]

# Признак «дела» в длинном пересланном посте из канала: дата / время / призыв к действию /
# дедлайн / событие. Если есть — НЕ глушим пост как SPAM, а пускаем в обычную классификацию
# и settle-разбор (чтобы из анонса/приглашения создалась задача). Чистый репост статьи/новости
# без таких сигналов остаётся SPAM (как раньше).
_FWD_ACTION_RE = re.compile(
    r"\b\d{1,2}[:.]\d{2}\b"                                   # время 19:00 / 19.00
    r"|\b\d{1,2}\s?(?:январ|феврал|март|апрел|ма[яй]|июн|июл|август|сентябр|октябр|ноябр|декабр)"  # 20 марта
    r"|\b(?:сегодня|завтра|послезавтра|в выходн|на выходн|на этой неделе|на следующей неделе"
    r"|в понедельник|во вторник|в сред|в четверг|в пятниц|в суббот|в воскресен)\b"
    r"|регистрац|запис(?:ь|аться|ыва)|приход(?:и|ите)|успе(?:й|ть|вай)|дедлайн|до \d{1,2}\b"
    r"|билет|оплат(?:а|и|ить)|купи\b|закаж|брониру|не забуд|подай заявк|заявк"
    r"|митап|созвон|вебинар|лекци|мастер-класс|воркшоп|собеседован",
    re.IGNORECASE,
)


def _fwd_has_action_signal(text: str) -> bool:
    """Есть ли в пересланном посте признак «дела» (дата/время/призыв/дедлайн/событие)."""
    return bool(_FWD_ACTION_RE.search(text))


_SEMAPHORE = asyncio.Semaphore(5)


@dataclass
class ClassificationResult:
    category: str
    confidence: float
    summary: str
    reasoning: str
    rule_matched: Optional[str] = None
    llm_latency_ms: Optional[int] = None
    llm_tokens_input: Optional[int] = None
    llm_tokens_output: Optional[int] = None


def _apply_rules(
    text: str,
    has_username: bool,
    has_photo: bool,
    msg_number: int,
    is_forwarded_from_channel: bool,
    thread_category: Optional[str] = None,
    trusted_sender: bool = False,
    established_category: Optional[str] = None,
) -> Optional[ClassificationResult]:
    lower = text.lower()
    has_url = bool(URL_RE.search(text))
    has_spam_keyword = any(kw in lower for kw in SPAM_KEYWORDS)

    if has_url and has_spam_keyword:
        return ClassificationResult(
            category="SPAM", confidence=0.95,
            summary="Ссылка + спам-ключевые слова",
            reasoning="URL + spam keyword match",
            rule_matched="url+keyword",
        )

    if INVITE_RE.search(text):
        return ClassificationResult(
            category="SPAM", confidence=0.95,
            summary="Приглашение в закрытый чат",
            reasoning="private chat invite link",
            rule_matched="invite_link",
        )

    for phrase_re in _SPAM_PHRASE_RE:
        if phrase_re.search(lower):
            return ClassificationResult(
                category="SPAM", confidence=0.9,
                summary=f"Спам-фраза обнаружена",
                reasoning=f"spam phrase: {phrase_re.pattern}",
                rule_matched="spam_phrase",
            )

    if is_forwarded_from_channel and len(text) > 200:
        # Длинный репост из канала: по умолчанию шум. НО не глушим, если есть признак «дела»
        # (дата/время/призыв/дедлайн/событие) ИЛИ отправитель доверенный (whitelisted/частый друг —
        # тогда пересланная им игра/анонс идёт в разбор, а не в SPAM). Холодные каналы/незнакомцы — шум.
        if not _fwd_has_action_signal(text) and not trusted_sender:
            return ClassificationResult(
                category="SPAM", confidence=0.85,
                summary="Длинный пересланный пост из канала",
                reasoning="forwarded channel message > 200 chars, no action signal",
                rule_matched="forwarded_channel_long",
            )
        # есть «дело» → проваливаемся к LLM (и затем в очередь разбора задач)

    if not has_username and not has_photo and msg_number == 1 and len(text) > 100:
        return ClassificationResult(
            category="SPAM", confidence=0.8,
            summary="Аноним без аватарки — длинное первое сообщение",
            reasoning="no username + no photo + long first message",
            rule_matched="anon_long_first_msg",
        )

    if FLAG_EMOJI_RE.match(text.strip()) and len(text.strip()) > 0:
        return ClassificationResult(
            category="SPAM", confidence=0.8,
            summary="Только флаги стран и цифры",
            reasoning="flag emojis + digits only",
            rule_matched="flags_digits",
        )

    stripped = text.strip()

    # Short greeting or emoji-only
    if len(stripped) <= 15:
        lower_stripped = stripped.lower()
        has_greeting = any(g in lower_stripped for g in GREETING_WORDS)
        is_emoji_only = bool(stripped) and not any(c.isalnum() for c in stripped)
        if has_greeting or is_emoji_only:
            return ClassificationResult(
                category="FRIEND", confidence=0.8,
                summary="Короткое приветствие или эмодзи",
                reasoning="short greeting/emoji <= 15 chars",
                rule_matched="short_greeting",
            )

    # Casual short message — clearly not business, skip LLM
    if len(stripped) <= 60 and CASUAL_PATTERNS.match(stripped):
        return ClassificationResult(
            category="FRIEND", confidence=0.85,
            summary="Бытовое сообщение",
            reasoning="casual pattern match, no LLM needed",
            rule_matched="casual_no_llm",
        )

    # Идущий дружеский тред: короткая чистая реплика → FRIEND без LLM (экономия на болтовне).
    # Извлечение задач/память дальше всё равно отработают по затиханию — здесь только классификация.
    if (thread_category == "FRIEND" and len(stripped) <= 40
            and not has_url and not has_spam_keyword and not is_forwarded_from_channel):
        return ClassificationResult(
            category="FRIEND", confidence=0.8,
            summary="Короткая реплика в идущем дружеском диалоге",
            reasoning="active FRIEND thread + short clean message — no LLM",
            rule_matched="friend_thread",
        )

    # УСТОЯВШИЙСЯ FRIEND-контакт (≥30 классифицированных, ≥90% FRIEND) → наследуем категорию
    # для чистых сообщений без LLM. P1.3: у Ники 72 UNCLEAR из 118 при 4214× FRIEND — LLM
    # игнорировал известную роль на коротких эмоциональных репликах («Я расстроена», «Роснефть»).
    # Только FRIEND (безопасно: не блокирует задачи, в отличие от SPAM); извлечение/память дальше работают.
    if (established_category == "FRIEND" and len(stripped) <= 300
            and not has_url and not has_spam_keyword and not is_forwarded_from_channel):
        return ClassificationResult(
            category="FRIEND", confidence=0.85,
            summary=stripped[:200],
            reasoning="established FRIEND contact (≥90% history) + clean message — inherited, no LLM",
            rule_matched="established_friend",
        )

    return None


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
async def _call_api(client: AsyncAnthropic, model: str, user_prompt: str):
    return await llm.create(client, 
        model=model,
        max_tokens=300,
        system=CLASSIFY_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
    )


async def classify(
    client: AsyncAnthropic,
    model: str,
    text: Optional[str],
    username: str,
    msg_number: int,
    has_username: bool,
    has_photo: bool,
    is_forwarded_from_channel: bool,
    over_budget: bool = False,
    corrections: Optional[list] = None,
    context: Optional[str] = None,
    thread_category: Optional[str] = None,
    trusted_sender: bool = False,
    established_category: Optional[str] = None,
) -> ClassificationResult:
    if not text:
        return ClassificationResult(
            category="UNCLEAR", confidence=0.5,
            summary="Нет текста (медиа-сообщение)",
            reasoning="no text content",
            rule_matched="no_text",
        )

    rule_result = _apply_rules(
        text=text,
        has_username=has_username,
        has_photo=has_photo,
        msg_number=msg_number,
        is_forwarded_from_channel=is_forwarded_from_channel,
        thread_category=thread_category,
        trusted_sender=trusted_sender,
        established_category=established_category,
    )
    if rule_result is not None:
        logger.debug(f"Rule [{rule_result.rule_matched}] → {rule_result.category}")
        return rule_result

    # Budget circuit-breaker: skip LLM entirely when over budget
    if over_budget:
        logger.warning(f"Budget exceeded — skip LLM for @{username}, fallback UNCLEAR")
        return ClassificationResult(
            category="UNCLEAR", confidence=0.0,
            summary=text[:200],
            reasoning="monthly budget exceeded — LLM skipped",
            rule_matched="budget_exceeded",
        )

    logger.debug(f"No rule for @{username}, calling Anthropic API…")
    from core.util import maybe_redact
    user_prompt = CLASSIFY_USER.format(
        username=username or "unknown",
        msg_number=msg_number,
        text=maybe_redact(text[:2000]),
    )
    # Conversation context — короткие реплики понятны в потоке диалога
    if context:
        user_prompt = (
            f"Контекст переписки (для понимания, не классифицируй его):\n{maybe_redact(context[:1500])}\n\n"
            + user_prompt
            + "\n\nЕсли это продолжение живого дружеского диалога и реплика короткая/бытовая — это FRIEND, не UNCLEAR."
        )
    # Few-shot from owner corrections — improves accuracy over time
    if corrections:
        examples = "\n".join(
            f'- "{c["text"][:120]}" → {c["correct_category"]}' for c in corrections[:15]
        )
        user_prompt = (
            f"Примеры ручных исправлений владельца (учитывай их стиль):\n{examples}\n\n"
            + user_prompt
        )

    try:
        async with _SEMAPHORE:
            loop = asyncio.get_running_loop()
            start = loop.time()
            response = await _call_api(client, model, user_prompt)
            latency_ms = int((loop.time() - start) * 1000)

        from core.util import loads_loose
        data = loads_loose(_llm_text(response))
        category = data.get("category", "UNCLEAR").upper()
        if category not in ("SPAM", "BUSINESS", "FRIEND", "UNCLEAR"):
            category = "UNCLEAR"

        logger.debug(f"LLM → {category} ({data.get('confidence', 0):.2f}) [{latency_ms}ms]")
        return ClassificationResult(
            category=category,
            confidence=float(data.get("confidence", 0.5)),
            summary=data.get("summary", ""),
            reasoning=data.get("reasoning", ""),
            rule_matched="llm",
            llm_latency_ms=latency_ms,
            llm_tokens_input=response.usage.input_tokens,
            llm_tokens_output=response.usage.output_tokens,
        )
    except Exception as exc:
        logger.error(f"Anthropic API failed: {exc} — fallback UNCLEAR")
        return ClassificationResult(
            category="UNCLEAR", confidence=0.0,
            summary=text[:200],
            reasoning=f"API error: {exc}",
            rule_matched="llm_error",
        )
