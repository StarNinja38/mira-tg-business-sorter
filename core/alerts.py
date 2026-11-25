"""
Owner alerting with rate-limit — surfaces silent degradation
(API failures, budget exceeded) to the owner without spamming.
"""
from loguru import logger


async def alert_owner(bot, db, owner_id: int, key: str, text: str, cooldown_minutes: int = 60) -> None:
    """Send an alert to the owner, throttled per `key` (reuses notification_log)."""
    try:
        from db import can_notify, log_notification
        # notification_log is keyed by (user_id, type); use a synthetic user_id=0 for system alerts
        if not await can_notify(db, 0, key, cooldown_minutes=cooldown_minutes):
            return
        from core.topics import send_owner
        await send_owner(bot, owner_id, "urgent", text)
        await log_notification(db, 0, key)
        logger.warning(f"Owner alerted [{key}]")
    except Exception as exc:
        logger.error(f"alert_owner failed [{key}]: {exc}")
