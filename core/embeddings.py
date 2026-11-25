"""
Optional semantic retrieval (RAG) over conversation history. OFF by default.

Lets the bot pull the most RELEVANT past messages by meaning (not just recency),
so it stays "in context" even about something said long ago.

Enable: pip install fastembed numpy + set ENABLE_RAG=true in .env.
Local CPU model (multilingual, free). All calls are guarded so nothing breaks
when the flag is off or the package isn't installed.
"""
import asyncio
from loguru import logger

_model = None
_lock = asyncio.Lock()
# small multilingual model — knows Russian, ~120MB RAM, 384-dim (без e5-префиксов)
_MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


async def _get_model():
    global _model
    if _model is None:
        async with _lock:
            if _model is None:
                from fastembed import TextEmbedding
                loop = asyncio.get_running_loop()
                logger.info("Loading RAG embedding model (one-time)…")
                _model = await loop.run_in_executor(
                    None, lambda: TextEmbedding(model_name=_MODEL_NAME)
                )
                logger.info("RAG embedding model ready")
    return _model


async def embed(text: str):
    """Return a float32 numpy vector for the text, or None on failure."""
    try:
        model = await _get_model()
        loop = asyncio.get_running_loop()
        vecs = await loop.run_in_executor(None, lambda: list(model.embed([text[:1000]])))
        return vecs[0] if vecs else None
    except Exception as exc:
        logger.warning(f"Embedding failed (RAG disabled for this call): {exc}")
        return None


def pack(vec) -> bytes:
    import numpy as np
    return np.asarray(vec, dtype="float32").tobytes()


def unpack(b):
    import numpy as np
    return np.frombuffer(b, dtype="float32")


def _cosine(a, b) -> float:
    import numpy as np
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


async def retrieve_relevant(db, user_id: int, query: str, k: int = 5, min_score: float = 0.55) -> list[str]:
    """Top-K most semantically relevant past messages for this user. Empty list on any issue."""
    try:
        from db import get_user_vectors
        qv = await embed(query)
        if qv is None:
            return []
        rows = await get_user_vectors(db, user_id, limit=2000)
        scored = []
        for text, blob in rows:
            try:
                score = _cosine(qv, unpack(blob))
            except Exception:
                continue
            if score >= min_score:
                scored.append((score, text))
        scored.sort(reverse=True)
        return [t for _, t in scored[:k]]
    except Exception as exc:
        logger.warning(f"RAG retrieval failed: {exc}")
        return []


async def retrieve_global(db, query: str, k: int = 6, min_score: float = 0.45) -> list[dict]:
    """Глобальный семантический поиск по ВСЕЙ истории (для «спроси память»).
    Возвращает [{who, text, score}] — кто сказал + текст. Пусто при любой проблеме."""
    try:
        from db import get_all_vectors_with_names
        qv = await embed(query)
        if qv is None:
            return []
        rows = await get_all_vectors_with_names(db, limit=6000)
        scored = []
        for text, blob, name, username in rows:
            try:
                score = _cosine(qv, unpack(blob))
            except Exception:
                continue
            if score >= min_score:
                who = name or (f"@{username}" if username else "?")
                scored.append((score, {"who": who, "text": text, "score": round(score, 2)}))
        scored.sort(key=lambda x: -x[0])
        return [d for _, d in scored[:k]]
    except Exception as exc:
        logger.warning(f"RAG global retrieval failed: {exc}")
        return []


async def index_message(db, user_id: int, text: str) -> None:
    """Embed + store one message for later semantic retrieval. No-op on failure."""
    if not text or len(text.strip()) < 8:
        return
    try:
        from db import store_message_vector
        vec = await embed(text)
        if vec is not None:
            await store_message_vector(db, user_id, text[:500], pack(vec))
    except Exception as exc:
        logger.warning(f"RAG index failed: {exc}")
