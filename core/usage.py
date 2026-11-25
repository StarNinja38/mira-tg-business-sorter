"""
Единый учёт расхода LLM. Любой вызов (кроме классификатора, который пишет токены в message_log)
логирует usage в таблицу llm_usage через record(). db регистрируется один раз при старте (set_db),
поэтому call-sites не обязаны протаскивать соединение. Best-effort: сбой записи не ломает поток.
"""
from loguru import logger

_db = None


def set_db(db) -> None:
    """Зарегистрировать общее соединение БД (вызывается в main при старте)."""
    global _db
    _db = db


async def record(model: str, usage, kind: str = "", db=None) -> None:
    """Записать вход/выход токенов одного вызова модели. usage — объект .usage из ответа Anthropic.
    db можно передать явно; иначе берётся зарегистрированный глобально. No-op если нет ни того, ни другого."""
    conn = db or _db
    if conn is None or usage is None:
        return
    try:
        await conn.execute(
            "INSERT INTO llm_usage (model, input_tokens, output_tokens, "
            "cache_creation_tokens, cache_read_tokens, kind) VALUES (?, ?, ?, ?, ?, ?)",
            (model, getattr(usage, "input_tokens", 0) or 0,
             getattr(usage, "output_tokens", 0) or 0,
             getattr(usage, "cache_creation_input_tokens", 0) or 0,   # prompt caching: запись (×1.25)
             getattr(usage, "cache_read_input_tokens", 0) or 0,       # prompt caching: чтение (×0.1)
             kind),
        )
        await conn.commit()
    except Exception as exc:
        logger.debug(f"usage record skip ({kind}): {exc}")
