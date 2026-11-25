# -*- coding: utf-8 -*-
"""
Smart scheduling (v3.1): подобрать свободный слот в календаре под встречу с контактом
и одним тапом создать событие + черновик «Давай {время}?» контакту.

Безопасность: черновик контакту уходит ТОЛЬКО через core.drafts.offer_draft → кнопка 📤
(одобрение владельцем, авто-отправки нет). bc_id берётся из settings_kv (см. handlers.business,
где он сохраняется при входящих business-сообщениях).
"""
import time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from loguru import logger

_pending: dict = {}      # token -> {user_id, username, slots:[(start,end)], mins, ts}
_counter = [0]
_TTL = 3600


def _prune():
    now = time.monotonic()
    for k in [k for k, v in _pending.items() if now - v.get("ts", now) > _TTL]:
        _pending.pop(k, None)


async def compute_slots(settings, period: str = "tomorrow", mins: int = 60) -> list:
    """Свободные окна на день (today/tomorrow) в рабочие часы. → [(start,end), ...]."""
    from core.calendar import get_busy_intervals, compute_free_slots
    from core.util import now_local
    tz = ZoneInfo(settings.timezone)
    now = now_local()
    d = now.date() + (timedelta(days=1) if period == "tomorrow" else timedelta(0))
    if period == "tomorrow":
        ws = datetime.combine(d, dtime(0, 0), tzinfo=tz)
    else:
        ws = now
    we = datetime.combine(d, dtime(23, 59), tzinfo=tz)
    try:
        busy = await get_busy_intervals(
            settings.google_service_account_path, settings.google_calendar_id,
            ws.isoformat(), we.isoformat())
    except Exception as exc:
        logger.error(f"scheduler busy fetch: {exc}")
        busy = []
    windows = compute_free_slots(busy, ws, we, settings.work_start_hour, settings.work_end_hour, mins)
    # Нарезаем свободные ОКНА на дискретные старты (на круглый час, шаг 2ч) — чтобы кнопки
    # были конкретным временем встречи, а не «окном 09:00–21:00».
    out = []
    step = timedelta(hours=2)
    dur = timedelta(minutes=mins)
    for w_s, w_e in windows:
        t = w_s.replace(minute=0, second=0, microsecond=0)
        if t < w_s:
            t += timedelta(hours=1)
        # не раньше «сейчас+30мин» для сегодня
        while t + dur <= w_e:
            out.append((t, t + dur))
            if len(out) >= 8:
                return out
            t += step
    return out


def stash(user_id: int, username: str, slots: list, mins: int) -> str:
    _prune()
    token = __import__('secrets').token_hex(4)   # v4.36 (B2): случайный токен — счётчик с 1 после рестарта путал карточки
    _pending[token] = {"user_id": user_id, "username": username, "slots": slots,
                       "mins": mins, "ts": time.monotonic()}
    return token


def get(token: str):
    return _pending.get(token)


def drop(token: str):
    _pending.pop(token, None)
