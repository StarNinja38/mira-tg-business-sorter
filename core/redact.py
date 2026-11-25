# -*- coding: utf-8 -*-
"""v4.106: номера банковских карт не попадают в базу, в модель и в vault.

Bot API 10.4 размечает номер карты сущностью bank_card_number. Её берём, если она есть,
а запасной путь — поиск 13–19 цифр (с пробелами или дефисами) с проверкой по Луну,
чтобы не трогать телефоны и обычные номера заказов. От номера остаются последние 4 цифры:
по ним понятно, о какой карте речь, а сам номер не восстановить.
"""
import re

_CARD_RE = re.compile(r"(?<![\d])(?:\d[ \-]?){12,18}\d(?![\d])")


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        n = ord(ch) - 48
        if alt:
            n *= 2
            if n > 9:
                n -= 9
        total += n
        alt = not alt
    return total % 10 == 0


def _label(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    return f"[карта ••{digits[-4:]}]" if len(digits) >= 4 else "[карта]"


def mask_cards(text, entities=None):
    """Заменить номера карт на «[карта ••1234]». None и пустая строка возвращаются как есть."""
    if not text:
        return text
    # 1) разметка Telegram (смещения в UTF-16), с конца, чтобы не сбить следующие
    spans = []
    for e in entities or ():
        etype = getattr(e, "type", None) or (e.get("type") if isinstance(e, dict) else None)
        if etype != "bank_card_number":
            continue
        off = getattr(e, "offset", None) if not isinstance(e, dict) else e.get("offset")
        ln = getattr(e, "length", None) if not isinstance(e, dict) else e.get("length")
        if off is None or ln is None:
            continue
        spans.append((off, ln))
    if spans:
        u16 = text.encode("utf-16-le")
        for off, ln in sorted(spans, reverse=True):
            a, b = off * 2, (off + ln) * 2
            raw = u16[a:b].decode("utf-16-le", errors="ignore")
            u16 = u16[:a] + _label(raw).encode("utf-16-le") + u16[b:]
        text = u16.decode("utf-16-le", errors="ignore")

    # 2) запасной путь без разметки
    def _sub(m):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return _label(digits)
        return m.group(0)

    return _CARD_RE.sub(_sub, text)
