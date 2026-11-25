"""
v4.29 Погода (по запросу владельца) — контекстно, а не «будильник-радио»:
- строка в утренней сводке (+ дождь-предупреждения к задачам со временем),
- строка «завтра» в вечернем итоге.
Источник: Open-Meteo — бесплатно, без ключей. Кэш 30 мин. Ноль LLM.
"""
import asyncio
import json
import time
import urllib.request

from loguru import logger

_CACHE = {"ts": 0.0, "data": None}

# WMO weather codes → эмодзи/слово (грубые группы)
_WMO = [
    ((0,), "☀️", "ясно"), ((1, 2), "🌤", "переменно"), ((3,), "☁️", "облачно"),
    ((45, 48), "🌫", "туман"), ((51, 53, 55, 56, 57), "🌦", "морось"),
    ((61, 63, 65, 66, 67, 80, 81, 82), "🌧", "дождь"),
    ((71, 73, 75, 77, 85, 86), "🌨", "снег"), ((95, 96, 99), "⛈", "гроза"),
]


def wmo_emoji(code) -> tuple:
    for codes, e, w in _WMO:
        if code in codes:
            return e, w
    return "🌡", ""


def _fetch(lat: float, lon: float) -> dict:
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
           "&hourly=precipitation_probability,weather_code"
           "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code"
           "&timezone=Europe%2FMoscow&forecast_days=2")
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode())


async def get_forecast(settings) -> dict:
    """Прогноз с кэшем 30 мин. {} при сбое (погода — best-effort, ничего не ломаем)."""
    if _CACHE["data"] and time.time() - _CACHE["ts"] < 1800:
        return _CACHE["data"]
    try:
        lat = getattr(settings, "weather_lat", 55.7558)
        lon = getattr(settings, "weather_lon", 37.6173)
        if not lat:
            return {}
        data = await asyncio.to_thread(_fetch, lat, lon)
        _CACHE.update(ts=time.time(), data=data)
        return data
    except Exception as exc:
        logger.debug(f"weather fetch skip: {exc}")
        return {}


def summary_day(data: dict, day: int = 0) -> str:
    """'🌤 +18…+24, без осадков' / '🌧 +14…+18, дождь (70%)'. day: 0=сегодня, 1=завтра."""
    try:
        d = data["daily"]
        e, w = wmo_emoji(d["weather_code"][day])
        t = f"{round(d['temperature_2m_min'][day]):+d}…{round(d['temperature_2m_max'][day]):+d}"
        p = d["precipitation_probability_max"][day] or 0
        tail = f", {w} ({p}%)" if p >= 40 and w else (f", {w}" if w else "")
        return f"{e} {t}{tail}"
    except Exception:
        return ""


def rain_hours_today(data: dict, threshold: int = 50) -> set:
    """Часы сегодня (0-23) с вероятностью осадков ≥ threshold%."""
    out = set()
    try:
        h = data["hourly"]
        for ts, p in zip(h["time"][:24], h["precipitation_probability"][:24]):
            if (p or 0) >= threshold:
                out.add(int(ts[11:13]))
    except Exception:
        pass
    return out
