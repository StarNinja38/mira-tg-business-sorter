"""
Google Calendar integration via service account.
Tasks with a date+time create a calendar event automatically.

Setup (one-time):
1. Google Cloud Console → create project → enable Google Calendar API
2. Create Service Account → download JSON key → put at gcal-service.json
3. Share your calendar with the service account email (Make changes to events)
4. Set GOOGLE_CALENDAR_ID in .env (your calendar email or 'primary')
"""
import asyncio
import json
import re
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from typing import Optional

from loguru import logger
from core.util import now_local


def _norm_summary(s: str) -> str:
    """Нормализация названия для дедупа: вниз-регистр, без эмодзи/❓/пунктуации, схлоп пробелов.
    «❓ Урок английского с @tutor_lena» и «Урок английского с @tutor_lena» → одно и то же."""
    s = (s or "").lower()
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)  # \w оставляет кириллицу/цифры, эмодзи/@ убираются
    return re.sub(r"\s+", " ", s).strip()


_sa_email_cache: dict = {}


def _sa_email(sa_path: str) -> Optional[str]:
    """client_email сервис-аккаунта (creator наших событий) — чтобы дедуп трогал только события бота."""
    if sa_path not in _sa_email_cache:
        try:
            with open(sa_path) as f:
                _sa_email_cache[sa_path] = (json.load(f) or {}).get("client_email")
        except Exception:
            _sa_email_cache[sa_path] = None
    return _sa_email_cache[sa_path]

_service = None
SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _build_service(sa_path: str):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_file(sa_path, scopes=SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _get_service(sa_path: str):
    global _service
    if _service is None:
        if not Path(sa_path).exists():
            return None
        _service = _build_service(sa_path)
    return _service


async def _bg(fn, default, timeout: float = 20.0):
    """C9: googleapiclient — блокирующий; без таймаута зависший сокет держит поток executor и весь
    digest-цикл (сводка/слоты/конфликты). wait_for + default → деградация вместо зависания."""
    try:
        return await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(None, fn), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(f"Google Calendar call timed out after {timeout}s ({getattr(fn, '__qualname__', 'call')})")
        return default


def _normalize_datetime(due: str, tz: str) -> Optional[tuple]:
    """Convert due string to (start_iso, end_iso). Returns None if no usable time."""
    try:
        if "T" in due:  # full datetime "2026-06-08T21:00:00"
            start = datetime.fromisoformat(due)
        elif ":" in due and "-" not in due:  # just time "21:00" → today
            today = now_local()
            h, m = due.split(":")[:2]
            start = today.replace(hour=int(h), minute=int(m[:2]), second=0, microsecond=0)
        else:
            return None  # date-only → no calendar event (it's a task, not a meeting)
        end = start + timedelta(hours=1)
        return start.isoformat(), end.isoformat()
    except Exception:
        return None


async def list_events(sa_path: str, calendar_id: str, time_min_iso: str, time_max_iso: str) -> list:
    """Список событий в окне [time_min, time_max] (RFC3339). Для анти-конфликта. [] при сбое."""
    def _run():
        svc = _get_service(sa_path)
        if not svc:
            return []
        try:
            r = svc.events().list(
                calendarId=calendar_id, timeMin=time_min_iso, timeMax=time_max_iso,
                singleEvents=True, orderBy="startTime", maxResults=10,
            ).execute()
            return [{"summary": e.get("summary", "(без названия)"),
                     "start": (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date"),
                     "end": (e.get("end") or {}).get("dateTime") or (e.get("end") or {}).get("date")}   # C5
                    for e in r.get("items", [])]
        except Exception as exc:
            logger.debug(f"calendar list failed: {exc}")
            return []
    return await _bg(_run, [])


async def list_upcoming_events(sa_path: str, calendar_id: str,
                               time_min_iso: str, time_max_iso: str) -> list:
    """События в окне [time_min, time_max] с id (для брифа-подготовки). [] при сбое.
    Только события с конкретным временем начала (dateTime), без all-day."""
    def _run():
        svc = _get_service(sa_path)
        if not svc:
            return []
        try:
            r = svc.events().list(
                calendarId=calendar_id, timeMin=time_min_iso, timeMax=time_max_iso,
                singleEvents=True, orderBy="startTime", maxResults=20,
            ).execute()
            out = []
            for e in r.get("items", []):
                start = (e.get("start") or {}).get("dateTime")
                if not start:
                    continue  # all-day → пропускаем (не «встреча с временем»)
                out.append({"id": e.get("id"), "summary": e.get("summary", "(без названия)"),
                            "start": start})
            return out
        except Exception as exc:
            logger.debug(f"calendar upcoming failed: {exc}")
            return []
    return await _bg(_run, [])


def _parse_rfc3339(s: str) -> Optional[datetime]:
    """RFC3339 → aware datetime. Терпит 'Z' (UTC). None при сбое."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


async def get_busy_intervals(sa_path: str, calendar_id: str,
                             time_min_iso: str, time_max_iso: str) -> list:
    """Занятые интервалы [(start_dt, end_dt), ...] (aware datetime) через FreeBusy API. [] при сбое."""
    def _run():
        svc = _get_service(sa_path)
        if not svc:
            return []
        try:
            r = svc.freebusy().query(body={
                "timeMin": time_min_iso, "timeMax": time_max_iso,
                "items": [{"id": calendar_id}],
            }).execute()
            busy = (r.get("calendars", {}).get(calendar_id, {}) or {}).get("busy", [])
            out = []
            for b in busy:
                s, e = _parse_rfc3339(b.get("start")), _parse_rfc3339(b.get("end"))
                if s and e:
                    out.append((s, e))
            return out
        except Exception as exc:
            logger.debug(f"freebusy failed: {exc}")
            return []
    return await _bg(_run, [])


def compute_free_slots(busy: list, window_start: datetime, window_end: datetime,
                       work_start_h: int, work_end_h: int, min_minutes: int) -> list:
    """Чистая функция: вычесть занятые интервалы из рабочих часов в окне [window_start, window_end].
    busy — список (start_dt, end_dt) (aware). Возвращает [(free_start, free_end), ...] длиннее min_minutes.
    Без сети — тестируется напрямую."""
    tz = window_start.tzinfo
    busy_sorted = sorted((bs, be) for bs, be in busy if be > bs)
    slots: list = []
    day = window_start.date()
    last_day = window_end.date()
    while day <= last_day:
        day_ws = datetime.combine(day, dtime(hour=work_start_h), tzinfo=tz)
        day_we = datetime.combine(day, dtime(hour=work_end_h), tzinfo=tz)
        seg_start = max(day_ws, window_start)
        seg_end = min(day_we, window_end)
        if seg_start < seg_end:
            cursor = seg_start
            for bs, be in busy_sorted:
                if be <= seg_start or bs >= seg_end:
                    continue
                bs_c = max(bs, seg_start)
                be_c = min(be, seg_end)
                if bs_c > cursor and (bs_c - cursor) >= timedelta(minutes=min_minutes):
                    slots.append((cursor, bs_c))
                cursor = max(cursor, be_c)
            if seg_end > cursor and (seg_end - cursor) >= timedelta(minutes=min_minutes):
                slots.append((cursor, seg_end))
        day += timedelta(days=1)
    return slots


async def check_conflict(sa_path: str, calendar_id: str, tz: str, due: str) -> Optional[str]:
    """Есть ли событие, пересекающееся со встречей в `due`? Возвращает название конфликта или None."""
    times = _normalize_datetime(due, tz)
    if not times:
        return None
    start_iso, end_iso = times
    events = await list_events(sa_path, calendar_id, start_iso, end_iso)
    return events[0]["summary"] if events else None


async def create_calendar_event(
    sa_path: str,
    calendar_id: str,
    timezone: str,
    summary: str,
    due: str,
    description: str = "",
    contact_uid: Optional[int] = None,
) -> Optional[str]:
    """Создать (или переиспользовать) событие Google Calendar. Возвращает event_id или None.

    Идемпотентность (правило проекта №2/№6): перед вставкой ищем СРЕДИ СОБЫТИЙ БОТА за тот же день
    совпадение по стабильному якорю — контакт (`id=<contact_uid>` в описании) или нормализ. название
    (без ❓/эмодзи). Нашли → patch (перенос/обновление существующего), не нашли → insert.
    Снимает дубли от уточнений (UNCLEAR→BUSINESS, плывущий тайтл) и переносов времени.
    События, созданные владельцем вручную, не трогаем (фильтр по creator == сервис-аккаунт)."""
    times = _normalize_datetime(due, timezone)
    if not times:
        return None  # no specific time → skip (task is enough)

    start_iso, end_iso = times
    # Окно того же дня для поиска существующего события (с явной таймзоной — RFC3339 для events().list)
    day_min_iso = day_max_iso = None
    target_dt = None
    try:
        sdt = datetime.fromisoformat(start_iso)
        if sdt.tzinfo is None:  # naive due от LLM → привязать к таймзоне календаря, иначе list 400 → дубль
            try:
                from zoneinfo import ZoneInfo
                sdt = sdt.replace(tzinfo=ZoneInfo(timezone))
            except Exception:
                pass
        target_dt = sdt
        day_min_iso = sdt.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        day_max_iso = sdt.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
    except Exception:
        pass
    bot_email = _sa_email(sa_path)
    norm_target = _norm_summary(summary)

    def _run():
        svc = _get_service(sa_path)
        if not svc:
            return None

        # --- search-before-create ---
        existing_id = None
        if day_min_iso and day_max_iso:
            try:
                r = svc.events().list(
                    calendarId=calendar_id, timeMin=day_min_iso, timeMax=day_max_iso,
                    singleEvents=True, orderBy="startTime", maxResults=50,
                ).execute()
                for e in r.get("items", []):
                    if e.get("status") == "cancelled":
                        continue
                    # дедупим только СВОИ события — ручные события владельца не трогаем
                    if bot_email and (e.get("creator") or {}).get("email") != bot_email:
                        continue
                    desc_e = e.get("description") or ""
                    # контакт: совпадение по ГРАНИЦЕ id (иначе id=12 ложно матчит id=123/1234)
                    same_contact = bool(contact_uid) and re.search(rf"id={contact_uid}(?![0-9])", desc_e)
                    # название: совпадение + БЛИЗКОЕ время (±3ч), иначе две разные встречи одного дня
                    # с типовым названием («Созвон» в 10 и в 18) схлопнулись бы в одну
                    same_title = False
                    if norm_target and _norm_summary(e.get("summary")) == norm_target:
                        if target_dt is None:
                            same_title = True
                        else:
                            e_start = (e.get("start") or {}).get("dateTime")
                            est = _parse_rfc3339(e_start) if e_start else None
                            same_title = (est is None) or abs((est - target_dt).total_seconds()) <= 3 * 3600
                    if same_contact or same_title:
                        existing_id = e.get("id")
                        break
            except Exception as exc:
                logger.debug(f"calendar dedup search failed: {exc}")

        body = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start_iso, "timeZone": timezone},
            "end": {"dateTime": end_iso, "timeZone": timezone},
        }
        if existing_id:
            try:
                svc.events().patch(calendarId=calendar_id, eventId=existing_id, body=body).execute()
                logger.info(f"Calendar event reused (dedup): {summary!r} @ {start_iso} → {existing_id}")
                return existing_id
            except Exception as exc:
                logger.debug(f"calendar dedup patch failed, fallback to insert: {exc}")

        body["reminders"] = {"useDefault": False, "overrides": [
            {"method": "popup", "minutes": 60},
            {"method": "popup", "minutes": 10},
        ]}
        created = svc.events().insert(calendarId=calendar_id, body=body).execute()
        return created.get("id")   # ВОЗВРАЩАЕМ ID (а не link) — чтобы потом обновлять/удалять

    try:
        loop = asyncio.get_running_loop()
        eid = await _bg(_run, None)
        if eid:
            logger.info(f"Calendar event ready: {summary!r} @ {start_iso} (id={eid})")
        return eid
    except Exception as exc:
        logger.error(f"Calendar event creation failed: {exc}")
        return None


async def update_calendar_event(sa_path: str, calendar_id: str, timezone: str,
                                event_id: str, summary: str, due: str) -> bool:
    """Обновить время (и название) существующего события по event_id. True при успехе."""
    times = _normalize_datetime(due, timezone)
    if not times:
        return False
    start_iso, end_iso = times

    def _run():
        svc = _get_service(sa_path)
        if not svc:
            return False
        try:
            svc.events().patch(
                calendarId=calendar_id, eventId=event_id,
                body={"summary": summary,
                      "start": {"dateTime": start_iso, "timeZone": timezone},
                      "end": {"dateTime": end_iso, "timeZone": timezone}},
            ).execute()
            return True
        except Exception as exc:
            logger.debug(f"calendar patch failed ({event_id}): {exc}")
            return False

    try:
        ok = await _bg(_run, False)
        if ok:
            logger.info(f"Calendar event updated: {summary!r} @ {start_iso}")
        return ok
    except Exception as exc:
        logger.error(f"Calendar update failed: {exc}")
        return False


async def delete_calendar_event(sa_path: str, calendar_id: str, event_id: str) -> bool:
    """Удалить событие (при удалении/закрытии задачи). True при успехе."""
    def _run():
        svc = _get_service(sa_path)
        if not svc:
            return False
        try:
            svc.events().delete(calendarId=calendar_id, eventId=event_id).execute()
            return True
        except Exception:
            return False
    try:
        return await _bg(_run, False)
    except Exception:
        return False


async def shift_event(sa_path: str, calendar_id: str, event_id: str, minutes: int) -> Optional[str]:
    """v4.107 виджет встречи: сдвинуть начало и конец на minutes, длительность та же.
    Вернёт новое начало (RFC3339) или None при сбое. Событие без времени (весь день) не трогаем."""
    def _run():
        svc = _get_service(sa_path)
        if not svc:
            return None
        try:
            ev = svc.events().get(calendarId=calendar_id, eventId=event_id).execute()
            s, e = (ev.get("start") or {}), (ev.get("end") or {})
            sd, ed = _parse_rfc3339(s.get("dateTime")), _parse_rfc3339(e.get("dateTime"))
            if not sd or not ed:
                return None
            delta = timedelta(minutes=minutes)
            body = {"start": {**s, "dateTime": (sd + delta).isoformat()},
                    "end": {**e, "dateTime": (ed + delta).isoformat()}}
            svc.events().patch(calendarId=calendar_id, eventId=event_id, body=body).execute()
            return body["start"]["dateTime"]
        except Exception as exc:
            logger.debug(f"calendar shift failed ({event_id}): {exc}")
            return None
    res = await _bg(_run, None)
    if res:
        logger.info(f"Calendar event {event_id} shifted {minutes:+d} min → {res}")
    return res
