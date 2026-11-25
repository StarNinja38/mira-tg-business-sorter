# -*- coding: utf-8 -*-
"""
Мультиаккаунт (v3.3): бот может обслуживать НЕСКОЛЬКО business-аккаунтов владельца.
Любой аккаунт, подключивший бота (business_connection), считается «владельцем» — чтобы
его ИСХОДЯЩИЕ не принимались за входящие от контакта. Уведомления при этом идут в ОДИН
основной аккаунт (settings.owner_user_id) — единый инбокс.

In-memory кэш (быстрый hot-path), персист в settings_kv ('owner_accounts' = JSON {uid: bc_id}).
"""
import json
from loguru import logger

_ids: set = set()      # вторичные аккаунты-владельцы (кроме основного)
_bc: dict = {}         # user_id -> business_connection_id (для отправки в нужный аккаунт)
_labels: dict = {}     # user_id -> человекочитаемая метка («Личный», «@user», «акк …»)
_KEY = "owner_accounts"
_KEY_LBL = "owner_labels"


async def load(db) -> None:
    """Загрузить аккаунты из settings_kv в кэш (на старте бота)."""
    global _ids, _bc, _labels
    try:
        from db import get_setting
        raw = await get_setting(db, _KEY)
        data = json.loads(raw) if raw else {}
        _bc = {int(k): v for k, v in data.items()}
        _ids = set(_bc.keys())
        rawl = await get_setting(db, _KEY_LBL)
        _labels = {int(k): v for k, v in (json.loads(rawl) if rawl else {}).items()}
        if _ids:
            logger.info(f"Owner accounts loaded: {sorted(_ids)} labels={_labels}")
    except Exception as exc:
        logger.warning(f"owners.load failed: {exc}")


def account_for_bc(bc_id):
    """Обратный поиск: какой аккаунт-владелец стоит за этим business_connection_id."""
    if not bc_id:
        return None
    for uid, b in _bc.items():
        if b and b == bc_id:
            return uid
    return None


def label(account_id, primary_id: int) -> str:
    """Человекочитаемая метка аккаунта. Для основного — пусто (он по умолчанию)."""
    if account_id is None or account_id == primary_id:
        return ""
    return _labels.get(account_id) or f"акк {str(account_id)[-4:]}"


def tag(account_id, primary_id: int) -> str:
    """Хвост-метка источника для уведомлений: «» для основного, « · 📲 <label>» для второго."""
    lbl = label(account_id, primary_id)
    return f" · 📲 {lbl}" if lbl else ""


async def set_label(db, account_id: int, lbl: str) -> None:
    global _labels
    _labels[account_id] = lbl
    try:
        from db import set_setting
        await set_setting(db, _KEY_LBL, json.dumps({str(k): v for k, v in _labels.items()}))
    except Exception:
        pass


def is_owner(primary_id: int, user_id: int) -> bool:
    """True, если сообщение от владельца (основной аккаунт ИЛИ любой подключивший бота)."""
    return user_id == primary_id or user_id in _ids


def bc_for(user_id):
    """business_connection_id аккаунта (если это аккаунт-владелец)."""
    return _bc.get(user_id)


def secondary_ids() -> list:
    """Список вторичных аккаунтов-владельцев (для /owner list)."""
    return sorted(_ids)


async def upsert(db, user_id: int, bc_id: str, enabled: bool) -> None:
    """Подключение/отключение бота аккаунтом (из business_connection-апдейта)."""
    global _ids, _bc
    if enabled:
        _bc[user_id] = bc_id
        _ids.add(user_id)
    else:
        _bc.pop(user_id, None)
        _ids.discard(user_id)
    try:
        from db import set_setting
        await set_setting(db, _KEY, json.dumps({str(k): v for k, v in _bc.items()}))
    except Exception as exc:
        logger.warning(f"owners.upsert persist failed: {exc}")
    logger.info(f"Owner account {'enabled' if enabled else 'disabled'}: {user_id} (всего: {sorted(_ids)})")
