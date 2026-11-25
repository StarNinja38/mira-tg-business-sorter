#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Smoke-тесты чистых функций ядра (без внешних API).
Запуск на сервере:  cd /opt/tg-business-sorter && ./venv/bin/python -m unittest tests.test_core -v
Покрывают то, что уже ловили багами: дедуп, окно дат (datetime-фикс), форматирование дат.
"""
import os
import sys
import asyncio
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestTitleDedup(unittest.TestCase):
    def test_similar_rephrasings(self):
        from db import _titles_similar
        # ≥2 общих контентных токена (после удаления стоп-слов) → схлопывается
        self.assertTrue(_titles_similar(
            "Поехать с @friend_max в вуз", "поехать с @friend_max в вуз завтра"))
        self.assertTrue(_titles_similar(
            "Зайти в Discord и обсудить с @friend_max", "зайти в discord обсудить с @friend_max"))

    def test_conservative_single_token(self):
        from db import _titles_similar
        # Одно общее контентное слово — НЕ схлопываем (консервативно; ловит семантический судья)
        self.assertFalse(_titles_similar("Помочь с курсовой", "Помочь с защитой"))

    def test_different_tasks_not_similar(self):
        from db import _titles_similar
        self.assertFalse(_titles_similar("Купить молоко", "Позвонить маме"))
        self.assertFalse(_titles_similar("Созвон", "Созвониться позже вечером"))

    def test_within_batch_dedup_same_title_different_day(self):
        """Fix #1: within-batch дедуп = _titles_similar AND not _diff_day.
        Два урока с ОДИНАКОВЫМ title на РАЗНЫЕ дни — НЕ дубль (обе создаются);
        тот же день — дубль; обе без даты — дубль (консервативно, как раньше)."""
        from db import _titles_similar
        from core.actions import _diff_day
        t = "Урок английского с @tutor_lena"
        is_dup = lambda da, db_: _titles_similar(t, t) and not _diff_day(da, db_)
        # разные дни — вторая задача НЕ должна отсеяться (главный кейс бага)
        self.assertFalse(is_dup("2026-07-08T14:00", "2026-07-10T21:00"))
        # тот же день — дубль
        self.assertTrue(is_dup("2026-07-08T14:00", "2026-07-08T18:00"))
        # обе без даты — дубль (прежнее поведение)
        self.assertTrue(is_dup(None, None))

    def test_normalize_strips_punct_emoji(self):
        from db import _normalize_title
        self.assertEqual(_normalize_title("❓ Встреча!!!"), _normalize_title("встреча"))


class TestAvatarRefresh(unittest.TestCase):
    """Логика обновления фото памяти из аватара (v3.5.1)."""
    def test_manual_not_overwritten(self):
        from core.photos import should_refresh_avatar
        self.assertFalse(should_refresh_avatar(True, "manual", "u1", "u2"))

    def test_confirmed_not_overwritten(self):
        from core.photos import should_refresh_avatar
        self.assertFalse(should_refresh_avatar(True, "confirmed", "u1", "u2"))

    def test_no_photo_sets_avatar(self):
        from core.photos import should_refresh_avatar
        self.assertTrue(should_refresh_avatar(False, None, None, "u2"))

    def test_avatar_changed_updates(self):
        from core.photos import should_refresh_avatar
        self.assertTrue(should_refresh_avatar(True, "avatar", "u1", "u2"))

    def test_avatar_unchanged_skips(self):
        from core.photos import should_refresh_avatar
        self.assertFalse(should_refresh_avatar(True, "avatar", "u1", "u1"))

    def test_legacy_null_src_refreshes(self):
        from core.photos import should_refresh_avatar
        # старое авто-фото без src и без unique_id → подхватить и начать отслеживать
        self.assertTrue(should_refresh_avatar(True, None, None, "u2"))


class TestCategoryFromRelationship(unittest.TestCase):
    """Категория задачи от роли контакта, а не от разовой классификации (v3.8.4)."""
    def test_business_roles(self):
        from core.actions import _category_from_relationship
        for r in ["коллега", "клиент", "деловой партнёр"]:
            self.assertEqual(_category_from_relationship(r), "BUSINESS", r)

    def test_friend_roles(self):
        from core.actions import _category_from_relationship
        for r in ["семья", "приятель", "друг", "фанат", "подписчик"]:
            self.assertEqual(_category_from_relationship(r), "FRIEND", r)

    def test_unknown_keeps_none(self):
        from core.actions import _category_from_relationship
        for r in ["сосед", "незнакомец", None, ""]:
            self.assertIsNone(_category_from_relationship(r), r)


class TestOwnerCommitSignal(unittest.TestCase):
    """Исходящее владельца с обязательством армит settle-разбор (v3.8.3)."""
    def _m(self, t):
        from handlers.business import _OWNER_COMMIT_RE
        return bool(_OWNER_COMMIT_RE.search(t))

    def test_commit_phrases_match(self):
        for t in ["Тогда через полтора - два часа буду", "в понедельник отдам плиту",
                  "привезу завтра", "приеду после 13", "готов отдать", "пришлю в 18:00"]:
            self.assertTrue(self._m(t), t)

    def test_smalltalk_no_match(self):
        for t in ["Хорошо", "ок спасибо", "понял тебя", "ага"]:
            self.assertFalse(self._m(t), t)


class TestDiffDay(unittest.TestCase):
    """Разные дни одного дела = разные задачи (не дубль) — v3.8."""
    def test_different_days_are_diff(self):
        from core.actions import _diff_day
        self.assertTrue(_diff_day("2026-06-16T21:00", "2026-06-19T18:00"))
        self.assertTrue(_diff_day("2026-06-16", "2026-06-19"))

    def test_same_day_not_diff(self):
        from core.actions import _diff_day
        self.assertFalse(_diff_day("2026-06-16T21:00", "2026-06-16T09:00"))
        self.assertFalse(_diff_day("2026-06-16", "2026-06-16T09:00"))

    def test_missing_date_not_diff(self):
        from core.actions import _diff_day
        self.assertFalse(_diff_day(None, "2026-06-19"))
        self.assertFalse(_diff_day("2026-06-19", None))
        self.assertFalse(_diff_day(None, None))


class TestForwardedActionSignal(unittest.TestCase):
    """Длинный пересланный пост → задача только если есть признак «дела» (v3.7)."""
    def test_time_is_action(self):
        from core.classifier import _fwd_has_action_signal
        self.assertTrue(_fwd_has_action_signal("Большой анонс митапа, начало в 19:00, ждём всех"))

    def test_date_is_action(self):
        from core.classifier import _fwd_has_action_signal
        self.assertTrue(_fwd_has_action_signal("Лекция состоится 20 марта в нашем пространстве"))

    def test_callword_is_action(self):
        from core.classifier import _fwd_has_action_signal
        self.assertTrue(_fwd_has_action_signal("Открыта регистрация на воркшоп, успей записаться"))

    def test_event_is_action(self):
        from core.classifier import _fwd_has_action_signal
        self.assertTrue(_fwd_has_action_signal("Приглашаем на вебинар по контенту для стримеров"))

    def test_plain_repost_no_action(self):
        from core.classifier import _fwd_has_action_signal
        self.assertFalse(_fwd_has_action_signal(
            "Сегодняшние новости индустрии: компания выпустила обновление, "
            "аналитики обсуждают рынок и делятся мнениями о трендах развития."))


class TestDueFormatting(unittest.TestCase):
    def test_normalize_due(self):
        from core.actions import _normalize_due
        self.assertIsNone(_normalize_due(None))
        self.assertEqual(_normalize_due("2026-06-10"), "2026-06-10")
        self.assertIn("T15:00", _normalize_due("15:00"))  # bare time → +дата сегодня
        # v4.111: перенос «2026-11-19 18:30» через пробел — календарь двигает только формат с «T»
        self.assertEqual(_normalize_due("2026-11-19 18:30"), "2026-11-19T18:30")
        self.assertEqual(_normalize_due("2026-11-19 9:05:00"), "2026-11-19T09:05")
        self.assertEqual(_normalize_due("2026-11-19T18:30"), "2026-11-19T18:30")

    def test_todoist_due_datetime_tz(self):
        from core.actions import _todoist_due_datetime
        out = _todoist_due_datetime("2026-06-10T20:00")
        # должно быть со смещением МСК (+03:00), не голый UTC
        self.assertTrue(out.endswith("+03:00"), out)
        self.assertIn("2026-06-10T20:00:00", out)

    def test_human_due(self):
        from core.util import human_due
        self.assertEqual(human_due(""), "")
        self.assertEqual(human_due("2026-12-25"), "25 декабря")
        out = human_due("2026-12-25T09:30")
        self.assertIn("09:30", out)
        self.assertIn("декабря", out)


class TestCommitmentHeuristic(unittest.TestCase):
    """Фолбэк is_commitment: обещание = владелец что-то ДОЛЖЕН сделать ДЛЯ человека."""
    OWNER = 111222333

    def test_commit_verb_for_contact(self):
        from core.actions import _looks_like_commitment
        self.assertTrue(_looks_like_commitment("Привезти плиту @Galya", 111, self.OWNER))
        self.assertTrue(_looks_like_commitment("Отвезти папу в клинику", 222, self.OWNER))
        self.assertTrue(_looks_like_commitment("Ответить @ivan по заказу", 333, self.OWNER))

    def test_self_task_not_commitment(self):
        from core.actions import _looks_like_commitment
        # self-задача владельца (user_id == owner) — не обещание человеку
        self.assertFalse(_looks_like_commitment("Купить перчатки", self.OWNER, self.OWNER))

    def test_no_verb_not_commitment(self):
        from core.actions import _looks_like_commitment
        self.assertFalse(_looks_like_commitment("Встретиться с @sk8", 444, self.OWNER))
        self.assertFalse(_looks_like_commitment("Посмотреть видео", 444, self.OWNER))


class TestUpdateBatchParse(unittest.TestCase):
    """Батч-проверка обновлений: парсер маппит индексы ответа на названия задач."""
    def test_maps_indices_to_titles(self):
        from core.actions import _parse_update_batch
        titles = ["Встреча с @a", "Урок с @b", "Купить X"]
        raw = '[{"i":1,"should_update":false,"updated_due_date":null},{"i":2,"should_update":true,"updated_due_date":"2026-07-01"}]'
        out = _parse_update_batch(raw, titles)
        # v4.36 (B5): ключ — индекс задачи (дубли названий «Урок» на разные дни больше не сливаются)
        self.assertFalse(out[0]["should_update"])
        self.assertTrue(out[1]["should_update"])
        self.assertEqual(out[1]["updated_due_date"], "2026-07-01")
        self.assertNotIn(2, out)

    def test_out_of_range_and_garbage_ignored(self):
        from core.actions import _parse_update_batch
        self.assertEqual(_parse_update_batch('[{"i":9,"should_update":true}]', ["A"]), {})
        self.assertEqual(_parse_update_batch('не json', ["A"]), {})


class TestRecentWindow(unittest.IsolatedAsyncioTestCase):
    """Главный регресс-тест: окно дат через datetime() (баг v1.8.1 — формат пробел vs T)."""

    async def asyncTest_setup_db(self):
        import aiosqlite
        db = await aiosqlite.connect(":memory:")
        await db.execute(
            "CREATE TABLE task_sync (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "todoist_task_id TEXT, notion_page_id TEXT, synced INTEGER DEFAULT 0, "
            "created_at TIMESTAMP, task_title TEXT, user_id INTEGER, closed_at TIMESTAMP, "
            "topic TEXT DEFAULT '', notion_status TEXT DEFAULT 'Новая', due_date TEXT)"
        )
        return db

    async def test_same_day_row_found(self):
        from db import task_exists_recently
        db = await self.asyncTest_setup_db()
        # строка создана 18ч назад, формат CURRENT_TIMESTAMP (пробел) — раньше выпадала из окна
        ts = (datetime.now(timezone.utc) - timedelta(hours=18)).strftime("%Y-%m-%d %H:%M:%S")
        await db.execute(
            "INSERT INTO task_sync (created_at, task_title, user_id) VALUES (?, ?, ?)",
            (ts, "Поехать с @friend_max в вуз", 1),
        )
        await db.commit()
        # должна найтись (в окне 24ч, похожее название) — валидирует datetime()-фикс
        self.assertTrue(await task_exists_recently(db, 1, "поехать с @friend_max в вуз"))
        # старше окна (30ч) — не находится
        old = (datetime.now(timezone.utc) - timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")
        await db.execute(
            "INSERT INTO task_sync (created_at, task_title, user_id) VALUES (?, ?, ?)",
            (old, "Купить вейп для @x", 2),
        )
        await db.commit()
        self.assertFalse(await task_exists_recently(db, 2, "купить вейп для @x"))
        await db.close()

    async def test_dated_not_dup_against_undated_placeholder(self):
        # Кейс @tutor_lena: бездатная ❓-заготовка не должна гасить датированные конкретизации.
        from db import task_exists_recently
        db = await self.asyncTest_setup_db()
        ts = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        await db.execute(
            "INSERT INTO task_sync (created_at, task_title, user_id, due_date) VALUES (?, ?, ?, ?)",
            (ts, "❓ Урок английского с @tutor_lena", 7, None),
        )
        await db.commit()
        # датированная «Урок английского» против бездатной заготовки → НЕ дубль (конкретизация)
        self.assertFalse(await task_exists_recently(db, 7, "Урок английского с @tutor_lena", due_date="2026-06-24T17:00"))
        # бездатный кандидат против бездатной заготовки → всё ещё дубль (не плодим расплывчатые)
        self.assertTrue(await task_exists_recently(db, 7, "Урок английского с @tutor_lena"))
        await db.close()


class TestFreeSlots(unittest.TestCase):
    """«найди время»: чистый расчёт свободных окон из занятых интервалов в рабочих часах."""

    def _dt(self, h, m=0, day=10):
        from zoneinfo import ZoneInfo
        return datetime(2026, 6, day, h, m, tzinfo=ZoneInfo("Europe/Moscow"))

    def test_gaps_between_busy(self):
        from core.calendar import compute_free_slots
        busy = [(self._dt(11), self._dt(12)), (self._dt(15), self._dt(16, 30))]
        slots = compute_free_slots(busy, self._dt(9), self._dt(21), 9, 21, 60)
        starts = [(s.hour, s.minute) for s, _ in slots]
        self.assertEqual(starts, [(9, 0), (12, 0), (16, 30)])

    def test_respects_min_duration(self):
        from core.calendar import compute_free_slots
        # 09:00–09:30 (30м) отсекается порогом 60м; 20:00–21:00 (60м) остаётся
        busy = [(self._dt(9, 30), self._dt(20))]
        slots = compute_free_slots(busy, self._dt(9), self._dt(21), 9, 21, 60)
        self.assertEqual(len(slots), 1)
        self.assertEqual((slots[0][0].hour, slots[0][1].hour), (20, 21))

    def test_clips_to_working_hours(self):
        from core.calendar import compute_free_slots
        # окно — целые сутки, но рабочие часы 9–21, занятого нет
        slots = compute_free_slots([], self._dt(0), self._dt(23, 59), 9, 21, 60)
        self.assertEqual(len(slots), 1)
        self.assertEqual((slots[0][0].hour, slots[0][1].hour), (9, 21))

    def test_overlapping_busy(self):
        from core.calendar import compute_free_slots
        # пересекающиеся 11–13 и 12–14 → слитно 11–14; свободно 09–11 и 14–21
        busy = [(self._dt(11), self._dt(13)), (self._dt(12), self._dt(14))]
        slots = compute_free_slots(busy, self._dt(9), self._dt(21), 9, 21, 60)
        self.assertEqual([(s.hour, e.hour) for s, e in slots], [(9, 11), (14, 21)])

    def test_nested_busy(self):
        from core.calendar import compute_free_slots
        # вложенный 12–13 внутри 11–15 → свободно 09–11 и 15–21 (не выдать слот внутри занятого)
        busy = [(self._dt(11), self._dt(15)), (self._dt(12), self._dt(13))]
        slots = compute_free_slots(busy, self._dt(9), self._dt(21), 9, 21, 60)
        self.assertEqual([(s.hour, e.hour) for s, e in slots], [(9, 11), (15, 21)])


class TestLLMCost(unittest.TestCase):
    """Прайсинг по семействам моделей — денежная логика /credits и стоп-крана."""

    def test_model_family(self):
        from db import _model_family
        self.assertEqual(_model_family("claude-haiku-4-5"), "haiku")
        self.assertEqual(_model_family("claude-sonnet-4-5"), "sonnet")
        self.assertEqual(_model_family("claude-opus-4-1"), "opus")
        self.assertEqual(_model_family("что-то-неизвестное"), "sonnet")  # консервативно

    def test_model_cost(self):
        from db import model_cost
        # 1M in + 1M out: Sonnet = $3 + $15 = $18 ; Haiku 4.5 = $1 + $5 = $6 (v4.36: было 0.8/4.0 — занижено)
        self.assertAlmostEqual(model_cost("claude-sonnet-4-5", 1_000_000, 1_000_000), 18.0, places=6)
        self.assertAlmostEqual(model_cost("claude-haiku-4-5", 1_000_000, 1_000_000), 6.0, places=6)
        self.assertEqual(model_cost("claude-haiku-4-5", 0, 0), 0.0)

    def test_model_cost_with_cache(self):
        from db import model_cost
        # Sonnet input $3/MTok: cache-write ×2 (ttl=1h) = $6.00, cache-read ×0.1 = $0.30
        self.assertAlmostEqual(model_cost("claude-sonnet-4-5", 0, 0, 1_000_000, 0), 6.00, places=6)
        self.assertAlmostEqual(model_cost("claude-sonnet-4-5", 0, 0, 0, 1_000_000), 0.30, places=6)


class TestSpendCombine(unittest.IsolatedAsyncioTestCase):
    """Единый учёт: расход = классификатор (Haiku, message_log) + llm_usage по своим тарифам."""

    async def test_combines_sources(self):
        import aiosqlite
        from db import _spend_since, get_usage_breakdown
        db = await aiosqlite.connect(":memory:")
        await db.execute("CREATE TABLE message_log (id INTEGER PRIMARY KEY, "
                         "llm_tokens_input INT, llm_tokens_output INT, created_at TIMESTAMP)")
        await db.execute("CREATE TABLE llm_usage (id INTEGER PRIMARY KEY, model TEXT, "
                         "input_tokens INT, output_tokens INT, "
                         "cache_creation_tokens INT DEFAULT 0, cache_read_tokens INT DEFAULT 0, "
                         "kind TEXT, created_at TIMESTAMP)")
        ts = "2026-06-09 10:00:00"
        await db.execute("INSERT INTO message_log (llm_tokens_input, llm_tokens_output, created_at) "
                         "VALUES (1000000, 1000000, ?)", (ts,))  # Haiku → $6.0
        await db.execute("INSERT INTO llm_usage (model, input_tokens, output_tokens, created_at) "
                         "VALUES ('claude-sonnet-4-5', 1000000, 1000000, ?)", (ts,))  # Sonnet → $18
        await db.commit()
        total = await _spend_since(db, "2026-06-01")
        self.assertAlmostEqual(total, 24.0, places=4)
        bd = await get_usage_breakdown(db, "2026-06-01")
        self.assertAlmostEqual(bd["haiku"]["cost"], 6.0, places=4)
        self.assertAlmostEqual(bd["sonnet"]["cost"], 18.0, places=4)
        await db.close()


class TestPulse(unittest.TestCase):
    """v4.84: дневное напоминание: один повод, по важности, без повторов и без выдумок."""
    TODAY = __import__("datetime").date(2026, 11, 1)
    WAIT = {"user_id": 7, "username": "", "who": "Полина Иванова", "text": "Скинешь договор?", "age_days": 1}
    TASK = {"id": 5, "title": "Оплатить <домен>", "due_date": "2026-11-01 18:00"}
    MEET = {"summary": "Созвон с клиентом", "start": "2026-11-01T16:00:00+03:00"}

    def test_silent_without_reason(self):
        from core.pulse import pick
        self.assertIsNone(pick([], [], [], {}, self.TODAY))

    def test_priority_person_task_meeting(self):
        from core.pulse import pick
        self.assertEqual(pick([self.WAIT], [self.TASK], [self.MEET], {}, self.TODAY)["kind"], "wait")
        self.assertEqual(pick([], [self.TASK], [self.MEET], {}, self.TODAY)["kind"], "task")
        self.assertEqual(pick([], [], [self.MEET], {}, self.TODAY)["kind"], "meeting")

    def test_reason_not_repeated_for_three_days(self):
        from core.pulse import pick
        seen = {"u7": "2026-10-30"}
        self.assertEqual(pick([self.WAIT], [self.TASK], [], seen, self.TODAY)["kind"], "task")
        self.assertIsNone(pick([self.WAIT], [], [], seen, self.TODAY))
        self.assertEqual(pick([self.WAIT], [], [], {"u7": "2026-10-29"}, self.TODAY)["kind"], "wait")

    def test_wait_text(self):
        from core.pulse import pick, render
        doc, text = render(pick([self.WAIT], [], [], {}, self.TODAY))
        self.assertIn("ждёт ответ со вчера. Ответить?", doc)
        self.assertIn("https://t.me/@id7", doc)
        self.assertIn('data="rep:7"', doc)
        self.assertIn("<code>Полина: что я обещал?</code>", doc)
        self.assertEqual(text, "Полина Иванова ждёт ответ со вчера. Ответить?\nСпроси меня: Полина: что я обещал?")
        doc, _ = render(pick([{**self.WAIT, "age_days": 3}], [], [], {}, self.TODAY))
        self.assertIn("уже 3 дня", doc)

    def test_task_text_is_escaped_and_counts_rest(self):
        from core.pulse import pick, render
        doc, _ = render(pick([], [self.TASK, {**self.TASK, "id": 6}, {**self.TASK, "id": 8}], [], {}, self.TODAY))
        self.assertIn("Сегодня к 18:00 осталось: <b>Оплатить &lt;домен&gt;</b>. И ещё 2 дела на сегодня.", doc)
        self.assertIn('data="td:5"', doc)
        self.assertIn('data="tmr:5"', doc)

    def test_meeting_text_without_button(self):
        from core.pulse import pick, render
        doc, _ = render(pick([], [], [self.MEET], {}, self.TODAY))
        self.assertIn("В <b>16:00</b> встреча: Созвон с клиентом.", doc)
        self.assertNotIn("tg-button", doc)

    def test_switch_in_app(self):
        from core.autonomy import SWITCHES
        self.assertIn("pulse", [s["id"] for s in SWITCHES])


class TestRichDigest(unittest.TestCase):
    """v4.83: вёрстка сводки: главное сверху, ничего не теряется, подписки на месте."""

    def _doc(self, **over):
        from datetime import date
        from core.digest import _rich_digest
        data = dict(overdue=[], due_today=[], due_tomorrow=[], bd_today=[], bd_tmr=[], pair_lines=[], commits=[],
                    pending=[], cal_events=[], task_names=set(), conflict_lines=[], rain_lines=[], waits=[],
                    silent=[], todoist_lines=[], social_lines=[], sub_lines=[], tip="")
        data.update(over)
        return _rich_digest(date(2026, 11, 1), {}, **data)

    def test_greeting_and_gist(self):
        doc = self._doc()
        self.assertTrue(doc.startswith("<p>Доброе утро, босс ☀️</p><p>&nbsp;</p>"))
        self.assertIn("воскресенье, 1 ноября", doc)
        self.assertIn("Срочного нет", doc)
        self.assertNotIn("<table", doc)

    def test_gist_plurals(self):
        from core.digest import _digest_gist
        self.assertEqual(_digest_gist(1, 1, 1), "Сегодня 1 дело, 1 человек ждёт ответа, 1 дело просрочено.")
        self.assertEqual(_digest_gist(2, 3, 11), "Сегодня 2 дела, 3 человека ждут ответа, 11 дел просрочено.")

    def test_day_is_one_timeline(self):
        doc = self._doc(pair_lines=["12:40 Матанализ (лк, А-100)"],
                        due_today=[{"id": 7, "title": "Созвон", "from": "Аня", "due_date": "2026-11-01T09:30"}],
                        cal_events=[{"summary": "Созвон", "start": "2026-11-01T09:30:00+03:00"},
                                    {"summary": "Встреча", "start": "2026-11-01T15:00:00+03:00"}],
                        task_names={"созвон"})
        self.assertLess(doc.index("Созвон"), doc.index("Матанализ"))
        self.assertLess(doc.index("Матанализ"), doc.index("Встреча"))
        self.assertEqual(doc.count("Созвон"), 1)
        self.assertIn('data="td:7"', doc)

    def test_nothing_lost(self):
        doc = self._doc(sub_lines=["Netflix завтра"], todoist_lines=["Купить"], social_lines=["YT +3"], tip="Совет",
                        overdue=[{"id": i, "title": f"Д{i}", "from": "x"} for i in range(8)],
                        waits=[{"user_id": 5, "username": "id5", "who": "Полина", "age_days": 3, "text": "Как дела?"}])
        for needle in ("Netflix", "Купить", "YT +3", "Совет", "и ещё 2", "t.me/@id5", 'data="rep:5"'):
            self.assertIn(needle, doc)
        self.assertIn("<details><summary>🔴 Просрочено (8)", doc)

    def test_sections_have_air(self):
        doc = self._doc(due_today=[{"id": 7, "title": "Созвон", "from": "Аня", "due_date": "2026-11-01"}],
                        commits=[{"name": "Оля", "username": "", "commitments": "скинуть файл"}])
        self.assertEqual(doc.count("<ul>"), 2)            # внутри разделов список
        self.assertEqual(doc.count("<p>&nbsp;</p>"), 3)   # под приветствием и между разделами
        self.assertNotIn('<br/><tg-button', doc)          # кнопки в строке дела

    def test_few_overdue_stay_open(self):
        doc = self._doc(overdue=[{"id": 1, "title": "Д", "from": "x"}])
        self.assertIn("<details open><summary>🔴 Просрочено (1)", doc)


class TestContactLink(unittest.TestCase):
    """v4.82: tg://user?id= сервер вырезает у незнакомых боту контактов, карточка теряла ссылку."""

    def test_username_goes_direct(self):
        from core.util import profile_link
        self.assertEqual(profile_link("idris_x", 5), "https://t.me/idris_x")

    def test_no_username_uses_id_link(self):
        from core.util import profile_link
        for stub in ("id123", "", None):
            self.assertEqual(profile_link(stub, 123), "https://t.me/@id123")

    def test_label_never_fake_mention(self):
        from core.util import contact_label
        self.assertEqual(contact_label("id123", 123, "Аня <3"), "Аня &lt;3")
        self.assertEqual(contact_label("id123", 123), "id123")
        self.assertEqual(contact_label("anna", 123, "Аня"), "@anna")


class TestPhotoVision(unittest.TestCase):
    """v4.85: описание фото идёт через llm.create, мышление выключено, дата не обрывается."""

    def test_call_params(self):
        import io
        from types import SimpleNamespace as NS
        from unittest.mock import AsyncMock, patch
        import handlers.business as hb
        resp = NS(content=[NS(type="text", text="Бронь 01.11 в 21:25")], usage=NS(input_tokens=1, output_tokens=1))
        client = NS(messages=NS(create=AsyncMock(return_value=resp)))
        bot = NS(get_file=AsyncMock(return_value=NS(file_path="p")), download_file=AsyncMock(return_value=io.BytesIO(b"jpg")))
        msg = NS(photo=[NS(file_size=10, file_id="f")])
        with patch.object(hb, "_rec", AsyncMock()):
            out = asyncio.run(hb._analyze_photo(client, "claude-haiku-5-5", bot, msg, "anna"))
        self.assertEqual(out, "Бронь 01.11 в 21:25")
        kw = client.messages.create.call_args.kwargs
        self.assertEqual(kw["thinking"], {"type": "disabled"})
        self.assertGreaterEqual(kw["max_tokens"], 1000)
        self.assertIn("даты, время", kw["messages"][0]["content"][1]["text"])


class TestLoadsLoose(unittest.TestCase):
    """P2.7: через loads_loose проходят ВСЕ LLM-ответы — регресс на fences/prose/обрыв."""

    def test_plain_json(self):
        from core.util import loads_loose
        self.assertEqual(loads_loose('{"a": 1}'), {"a": 1})

    def test_markdown_fences(self):
        from core.util import loads_loose
        self.assertEqual(loads_loose('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(loads_loose('```\n{"a": 1}\n```'), {"a": 1})

    def test_leading_prose(self):
        from core.util import loads_loose
        self.assertEqual(loads_loose('Вот JSON:\n{"tasks": []}'), {"tasks": []})

    def test_truncated_raises(self):
        from core.util import loads_loose
        import json
        with self.assertRaises(json.JSONDecodeError):
            loads_loose('{"a": "обрезанная стро')  # незакрытый JSON НЕ парсится молча

    def test_self_correction_takes_last(self):
        from core.util import loads_loose
        raw = '{"done": [0], "reason": "не выполнена"}\n\nИсправляю:\n\n{"done": [], "reason": "обещание"}'
        self.assertEqual(loads_loose(raw)["done"], [])

    def test_trailing_prose_with_brackets(self):
        from core.util import loads_loose
        self.assertEqual(loads_loose('{"a": 1}\n\nСм. пункт [1] и {примечание}'), {"a": 1})

    def test_done_prompt_asks_reason_first(self):
        # Без мышления модель решает по первому полю: вердикт раньше вывода давал «выполнена» с текстом «нет»
        from core.actions import DONE_BATCH_USER
        text = DONE_BATCH_USER.format(task_list="0. x", conversation="y")
        self.assertLess(text.rindex('"reason"'), text.rindex('"done"'))


class TestClipTitle(unittest.TestCase):
    """P2.7: _clip_title родился из бага «обрезка обещаний на полуслове» (item[:80])."""

    def test_short_untouched(self):
        from core.actions import _clip_title
        self.assertEqual(_clip_title("скинуть ей денег"), "скинуть ей денег")

    def test_long_cuts_at_word_boundary(self):
        from core.actions import _clip_title
        s = ("заработать минимум 50 тысяч после Питера для совместной поездки в Китай "
             "(обещал что это более чем возможно) и ещё длинный хвост про детали и сроки поездки весной")
        out = _clip_title(s, limit=80)
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(len(out), 81)
        # не рвём слово: перед многоточием — завершённое слово (нет обрыва типа 'обещал ч…')
        self.assertNotIn(out[:-1].rstrip()[-1], (" ",))
        self.assertIn(out[:-1].rstrip().split()[-1], s.split())

    def test_fits_exact_limit(self):
        from core.actions import _clip_title
        s = "x" * 160
        self.assertEqual(_clip_title(s, limit=160), s)  # ровно в лимит — без многоточия


class TestClearPendingGuard(unittest.IsolatedAsyncioTestCase):
    """P2.7: guard last_activity в clear_pending_analysis — ФИКС тихой потери v3.24.
    Сообщение, пришедшее во время LLM-разбора, не должно затираться сбросом dirty."""

    async def _db(self):
        import aiosqlite
        db = await aiosqlite.connect(":memory:")
        await db.execute(
            "CREATE TABLE pending_analysis (user_id INTEGER PRIMARY KEY, username TEXT, "
            "category TEXT, last_activity TEXT, dirty INTEGER DEFAULT 1)")
        await db.commit()
        return db

    async def test_clear_with_matching_snapshot(self):
        from db import mark_pending_analysis, get_settled_pending, clear_pending_analysis
        db = await self._db()
        await mark_pending_analysis(db, 1, "user", "FRIEND")
        row = (await (await db.execute(
            "SELECT last_activity FROM pending_analysis WHERE user_id=1")).fetchone())
        await clear_pending_analysis(db, 1, row[0])  # снапшот совпадает → dirty снят
        dirty = (await (await db.execute(
            "SELECT dirty FROM pending_analysis WHERE user_id=1")).fetchone())[0]
        self.assertEqual(dirty, 0)
        await db.close()

    async def test_new_message_during_analysis_not_lost(self):
        from db import mark_pending_analysis, clear_pending_analysis
        db = await self._db()
        await mark_pending_analysis(db, 1, "user", "FRIEND")
        stale_snapshot = (await (await db.execute(
            "SELECT last_activity FROM pending_analysis WHERE user_id=1")).fetchone())[0]
        await asyncio.sleep(0.01)
        await mark_pending_analysis(db, 1, "user", "FRIEND")  # «новое сообщение» во время разбора
        await clear_pending_analysis(db, 1, stale_snapshot)   # clear со СТАРЫМ снапшотом
        dirty = (await (await db.execute(
            "SELECT dirty FROM pending_analysis WHERE user_id=1")).fetchone())[0]
        self.assertEqual(dirty, 1)  # dirty ОСТАЛСЯ — тред разберётся следующим проходом
        await db.close()


class TestReconcileCommitmentsSplit(unittest.IsolatedAsyncioTestCase):
    """P2.7: сплиттер обещаний — пустое/прочерки не создают задач (early return до внешних API)."""

    async def test_empty_and_dashes_create_nothing(self):
        from core.actions import reconcile_commitment_tasks
        self.assertEqual(await reconcile_commitment_tasks(None, 1, "u", "FRIEND", ""), 0)
        self.assertEqual(await reconcile_commitment_tasks(None, 1, "u", "FRIEND", "—; -; \n"), 0)
        self.assertEqual(await reconcile_commitment_tasks(None, 1, "u", "FRIEND", None), 0)


class TestMergeStruct(unittest.TestCase):
    """P1.1 (дифф-энрич): merge не теряет пропущенные секции и дедупит хронологию."""

    def test_omitted_section_preserved_returned_replaced(self):
        import json as _json
        from core.vault import _merge_struct
        old = _json.dumps({"sections": {"key": "старое ключевое", "habits": "старый быт"},
                           "timeline": [{"date": "06-01", "event": "было"}]})
        sec, tl = _merge_struct(old, {"habits": "новый быт"}, None)
        self.assertEqual(sec["key"], "старое ключевое")   # пропущенная — сохранена
        # v4.47 «Память 2.0»: старый формат «секция целиком» = append-only — старое НЕ стирается, новое дописано с датой
        self.assertTrue(sec["habits"].startswith("старый быт; ["), sec["habits"])
        self.assertTrue(sec["habits"].endswith("] новый быт"))
        self.assertEqual(len(tl), 1)                       # хронология не тронута

    def test_empty_value_does_not_wipe(self):
        import json as _json
        from core.vault import _merge_struct
        old = _json.dumps({"sections": {"key": "ценные факты"}, "timeline": []})
        sec, _ = _merge_struct(old, {"key": ""}, None)
        self.assertEqual(sec["key"], "ценные факты")       # пустая строка НЕ затирает

    def test_timeline_dedup_and_cap(self):
        import json as _json
        from core.vault import _merge_struct
        old = _json.dumps({"sections": {}, "timeline": [
            {"date": f"06-{i:02d}", "event": f"событие {i}"} for i in range(1, 11)]})
        new_tl = [{"date": "06-05", "event": "Событие 5"},          # дубль (регистр) — не добавится
                  {"date": "07-01", "event": "новое"}] + [
                  {"date": f"07-{i:02d}", "event": f"св {i}"} for i in range(2, 8)]
        _, tl = _merge_struct(old, {}, new_tl)
        events = [t["event"] for t in tl]
        self.assertEqual(events.count("Событие 5") + events.count("событие 5"), 1)
        self.assertEqual(len(tl), 17)                      # v4.47: без капа — 10 старых + 7 новых (дубль отсеян)
        self.assertEqual(tl[-1]["event"], "св 7")          # свежие — в хвосте, сохранены

    def test_anchor_preserved_on_cap(self):
        """v4.47: кап хронологии снят — все вехи сохраняются, порядок старое→новое."""
        import json as _json
        from core.vault import _merge_struct
        old = _json.dumps({"sections": {}, "timeline": [
            {"date": f"06-{i:02d}", "event": f"e{i}"} for i in range(1, 25)]})  # 24 старых
        _, tl = _merge_struct(old, {}, [{"date": "07-01", "event": "новое"}])   # +1 = 25
        self.assertEqual(len(tl), 25)                      # v4.47: кап снят — хронология полная
        self.assertEqual(tl[0]["event"], "e1")             # anchor сохранён
        self.assertEqual(tl[-1]["event"], "новое")         # свежее тоже

    def test_nonstring_event_no_crash(self):
        """fix #9: не-строковый event (битая/ручная БД) не роняет flatten/merge."""
        import json as _json
        from core.vault import _merge_struct, _flatten_struct
        bad = _json.dumps({"sections": {}, "timeline": [{"date": "01-01", "event": 123}]})
        sec, tl = _merge_struct(bad, {"key": "x"}, [])
        self.assertIsInstance(_flatten_struct(sec, tl), str)   # не AttributeError


class TestLLMFallback(unittest.TestCase):
    """v4.3 гибрид-фолбэк: маппинг моделей + конверсия Anthropic→OpenAI формата."""

    def test_model_slug_mapping(self):
        from core.llm import _or_slug
        self.assertEqual(_or_slug("claude-sonnet-4-5"), "anthropic/claude-sonnet-4.5")
        self.assertEqual(_or_slug("claude-haiku-4-5"), "anthropic/claude-haiku-4.5")
        self.assertEqual(_or_slug("anthropic/claude-sonnet-4.5"), "anthropic/claude-sonnet-4.5")

    def test_system_cache_control_stripped(self):
        """system-блок с cache_control → чистый system-role, cache_control отброшен."""
        from core.llm import _msgs_to_openai
        sys_ = [{"type": "text", "text": "SYS",
                 "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
        oi = _msgs_to_openai(sys_, [{"role": "user", "content": "hi"}])
        self.assertEqual(oi, [{"role": "system", "content": "SYS"},
                              {"role": "user", "content": "hi"}])

    def test_resp_shape_reads_like_anthropic(self):
        from core.llm import _Resp, _Usage
        r = _Resp("текст", _Usage(10, 5))
        self.assertEqual(r.content[0].text, "текст")
        self.assertEqual(r.usage.input_tokens, 10)
        self.assertEqual(r.usage.output_tokens, 5)
        self.assertEqual(getattr(r.usage, "cache_read_input_tokens", 0), 0)  # usage.record safe


class TestContactMemoryClip(unittest.TestCase):
    """v4.3: _clip режет по границе слова, а не посреди (баг «обрезаются слова»)."""

    def test_short_unchanged(self):
        from core.vault import _clip
        self.assertEqual(_clip("привет мир", 100), "привет мир")

    def test_no_midword_cut(self):
        from core.vault import _clip
        s = "ездили к нему в гости и обсуждали рабочие проблемы подробно"
        out = _clip(s, 30)
        self.assertTrue(len(out) <= 30)
        self.assertFalse(out.endswith("проб"))          # не оборван посреди слова
        self.assertTrue(s.startswith(out))              # префикс исходного
        self.assertNotIn(out[-1], " ")                  # без хвостового пробела

    def test_long_single_token_hard_cut(self):
        from core.vault import _clip
        s = "a" * 100                                    # нет пробелов → жёстко
        self.assertEqual(len(_clip(s, 40)), 40)

    def test_trailing_punct_stripped(self):
        from core.vault import _clip
        out = _clip("сегодня встреча важная; потом ещё", 22)
        self.assertFalse(out.endswith(";"))
        self.assertFalse(out.endswith(" "))


class TestSecondOpinionTrigger(unittest.TestCase):
    """v4.4: _SCHEDULE_RE ловит сигналы расписания (триггер второго мнения на extract=0)."""

    def test_schedule_signals_match(self):
        from core.actions import _SCHEDULE_RE
        for s in ["Давай в среду в 14:00", "Ср в 14 с Майкой, Пт 21 со мной",
                  "08.07 встреча", "увидимся завтра", "в 21 час", "в пятницу вечером",
                  "созвон 15.03"]:
            self.assertTrue(_SCHEDULE_RE.search(s), f"должно матчить: {s!r}")

    def test_non_schedule_no_match(self):
        from core.actions import _SCHEDULE_RE
        for s in ["спасибо большое, отлично!", "ок понял", "да, супер",
                  "как дела?", "красивое фото"]:
            self.assertFalse(_SCHEDULE_RE.search(s), f"НЕ должно матчить: {s!r}")


class TestWeekdayFix(unittest.TestCase):
    """v4.5: _snap_weekday_dates чинит +1-ошибку модели в днях недели (кейс @tutor_lena)."""

    def test_pair_by_hour_two_weekdays(self):
        from core.actions import _snap_weekday_dates
        tasks = [{"title": "a", "due_date": "2026-07-09T14:00"},   # модель: чт (ошибка)
                 {"title": "b", "due_date": "2026-07-11T21:00"}]   # модель: сб (ошибка)
        _snap_weekday_dates(tasks, "Ср в 14 с Майкой, Пт 21 со мной")
        self.assertEqual(tasks[0]["due_date"], "2026-07-08T14:00")  # ср
        self.assertEqual(tasks[1]["due_date"], "2026-07-10T21:00")  # пт

    def test_single_weekday_no_time(self):
        from core.actions import _snap_weekday_dates
        tasks = [{"title": "x", "due_date": "2026-07-09"}]          # 09=чт
        _snap_weekday_dates(tasks, "давай во вторник")
        self.assertEqual(tasks[0]["due_date"], "2026-07-07")        # вт той же недели

    def test_no_weekday_untouched(self):
        from core.actions import _snap_weekday_dates
        tasks = [{"title": "x", "due_date": "2026-07-09T14:00"}]
        _snap_weekday_dates(tasks, "завтра в 14 позвонить")
        self.assertEqual(tasks[0]["due_date"], "2026-07-09T14:00")  # без дня недели — не трогаем

    def test_correct_weekday_untouched(self):
        from core.actions import _snap_weekday_dates
        tasks = [{"title": "x", "due_date": "2026-07-08T14:00"}]    # 08=ср уже верно
        _snap_weekday_dates(tasks, "в среду в 14")
        self.assertEqual(tasks[0]["due_date"], "2026-07-08T14:00")

    def test_ambiguous_two_weekdays_near_hour_untouched(self):
        """bug #7: два РАЗНЫХ дня недели впритык к часу → неоднозначно, дату не трогаем."""
        from core.actions import _snap_weekday_dates
        tasks = [{"title": "x", "due_date": "2026-07-09T14:00"}]
        _snap_weekday_dates(tasks, "ср чт в 14")   # оба дня в ~3-6 симв. от «14»
        self.assertEqual(tasks[0]["due_date"], "2026-07-09T14:00")


class TestJitOverlap(unittest.TestCase):
    """v4.21: JIT-шпаргалка — пересечение темы сообщения с открытыми задачами/долгами."""

    def test_task_overlap(self):
        from handlers.business import _jit_topic_overlap
        self.assertEqual(_jit_topic_overlap("когда заберёшь плату?", ["Забрать плату у Ивана"], ""),
                         "Забрать плату у Ивана")

    def test_commitment_overlap(self):
        from handlers.business import _jit_topic_overlap
        self.assertEqual(_jit_topic_overlap("ну что с монтажом?", [], "доделать монтаж ролика"),
                         "обещание")

    def test_no_overlap(self):
        from handlers.business import _jit_topic_overlap
        self.assertIsNone(_jit_topic_overlap("привет как дела", ["Забрать плату"], "вернуть долг"))

    def test_short_words_ignored(self):
        from handlers.business import _jit_topic_overlap
        self.assertIsNone(_jit_topic_overlap("да ок ну", ["Забрать плату"], ""))


class TestMeetingAsk(unittest.TestCase):
    """v4.26: триггер «спрашивает о встрече»."""

    def test_asks_match(self):
        from core.meeting import MEETING_ASK_RE
        for s in ["когда встретимся?", "Во сколько тебе удобно?", "давай созвонимся",
                  "когда можем пересечься", "найдём время на неделе?", "когда сможешь?"]:
            self.assertTrue(MEETING_ASK_RE.search(s), s)

    def test_non_asks_clean(self):
        from core.meeting import MEETING_ASK_RE
        for s in ["привет, как дела?", "встретил его вчера", "созвон прошёл отлично",
                  "удобно получилось", "во сколько ты вчера лёг?"]:
            self.assertFalse(MEETING_ASK_RE.search(s), s)


class TestMailAccounts(unittest.TestCase):
    """v4.28: парсер EMAIL_ACCOUNTS (app-пароли Gmail содержат пробелы!)."""

    def test_multi_accounts(self):
        from core.mailwatch import parse_accounts
        got = parse_accounts("a@gmail.com:aaaa bbbb cccc dddd, b@gmail.com:xxxx yyyy")
        self.assertEqual(got, [("a@gmail.com", "aaaa bbbb cccc dddd"),
                               ("b@gmail.com", "xxxx yyyy")])

    def test_empty_and_garbage(self):
        from core.mailwatch import parse_accounts
        self.assertEqual(parse_accounts(""), [])
        self.assertEqual(parse_accounts("no-colon-here, :"), [])


class TestBdayDates(unittest.TestCase):
    """v4.30: нормализация дат дня рождения (ввод через Миру свободным текстом)."""

    def test_formats(self):
        from core.dates import norm_bdate
        self.assertEqual(norm_bdate("12 мая"), ("05-12", None))
        self.assertEqual(norm_bdate("12 мая 1999"), ("05-12", 1999))
        self.assertEqual(norm_bdate("07.11"), ("11-07", None))
        self.assertEqual(norm_bdate("03.01.1975"), ("01-03", 1975))
        self.assertEqual(norm_bdate("1999-05-12"), ("05-12", 1999))
        self.assertEqual(norm_bdate("05-12"), ("05-12", None))

    def test_garbage(self):
        from core.dates import norm_bdate
        self.assertEqual(norm_bdate(""), (None, None))
        self.assertEqual(norm_bdate("скоро"), (None, None))
        self.assertEqual(norm_bdate("45.99"), (None, None))
        self.assertEqual(norm_bdate("32 мая"), (None, None))

    def test_helpers(self):
        from core.dates import bdays_on, fmt_mmdd, slug_for, turning_age
        self.assertEqual(fmt_mmdd("05-12"), "12.05")
        self.assertEqual(slug_for("Ваня", 123), "123")
        self.assertEqual(slug_for("Мама Люба", None), "мамалюба")
        self.assertEqual(turning_age({"year": 1999}, 2026), 27)
        self.assertIsNone(turning_age({"year": None}, 2026))
        bds = [{"who": "Ваня", "mmdd": "05-12"}, {"who": "мама", "mmdd": "01-03"}]
        self.assertEqual(bdays_on(bds, "05-12")[0]["who"], "Ваня")
        self.assertEqual(bdays_on(bds, "12-31"), [])


class TestNoUndefinedGlobals(unittest.TestCase):
    """v4.33: страж от класса бага «NameError внутри except→debug» (6 функций молчали неделю).
    Обходит байткод всех функций модулей и проверяет, что каждый LOAD_GLOBAL резолвится."""

    MODULES = ["core.actions", "core.vault", "core.digest", "core.llm", "core.meeting", "core.mailwatch",
               "core.schedule", "core.dates", "core.tasks_loop", "core.drafts", "core.topics", "core.forwarder",
               "handlers.owner", "handlers.callbacks", "handlers.business", "handlers.commands", "db", "core.mira", "core.roles", "core.contact_memory", "core.social", "core.subscriptions", "core.mira_api"]

    def _walk(self, code, mod, out):
        import dis, builtins, types
        for ins in dis.get_instructions(code):
            if ins.opname == "LOAD_GLOBAL" and ins.argval not in vars(mod) and not hasattr(builtins, ins.argval):
                out.add(f"{mod.__name__}.{code.co_qualname}: {ins.argval}")
        for c in code.co_consts:
            if isinstance(c, types.CodeType):
                self._walk(c, mod, out)

    def test_all_globals_resolve(self):
        import importlib, types
        missing = set()
        for name in self.MODULES:
            mod = importlib.import_module(name)
            for obj in list(vars(mod).values()):
                fn = getattr(obj, "__func__", obj)
                code = getattr(fn, "__code__", None)
                if code is not None and getattr(fn, "__module__", None) == mod.__name__:
                    self._walk(code, mod, missing)
                elif isinstance(obj, type) and getattr(obj, "__module__", None) == mod.__name__:
                    for m in vars(obj).values():
                        mf = getattr(m, "__func__", m)
                        if getattr(mf, "__code__", None) is not None:
                            self._walk(mf.__code__, mod, missing)
        self.assertEqual(missing, set(), f"неопределённые глобалы: {sorted(missing)}")


class TestTail(unittest.TestCase):
    """v4.35: контекст режется с головы (сохраняем свежее), по границе строки."""

    def test_tail_keeps_recent(self):
        from core.actions import _tail
        lines = [f"[2026-09-0{i%9+1}] msg {i} " + "x" * 40 for i in range(60)]
        s = "\n".join(lines)
        t = _tail(s, 500)
        self.assertTrue(t.endswith(lines[-1]))            # последнее сообщение сохранено
        self.assertTrue(t.startswith("[2026"))             # начало — с границы строки
        self.assertLessEqual(len(t), 500)
        self.assertEqual(_tail("short", 100), "short")

    def test_cancelled_wording(self):
        from core.actions import _looks_cancelled
        for t in ("Не забирать Нику с работы (Арбат) — владелец занят в 21:00", "Отменено: созвон с Владом", "Больше не нужно привозить таблетки", "не актуально"):
            self.assertTrue(_looks_cancelled(t), t)
        for t in ("Не забыть купить молоко", "Забрать Нику с работы", "Привезти таблетки после 15:00"):
            self.assertFalse(_looks_cancelled(t), t)

    def test_retitle_completion_guard(self):
        from core.actions import _looks_completed
        for bad in ["Отдано Владосу 4120₽ (переведено на Озон)", "Отдано Тохе 2000₽", "Сделано: отправить договор",
                    "Оплатил такси", "Отдать Тохе 2000₽ (переведено на Озон)"]:
            self.assertTrue(_looks_completed(bad), bad)
        for ok in ["Отдать Владосу 2500₽ за совместные расходы", "Урок английского с @tutor_lena", "Перезвонить @vlad_f"]:
            self.assertFalse(_looks_completed(ok), ok)


class TestRichBlocks(unittest.TestCase):
    """v4.34: хелперы rich-блоков Bot API 10.3 (синтаксис проверен живым probe 08.09)."""

    def test_button_variants(self):
        from core.richmsg import button, button_row, expandable_quote, table
        self.assertEqual(button("✓", "td:5", "success"),
                         '<tg-button type="callback_data" data="td:5" style="success">✓</tg-button>')
        self.assertEqual(button("x", "td:5", disabled=True), '<tg-button type="disabled">x</tg-button>')
        row = button_row([button("a", "k:1"), button("b", "k:2")])
        self.assertTrue(row.startswith('<tg-button-row align="left">') and row.endswith("</tg-button-row>"))
        self.assertEqual(expandable_quote("hi"), "<blockquote expandable>hi</blockquote>")
        self.assertIn("<table bordered striped compact>", table(["a"], [["1"]], compact=True))
        self.assertIn("<table>", table(["a"], [["1"]], bordered=False, striped=False))


class TestClipSection(unittest.TestCase):
    """v4.32: обрезка секции памяти по границе записи «; », не слова."""

    def test_cuts_at_entry_boundary(self):
        from core.vault import _clip_section
        s = "первая запись про дела; вторая запись подлиннее про планы; третья запись хвост"
        out = _clip_section(s, len(s) - 5)
        self.assertEqual(out, "первая запись про дела; вторая запись подлиннее про планы")

    def test_short_untouched_and_word_fallback(self):
        from core.vault import _clip_section
        self.assertEqual(_clip_section("коротко", 100), "коротко")
        one = "одна сплошная запись без разделителей совсем " * 20
        out = _clip_section(one, 300)
        self.assertLessEqual(len(out), 300)
        self.assertFalse(out.endswith(" "))


class TestSchedule(unittest.TestCase):
    """v4.31: расписание МИРЭА — чёт/нечёт недели, пары, занятость."""

    _JSON = ('{"group":"БСБО-22-24","semester_start":"2026-09-01","weeks":{'
             '"odd":{"2":[{"n":2,"subject":"Матанализ","type":"лк","room":"А-100"},'
             '{"n":1,"subject":"БД","type":"пр"}]},'
             '"even":{"2":[{"n":3,"subject":"Философия"}]}}}')

    def _tmp(self):
        import tempfile
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        f.write(self._JSON); f.close()
        return f.name

    def test_parity(self):
        from datetime import date
        from core.schedule import parity, week_number
        start = date(2026, 9, 1)  # вторник, неделя 1 = odd
        self.assertEqual(week_number(date(2026, 9, 1), start), 1)
        self.assertEqual(parity(date(2026, 9, 5), start), "odd")     # сб той же недели
        self.assertEqual(parity(date(2026, 9, 8), start), "even")    # вт следующей
        self.assertEqual(parity(date(2026, 9, 15), start), "odd")    # через две

    def test_pairs_sorted_and_parity_pick(self):
        from datetime import date
        from core.schedule import busy_blocks, pairs_for
        p = self._tmp()
        odd_tue = pairs_for(date(2026, 9, 1), p)      # вт нечётной: БД(1), Матанализ(2)
        self.assertEqual([x["subject"] for x in odd_tue], ["БД", "Матанализ"])
        even_tue = pairs_for(date(2026, 9, 8), p)     # вт чётной: Философия(3)
        self.assertEqual([x["subject"] for x in even_tue], ["Философия"])
        self.assertEqual(pairs_for(date(2026, 9, 2), p), [])          # ср — пусто
        self.assertEqual(pairs_for(date(2026, 8, 31), p), [])         # до семестра
        self.assertEqual(busy_blocks(date(2026, 9, 1), p), [(540, 630), (640, 730)])

    def test_no_file(self):
        from datetime import date
        from core.schedule import digest_lines, pairs_for
        self.assertEqual(pairs_for(date(2026, 9, 1), "/nonexistent/x.json"), [])
        self.assertEqual(digest_lines(date(2026, 9, 1), "/nonexistent/x.json"), [])


class TestPackageB(unittest.TestCase):
    """v4.36 (пакет B): анти-дабл-клик и случайные токены карточек."""

    def test_double_click_guard(self):
        from handlers.callbacks import _double_click, _CLICKS
        _CLICKS.clear()
        class Q:  # минимальный дубль CallbackQuery
            def __init__(self, mid, data): self.message = type("M", (), {"message_id": mid})(); self.data = data
        self.assertFalse(_double_click(Q(1, "drf:ab:ok")))
        self.assertTrue(_double_click(Q(1, "drf:ab:ok")))       # второй тап той же кнопки — блок
        self.assertFalse(_double_click(Q(1, "drf:ab:no")))      # другая кнопка на той же карточке — ок
        self.assertFalse(_double_click(Q(2, "drf:ab:ok")))      # та же кнопка на другой карточке — ок

    def test_tokens_random(self):
        from core.drafts import _new_token
        toks = {_new_token() for _ in range(200)}
        self.assertEqual(len(toks), 200)
        self.assertTrue(all(len(t) == 8 and ":" not in t for t in toks))

    def test_mail_parse_accounts_still_ok(self):
        from core.mailwatch import parse_accounts, _PER_PASS
        self.assertEqual(parse_accounts("a@x.com:p q r"), [("a@x.com", "p q r")])
        self.assertEqual(_PER_PASS, 10)


class TestRoles(unittest.TestCase):
    """v4.37: подбор ролей для «✏️ Поправить» — из CRM, по категории догадки."""

    def test_pick_roles_by_category(self):
        from core.roles import pick_roles
        rows = [("подписчик", 40), ("клиент", 12), ("одногруппник", 9), ("unknown", 8),
                ("преподаватель английского", 3), ("мама", 1)]
        self.assertEqual(pick_roles(rows, "BUSINESS"), ["клиент", "преподаватель английского"])
        self.assertEqual(pick_roles(rows, "FRIEND"), ["подписчик", "одногруппник", "мама"])
        self.assertEqual(pick_roles(rows, "UNCLEAR", limit=2), ["подписчик", "клиент"])   # любые, кроме служебных


class TestPackageC(unittest.TestCase):
    """v4.38 (пакет C): валидация дат др, ноль-паддинг времени пар, датированные id моделей, regex встреч."""

    def test_bdate_validation(self):
        from core.dates import norm_bdate
        self.assertEqual(norm_bdate("31.02"), (None, None))
        self.assertEqual(norm_bdate("30 февраля"), (None, None))
        self.assertEqual(norm_bdate("29.02"), ("02-29", None))

    def test_pair_time_zero_pad(self):
        from core.schedule import pair_time
        self.assertEqual(pair_time({"time": "9:00-10:30"}), "09:00-10:30")
        self.assertEqual(pair_time({"n": 1}), "09:00-10:30")

    def test_or_slug_dated(self):
        from core.llm import _or_slug
        self.assertEqual(_or_slug("claude-sonnet-4-5-20250929"), "anthropic/claude-sonnet-4.5")
        self.assertEqual(_or_slug("claude-sonnet-4-5"), "anthropic/claude-sonnet-4.5")

    def test_meeting_ask_negatives(self):
        from core.meeting import MEETING_ASK_RE
        for s in ["когда сможешь скинуть договор?", "когда тебе удобно ответить?", "когда сможешь посмотреть?"]:
            self.assertFalse(MEETING_ASK_RE.search(s), s)
        for s in ["когда сможешь?", "когда тебе удобно?", "когда встретимся?"]:
            self.assertTrue(MEETING_ASK_RE.search(s), s)

    def test_first_text(self):
        from core.richmsg import first_text
        class B:  # блок с текстом / без
            def __init__(self, t=None): self.text = t
        class R:
            def __init__(self, blocks): self.content = blocks
        self.assertEqual(first_text(R([B(None), B("ok")])), "ok")
        self.assertEqual(first_text(R([])), "")


class TestContactMemory2(unittest.TestCase):
    """v4.47 «Память 2.0»: append-only слияние без клипов, датировка, retire → хронология, старый формат не стирает."""

    def test_merge_add_dedup_retire(self):
        import json
        from core.contact_memory import merge, split_entries, SECTION_KEYS, norm_entry
        old = json.dumps({"sections": {"key": "мастер по ремонту техники; недавно устроился на работу", "money": "должен Владу 2500"},
                          "timeline": [{"date": "09-01", "event": "начало семестра"}]})
        sec, tl, changed, meta = merge(old, {"study_work": ["ищет работу: Т-Банк отказал", "Мастер по ремонту техники"], "money": []},
                                 ["недавно устроился на работу"], [{"date": "09-09", "event": "собеседование в Альфа-Банк 15:00"}], stamp="09.09.26")
        self.assertEqual(meta[norm_entry("Мастер по ремонту техники")]["cf"], "09.09.26")   # повтор = подтверждение
        self.assertIn("[09.09.26] ищет работу: Т-Банк отказал", split_entries(sec["study_work"]))
        self.assertNotIn("Мастер по ремонту техники", sec["study_work"])          # дубль по norm — пропущен
        self.assertEqual(split_entries(sec["key"]), ["мастер по ремонту техники"])   # retire убрал запись
        self.assertTrue(any("устарело: недавно устроился" in t["event"] for t in tl))
        self.assertEqual(tl[0]["event"], "начало семестра")
        self.assertEqual(sorted(changed), ["key", "study_work"])
        self.assertTrue(all(k in sec for k in SECTION_KEYS))

    def test_strip_label_echo(self):
        from core.contact_memory import strip_label_echo, merge, split_entries
        self.assertEqual(strip_label_echo("КЛЮЧЕВОЕ через ; — Девушка владельца Миши"), "Девушка владельца Миши")
        self.assertEqual(strip_label_echo("ДЕНЬГИ: должен 2500"), "должен 2500")
        self.assertEqual(strip_label_echo("[09.09.26] ПЛАНЫ И ДОГОВОРЁННОСТИ: поездка в Грецию"), "[09.09.26] поездка в Грецию")
        self.assertEqual(strip_label_echo("обычная запись: с двоеточием"), "обычная запись: с двоеточием")
        self.assertEqual(strip_label_echo("КЛЮЧЕВОЕ через"), "")                     # эхо как отдельная запись → выброс
        self.assertEqual(strip_label_echo("— Девушка владельца Миши"), "Девушка владельца Миши")
        self.assertEqual(strip_label_echo("[09.09.26] — поездка"), "[09.09.26] поездка")
        self.assertEqual(strip_label_echo("Работает грумером"), "Работает грумером")
        sec, _, _, _ = merge(None, {"key": ["КЛЮЧЕВОЕ: папа владельца"]}, None, None, stamp="09.09.26")
        self.assertEqual(split_entries(sec["key"]), ["[09.09.26] папа владельца"])

    def test_gender(self):
        import json
        from core.contact_memory import gender_of, gender_label, render_overview
        self.assertEqual(gender_of({"facts_json": json.dumps({"gender": "ж"}), "relationship": "друг"}), "ж")
        self.assertEqual(gender_of({"facts_json": "{}", "relationship": "папа"}), "м")
        self.assertEqual(gender_label({"relationship": "рекрутер"}), "не определён")
        html = render_overview({"user_id": 1, "username": "x", "name": "Ника", "relationship": "девушка",
                                "facts_json": json.dumps({"sections": {"key": "a; b", "money": "c"}, "timeline": [{"date": "09-01", "event": "e"}]})})
        self.assertIn("<td>Пол</td><td>женский</td>", html)
        self.assertIn('data="dsec:1:money"', html); self.assertIn('data="dsec:1:timeline"', html)

    def test_meta_objects_and_marks(self):
        from core.contact_memory import merge, entry_marks, compose_facts_json, norm_entry
        import json
        sec, tl, ch, meta = merge(None, {"money": [{"t": "должен Владу 2500", "c": "факт", "by": "сам", "src": [101, 102]},
                                                   {"t": "видимо, копит на машину", "c": "предп", "by": "другой"}]}, None, None, stamp="09.09.26")
        self.assertEqual(meta[norm_entry("должен Владу 2500")]["src"], [101, 102])
        self.assertIn("предположение", entry_marks(meta, "[09.09.26] видимо, копит на машину"))
        self.assertIn("источник: #101", entry_marks(meta, "должен Владу 2500"))
        fj = compose_facts_json(sec, tl, meta, json.dumps({"gender": "м", "style": "кратко"}))
        d = json.loads(fj); self.assertEqual(d["gender"], "м"); self.assertIn("meta", d)
        sec2, _, _, meta2 = merge(fj, {"money": ["должен Владу 2500"]}, None, None, stamp="10.09.26")
        self.assertEqual(meta2[norm_entry("должен Владу 2500")]["cf"], "10.09.26")
        self.assertIn("подтв. 10.09.26", entry_marks(meta2, "должен Владу 2500"))

    def test_rhythm_text_and_person_brief(self):
        from core.contact_memory import rhythm_text, person_brief
        self.assertEqual(rhythm_text({}), "")
        t = rhythm_text({"msgs30": 40, "top_hours": [21, 22], "reply_min": 12, "initiator": 0.4, "pauses7": 2})
        self.assertIn("40 сообщ.", t); self.assertIn("21/22 ч", t); self.assertIn("~12 мин", t); self.assertIn("40%", t)
        self.assertTrue(person_brief({"name": "Веста", "kind": "питомец", "who": "собака Ники", "facts": ["болела", "лечится"]}).startswith("🐾 Веста — собака Ники: болела; лечится"))

    def test_merge_legacy_sections_never_erase(self):
        import json
        from core.contact_memory import merge, split_entries
        old = json.dumps({"sections": {"prefs": "любит Dota 2; пьёт Red Bull"}, "timeline": []})
        sec, _, _, _ = merge(old, None, None, None, stamp="09.09.26", legacy_sections={"prefs": "любит Dota 2; ест шаурму"})
        self.assertEqual(split_entries(sec["prefs"]), ["любит Dota 2", "пьёт Red Bull", "[09.09.26] ест шаурму"])

    def test_no_clip_and_flatten(self):
        import json
        from core.contact_memory import merge, flatten
        big = [f"запись номер {i} с длинным текстом про что-то важное" for i in range(300)]
        sec, tl, _, _ = merge(None, {"other": big}, None, [{"date": "01-01", "event": f"e{i}"} for i in range(60)], stamp="01.01.26")
        self.assertEqual(len(sec["other"].split("; ")), 300)
        self.assertEqual(len(tl), 60)
        self.assertIn("ХРОНОЛОГИЯ", flatten(sec, tl, "коротко"))
        self.assertTrue(flatten(sec, tl, "коротко").endswith("СТИЛЬ ПИСЬМА: коротко"))

    def test_full_text_and_entries(self):
        import json
        from core.contact_memory import full_text, entries_of
        fj = json.dumps({"sections": {"key": "папа владельца", "money": "перевёл 5000"}, "timeline": [{"date": "09-01", "event": "ужин"}]})
        prof = {"name": "Сергей", "username": "serg_demo", "relationship": "папа", "summary": "Отец.", "facts_json": fj}
        txt = full_text(prof)
        self.assertIn("[КЛЮЧЕВОЕ]", txt); self.assertIn("• перевёл 5000", txt); self.assertIn("09-01 ужин", txt)
        self.assertEqual(entries_of(fj), [("key", "папа владельца"), ("money", "перевёл 5000"), ("timeline", "09-01 ужин")])


class TestSocial(unittest.TestCase):
    """v4.53 соцсети: чистые помощники."""

    def test_helpers(self):
        from core.social import _iso_dur_sec, _fmt, _delta, yt_enabled, tw_enabled
        self.assertEqual(_iso_dur_sec("PT1H2M3S"), 3723); self.assertEqual(_iso_dur_sec("PT45S"), 45); self.assertEqual(_iso_dur_sec("bad"), 10 ** 6)
        self.assertEqual(_fmt(1234567), "1 234 567"); self.assertEqual(_fmt(None), "—")
        self.assertEqual(_delta(110, 100), " (+10)"); self.assertEqual(_delta(90, 100), " (-10)"); self.assertEqual(_delta(5, 5), " (без изменений)"); self.assertEqual(_delta(None, 1), "")
        class S: youtube_api_key = None; youtube_channel_id = "@x"; twitch_client_id = "a"; twitch_client_secret = "b"; twitch_login = "c"
        self.assertFalse(yt_enabled(S())); self.assertTrue(tw_enabled(S()))


class TestSocialCSV(unittest.TestCase):
    def test_parse_tiktok_like(self):
        from core.social import parse_metrics_csv, detect_platform
        raw = "Date,Video Views,Profile Views,Likes,Comments,Shares\n2026-09-01,1200,30,80,5,2\n2026-09-02,\"1,5\",10,40,1,0\n"
        rows, unknown = parse_metrics_csv(raw)
        self.assertEqual(unknown, [])
        self.assertEqual({r["metric"] for r in rows}, {"views", "profile_views", "likes", "comments", "shares"})
        self.assertEqual([r["value"] for r in rows if r["day"] == "2026-09-01" and r["metric"] == "views"], [1200.0])
        self.assertEqual(detect_platform("TikTok_Analytics_export.csv"), "tiktok")
        self.assertEqual(detect_platform("data.csv", "это инстаграм"), "instagram")
        self.assertIsNone(detect_platform("data.csv"))

    def test_parse_semicolon_ru(self):
        from core.social import parse_metrics_csv
        raw = "\ufeffДата;Охват;Подписчики;Что-то ещё\n01.09.2026;500;120;abc\n02.09.2026;650;123;def\n"
        rows, unknown = parse_metrics_csv(raw)
        self.assertIn("что-то ещё", unknown)
        self.assertEqual(sorted({r["metric"] for r in rows}), ["followers", "reach"])
        self.assertEqual([r["value"] for r in rows if r["metric"] == "followers"], [120.0, 123.0])


class TestMiraV2(unittest.TestCase):
    """v4.44 Мира-чат 2.0 Ф1: матчер упомянутых контактов (склонения по стему)."""

    def test_mentioned_contacts(self):
        from core.mira import mentioned_contacts
        rows = [(1, "nika_demo", "Ника", "девушка", "f"), (2, "vlad_f", "Владос", "друг", "f"),
                (3, "tutor_lena", "Anastasia Kaneva", "преподаватель английского", "f"), (4, "serg_demo", "Сергей", "папа", "f")]
        self.assertEqual([r[0] for r in mentioned_contacts("расскажи про работу Ники", rows)], [1])
        self.assertEqual([r[0] for r in mentioned_contacts("что я должен Владосу?", rows)], [2])
        self.assertEqual([r[0] for r in mentioned_contacts("напиши @tutor_lena что опоздаю", rows)], [3])
        self.assertEqual(mentioned_contacts("какие задачи на завтра", rows), [])
        self.assertEqual([r[0] for r in mentioned_contacts("расскажи про моего папу", rows)], [4])   # по родству
        self.assertEqual([r[0] for r in mentioned_contacts("что скажешь про маму?", rows + [(9, "kat", "Катрин", "мама", "f")])], [9])

    def test_forward_meta(self):
        from core.mira import forward_meta
        class U: id = 5; first_name = "Ваня"; last_name = None; username = "vanya"
        class OU: sender_user = U()
        class OH: sender_user = None; sender_user_name = "Скрытый"
        class Ch: title = "Новости"; username = None
        class OC: sender_user = None; sender_user_name = None; chat = Ch()
        class M:
            def __init__(self, fo): self.forward_origin = fo
        self.assertEqual(forward_meta(M(None)), {})
        self.assertEqual(forward_meta(M(OU()))["uid"], 5)
        self.assertEqual(forward_meta(M(OU()))["name"], "Ваня")
        self.assertEqual(forward_meta(M(OH()))["kind"], "hidden")
        self.assertEqual(forward_meta(M(OC()))["kind"], "channel")

    def test_dated_tail(self):
        from core.mira import _dated_tail
        import json
        fj = json.dumps({"sections": {"key": "устроился на работу", "other": "Греция 63к; [08.09] работу НЕ нашёл, Т-Банк отказал; [08.09] 09.09 собес в Альфа"}})
        out = _dated_tail(fj)
        self.assertIn("[08.09] работу НЕ нашёл", out)
        self.assertNotIn("Греция", out)
        self.assertEqual(_dated_tail(None), "")
        self.assertEqual(_dated_tail("не json"), "")

    def test_norm_summary_task_vs_own_event(self):
        """v4.45.2: задача и её же событие в календаре нормализуются одинаково → не «пересечение»."""
        from core.calendar import _norm_summary
        self.assertEqual(_norm_summary("Урок английского с @tutor_lena"), _norm_summary("❓ Урок английского с @tutor_lena"))
        self.assertNotEqual(_norm_summary("Урок английского с @tutor_lena"), _norm_summary("Созвон с Владом"))

    def test_todoist_section(self):
        """v4.46: все 5 секций Todoist задействованы; владельческая задача без времени → Личное."""
        from core.actions import todoist_section
        class S: todoist_section_business="B"; todoist_section_meeting="M"; todoist_section_unclear="U"; todoist_section_friend="F"; todoist_section_commitment="C"
        s = S()
        self.assertEqual(todoist_section(s, is_commitment=True, due="2026-09-09T15:00"), "C")
        self.assertEqual(todoist_section(s, due="2026-09-09T15:00", owner_created=True), "M")
        self.assertEqual(todoist_section(s, due="2026-09-09", owner_created=True), "F")
        self.assertEqual(todoist_section(s, category="BUSINESS"), "B")
        self.assertEqual(todoist_section(s, category="UNCLEAR"), "U")
        self.assertIsNone(todoist_section(s, category="SPAM"))

    def test_dialog_window_tail(self):
        from core.mira import dialog_window_text
        rows = [(1.0, "owner", "старое " * 50, "[]"), (2.0, "mira", "новое", '["show_day"]')]
        out = dialog_window_text(rows, max_chars=60)
        self.assertTrue(out.endswith("Мира: новое [инструменты: show_day]"))
        self.assertLessEqual(len(out), 60)


class TestMailAlerts(unittest.TestCase):
    """v4.64: тревога про почту — только после часа сбоев подряд; успех сбрасывает счётчик."""

    def test_alert_after_hour(self):
        import asyncio, aiosqlite, time
        from unittest import mock
        import core.mailwatch as mw
        sent = []

        async def fake_alert(bot, db, owner_id, key, text, cooldown_minutes=60):
            sent.append(text)

        async def run():
            db = await aiosqlite.connect(":memory:")
            await db.execute("CREATE TABLE settings_kv (key TEXT PRIMARY KEY, value TEXT)")
            await db.commit()
            with mock.patch("core.alerts.alert_owner", fake_alert), mock.patch("core.topics.send_owner", mock.AsyncMock()) as so:
                await mw._mail_fail(None, db, 1, "a@x.com", "TimeoutError")   # первый сбой — молчим
                await mw._mail_fail(None, db, 1, "a@x.com", "TimeoutError")   # меньше часа — молчим
                self.assertEqual(sent, [])
                await db.execute("UPDATE settings_kv SET value=? WHERE key=?", (str(time.time() - 3700), "mailfail:a@x.com"))
                await db.commit()
                await mw._mail_fail(None, db, 1, "a@x.com", "TimeoutError")   # час подряд — тревога
                await mw._mail_fail(None, db, 1, "a@x.com", "TimeoutError")   # повторно не шлём
                self.assertEqual(len(sent), 1)
                await mw._mail_ok(None, db, 1, "a@x.com")                     # вернулась — «снова работает»
                self.assertEqual(so.await_count, 1)
                await mw._mail_fail(None, db, 1, "a@x.com", "TimeoutError")   # новый отсчёт
                self.assertEqual(len(sent), 1)
            await db.close()
        asyncio.run(run())


class TestModeration(unittest.IsolatedAsyncioTestCase):
    """v4.89: модерация обсуждения канала — правила, карточка владельцу, решение кнопкой."""

    def test_signals_reason(self):
        """v4.109: причина списком признаков — пример из жалобы владельца 13.11 (вербовка в чужой канал)."""
        from core.moderation import signals
        text = ("Привет! Делаем бесплатный разбор канала и даём бесплатного менеджера. Онлайн и доход, "
                "помогли уже 500+ новичкам. Зайди на 5 минут: https://t.me/somechannel")
        self.assertEqual(signals(text), ["внешняя t.me-ссылка", "обещание дохода", "бесплатная услуга", "призыв перейти"])
        self.assertEqual(signals("залетай https://t.me/+AbCdEf")[0], "приглашение в закрытый чат")
        self.assertEqual(signals("классный стрим"), [])

    async def test_restore_and_newbie_ban(self):
        """v4.109: «Вернуть» переотправляет текст от бота; новичок с явным спамом — бан сразу, «Разбанить и вернуть»."""
        from unittest import mock
        from core import moderation
        moderation._group.update(id=None, at=0.0)
        db, bot, settings = await self._env()
        bot.send_message = mock.AsyncMock(); bot.unban_chat_member = mock.AsyncMock()
        with mock.patch("core.topics.send_owner", mock.AsyncMock()) as send:
            # старый участник (вступления не видели) — удаление без бана, кнопка «Вернуть»
            await moderation.check(self._msg("залетай https://t.me/+AbCdEf"), bot, db, settings, None)
            bot.ban_chat_member.assert_not_called()
            tok = send.call_args.kwargs["reply_markup"].inline_keyboard[0][1].callback_data.split(":")[2]
            self.assertIn("приглашение в закрытый чат", send.call_args.args[3])
            self.assertEqual(await moderation.decide(bot, db, tok, "back"), "Вернула сообщение")
            sent = bot.send_message.call_args
            self.assertEqual(sent.args[0], -1001)
            self.assertIn("Спамер", sent.args[1]); self.assertIn("t.me/+AbCdEf", sent.args[1])
            self.assertEqual(await moderation.decide(bot, db, tok, "back"), "Уже решено")
            # новичок: вступил сейчас, первое сообщение — удаление и бан сразу
            await moderation.note_join(db, -1001, [mock.MagicMock(id=777, is_bot=False)])
            await moderation.check(self._msg("заработок на крипте тут https://x.io", uid=777), bot, db, settings, None)
            bot.ban_chat_member.assert_awaited_once_with(-1001, 777)
            self.assertIn("забанила новичка", send.call_args.args[3])
            self.assertIn("первое сообщение новичка", send.call_args.args[3])
            kb = send.call_args.kwargs["reply_markup"].inline_keyboard
            self.assertEqual([b.callback_data.split(":")[1] for row in kb for b in row], ["unban", "ok"])
            tok2 = kb[0][0].callback_data.split(":")[2]
            # второе сообщение того же человека — уже не новичок
            await moderation.note_join(db, -1001, [mock.MagicMock(id=778, is_bot=False)])
            await moderation.check(self._msg("как дела", uid=778), bot, db, settings, None)
            await moderation.check(self._msg("залетай https://t.me/+AbCdEf", uid=778), bot, db, settings, None)
            self.assertEqual(bot.ban_chat_member.await_count, 1)
        self.assertIn("Разбанила", await moderation.decide(bot, db, tok2, "unban"))
        bot.unban_chat_member.assert_awaited_once_with(-1001, 777, only_if_banned=True)
        bot.send_message.side_effect = RuntimeError("down")
        with mock.patch("core.topics.send_owner", mock.AsyncMock()) as send:
            await moderation.check(self._msg("залетай https://t.me/+AbCdEf", uid=900), bot, db, settings, None)
            tok3 = send.call_args.kwargs["reply_markup"].inline_keyboard[0][1].callback_data.split(":")[2]
        self.assertIn("Не вышло", await moderation.decide(bot, db, tok3, "back"))
        bot.send_message.side_effect = None
        self.assertEqual(await moderation.decide(bot, db, tok3, "back"), "Вернула сообщение")   # можно нажать ещё раз
        await db.close()


    def test_rules(self):
        from core.moderation import rule_verdict
        self.assertEqual(rule_verdict("залетай https://t.me/+AbCdEf")[0], "spam")
        self.assertEqual(rule_verdict("пассивный доход 300к в месяц, пиши в лс")[0], "spam")
        self.assertEqual(rule_verdict("заработок на крипте тут https://x.io")[0], "spam")
        self.assertEqual(rule_verdict("подпишись на мой канал t.me/mychannel")[0], "ad")
        self.assertEqual(rule_verdict("ты идиот, стрим скучный")[0], "rude")
        for ok in ("классный стрим, спасибо!", "бля, ну это было сильно", "когда следующий стрим?", "урок английского завтра?"):
            self.assertIsNone(rule_verdict(ok), ok)

    async def _env(self):
        import aiosqlite
        from unittest import mock
        db = await aiosqlite.connect(":memory:")
        await db.execute("CREATE TABLE settings_kv (key TEXT PRIMARY KEY, value TEXT)")
        bot = mock.MagicMock()
        bot.get_chat = mock.AsyncMock(return_value=mock.MagicMock(linked_chat_id=-1001))
        bot.delete_message = mock.AsyncMock(); bot.ban_chat_member = mock.AsyncMock()
        settings = mock.MagicMock(owner_user_id=111222333, post_channel="@St4rNinja38", anthropic_model="m")
        return db, bot, settings

    def _msg(self, text, chat=-1001, uid=555, sender_chat=None):
        from unittest import mock
        user = mock.MagicMock(id=uid, is_bot=False, username="spammer", full_name="Спамер")
        return mock.MagicMock(text=text, caption=None, from_user=user, sender_chat=sender_chat,
                              chat=mock.MagicMock(id=chat, title="Обсуждение"), message_id=77)

    async def test_card_only_in_discussion_and_not_for_owner(self):
        from unittest import mock
        from core import moderation
        moderation._group.update(id=None, at=0.0)
        db, bot, settings = await self._env()
        from core import autonomy
        await autonomy.toggle(db, "modauto", False)              # здесь проверяем карточку, без автоудаления
        with mock.patch("core.topics.send_owner", mock.AsyncMock()) as send:
            self.assertTrue(await moderation.check(self._msg("залетай https://t.me/+AbCdEf"), bot, db, settings, None))
            kb = send.call_args.kwargs["reply_markup"].inline_keyboard
            self.assertEqual([b.callback_data.split(":")[1] for row in kb for b in row], ["del", "ban", "ok"])
            self.assertIn("спам", send.call_args.args[3])
            bot.delete_message.assert_not_called()                                   # сама ничего не удаляет
            self.assertFalse(await moderation.check(self._msg("залетай https://t.me/+x", chat=-2002), bot, db, settings, None))
            self.assertFalse(await moderation.check(self._msg("https://t.me/+x", uid=111222333), bot, db, settings, None))
            self.assertFalse(await moderation.check(self._msg("https://t.me/+x", sender_chat=object()), bot, db, settings, None))
            self.assertFalse(await moderation.check(self._msg("короче"), bot, db, settings, None))   # короткое — без модели
            from core import autonomy
            await autonomy.toggle(db, "moderation", False)
            self.assertFalse(await moderation.check(self._msg("залетай https://t.me/+AbCdEf"), bot, db, settings, None))
        self.assertEqual(send.await_count, 1)
        bot.get_chat.assert_awaited_once()                                           # id группы в кэше
        await db.close()

    async def test_decide_by_button_once(self):
        from unittest import mock
        from core import moderation
        moderation._group.update(id=None, at=0.0)
        db, bot, settings = await self._env()
        from core import autonomy
        await autonomy.toggle(db, "modauto", False)              # здесь проверяем карточку, без автоудаления
        tokens = []
        with mock.patch("core.topics.send_owner", mock.AsyncMock()) as send:
            for _ in range(3):
                await moderation.check(self._msg("залетай https://t.me/+AbCdEf"), bot, db, settings, None)
                tokens.append(send.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data.split(":")[2])
        self.assertEqual(await moderation.decide(bot, db, tokens[0], "ok"), "Оставила")
        bot.delete_message.assert_not_called()
        self.assertEqual(await moderation.decide(bot, db, tokens[1], "del"), "Удалила")
        bot.ban_chat_member.assert_not_called()
        self.assertIn("в бане", await moderation.decide(bot, db, tokens[2], "ban"))
        bot.ban_chat_member.assert_awaited_once_with(-1001, 555)
        self.assertEqual(await moderation.decide(bot, db, tokens[2], "ban"), "Уже решено")
        bot.delete_message.side_effect = RuntimeError("no rights")
        with mock.patch("core.topics.send_owner", mock.AsyncMock()) as send:
            await moderation.check(self._msg("залетай https://t.me/+AbCdEf"), bot, db, settings, None)
            tok = send.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data.split(":")[2]
        self.assertIn("права", await moderation.decide(bot, db, tok, "del"))
        await db.close()


    async def test_sure_spam_deleted_at_once(self):
        """v4.92: приглашение и заработок со ссылкой удаляются сразу, остальное по-прежнему карточкой."""
        from unittest import mock
        from core import moderation
        moderation._group.update(id=None, at=0.0)
        db, bot, settings = await self._env()
        with mock.patch("core.topics.send_owner", mock.AsyncMock()) as send:
            self.assertTrue(await moderation.check(self._msg("залетай https://t.me/+AbCdEf"), bot, db, settings, None))
            bot.delete_message.assert_awaited_once_with(-1001, 77)
            kb = send.call_args.kwargs["reply_markup"].inline_keyboard
            self.assertEqual([b.callback_data.split(":")[1] for row in kb for b in row], ["ban", "back", "ok"])
            self.assertIn("Удалила спам", send.call_args.args[3])
            self.assertNotIn("уже не вернуть", send.call_args.args[3])
            tok = kb[0][0].callback_data.split(":")[2]
            for text in ("подпишись на мой канал t.me/mychannel", "пассивный доход 300к в месяц, пиши в лс", "ты идиот, стрим скучный"):
                await moderation.check(self._msg(text), bot, db, settings, None)
                self.assertIn("Удалить", send.call_args.kwargs["reply_markup"].inline_keyboard[0][0].text, text)
            self.assertEqual(bot.delete_message.await_count, 1)                       # остальное не удалено
        self.assertIn("в бане", await moderation.decide(bot, db, tok, "ban"))
        bot.ban_chat_member.assert_awaited_once_with(-1001, 555)
        self.assertEqual(bot.delete_message.await_count, 1)                           # второй раз не удаляет
        bot.delete_message.side_effect = RuntimeError("no rights")
        with mock.patch("core.topics.send_owner", mock.AsyncMock()) as send:
            await moderation.check(self._msg("залетай https://t.me/+AbCdEf"), bot, db, settings, None)
            self.assertEqual(len(send.call_args.kwargs["reply_markup"].inline_keyboard[0]), 2)
            self.assertIn("Удалить", send.call_args.kwargs["reply_markup"].inline_keyboard[0][0].text)   # не вышло — обычная карточка
        await db.close()

class TestBrowser(unittest.IsolatedAsyncioTestCase):
    """v4.86: браузер Obscura — разбор ссылок, защита форм, протокол CDP на поддельном сервере."""

    def test_check_url(self):
        from core import browser
        self.assertEqual(browser.check_url(" https://example.com/a "), "https://example.com/a")
        for bad in ("javascript:alert(1)", "file:///etc/passwd", "ftp://x.ru", "https://", ""):
            with self.assertRaises(browser.BrowserError):
                browser.check_url(bad)

    def test_sensitive_fields_and_values(self):
        from core.browser import looks_sensitive
        self.assertTrue(looks_sensitive({"type": "password", "name": "p"}))
        self.assertTrue(looks_sensitive({"type": "text", "name": "x", "label": "Номер карты"}))
        self.assertTrue(looks_sensitive({"type": "text", "name": "x", "autocomplete": "cc-number"}))
        self.assertTrue(looks_sensitive({"type": "text", "name": "comment"}, "4111 1111 1111 1111"))
        self.assertFalse(looks_sensitive({"type": "tel", "name": "phone", "label": "Телефон"}, "+79991234567"))
        self.assertFalse(looks_sensitive({"type": "text", "name": "guests"}, "4"))

    def test_ddg_link(self):
        from core.browser import ddg_link
        self.assertEqual(ddg_link("//duckduckgo.com/l/?uddg=https%3A%2F%2Fcafe.ru%2Fmenu&rut=x"), "https://cafe.ru/menu")
        self.assertEqual(ddg_link("https://cafe.ru/"), "https://cafe.ru/")

    def test_clean_md_and_merge_fields(self):
        from core.browser import clean_md, merge_fields
        self.assertEqual(clean_md("![logo](data:image/svg+xml;base64,AAAA)\n\n\n\n# Кафе ![x](https://a/b.png)"), "# Кафе")
        self.assertEqual(clean_md("[](https://zoon.ru/)\n\t\t\n \t \n\t\tПоиск   по названию\n\n\n\nМеню"), "Поиск по названию\n\nМеню")
        rows = [{"form": 0, "key": "size", "type": "radio", "value": "small", "options": []},
                {"form": 0, "key": "size", "type": "radio", "value": "large", "options": []},
                {"form": 0, "key": "name", "type": "text", "value": "", "options": []}]
        merged = merge_fields(rows)
        self.assertEqual([(m["key"], m["options"]) for m in merged], [("size", ["small", "large"]), ("name", [])])

    def test_excerpt_keeps_places_after_menu(self):
        from core.browser import excerpt
        menu = "\n".join(f"[Раздел {i}](https://site.ru/r{i})\nБульвар Рокоссовского\nЛенинский проспект" for i in range(400))
        places = "\n".join(f"Хинкальная №{i}\nрейтинг 4,{i}\nул. Казакова, {i}\n+7 (495) 000-00-0{i}" for i in range(8))
        out = excerpt(menu + "\n" + places, limit=6000)
        self.assertNotIn("https://", out)
        self.assertEqual(sum(f"ул. Казакова, {i}" in out for i in range(8)), 8)   # все места, а не только начало
        self.assertIn("Хинкальная №7", out)
        self.assertEqual(excerpt("[Кафе](https://c.ru) ул. Мира 1"), "Кафе ул. Мира 1")

    def test_clean_md_drops_dot_lines(self):
        from core.browser import clean_md
        self.assertEqual(clean_md("." * 77 + "\n" + "." * 77 + "\nХинкальная, ул. Казакова 8\n---"), "Хинкальная, ул. Казакова 8\n---")

    def test_bot_wall(self):
        from core.browser import is_bot_wall
        self.assertTrue(is_bot_wall("Мы проверяем, что вы не робот, через несколько секунд вы будете перенаправлены на сайт."))
        self.assertTrue(is_bot_wall("Checking your browser before accessing example.com"))
        self.assertTrue(is_bot_wall("## Устанавливаем безопасное соединение с Restoclub\n\nПожалуйста, подождите ..."))
        self.assertFalse(is_bot_wall("Хинкальная у Курской, меню и цены. " * 60 + "captcha"))
        self.assertFalse(is_bot_wall("Хинкальная у Курской, бронь по телефону"))

    def test_plan_problems(self):
        from core.browser import plan_problems
        fields = [{"form": 0, "key": "name", "type": "text"}, {"form": 0, "key": "card", "type": "text"}]
        self.assertEqual(plan_problems(fields, 0, {"name": "Михаил"}), [])
        probs = plan_problems(fields, 0, {"name": "М", "card": "1", "date": "пт"})
        self.assertEqual(len(probs), 2)
        self.assertTrue(any("date" in p for p in probs) and any("card" in p for p in probs))

    async def _fake_obscura(self, results: dict):
        """Сервер с ответами CDP по имени метода. Возвращает (url, лог вызовов)."""
        from aiohttp import web
        calls = []

        async def version(req):
            if req.headers.get("Authorization") != "Bearer t":
                return web.Response(status=401)
            return web.json_response({"webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/browser"})

        async def ws(req):
            sock = web.WebSocketResponse()
            await sock.prepare(req)
            async for msg in sock:
                data = msg.json()
                calls.append(data)
                res = results.get(data["method"], {})
                if res == "hang":                     # сайт, который не отвечает
                    continue
                await sock.send_json({"id": data["id"], **({"error": res["error"]} if "error" in res else {"result": res})})
            return sock

        app = web.Application()
        app.router.add_get("/json/version", version)
        app.router.add_get("/devtools/browser", ws)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.addAsyncCleanup(runner.cleanup)
        return f"http://127.0.0.1:{port}", calls

    async def test_read_over_cdp(self):
        from unittest import mock
        from core import browser
        url, calls = await self._fake_obscura({"Target.createTarget": {"targetId": "T1"},
                                               "Target.attachToTarget": {"sessionId": "S1"},
                                               "LP.getMarkdown": {"markdown": "# Кафе\nбронь по телефону"}})
        with mock.patch.object(browser, "OBSCURA_URL", url), mock.patch.object(browser, "token", return_value="t"):
            text = await browser.read("https://cafe.ru")
        self.assertIn("бронь по телефону", text)
        methods = [c["method"] for c in calls]
        self.assertEqual(methods, ["Target.createTarget", "Target.attachToTarget", "Page.navigate", "LP.getMarkdown", "Target.closeTarget"])
        nav = calls[2]
        self.assertEqual((nav["sessionId"], nav["params"]["waitUntil"]), ("S1", "load"))
        self.assertNotIn("sessionId", calls[0])

    async def test_search_and_errors(self):
        from unittest import mock
        from core import browser
        rows = [{"title": " Хинкальная ", "href": "//duckduckgo.com/l/?uddg=https%3A%2F%2Fkhinkali.ru", "snippet": "у Курской "}]
        url, _ = await self._fake_obscura({"Target.createTarget": {"targetId": "T"}, "Target.attachToTarget": {"sessionId": "S"},
                                           "Runtime.evaluate": {"result": {"value": rows}}})
        with mock.patch.object(browser, "OBSCURA_URL", url), mock.patch.object(browser, "token", return_value="t"):
            found = await browser.search("хинкальная")
        self.assertEqual(found, [{"title": "Хинкальная", "url": "https://khinkali.ru", "snippet": "у Курской"}])

        url, _ = await self._fake_obscura({"Target.createTarget": {"targetId": "T"}, "Target.attachToTarget": {"sessionId": "S"},
                                           "Page.navigate": {"errorText": "net::ERR_NAME_NOT_RESOLVED"}})
        with mock.patch.object(browser, "OBSCURA_URL", url), mock.patch.object(browser, "token", return_value="t"):
            with self.assertRaises(browser.BrowserError):
                await browser.read("https://nope.invalid")
        with mock.patch.object(browser, "OBSCURA_URL", url), mock.patch.object(browser, "token", return_value="wrong"):
            with self.assertRaises(browser.BrowserError):
                await browser.read("https://cafe.ru")
        with mock.patch.object(browser, "token", return_value=""):
            self.assertFalse(browser.available())
            with self.assertRaises(browser.BrowserError):
                await browser.read("https://cafe.ru")

    async def test_slow_site_is_browser_error(self):
        from unittest import mock
        from core import browser
        url, _ = await self._fake_obscura({"Target.createTarget": {"targetId": "T"}, "Target.attachToTarget": {"sessionId": "S"},
                                           "Page.navigate": "hang"})
        with mock.patch.object(browser, "OBSCURA_URL", url), mock.patch.object(browser, "token", return_value="t"), \
             mock.patch.object(browser, "_NAV_TIMEOUT", 0.3):
            with self.assertRaisesRegex(browser.BrowserError, "не ответил"):
                await browser.read("https://slow.ru")

    async def test_card_stops_thinking_indicator(self):
        import asyncio
        from unittest import mock
        from core import richmsg
        push = mock.AsyncMock(return_value=False)
        with mock.patch.object(richmsg, "_push_draft", push):
            async with richmsg.thinking(object(), 42, None, "🔥 Думаю", timer=True) as t:
                await asyncio.sleep(0.05)
                await richmsg.pause_thinking(42)            # карточка ушла
                n = push.await_count
                self.assertTrue(t._task.done())
                await asyncio.sleep(0.1)
                self.assertEqual(push.await_count, n)       # больше не перерисовываем
            await richmsg.pause_thinking(42)                # после выхода — ничего не делает
        self.assertNotIn(42, richmsg._active_thinking)

    async def test_explore_skips_closed_and_empty(self):
        import asyncio
        from unittest import mock
        from core import browser, mira_tools
        rows = [{"title": t, "url": f"https://{t}.ru", "snippet": ""} for t in ("zoon", "empty", "afisha", "yell", "spravker", "orgzz", "x")]

        async def fake_read(u):
            if "zoon" in u:
                raise browser.BrowserError("сайт закрыт проверкой «я не робот»")
            if "afisha" in u:
                await asyncio.sleep(0.05)
            if "spravker" in u:
                await asyncio.sleep(5)                 # медленный сайт не должен держать ответ
            return "мало" if "empty" in u else f"Хинкальная, ул. Казакова 8 ({u}) " * 20

        with mock.patch.object(browser, "search", mock.AsyncMock(return_value=rows)), mock.patch.object(browser, "read", fake_read):
            t0 = asyncio.get_running_loop().time()
            res = await browser.explore("грузинский ресторан", pages=2, tries=5)
            self.assertLess(asyncio.get_running_loop().time() - t0, 1)
            self.assertEqual([r["title"] for r, _ in res["pages"]], ["afisha", "yell"])     # порядок выдачи
            self.assertEqual(sorted(u for u, _ in res["skipped"]), ["https://empty.ru", "https://zoon.ru"])
            self.assertEqual([r["title"] for r in res["more"]], ["spravker", "orgzz", "x"])
            out = await mira_tools._browse({"action": "find", "query": "грузинский ресторан"})
        self.assertIn("=== afisha", out)
        self.assertIn("Не открылись: https://zoon.ru", out)
        self.assertIn("Ещё ссылки: spravker", out)
        with mock.patch.object(browser, "read", mock.AsyncMock(side_effect=browser.BrowserError("закрыт"))):
            self.assertIn("следующую ссылку", await mira_tools._browse({"action": "read", "url": "https://zoon.ru"}))

    async def test_form_card_and_submit_only_by_button(self):
        import aiosqlite
        from unittest import mock
        from core import browser, mira_tools
        from db import get_setting
        db = await aiosqlite.connect(":memory:")
        await db.execute("CREATE TABLE settings_kv (key TEXT PRIMARY KEY, value TEXT)")
        fields = [{"form": 0, "key": "name", "type": "text"}, {"form": 0, "key": "guests", "type": "number"}]
        msg = mock.MagicMock(); msg.reply = mock.AsyncMock()
        with mock.patch.object(browser, "form_fields", mock.AsyncMock(return_value=fields)), \
             mock.patch.object(browser, "submit", mock.AsyncMock(return_value={"text": "Спасибо, ждём вас", "url": "https://cafe.ru/ok", "pay": False})) as sub:
            out = await mira_tools._form_card({"url": "https://cafe.ru/book", "values": {"name": "Михаил", "guests": 4},
                                               "summary": "Стол на пятницу 20:00"}, message=msg, db=db, dry=False)
            self.assertIn("кнопка владельца", out)
            sub.assert_not_called()                       # из диалога ничего не уходит
            tok = msg.reply.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data.split(":", 1)[1]
            res = await mira_tools.submit_form(db, None, tok)
            self.assertIn("Спасибо, ждём вас", res)
            sub.assert_called_once_with("https://cafe.ru/book", 0, {"name": "Михаил", "guests": "4"})
            self.assertEqual(await mira_tools.submit_form(db, None, tok), "Карточка устарела")   # второе нажатие
            self.assertEqual(await get_setting(db, f"webform:{tok}"), "")
            bad = await mira_tools._form_card({"url": "https://cafe.ru/book", "values": {"card": "4111111111111111"},
                                               "summary": "x"}, message=msg, db=db, dry=False)
            self.assertIn("не могу заполнить", bad)
        await db.close()

    async def test_payment_link_instead_of_card(self):
        import aiosqlite
        from unittest import mock
        from core import browser, mira_tools
        # после отправки сайт ведёт на оплату — Mira присылает ссылку, карту не вводит
        db = await aiosqlite.connect(":memory:")
        await db.execute("CREATE TABLE settings_kv (key TEXT PRIMARY KEY, value TEXT)")
        fields = [{"form": 0, "key": "name", "type": "text"}]
        msg = mock.MagicMock(); msg.reply = mock.AsyncMock()
        paid = {"text": "Бронь создана. Внесите депозит 2000 ₽", "url": "https://yookassa.ru/checkout/abc", "pay": True}
        with mock.patch.object(browser, "form_fields", mock.AsyncMock(return_value=fields)), \
             mock.patch.object(browser, "submit", mock.AsyncMock(return_value=paid)):
            await mira_tools._form_card({"url": "https://cafe.ru/book", "values": {"name": "Михаил"}, "summary": "Стол"},
                                        message=msg, db=db, dry=False)
            tok = msg.reply.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data.split(":", 1)[1]
            res = await mira_tools.submit_form(db, None, tok)
        self.assertIn("дошла до оплаты", res)
        self.assertIn("https://yookassa.ru/checkout/abc", res)
        await db.close()
        # карта прямо в форме брони — карточки нет, только ссылка и что вписать
        fields = [{"form": 0, "key": "name", "type": "text"},
                  {"form": 0, "key": "cc_number", "type": "text", "label": "Номер карты", "required": True}]
        msg2 = mock.MagicMock(); msg2.reply = mock.AsyncMock()
        with mock.patch.object(browser, "form_fields", mock.AsyncMock(return_value=fields)):
            out = await mira_tools._form_card({"url": "https://hotel.ru/book", "values": {"name": "Михаил"}, "summary": "Номер"},
                                              message=msg2, db=None, dry=False)
        self.assertIn("оплата картой", out)
        self.assertIn("https://hotel.ru/book", out)
        msg2.reply.assert_not_called()

    def test_needs_payment(self):
        from core.browser import needs_payment
        self.assertTrue(needs_payment("https://yoomoney.ru/checkout/x", [], [], ""))
        self.assertTrue(needs_payment("https://cafe.ru/done", ["https://widget.cloudpayments.ru/frame"], [], ""))
        self.assertTrue(needs_payment("https://cafe.ru/done", [], [{"key": "cvv", "type": "text"}], ""))
        self.assertTrue(needs_payment("https://cafe.ru/done", [], [], "Бронь почти готова. Перейти к оплате"))
        self.assertFalse(needs_payment("https://cafe.ru/done", [], [], "Спасибо! Ждём вас в пятницу в 20:00"))
        self.assertFalse(needs_payment("https://cafe.ru/login", [], [{"key": "password", "type": "password"}], "Войти"))

    def test_tools_registered(self):
        from core import connectors, mira, mira_tools
        self.assertEqual(mira_tools.NAMES["browse"], "browser")
        self.assertIn("web_form", mira.WRITE_TOOLS)
        self.assertNotIn("web_form", [t["name"] for t in mira.READ_TOOLS])
        self.assertIn("browse", [t["name"] for t in mira.READ_TOOLS])
        self.assertIn("browser", [c["id"] for c in connectors.CONNECTORS])


class TestMiraApi(unittest.TestCase):
    """v4.62: вход «Мира в чёлке» — авторизация и привязка только к localhost."""

    def test_auth(self):
        from types import SimpleNamespace as N
        from core.mira_api import _authorized
        self.assertTrue(_authorized(N(headers={"Authorization": "Bearer abc"}), "abc"))
        self.assertFalse(_authorized(N(headers={"Authorization": "Bearer abd"}), "abc"))
        self.assertFalse(_authorized(N(headers={}), "abc"))
        self.assertFalse(_authorized(N(headers={"Authorization": "Bearer "}), ""))   # пустой токен = выключено

    def test_localhost_only(self):
        import inspect
        import core.mira_api as m
        src = inspect.getsource(m.start)
        self.assertIn('"127.0.0.1"', src)
        self.assertNotIn("0.0.0.0", src)


class TestFindSlotsDay(unittest.TestCase):
    """v4.61.2: окна конкретного дня за пределами 4-дневного горизонта."""

    def test_only_day_far(self):
        import asyncio, aiosqlite
        from types import SimpleNamespace as N
        from datetime import timedelta
        from core.meeting import find_slots
        from core.util import today_local
        st = N(google_calendar_id=None, work_start_hour=10, work_end_hour=20)
        target = today_local() + timedelta(days=6)

        async def run():
            db = await aiosqlite.connect(":memory:")
            slots = await find_slots(st, db, days=1, count=12, only_day=target)
            await db.close()
            return slots
        slots = asyncio.run(run())
        self.assertTrue(slots)
        self.assertTrue(all(s.startswith(target.isoformat()) for s in slots))
        self.assertGreater(len(slots), 1)


class TestClaude5(unittest.TestCase):
    """v4.61: миграция на Sonnet 5.5 — параметры запроса и чтение ответа по типу блока."""

    def test_adapt_params(self):
        from core.llm import adapt_params
        k = adapt_params("claude-sonnet-5-5", {"model": "claude-sonnet-5-5", "max_tokens": 80, "temperature": 0})
        self.assertNotIn("temperature", k)
        self.assertEqual(k["thinking"], {"type": "between_tools"})
        self.assertIn(k["output_config"]["effort"], ("low", "medium", "high"))
        k2 = adapt_params("claude-sonnet-5-5", {"thinking": {"type": "adaptive"}, "output_config": {"effort": "medium"}})
        self.assertEqual(k2["thinking"], {"type": "adaptive"}); self.assertEqual(k2["output_config"]["effort"], "medium")
        k4 = adapt_params("claude-haiku-5-5", {"model": "claude-haiku-5-5", "max_tokens": 60, "temperature": 0})
        self.assertEqual(k4["thinking"], {"type": "disabled"}); self.assertNotIn("temperature", k4)
        self.assertEqual(k4["max_tokens"], 600)   # запас под новый токенизатор
        self.assertEqual(adapt_params("claude-haiku-5-5", {"max_tokens": 1024})["max_tokens"], 2048)
        k3 = adapt_params("claude-haiku-4-5", {"temperature": 0, "max_tokens": 10})
        self.assertEqual(k3, {"temperature": 0, "max_tokens": 10})   # Haiku 4.5 — без изменений

    def test_text_of(self):
        from types import SimpleNamespace as N
        from core.llm import text_of
        r = N(content=[N(type="thinking", thinking=""), N(type="text", text='{"a":1}')])
        self.assertEqual(text_of(r), '{"a":1}')
        self.assertEqual(text_of(N(content=[N(type="text", text="ok")])), "ok")

    def test_no_positional_reads(self):
        import pathlib, re
        root = pathlib.Path(__file__).resolve().parent.parent
        bad = [str(p) for d in ("core", "handlers") for p in (root / d).glob("*.py")
               if p.name != "llm.py" and re.search(r"\.content\[0\]\.text", p.read_text())]
        self.assertEqual(bad, [])

    def test_pricing(self):
        from db import model_cost
        self.assertAlmostEqual(model_cost("claude-sonnet-5-5", 1_000_000, 0), 2.0)
        self.assertAlmostEqual(model_cost("claude-sonnet-4-5", 1_000_000, 0), 3.0)
        self.assertAlmostEqual(model_cost("claude-haiku-4-5", 0, 1_000_000), 5.0)
        self.assertAlmostEqual(model_cost("claude-haiku-5-5", 1_000_000, 1_000_000), 0.60)
        self.assertAlmostEqual(model_cost("claude-sonnet-5-5", 0, 0, cache_read=1_000_000), 0.10)


class TestSubscriptions(unittest.TestCase):
    """v4.59: подписки — разбор ввода, периоды, итог, roll_forward."""

    def test_parse(self):
        from core import subscriptions as S
        from datetime import date
        self.assertEqual(S.parse_amount("20$"), (20.0, "$"))
        self.assertEqual(S.parse_amount("199 ₽"), (199.0, "₽"))
        self.assertEqual(S.parse_amount("€9,99"), (9.99, "€"))
        self.assertEqual(S.parse_amount(20), (20.0, None))
        self.assertEqual(S.norm_period("ежемесячно"), "month")
        self.assertEqual(S.norm_period("в год"), "year")
        self.assertEqual(S.norm_currency("руб"), "₽")
        t = date(2026, 9, 22)
        self.assertEqual(S.parse_next_charge("5", today=t), date(2026, 10, 5))
        self.assertEqual(S.parse_next_charge("25-го", today=t), date(2026, 9, 25))
        self.assertEqual(S.parse_next_charge("12.10", today=t), date(2026, 10, 12))
        self.assertEqual(S.parse_next_charge("01.03", today=t), date(2027, 3, 1))
        self.assertEqual(S.parse_next_charge("2026-11-30", today=t), date(2026, 11, 30))
        self.assertEqual(S.parse_next_charge("завтра", today=t), date(2026, 9, 23))
        self.assertIsNone(S.parse_next_charge("хз", today=t))
        self.assertEqual(S.add_period(date(2026, 1, 31), "month"), date(2026, 2, 28))
        self.assertEqual(S.add_period(date(2026, 9, 22), "year"), date(2027, 9, 22))

    def test_totals(self):
        from core import subscriptions as S
        subs = [{"amount": 199, "currency": "₽", "period": "month"}, {"amount": 1200, "currency": "₽", "period": "year"},
                {"amount": 20, "currency": "$", "period": "month"}]
        self.assertEqual(S.totals(subs), "299 ₽ + 20 $")

    def test_crud_and_roll(self):
        import asyncio, aiosqlite
        from core import subscriptions as S
        from datetime import date

        async def run():
            db = await aiosqlite.connect(":memory:")
            r = await S.add(db, "ChatGPT", "20$", period="month", next_charge="2026-09-01")
            self.assertEqual((r["amount"], r["currency"], r["period"]), (20.0, "$", "month"))
            r2 = await S.add(db, "chatgpt", 25, "$", "month", "2026-10-05")   # то же имя → обновление
            self.assertTrue(r2["updated"]); self.assertEqual(r2["amount"], 25.0)
            self.assertEqual(len(await S.list_all(db)), 1)
            await S.add(db, "Spotify", 199, "₽", "month", "2026-08-15")
            n = await S.roll_forward(db, today=date(2026, 9, 22))
            self.assertEqual(n, 2)   # 15.08 и 15.09 прошли
            sp = (await S.find(db, "spot"))
            self.assertEqual(sp["next_charge"], "2026-10-15")
            async with db.execute("SELECT COUNT(*) FROM subscription_log") as c:
                self.assertEqual((await c.fetchone())[0], 2)
            r3 = await S.update_by_args(db, sp, {"new_name": "Spotify Premium", "amount": "249₽", "next_charge": "2026-11-01"})
            self.assertEqual((r3["name"], r3["amount"], r3["next_charge"]), ("Spotify Premium", 249.0, "2026-11-01"))
            await S.cancel(db, sp["id"])
            self.assertEqual(len(await S.list_all(db)), 1)
            self.assertEqual(len(await S.list_all(db, active_only=False)), 2)
            html = S.render_rich(await S.list_all(db), today=date(2026, 10, 4))
            self.assertIn("sub:del:", html); self.assertIn("завтра", html)
            await db.close()
        asyncio.run(run())


class TestV458(unittest.TestCase):
    """v4.58: предохранители переименования + отмена не в начале названия + голос в отдельном процессе."""

    def test_retitle_guard(self):
        from core.actions import _retitle_guard as g
        ok = [
            ("Перевести Владосу деньги за еду из БК", "Перевести Владосу 4120₽ (пиво 990₽, калик 1560₽, БК 1425₽)", "с тебя 4120: пиво 990, калик 1560, бк 1425"),
            ("❓ Встреча с @friend_eva", "Встреча с @friend_eva в 17:00", "давай в 17:00"),
            ("Командный проект по Питону (Горбатов)", "Командный проект по Питону (Горбатов): №17 — Импорт/экспорт данных", "тема №17"),
            ("Стажировка в Альфа-банке (@recruiter_n)", "Стажировка в Альфа-банке (@recruiter_n) 16.09 в 8:30, м. ЦСКА, Гризодубовой 2", "16.09 в 8:30"),
            ("Убрать бутылку водки (она должна быть выпита)", "Убрать бутылку водки (Саня предложил отвезти Владу)", ""),
        ]
        for o, n, f in ok:
            self.assertIsNone(g(o, n, f), n)
        bad = [
            ("Перевести Владосу 3535₽ (кальянная, настойка, такси)", "Перевести Владосу 3975₽ (пиво 990₽, калик 1560₽, бк 1425₽)", "in: Ктстати можно"),
            ("Прийти к @nika_demo на Арбат (она одна и скучно)", "Отвезти @nika_demo домой на Автозаводскую 23 корп.2", "отвези меня домой"),
        ]
        for o, n, f in bad:
            self.assertTrue(g(o, n, f), n)

    def test_cancel_anywhere(self):
        from core.actions import _looks_cancelled as c
        for t in ["Пригласить @friend_eva на ДР 14 сентября в кальянку (отменено: съёмка)", "Собеседование отложено (слишком много мест)",
                  "Ника уже заказала такси домой (белая Kia)", "Ника уже едет домой на такси, задача неактуальна"]:
            self.assertTrue(c(t), t)
        for t in ["Перевести Владосу 3975₽", "Не забыть паспорт", "Уточнить, отменили ли пару", "Встреча с Никой после пар"]:
            self.assertFalse(c(t), t)

    def test_transcribe_is_out_of_process(self):
        import inspect
        import core.transcribe as tr
        src = inspect.getsource(tr)
        self.assertIn("core.transcribe_worker", src)
        self.assertNotIn("WhisperModel(", src)          # модель не должна жить в процессе бота
        self.assertTrue(hasattr(tr, "_sem"))

    def test_day_block_marks_current_pair(self):
        import inspect
        import core.mira as m
        src = inspect.getsource(m._day_block)
        self.assertIn("ИДЁТ СЕЙЧАС", src); self.assertIn("СЛЕДУЮЩАЯ", src)

    def test_settle_passes_fresh_thread(self):
        import inspect
        import core.tasks_loop as tl
        self.assertIn("thread_text=recent", inspect.getsource(tl))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestContactMemorySlice(unittest.TestCase):
    """v4.65: в промпт обновления — выборка памяти по смыслу; повторы по смыслу сворачиваются в существующие."""

    @staticmethod
    def _vec(text):
        # «эмбеддинг» по ключевым словам: одинаковая тема → один вектор
        t = text.lower()
        for i, w in enumerate(["машин", "кошк", "работ", "долг", "суши"]):
            if w in t:
                return [1.0 if j == i else 0.0 for j in range(6)]
        return [0.0] * 5 + [1.0]

    def _run(self, coro_fn):
        import asyncio, aiosqlite, json
        from unittest import mock
        import core.contact_memory as D
        from core.embeddings import pack

        async def fake_embed(text):
            return self._vec(text)

        async def run():
            db = await aiosqlite.connect(":memory:")
            await D._ensure_table(db)
            secs = {"key": "лучший друг", "property": "12.09.26: машина Kia Rio серая",
                    "personal": "живёт с кошкой Мусей", "study_work": "; ".join(f"работа запись номер {i} про офис" for i in range(1500)),
                    "money": "долг 3000 за пиццу"}
            fj = json.dumps({"sections": secs, "timeline": [{"date": f"0{i % 9 + 1}.09", "event": f"событие {i}"} for i in range(20)]},
                            ensure_ascii=False)
            for sec, entry in D.entries_of(fj):
                await db.execute("INSERT INTO contact_memory_vectors(user_id, section, entry, vector, created_at) VALUES (1,?,?,?,0)",
                                 (sec, entry, pack(self._vec(f"{sec}: {entry}"))))
            await db.commit()
            with mock.patch("core.embeddings.embed", fake_embed):
                r = await coro_fn(D, db, {"user_id": 1, "username": "x", "facts_json": fj})
            await db.close()
            return r
        return asyncio.run(run())

    def test_slice_keeps_key_and_relevant_counts_rest(self):
        out = self._run(lambda D, db, p: D.prompt_slice(db, p, "#1 Он: купил новые диски на машину\n#2 Ты: класс"))
        self.assertIsNotNone(out)
        self.assertIn("лучший друг", out)                 # «ключевое» — всегда целиком
        self.assertIn("Kia Rio", out)                     # близкое к переписке
        self.assertNotIn("номер 7 про офис", out)         # старое нерелевантное — только счётчиком
        self.assertIn("номер 1499 про офис", out)         # v4.67: последние 10 записей раздела — всегда
        self.assertIn("номер 1490 про офис", out)
        self.assertNotIn("номер 1489 про офис", out)
        self.assertIn("ещё 1490 записей", out)
        self.assertIn("событие 19", out)                  # свежая хронология
        self.assertNotIn("событие 3;", out)

    def test_small_contact_memory_goes_full(self):
        import json
        out = self._run(lambda D, db, p: D.prompt_slice(db, dict(p, facts_json=json.dumps({"sections": {"key": "друг"}})), "машина"))
        self.assertIsNone(out)

    def test_near_dup_folds_into_existing(self):
        add = {"property": [{"t": "ездит на серой машине Kia", "c": "факт", "src": [3]}],
               "money": ["у него кошка"],                 # повтор из другой секции — отбросить
               "prefs": ["любит суши", "обожает суши"]}   # новое и повтор внутри ответа
        kept, n = self._run(lambda D, db, p: D.drop_near_duplicates(db, 1, add))
        self.assertEqual(n, 3)
        self.assertEqual(kept["property"][0]["t"], "12.09.26: машина Kia Rio серая")   # текст существующей → merge бампнет cf
        self.assertEqual(kept["property"][0]["src"], [3])
        self.assertNotIn("money", kept)
        self.assertEqual(kept["prefs"], ["любит суши", "любит суши"])   # новое осталось, повтор свернулся в него (merge склеит)


class TestCommandLog(unittest.TestCase):
    """v4.71: в лог идёт только название команды владельца, сбой лога команду не ломает."""

    def test_command_name(self):
        from core.command_log import command_name
        self.assertEqual(command_name("/about @nika что она любит"), "about")
        self.assertEqual(command_name("/Stats@Sort38_bot"), "stats")
        self.assertEqual(command_name("/обещания"), "обещания")
        for text in ("купи молоко", "/opt/tg-business-sorter", "", None):
            self.assertEqual(command_name(text), "")

    def _feed(self, sender_id, text, db=None):
        import asyncio
        import datetime
        from types import SimpleNamespace
        from aiogram import Bot, Dispatcher, Router
        from aiogram.filters import Command
        from aiogram.types import Chat, Message, Update, User
        from core.command_log import log_commands

        async def run():
            calls = []
            dp, router = Dispatcher(), Router()
            dp.message.outer_middleware(log_commands)

            @router.message(Command("about"))
            async def about(message):
                calls.append(message.text)

            dp.include_router(router)
            msg = Message(message_id=1, date=datetime.datetime(2026, 10, 31), text=text,
                          chat=Chat(id=sender_id, type="private"),
                          from_user=User(id=sender_id, is_bot=False, first_name="x"))
            own_db = db or await self._memory_db()
            await dp.feed_update(Bot("123:" + "a" * 35), Update(update_id=1, message=msg),
                                 db=own_db, settings=SimpleNamespace(owner_user_id=1))
            rows = []
            if not isinstance(own_db, str):
                async with own_db.execute("SELECT command FROM command_log") as cur:
                    rows = [r[0] for r in await cur.fetchall()]
                await own_db.close()
            return calls, rows

        return asyncio.run(run())

    @staticmethod
    async def _memory_db():
        import aiosqlite
        db = await aiosqlite.connect(":memory:")
        await db.execute("CREATE TABLE command_log (command TEXT)")
        return db

    def test_owner_logged_name_only(self):
        calls, rows = self._feed(1, "/about @nika секрет")
        self.assertEqual(rows, ["about"])
        self.assertEqual(calls, ["/about @nika секрет"])   # сама команда отработала

    def test_stranger_not_logged(self):
        self.assertEqual(self._feed(2, "/about @nika")[1], [])

    def test_log_failure_keeps_command(self):
        calls, _ = self._feed(1, "/about @nika", db="сломанная база")
        self.assertEqual(calls, ["/about @nika"])


class TestRealThreadInput(unittest.TestCase):
    """v4.70: eval на настоящих переписках собирает вход тем же кодом, что и бот, на момент задачи."""

    def test_settle_context(self):
        from core.actions import build_settle_context
        text = build_settle_context("[2026-10-30] Ты: завтра покажи", "память")
        self.assertLess(text.index("завтра покажи"), text.index("память"))
        self.assertNotIn("Справка", build_settle_context("тред"))

    def test_until_cuts_context(self):
        import asyncio
        import aiosqlite
        from db import vault_get_context

        async def run():
            async with aiosqlite.connect(":memory:") as db:
                await db.execute("CREATE TABLE conversations (user_id INT, direction TEXT, username TEXT, "
                                 "text TEXT, media_type TEXT, created_at TEXT)")
                await db.executemany("INSERT INTO conversations VALUES (1,'in','x',?,NULL,?)", [
                    ("раньше окна", "2026-10-27 12:00:00"), ("в окне", "2026-10-29 12:00:00"),
                    ("после среза", "2026-10-30 12:00:00")])
                return await vault_get_context(db, 1, limit=25, since_hours=48, until="2026-10-30 00:00:00")

        text = asyncio.run(run())
        self.assertIn("в окне", text)
        self.assertNotIn("раньше окна", text)
        self.assertNotIn("после среза", text)


class TestChecklistDone(unittest.TestCase):
    """v4.70: галочка закрывает, снятая галочка возвращает, без маппинга — след в логе."""

    def _run(self, ctd, reply_to, rows):
        import asyncio
        from types import SimpleNamespace
        from unittest import mock
        from handlers.business import _handle_checklist_done

        async def get_setting(db, key):
            return '{"1": 10, "2": 20}' if key == "chk:7" else None

        async def get_sync(db, sync_id):
            return rows.get(sync_id)

        done, back, sent = [], [], []
        async def note(lst, *a):
            lst.append(a[-1])
        with mock.patch("db.get_setting", get_setting), mock.patch("db.get_sync_by_id", get_sync), \
             mock.patch("db.set_setting", mock.AsyncMock()), \
             mock.patch("db.mark_sync_done_by_id", lambda d, i: note(done, i)), \
             mock.patch("db.reopen_task_sync_by_id", lambda d, i: note(back, i)), \
             mock.patch("core.sync.complete_todoist_task", mock.AsyncMock()), \
             mock.patch("core.actions._reopen_todoist", mock.AsyncMock()), \
             mock.patch("core.actions._delete_task_calendar_event", mock.AsyncMock()), \
             mock.patch("core.topics.send_owner", lambda b, o, t, text: note(sent, text)):
            settings = SimpleNamespace(todoist_api_token="t", owner_user_id=1)
            asyncio.run(_handle_checklist_done(None, None, settings, ctd, reply_to))
        return done, back, sent

    def test_done_and_undone(self):
        from types import SimpleNamespace as NS
        rows = {10: {"title": "a", "synced": 0, "todoist_id": "1"}, 20: {"title": "b", "synced": 1, "todoist_id": "2"}}
        ctd = NS(checklist_message=NS(message_id=7), marked_as_done_task_ids=[1], marked_as_not_done_task_ids=[2])
        done, back, sent = self._run(ctd, None, rows)
        self.assertEqual((done, back), ([10], [20]))
        self.assertIn("Закрыла", sent[0])
        self.assertIn("Вернула", sent[0])

    def test_reply_fallback_and_no_mapping(self):
        from types import SimpleNamespace as NS
        rows = {10: {"title": "a", "synced": 0, "todoist_id": "1"}}
        ctd = NS(checklist_message=None, marked_as_done_task_ids=[1], marked_as_not_done_task_ids=None)
        self.assertEqual(self._run(ctd, NS(message_id=7), rows)[0], [10])
        self.assertEqual(self._run(ctd, None, rows)[0], [])


class TestAccessAlert(unittest.TestCase):
    """v4.66: 403/401 от Anthropic → тревога владельцу (раз в 6 ч), первый успех → «снова отвечает»."""

    def test_alert_and_recovery(self):
        import asyncio, os, tempfile
        from unittest import mock
        import core.llm as L

        class PermissionDeniedError(Exception):
            status_code = 403

        err = PermissionDeniedError("Error code: 403 - {'type': 'error', 'error': {'type': 'permission_error', 'message': "
                                    "\"This organization can't receive replies from Claude right now. See https://x.example/verify\", "
                                    "'details': {'error_code': 'verification_required'}}}")
        sent = []

        class FakeClient:
            def __init__(self):
                self.fail = True
                self.messages = self

            async def create(self, **kw):
                if self.fail:
                    raise err
                return "ok"

        async def run(tmp):
            c = FakeClient()
            with mock.patch.object(L.settings, "db_path", os.path.join(tmp, "bot.db")), \
                 mock.patch.object(L.settings, "openrouter_api_key", ""), \
                 mock.patch.object(L, "_send_owner_raw", lambda t: sent.append(t) or True):
                for _ in range(3):
                    with self.assertRaises(PermissionDeniedError):
                        await L.create(c, model="claude-haiku-5-5", max_tokens=10, messages=[])
                self.assertEqual(len(sent), 1)                       # одна тревога, а не на каждый вызов
                self.assertIn("verification_required", sent[0])
                self.assertNotIn("https://", sent[0])                # ссылок из ошибки в алерте нет
                c.fail = False
                self.assertEqual(await L.create(c, model="claude-haiku-5-5", max_tokens=10, messages=[]), "ok")
                self.assertEqual(len(sent), 2)
                self.assertIn("снова отвечает", sent[1])
                await L.create(c, model="claude-haiku-5-5", max_tokens=10, messages=[])
                self.assertEqual(len(sent), 2)                       # дальше тишина
                c.fail = True                                        # новый простой — тревога сразу
                with self.assertRaises(PermissionDeniedError):
                    await L.create(c, model="claude-haiku-5-5", max_tokens=10, messages=[])
                self.assertEqual(len(sent), 3)

        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(run(tmp))

    def test_credit_balance_not_access(self):
        import core.llm as L

        class BadRequestError(Exception):
            status_code = 400
        self.assertFalse(L._is_access_error(BadRequestError("credit balance is too low")))


class TestRoutines(unittest.TestCase):
    """v4.72: расписание «Я напишу сама», доставка, запрет записи в поручениях."""

    TZ = timezone(timedelta(hours=3))

    def _dt(self, *a):
        return datetime(*a, tzinfo=self.TZ)

    def test_daily_and_weekly(self):
        from core.routines import build_schedule, next_fire
        daily = build_schedule({"every": "daily", "time": "9:00"})
        self.assertEqual(next_fire(daily, self._dt(2026, 11, 2, 8, 0)), self._dt(2026, 11, 2, 9, 0))
        self.assertEqual(next_fire(daily, self._dt(2026, 11, 2, 9, 0)), self._dt(2026, 11, 3, 9, 0))
        weekly = build_schedule({"every": "weekly", "time": "20:00", "days": ["вс", "среда"]})
        self.assertEqual(weekly["days"], [2, 6])
        self.assertEqual(next_fire(weekly, self._dt(2026, 11, 2, 12, 0)), self._dt(2026, 11, 4, 20, 0))   # пн → ср
        self.assertEqual(next_fire(weekly, self._dt(2026, 11, 8, 20, 0)), self._dt(2026, 11, 11, 20, 0))  # вс после срока → ср

    def test_monthly_short_month(self):
        from core.routines import build_schedule, next_fire
        s = build_schedule({"every": "monthly", "time": "10:00", "day": 31})
        self.assertEqual(next_fire(s, self._dt(2026, 11, 2, 0, 0)), self._dt(2026, 11, 30, 10, 0))
        self.assertEqual(next_fire(s, self._dt(2026, 12, 31, 11, 0)), self._dt(2027, 1, 31, 10, 0))

    def test_hours_stay_in_window(self):
        from core.routines import build_schedule, next_fire
        s = build_schedule({"every": "hours", "hours": 2})
        self.assertEqual(next_fire(s, self._dt(2026, 11, 2, 7, 0)), self._dt(2026, 11, 2, 9, 0))
        self.assertEqual(next_fire(s, self._dt(2026, 11, 2, 11, 30)), self._dt(2026, 11, 2, 13, 0))
        self.assertEqual(next_fire(s, self._dt(2026, 11, 2, 21, 0)), self._dt(2026, 11, 3, 9, 0))         # ночью тихо

    def test_once_and_bad_input(self):
        from datetime import date
        from core.routines import build_schedule, next_fire
        s = build_schedule({"every": "once", "time": "09:00", "date": "2026-11-02"}, today=date(2026, 11, 1))
        self.assertEqual(next_fire(s, self._dt(2026, 11, 1, 22, 0)), self._dt(2026, 11, 2, 9, 0))
        self.assertIsNone(next_fire(s, self._dt(2026, 11, 2, 9, 0)))
        for bad in ({"every": "daily", "time": "25:00"}, {"every": "weekly", "time": "9:00"},
                    {"every": "monthly", "time": "9:00", "day": 40}, {"every": "hours", "hours": 2, "from": "22:00", "to": "09:00"},
                    {"every": "once", "time": "9:00", "date": "2026-10-01"}, {"every": "yearly"}):
            with self.assertRaises(ValueError):
                build_schedule(bad, today=date(2026, 11, 1))

    def _db(self):
        import aiosqlite
        return aiosqlite.connect(":memory:")

    def _settings(self):
        from types import SimpleNamespace
        return SimpleNamespace(owner_user_id=1, anthropic_monthly_budget=0)

    def _bot(self, fail=False):
        from unittest import mock
        bot = mock.AsyncMock()
        if fail:
            bot.send_message.side_effect = RuntimeError("telegram down")
        return bot

    async def _seed(self, db, kind, every, due):
        import json
        from core import routines as R
        await R._ensure(db)
        cur = await db.execute("INSERT INTO routines (kind, text, schedule, next_run) VALUES (?,?,?,?)",
                               (kind, "выпей воды", json.dumps(every), int(due.timestamp())))
        await db.commit()
        return cur.lastrowid

    async def _next_run(self, db, rid):
        async with db.execute("SELECT next_run FROM routines WHERE id=?", (rid,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    def test_remind_delivers_once_and_advances(self):
        from unittest import mock
        from core import routines as R

        async def run():
            async with self._db() as db:
                now = self._dt(2026, 11, 2, 9, 0, 10)
                rid = await self._seed(db, "remind", {"every": "daily", "time": "09:00"}, self._dt(2026, 11, 2, 9, 0))
                bot = self._bot()
                with mock.patch("core.mira.dialog_add", mock.AsyncMock()) as said:
                    self.assertEqual(await R.tick(bot, db, self._settings(), None, now=now), 1)
                    self.assertEqual(await R.tick(bot, db, self._settings(), None, now=now), 0)
                bot.send_message.assert_awaited_once()
                said.assert_awaited_once()
                self.assertEqual(await self._next_run(db, rid), int(self._dt(2026, 11, 3, 9, 0).timestamp()))
        asyncio.run(run())

    def test_remind_retries_after_telegram_failure(self):
        from core import routines as R

        async def run():
            async with self._db() as db:
                due = self._dt(2026, 11, 2, 9, 0)
                rid = await self._seed(db, "remind", {"every": "daily", "time": "09:00"}, due)
                self.assertEqual(await R.tick(self._bot(fail=True), db, self._settings(), None, now=due), 0)
                self.assertEqual(await self._next_run(db, rid), int(due.timestamp()))
        asyncio.run(run())

    def test_downtime_is_skipped_not_replayed(self):
        from core import routines as R

        async def run():
            async with self._db() as db:
                rid = await self._seed(db, "remind", {"every": "daily", "time": "09:00"}, self._dt(2026, 11, 2, 9, 0))
                bot = self._bot()
                self.assertEqual(await R.tick(bot, db, self._settings(), None, now=self._dt(2026, 11, 2, 12, 0)), 0)
                bot.send_message.assert_not_awaited()
                self.assertEqual(await self._next_run(db, rid), int(self._dt(2026, 11, 3, 9, 0).timestamp()))
        asyncio.run(run())

    def test_once_is_deleted_after_delivery(self):
        from unittest import mock
        from core import routines as R

        async def run():
            async with self._db() as db:
                due = self._dt(2026, 11, 2, 9, 0)
                rid = await self._seed(db, "remind", {"every": "once", "time": "09:00", "date": "2026-11-02"}, due)
                with mock.patch("core.mira.dialog_add", mock.AsyncMock()):
                    await R.tick(self._bot(), db, self._settings(), None, now=due)
                self.assertIsNone(await self._next_run(db, rid))
        asyncio.run(run())

    def test_order_failure_advances_and_tells_owner(self):
        from unittest import mock
        from core import routines as R

        async def run():
            async with self._db() as db:
                due = self._dt(2026, 11, 2, 9, 0)
                rid = await self._seed(db, "order", {"every": "daily", "time": "09:00"}, due)
                bot = self._bot()
                with mock.patch("core.mira.dialog_add", mock.AsyncMock()), \
                     mock.patch("core.mira.assistant_context", mock.AsyncMock(side_effect=RuntimeError("llm"))), \
                     mock.patch("db.is_over_budget", mock.AsyncMock(return_value=False)):
                    await R.tick(bot, db, self._settings(), None, now=due)
                self.assertIn("Не вышло", bot.send_message.await_args.args[1])
                self.assertEqual(await self._next_run(db, rid), int(self._dt(2026, 11, 3, 9, 0).timestamp()))
        asyncio.run(run())

    def test_order_limits(self):
        from core import routines as R

        async def run():
            async with self._db() as db:
                with self.assertRaises(ValueError):
                    await R.add(db, {"kind": "order", "text": "курс", "every": "hours", "hours": 2})
                for _ in range(R.MAX_ORDERS):
                    await R.add(db, {"kind": "order", "text": "план", "every": "daily", "time": "09:00"})
                with self.assertRaises(ValueError):
                    await R.add(db, {"kind": "order", "text": "ещё", "every": "daily", "time": "10:00"})
                self.assertIn("добавила", await R.add(db, {"text": "вода", "every": "hours", "hours": 2}))
        asyncio.run(run())

    def test_scheduled_orders_cannot_write(self):
        from types import SimpleNamespace
        from unittest import mock
        from core import mira

        calls = []

        async def create(client, **kw):
            calls.append([t["name"] for t in kw["tools"]])
            if len(calls) == 1:
                block = SimpleNamespace(type="tool_use", id="t1", name="create_task", input={"title": "x"})
                return SimpleNamespace(content=[block], stop_reason="tool_use", usage=None)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="готово")], stop_reason="end_turn", usage=None)

        async def run():
            settings = SimpleNamespace(owner_name="Миша", anthropic_model_smart="claude-haiku-4-5", owner_address="босс")
            with mock.patch("core.mira.llm.create", create), mock.patch("core.mira._rec", mock.AsyncMock()), \
                 mock.patch("core.mira._run_tool", mock.AsyncMock()) as tool:
                reply, used, _ = await mira.think(None, settings, "ctx", "план", read_only=True)
            tool.assert_not_awaited()
            self.assertEqual(reply, "готово")
            self.assertNotIn("create_task", calls[0])
            self.assertIn("show_day", calls[0])
        asyncio.run(run())


class TestTips(unittest.TestCase):
    """v4.72: подсказка недели — по понедельникам, без использованных команд, день стабилен."""

    def test_pick_skips_used_and_shown(self):
        from core import tips as T
        self.assertEqual(T.pick_tip([], {"routines"})[0], "slots")
        self.assertEqual(T.pick_tip(["slots"], {"routines"})[0], "promises")
        self.assertEqual(T.pick_tip([t[0] for t in T.TIPS], set())[0], T.TIPS[0][0])   # круг закончился

    def test_only_monday_and_stable_within_day(self):
        import aiosqlite
        from datetime import date
        from unittest import mock
        from core import tips as T
        store = {}

        async def get_setting(db, key):
            return store.get(key)

        async def set_setting(db, key, value):
            store[key] = value

        async def run():
            async with aiosqlite.connect(":memory:") as db:
                await db.execute("CREATE TABLE command_log (command TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
                await db.execute("INSERT INTO command_log (command) VALUES ('routines')")
                with mock.patch("db.get_setting", get_setting), mock.patch("db.set_setting", set_setting):
                    self.assertEqual(await T.weekly_tip(db, date(2026, 11, 3)), "")
                    first = await T.weekly_tip(db, date(2026, 11, 2))
                    self.assertIn("свободен", first)
                    self.assertEqual(await T.weekly_tip(db, date(2026, 11, 2)), first)
                    self.assertNotEqual(await T.weekly_tip(db, date(2026, 11, 9)), first)
        asyncio.run(run())


class TestGreet(unittest.IsolatedAsyncioTestCase):
    """v4.91: приветствие новичков и ссылки под постом, только в обсуждении своего канала."""

    async def asyncSetUp(self):
        import aiosqlite
        from unittest import mock
        from core import greet, moderation
        moderation._group.update(id=None, at=0.0)
        greet._shown.clear()
        self.db = await aiosqlite.connect(":memory:")
        await self.db.execute("CREATE TABLE settings_kv (key TEXT PRIMARY KEY, value TEXT)")
        self.bot = mock.MagicMock()
        self.bot.get_chat = mock.AsyncMock(return_value=mock.MagicMock(linked_chat_id=-1001))
        self.bot.delete_message = mock.AsyncMock()
        self.sent = iter(range(100, 200))
        self.bot.send_message = mock.AsyncMock(side_effect=lambda *a, **k: mock.MagicMock(message_id=next(self.sent)))
        self.settings = mock.MagicMock(owner_user_id=111222333, post_channel="@St4rNinja38")

    async def asyncTearDown(self):
        await self.db.close()

    def _join(self, *users, chat=-1001):
        from types import SimpleNamespace
        return SimpleNamespace(chat=SimpleNamespace(id=chat), message_id=5,
                               new_chat_members=[SimpleNamespace(id=i, is_bot=b, first_name=n, username=None) for i, n, b in users])

    async def test_welcome_names_only_people_in_discussion(self):
        from core import greet
        self.assertTrue(await greet.welcome(self._join((1, "Аня <3", False), (2, "SpamBot", True), (111222333, "Я", False)), self.bot, self.db, self.settings))
        text = self.bot.send_message.await_args.args[1]
        self.assertIn('<a href="tg://user?id=1">Аня &lt;3</a>', text)
        self.assertNotIn("SpamBot", text)
        self.assertNotIn("tg://user?id=111222333", text)
        self.assertFalse(await greet.welcome(self._join((3, "Боря", False), chat=-2002), self.bot, self.db, self.settings))
        self.assertFalse(await greet.welcome(self._join((4, "Бот", True)), self.bot, self.db, self.settings))
        self.assertEqual(self.bot.send_message.await_count, 1)

    async def test_only_one_greeting_stays(self):
        from core import greet
        await greet.welcome(self._join((1, "Аня", False)), self.bot, self.db, self.settings)
        await greet.welcome(self._join((2, "Боря", False)), self.bot, self.db, self.settings)
        self.bot.delete_message.assert_awaited_once_with(-1001, 100)
        self.assertEqual(greet._shown[-1001], 101)

    async def test_post_links_reply_and_switches(self):
        from core import autonomy, greet
        post = self._join()
        self.assertTrue(await greet.post_links(post, self.bot, self.db, self.settings))
        args = self.bot.send_message.await_args
        self.assertIn("st4rninja38", args.args[1])
        urls = [b.url for row in args.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertIn("https://donatex.gg/donate/StarNinja38", urls)
        self.assertIn("https://t.me/starninjaMAN", urls)
        self.assertEqual(args.kwargs["reply_markup"].inline_keyboard[0][0].style, "primary")
        self.assertEqual(args.kwargs["reply_to_message_id"], 5)
        await autonomy.toggle(self.db, "postlinks", False)
        await autonomy.toggle(self.db, "greet", False)
        self.assertFalse(await greet.post_links(post, self.bot, self.db, self.settings))
        self.assertFalse(await greet.welcome(self._join((1, "Аня", False)), self.bot, self.db, self.settings))
        self.assertEqual(self.bot.send_message.await_count, 1)


class TestTwitchChat(unittest.IsolatedAsyncioTestCase):
    """v4.93: Mira в чате Twitch — спам-атака, ответы на упоминание с лимитами, отправка, без входа."""

    async def asyncSetUp(self):
        import aiosqlite, json
        from unittest import mock
        from core import twitch_chat
        twitch_chat._buf.clear()
        twitch_chat._state.update(channel="900", last_reply=0.0, users={}, alert=0.0, since=0)
        self.db = await aiosqlite.connect(":memory:")
        await self.db.execute("CREATE TABLE settings_kv (key TEXT PRIMARY KEY, value TEXT)")
        await self.db.execute("INSERT INTO settings_kv VALUES ('twitch:bot', '{}'), ('twitch:bot_user', ?)", (json.dumps({"id": "77", "login": "miraninja38"}),))
        self.settings = mock.MagicMock(owner_user_id=111222333, twitch_login="st4rninja38", anthropic_model="m")

    async def asyncTearDown(self):
        await self.db.close()

    def _ev(self, login, text, uid="5"):
        return {"chatter_user_id": uid, "chatter_user_login": login, "chatter_user_name": login, "message_id": f"m-{login}", "message": {"text": text}}

    def test_attack(self):
        import time
        from core import twitch_chat
        now = time.time()
        for i in range(3):
            twitch_chat._buf.append({"ts": now, "login": f"u{i}", "name": "", "text": "КУПИ ФОЛЛОВЕРОВ дешево!!", "id": "x"})
        self.assertIsNone(twitch_chat._attack(now))
        twitch_chat._buf.append({"ts": now, "login": "u9", "name": "", "text": "купи фолловеров дешево", "id": "x"})
        self.assertIn("одинаковые", twitch_chat._attack(now))
        twitch_chat._buf.clear()
        for i in range(6):
            twitch_chat._buf.append({"ts": now, "login": "a", "name": "", "text": f"глянь bit.ly/x{i}", "id": "x"})
        self.assertIn("ссылок", twitch_chat._attack(now))

    async def test_reply_on_mention_with_limits(self):
        from unittest import mock
        from core import autonomy, twitch_chat
        resp = mock.MagicMock()
        with mock.patch("core.llm.create", mock.AsyncMock(return_value=resp)), mock.patch("core.llm.text_of", return_value="Привет! Стрим в 22"), \
             mock.patch("core.usage.record", mock.AsyncMock()), mock.patch.object(twitch_chat, "say", mock.AsyncMock()) as say:
            await twitch_chat._on_message(None, self.db, self.settings, None, self._ev("vasya", "когда стрим?"))
            say.assert_not_called()                                                    # без упоминания молчит
            await twitch_chat._on_message(None, self.db, self.settings, None, self._ev("vasya", "@MiraNinja38 когда стрим?"))
            say.assert_awaited_once()
            self.assertEqual(say.await_args.kwargs["reply_to"], "m-vasya")
            await twitch_chat._on_message(None, self.db, self.settings, None, self._ev("petya", "miraninja38 привет"))
            self.assertEqual(say.await_count, 1)                                       # не чаще раза в 15 с
            twitch_chat._state["last_reply"] = 0
            await twitch_chat._on_message(None, self.db, self.settings, None, self._ev("miraninja38", "@miraninja38 эхо", uid="77"))
            self.assertEqual(say.await_count, 1)                                       # свои сообщения не читает
            await autonomy.toggle(self.db, "twreply", False)
            await twitch_chat._on_message(None, self.db, self.settings, None, self._ev("petya", "@miraninja38 привет"))
            self.assertEqual(say.await_count, 1)
        self.assertEqual(len(twitch_chat._buf), 4)

    async def test_say_and_tool(self):
        from unittest import mock
        from core import twitch_chat
        call = mock.AsyncMock(return_value={"data": [{"is_sent": True}]})
        with mock.patch.object(twitch_chat, "_call", call):
            self.assertEqual(await twitch_chat.tool({"action": "say", "text": "Донат: https://donatex.gg/donate/StarNinja38"}, self.db, self.settings), "отправила")
            body = call.await_args.kwargs["body"]
            self.assertEqual((body["broadcaster_id"], body["sender_id"]), ("900", "77"))
            call.return_value = {"data": [{"is_sent": False, "drop_reason": {"message": "ссылки запрещены"}}]}
            self.assertIn("ссылки запрещены", await twitch_chat.tool({"action": "say", "text": "x"}, self.db, self.settings))
        await self.db.execute("DELETE FROM settings_kv")
        self.assertIn("/twitch", await twitch_chat.tool({"action": "say", "text": "x"}, self.db, self.settings))

    async def test_mira_tool_is_write_and_dry(self):
        from unittest import mock
        from core import mira, mira_tools
        self.assertIn("twitch", mira.WRITE_TOOLS)
        self.assertEqual(mira_tools.NAMES["twitch"], "twitch")
        with mock.patch("core.connectors.enabled", mock.AsyncMock(return_value=True)):
            out = await mira_tools.run("twitch", {"action": "say", "text": "x"}, message=None, db=self.db, settings=self.settings, client=None, dry=True)
        self.assertTrue(out.startswith("(dry)"))

class TestModScreen(unittest.IsolatedAsyncioTestCase):
    """v4.94: журнал модерации, карточки без решения, данные экрана «Модерация»."""

    async def test_log_pending_and_screen(self):
        import aiosqlite, time
        from unittest import mock
        from core import autonomy, modlog, moderation, twitch_chat
        from core.webapp_api import _moderation
        moderation._group.update(id=None, at=0.0)
        twitch_chat._buf.clear()
        db = await aiosqlite.connect(":memory:")
        await db.execute("CREATE TABLE settings_kv (key TEXT PRIMARY KEY, value TEXT)")
        await autonomy.toggle(db, "modauto", False)
        bot = mock.MagicMock()
        bot.get_chat = mock.AsyncMock(return_value=mock.MagicMock(linked_chat_id=-1001))
        bot.delete_message = mock.AsyncMock(); bot.ban_chat_member = mock.AsyncMock()
        settings = mock.MagicMock(owner_user_id=111222333, post_channel="@St4rNinja38", anthropic_model="m", twitch_login="st4rninja38",
                                  twitch_client_id="", twitch_client_secret="")
        user = mock.MagicMock(id=555, is_bot=False, username="spammer", full_name="Спамер")
        msg = mock.MagicMock(text="подпишись на мой канал t.me/mychannel", caption=None, from_user=user, sender_chat=None,
                             chat=mock.MagicMock(id=-1001, title="Обсуждение"), message_id=77)
        with mock.patch("core.topics.send_owner", mock.AsyncMock()):
            await moderation.check(msg, bot, db, settings, None)
        cards = await moderation.pending(db)
        self.assertEqual(len(cards), 1)
        self.assertEqual((cards[0]["who"], cards[0]["label"]), ("Спамер (@spammer)", "реклама или чужая ссылка"))
        screen = await _moderation(bot, db, settings)
        self.assertEqual((screen["today"]["cards"], len(screen["pending"]), screen["telegram"]["chat"]), (1, 1, True))
        self.assertFalse(screen["twitch"]["connected"])
        self.assertIn("modauto", [s["id"] for s in screen["switches"]])
        self.assertNotIn("digest", [s["id"] for s in screen["switches"]])
        self.assertIn("в бане", await moderation.decide(bot, db, cards[0]["token"], "ban"))
        self.assertEqual(await moderation.pending(db), [])
        log = await modlog.recent(db)
        self.assertEqual([(l["action"], l["actor"]) for l in log], [("ban", "owner"), ("card", "mira")])
        self.assertEqual((await modlog.today(db, time.time() - 60))["ban"], 1)
        await db.close()

class TestYouTubeChat(unittest.IsolatedAsyncioTestCase):
    """v4.95: Mira в живом чате YouTube — история без ответов, ответ на упоминание, без эфира не пишет, бан по имени из чата."""

    async def asyncSetUp(self):
        import aiosqlite, json
        from unittest import mock
        from core import youtube_chat
        youtube_chat._buf.clear()
        youtube_chat._reset()
        youtube_chat._state.update(last_reply=0.0, users={}, alert=0.0)
        self.db = await aiosqlite.connect(":memory:")
        await self.db.execute("CREATE TABLE settings_kv (key TEXT PRIMARY KEY, value TEXT)")
        await self.db.execute("INSERT INTO settings_kv VALUES ('youtube:bot', '{}'), ('youtube:bot_user', ?)", (json.dumps({"id": "UCmira", "title": "Mira"}),))
        self.settings = mock.MagicMock(owner_user_id=111222333, anthropic_model="m")

    async def asyncTearDown(self):
        await self.db.close()

    def _item(self, cid, name, text, i="x"):
        return {"id": i, "snippet": {"type": "textMessageEvent", "displayMessage": text}, "authorDetails": {"channelId": cid, "displayName": name}}

    async def test_history_then_mention_reply(self):
        from unittest import mock
        from core import youtube_chat
        youtube_chat._state["chat"] = "CHAT"
        pages = [{"nextPageToken": "p1", "pollingIntervalMillis": 2000, "items": [self._item("UC1", "Олег", "@Mira старый вопрос")]},
                 {"nextPageToken": "p2", "items": [self._item("UCmira", "Mira", "@Mira эхо"), self._item("UC2", "Аня", "@Mira когда стрим?", "m2")]}]
        call = mock.AsyncMock(side_effect=pages + [{}])
        with mock.patch.object(youtube_chat, "_call", call), mock.patch("core.llm.create", mock.AsyncMock(return_value=mock.MagicMock())), \
             mock.patch("core.llm.text_of", return_value="в 22:00"), mock.patch("core.usage.record", mock.AsyncMock()):
            self.assertEqual(await youtube_chat._poll(None, self.db, self.settings, None), youtube_chat.POLL_MIN)
            self.assertEqual(call.await_count, 1)                                      # история: без ответа
            await youtube_chat._poll(None, self.db, self.settings, None)
            self.assertEqual(call.await_args_list[1].args[4]["pageToken"], "p1")
            sent = call.await_args_list[2]
            self.assertEqual(sent.args[2:4], ("POST", "/liveChat/messages"))
            self.assertEqual(sent.args[5]["snippet"]["textMessageDetails"]["messageText"], "@Аня в 22:00")
        self.assertEqual([r["name"] for r in youtube_chat._buf], ["Олег", "Аня"])

    async def test_no_live_and_ban_by_name(self):
        from unittest import mock
        from core import youtube_chat
        self.assertIn("нет эфира", await youtube_chat.tool({"action": "say", "text": "привет"}, self.db, self.settings))
        youtube_chat._state["chat"] = "CHAT"
        self.assertIn("не вижу", await youtube_chat.tool({"action": "timeout", "user": "Вася", "minutes": 5}, self.db, self.settings))
        youtube_chat._buf.append({"ts": 1, "login": "UCvasya", "name": "Вася", "text": "спам", "id": "m"})
        call = mock.AsyncMock(return_value={"id": "BAN1"})
        with mock.patch.object(youtube_chat, "_call", call):
            self.assertIn("таймауте на 5", await youtube_chat.tool({"action": "timeout", "user": "@вася", "minutes": 5}, self.db, self.settings))
            body = call.await_args.args[5]["snippet"]
            self.assertEqual((body["type"], body["banDurationSeconds"], body["bannedUserDetails"]["channelId"]), ("temporary", 300, "UCvasya"))
            self.assertIn("разбанен", await youtube_chat.tool({"action": "unban", "user": "Вася"}, self.db, self.settings))
            self.assertEqual(call.await_args.args[4], {"id": "BAN1"})

    async def test_modes_delete_violations(self):
        """v4.98: режимы держит Мира — медленный, только смайлы, без повторов; владелец не трогается, лимит удалений."""
        from unittest import mock
        from core import youtube_chat
        self.assertIn("Studio", await youtube_chat.tool({"action": "mode", "mode": "subscribers", "on": True}, self.db, self.settings))
        self.assertIn("раз в 30", await youtube_chat.tool({"action": "mode", "mode": "slow", "on": True, "value": 30}, self.db, self.settings))
        await youtube_chat.set_mode(self.db, "unique", True)
        youtube_chat._state["chat"], youtube_chat._state["page"] = "CHAT", "p0"
        def it(cid, name, text, i, sec, **flags):
            d = self._item(cid, name, text, i)
            d["snippet"]["publishedAt"] = f"2026-11-05T20:00:{sec:02d}Z"
            d["authorDetails"].update(flags)
            return d
        page = {"items": [it("UC1", "Олег", "привет всем", "a", 0), it("UC1", "Олег", "ещё", "b", 10),      # медленный: через 10 с
                          it("UC2", "Аня", "привет всем", "c", 20),                                     # повтор чужого
                          it("UC3", "Стример", "раз", "d", 21, isChatOwner=True), it("UC3", "Стример", "два", "e", 22, isChatOwner=True)]}
        call = mock.AsyncMock(side_effect=[page, {}, {}])
        with mock.patch.object(youtube_chat, "_call", call):
            await youtube_chat._poll(None, self.db, self.settings, None)
        self.assertEqual([c.args[4] for c in call.await_args_list[1:]], [{"id": "b"}, {"id": "c"}])
        self.assertEqual([r["id"] for r in youtube_chat._buf], ["a", "d", "e"])
        await youtube_chat.set_mode(self.db, "slow", False)
        await youtube_chat.set_mode(self.db, "unique", False)
        await youtube_chat.set_mode(self.db, "emote", True)
        m = await youtube_chat.modes(self.db)
        self.assertEqual(youtube_chat._violation({"ts": 0, "login": "UC9", "text": "😂🔥 :hand-pink-waving:"}, {}, m), "")
        self.assertEqual(youtube_chat._violation({"ts": 0, "login": "UC9", "text": "ахах 😂"}, {}, m), "только смайлы")
        with mock.patch.object(youtube_chat, "DELETE_DAILY_CAP", 0), mock.patch.object(youtube_chat, "_call", mock.AsyncMock()) as c:
            self.assertFalse(await youtube_chat._enforce(None, self.db, self.settings, {"id": "x", "name": "Вася", "text": "т"}, "только смайлы"))
            c.assert_not_awaited()

    async def test_strikes_timeout_and_ban_list(self):
        """v4.98: три удалённых нарушения — таймаут от Миры, он в списке банов, разбан по имени из списка."""
        from unittest import mock
        from core import youtube_chat
        await youtube_chat.set_mode(self.db, "slow", True, 30)
        youtube_chat._state["chat"], youtube_chat._state["page"] = "CHAT", "p0"
        items = []
        for i, sec in enumerate((0, 5, 10, 15)):
            d = self._item("UC7", "Флудер", f"сообщение {i}", f"m{i}")
            d["snippet"]["publishedAt"] = f"2026-11-05T20:00:{sec:02d}Z"
            items.append(d)
        call = mock.AsyncMock(side_effect=[{"items": items}, {}, {}, {}, {"id": "BAN7"}])
        with mock.patch.object(youtube_chat, "_call", call):
            await youtube_chat._poll(None, self.db, self.settings, None)
        ban = call.await_args_list[-1]
        self.assertEqual((ban.args[3], ban.args[5]["snippet"]["banDurationSeconds"]), ("/liveChat/bans", youtube_chat.STRIKE_TIMEOUT))
        lst = youtube_chat.bans()
        self.assertEqual((lst[0]["name"], lst[0]["actor"]), ("Флудер", "mira"))
        with mock.patch.object(youtube_chat, "_call", mock.AsyncMock(return_value={})) as c:
            self.assertIn("разбанен", await youtube_chat.unban(self.db, self.settings, "флудер"))
            self.assertEqual(c.await_args.args[4], {"id": "BAN7"})
        self.assertEqual(youtube_chat.bans(), [])

    def test_registered_as_write_tool(self):
        from core import mira, mira_tools, youtube_chat
        self.assertIn("youtube", mira.WRITE_TOOLS)
        self.assertEqual(mira_tools.NAMES["youtube"], "youtube")
        html = '<link rel="canonical" href="https://www.youtube.com/watch?v=abcdefghijk">'
        self.assertEqual(youtube_chat._VIDEO.search(html).group(1), "abcdefghijk")

class TestStreamReport(unittest.IsolatedAsyncioTestCase):
    """v4.99: анонс стрима кнопкой (один раз, YouTube-ссылка только при эфире) и один итог со сводками чатов."""

    async def asyncSetUp(self):
        import aiosqlite
        from unittest import mock
        from core import social, youtube_chat
        youtube_chat._reset()
        self.db = await aiosqlite.connect(":memory:")
        await self.db.execute("CREATE TABLE settings_kv (key TEXT PRIMARY KEY, value TEXT)")
        await self.db.execute("CREATE TABLE digest_state (key TEXT PRIMARY KEY, value TEXT)")
        await social.ensure_tables(self.db)
        self.settings = mock.MagicMock(owner_user_id=1, twitch_login="st4rninja38", post_channel="@chan", github_token="", vault_repo="")

    async def asyncTearDown(self):
        from core import youtube_chat
        youtube_chat._reset()
        await self.db.close()

    async def test_announce_once_and_publish(self):
        from unittest import mock
        from core import social, stream_report, youtube_chat
        live = {"stream_id": "42", "title": "Хоррор на ночь", "game": "Phasmophobia", "thumb": "", "started_at": "2026-11-05T18:00:00Z"}
        bot = mock.MagicMock(send_message=mock.AsyncMock(), send_photo=mock.AsyncMock(return_value=mock.MagicMock(message_id=777)))
        with mock.patch.object(social, "twitch_followers", mock.AsyncMock(return_value=59)), \
             mock.patch.object(stream_report, "_card", mock.AsyncMock(return_value=b"jpg")), \
             mock.patch.object(stream_report, "_owner_photo", mock.AsyncMock()) as owner:
            await stream_report.on_live(bot, self.db, self.settings, live)
            await stream_report.on_live(bot, self.db, self.settings, live)
            self.assertEqual(owner.await_count, 1)
            kb = owner.await_args.args[4]
            self.assertEqual(kb.inline_keyboard[0][0].callback_data, "sra:42")
            self.assertIn("Phasmophobia", owner.await_args.args[3])
            bot.send_photo.assert_not_awaited()                                 # в канал только по кнопке
            youtube_chat._state.update(chat="C", video="abcdefghijk")
            self.assertEqual(await stream_report.publish(bot, self.db, self.settings, "42"), "Анонс в канале")
            self.assertIn("уже", await stream_report.publish(bot, self.db, self.settings, "42"))
        self.assertEqual(bot.send_photo.await_count, 1)
        chan = bot.send_photo.await_args.args[0]
        kw = bot.send_photo.await_args.kwargs
        self.assertEqual(chan, "@chan")
        self.assertIn("Стрим уже запущен", kw["caption"])
        self.assertIn("🎮 Phasmophobia", kw["caption"])
        urls = [b.url for b in kw["reply_markup"].inline_keyboard[0]]
        self.assertEqual(urls, ["https://twitch.tv/st4rninja38", "https://youtu.be/abcdefghijk"])

    async def test_post_follows_stream_and_finishes(self):
        """v4.114: смена игры правит подпись; обрыв < 15 мин — тот же пост; стрима нет 15 мин — версия «после эфира»."""
        import json, time
        from unittest import mock
        from core import social, stream_report, youtube_chat
        now = time.time()
        live = {"stream_id": "50", "title": "Рыбалка", "game": "Russian Fishing 4", "thumb": "",
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3 * 3600 - 720))}
        bot = mock.MagicMock(send_photo=mock.AsyncMock(return_value=mock.MagicMock(message_id=9)),
                             edit_message_caption=mock.AsyncMock(), edit_message_media=mock.AsyncMock())
        with mock.patch.object(social, "twitch_followers", mock.AsyncMock(return_value=1)), \
             mock.patch.object(social, "twitch_last_vod", mock.AsyncMock(return_value="https://www.twitch.tv/videos/1")), \
             mock.patch.object(stream_report, "_card", mock.AsyncMock(return_value=b"jpg")), \
             mock.patch.object(stream_report, "_owner_photo", mock.AsyncMock()) as owner:
            youtube_chat._state.update(chat="C", video="yt0video000")              # эфир шёл и на YouTube
            await stream_report.on_live(bot, self.db, self.settings, live)
            await stream_report.publish(bot, self.db, self.settings, "50")
            await stream_report.on_live(bot, self.db, self.settings, live)          # ничего не поменялось
            bot.edit_message_caption.assert_not_awaited()
            youtube_chat._reset()                                                   # эфир на YouTube кончился раньше Twitch
            await stream_report.on_live(bot, self.db, self.settings, {**live, "game": "Just Chatting"})
            self.assertIn("Just Chatting", bot.edit_message_caption.await_args.kwargs["caption"])
            self.assertEqual(len(bot.edit_message_caption.await_args.kwargs["reply_markup"].inline_keyboard[0]), 1)   # кнопка YouTube ушла
            await stream_report.on_offline(bot, self.db, self.settings, time.time() + 300)   # обрыв 5 мин
            await stream_report.on_live(bot, self.db, self.settings, {**live, "stream_id": "51", "game": "Just Chatting"})
            self.assertEqual(owner.await_count, 1)                                  # второго черновика нет
            for v, ago in ((12, 3 * 3600), (38, 3600)):
                await self.db.execute("INSERT INTO social_stats (platform, entity, metric, value, day, ts) VALUES ('twitch','stream:51','viewers',?,?,?)",
                                      (v, "2026-11-22", now - ago))
            await stream_report.on_offline(bot, self.db, self.settings, time.time() + 600)   # рано
            bot.edit_message_media.assert_not_awaited()
            await stream_report.on_offline(bot, self.db, self.settings, time.time() + 16 * 60)
            await stream_report.on_offline(bot, self.db, self.settings, time.time() + 30 * 60)
        self.assertEqual(bot.edit_message_media.await_count, 1)
        kw = bot.edit_message_media.await_args.kwargs
        self.assertEqual(kw["message_id"], 9)
        self.assertIn("Эфир окончен", kw["media"].caption)
        self.assertIn("3 ч 12 мин", kw["media"].caption)
        self.assertIn("пик 38 зрителей", kw["media"].caption)
        self.assertIn("спасибо", kw["media"].caption)
        self.assertNotIn("Just Chatting", kw["media"].caption)                  # после эфира игра не нужна
        urls = [(b.text, b.url) for b in kw["reply_markup"].inline_keyboard[0]]
        self.assertEqual(urls, [("▶️ Запись на Twitch", "https://www.twitch.tv/videos/1"),
                                ("▶️ Запись на YouTube", "https://youtu.be/yt0video000")])

    def test_youtube_live_markers(self):
        from core import youtube_chat
        self.assertTrue(youtube_chat._is_live('..."isLive":true,"keywords":[...'))
        self.assertTrue(youtube_chat._is_live('"isLiveNow":true'))
        self.assertFalse(youtube_chat._is_live('"isLive":true ... "isUpcoming":true'))
        self.assertFalse(youtube_chat._is_live('"isLiveContent":true'))

    def test_card_renders(self):
        from core import stream_card
        jpg = stream_card.render(None, None, "StarNinja38", True)
        self.assertEqual(jpg[:2], b"\xff\xd8")
        self.assertGreater(len(stream_card.render(None, None, "x", False)), 10000)

    async def test_report_merges_and_takes_over_summaries(self):
        import json, time
        from unittest import mock
        from core import social, stream_report
        now = time.time()
        await social._item(self.db, "twitch", "stream:7", "Backrooms", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3 * 3600)), "", "stream")
        for ago, v in ((3 * 3600 - 60, 5), (2 * 3600, 14), (3600, 9)):
            await self.db.execute("INSERT INTO social_stats (platform, entity, metric, value, day, ts) VALUES ('twitch','stream:7','viewers',?,?,?)",
                                  (v, "2026-11-05", now - ago))
        await self.db.execute("INSERT INTO settings_kv VALUES ('streamstart:7', ?)", (json.dumps({"followers": 57, "ts": now - 3 * 3600}),))
        await self.db.execute("INSERT INTO settings_kv VALUES ('twitch:last_summary', ?)", (json.dumps({"ts": now - 600, "n": 120, "text": "Спрашивали про следующую игру"}),))
        await self.db.execute("INSERT INTO settings_kv VALUES ('youtube:last_summary', ?)", (json.dumps({"ts": now - 5 * 86400, "n": 3, "text": "старая"}),))
        await self.db.execute("INSERT INTO settings_kv VALUES ('youtube:last_live', ?)", (json.dumps({"end": now - 3000, "n": 31}),))
        self.assertTrue(await stream_report.covers(self.db))                    # сводки пока копит итог
        with mock.patch.object(social, "twitch_followers", mock.AsyncMock(return_value=60)), \
             mock.patch("core.topics.send_owner", mock.AsyncMock()) as owner:
            await stream_report.tick(None, self.db, self.settings, now - 3600 + 60)      # рано: стрим только что кончился
            owner.assert_not_awaited()
            await stream_report.tick(None, self.db, self.settings, now)
            await stream_report.tick(None, self.db, self.settings, now + 600)
        self.assertEqual(owner.await_count, 1)
        text = owner.await_args.args[3]
        self.assertIn("🎬 Итог стрима: Backrooms", text)
        self.assertIn("2 ч", text)
        self.assertIn("пик 14", text)
        self.assertIn("+3 фолловера", text)
        self.assertIn("в чате 31 сообщение", text)
        self.assertIn("Спрашивали про следующую игру", text)
        self.assertNotIn("старая", text)
        self.assertFalse(await stream_report.covers(self.db))                   # итог ушёл — поздние сводки идут сами

class TestYouTubeBreakdowns(unittest.IsolatedAsyncioTestCase):
    """v4.95: разрезы YouTube Analytics за 28 дней и их строки в сводке; сбой одного разреза не ломает остальные."""

    async def test_collect_and_summary(self):
        import aiosqlite
        from unittest import mock
        from core import social
        db = await aiosqlite.connect(":memory:")
        await social.ensure_tables(db)
        await social._item(db, "youtube", "VID1", "Хоррор на двоих", "", "", "video")
        answers = {"insightTrafficSourceType": {"rows": [["YT_SEARCH", 60], ["SHORTS", 30], ["EXT_URL", 10]]},
                   "ageGroup,gender": {"rows": [["age18-24", "male", 55.0], ["age25-34", "male", 30.0]]},
                   "deviceType": {"rows": [["MOBILE", 80], ["DESKTOP", 20]]},
                   "elapsedVideoTimeRatio": {"rows": [[0.25, 0.7], [0.5, 0.5], [0.75, 0.4], [0.95, 0.3]]}}
        async def fake(url, params=None, headers=None, timeout=20):
            if params["dimensions"] == "country":
                raise RuntimeError("HTTP 403")
            return answers[params["dimensions"]]
        with mock.patch.object(social, "_get", fake):
            out = await social._yta_breakdowns(db, {}, "2026-10-07", "2026-11-03", ["VID1"])
            await social._yta_breakdowns(db, {}, "2026-10-07", "2026-11-03", ["VID1"])   # второй сбор за день не задваивает
        self.assertEqual((out["traffic"], out["demo"], out["device"], out["retention"]), (3, 2, 2, 1))
        self.assertNotIn("geo", out)
        text = await social.breakdown_summary(db)
        self.assertIn("поиск YouTube 60%", text)
        self.assertIn("лента Shorts 30%", text)
        self.assertIn("18-24 м 55%", text)
        self.assertIn("телефон 80%", text)
        self.assertIn("«Хоррор на двоих»: 25% ролика — 70% зрителей", text)
        await social._put(db, "youtube", "channel", "subscribers", 407)
        await social._put(db, "youtube", "analytics", "views", 120, day=__import__("datetime").date.today().isoformat())
        full = await social.summary(db, "youtube", 7)                               # общая сводка не сломана и видит разрезы
        self.assertIn("подписчиков 407", full)
        self.assertIn("источники просмотров", full)
        await db.close()

    async def test_fresh_uploads_in_summary(self):
        """v4.98: свежие шортсы с одним замером видны в сводке «последние загрузки», новые сверху."""
        import aiosqlite
        from datetime import date
        from core import social
        db = await aiosqlite.connect(":memory:")
        await social.ensure_tables(db)
        today = date.today().isoformat()
        await social._put(db, "youtube", "channel", "subscribers", 407)
        for vid, title, pub, views in (("S1", "Старый шортс", "2026-10-22T10:00:00Z", 1260), ("S2", "Знал же", "2026-11-04T10:00:00Z", 156)):
            await social._item(db, "youtube", vid, title, pub, "", "short")
            await social._put(db, "youtube", vid, "views", views, day=today)
            await social._put(db, "youtube", vid, "likes", 9, day=today)
        full = await social.summary(db, "youtube", 7)
        self.assertIn("последние загрузки", full)
        self.assertIn("[short] 2026-11-04 Знал же — 156 просм., 9 лайков", full)
        self.assertLess(full.index("Знал же"), full.index("Старый шортс"))
        self.assertEqual(social._iso_dur_sec("PT2M30S") <= 180, True)
        await db.close()

class TestWebappAvatars(unittest.TestCase):
    """v4.90: аватары контактов в приложении — уменьшаются, кэшируются, без фото буква."""

    def test_avatar_thumb_cache_and_fallback(self):
        import asyncio, io, tempfile
        from unittest import mock
        from PIL import Image
        from core import webapp_api

        async def run():
            import aiosqlite
            tmp = tempfile.mkdtemp()
            db = await aiosqlite.connect(":memory:")
            await db.execute("CREATE TABLE contact_profiles (user_id INTEGER, photo_file_id TEXT, photo_unique_id TEXT)")
            await db.execute("INSERT INTO contact_profiles VALUES (1, 'FILE', 'U1'), (2, NULL, NULL)")
            raw = io.BytesIO(); Image.new("RGB", (640, 480), "red").save(raw, "JPEG")
            bot = mock.MagicMock()
            bot.get_file = mock.AsyncMock(return_value=mock.MagicMock(file_path="photos/a.jpg"))
            bot.download_file = mock.AsyncMock(side_effect=lambda *_: io.BytesIO(raw.getvalue()))
            settings = mock.MagicMock(db_path=f"{tmp}/bot.db")
            pic = await webapp_api._avatar(bot, db, settings, 1)
            self.assertTrue(pic.startswith("data:image/jpeg;base64,"))
            import base64
            self.assertEqual(Image.open(io.BytesIO(base64.b64decode(pic.split(",", 1)[1]))).size, (96, 96))
            self.assertEqual(await webapp_api._avatar(bot, db, settings, 1), pic)
            bot.get_file.assert_awaited_once()                       # второй раз с диска
            self.assertIsNone(await webapp_api._avatar(bot, db, settings, 2))   # без фото — буква
            bot.get_file.side_effect = RuntimeError("file is gone")
            await db.execute("UPDATE contact_profiles SET photo_unique_id='U2' WHERE user_id=1")
            self.assertIsNone(await webapp_api._avatar(bot, db, settings, 1))   # сбой Telegram не роняет ответ
            await db.close()
        asyncio.run(run())


class TestWebappAuth(unittest.TestCase):
    """v4.73: мини-приложение открывает только владелец с настоящей подписью Telegram."""

    def _init(self, user_id=1, auth_date=1000, token="123:abc"):
        import hashlib
        import hmac
        import json
        from urllib.parse import urlencode
        fields = {"auth_date": str(auth_date), "user": json.dumps({"id": user_id})}
        check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        return urlencode(fields)

    def test_page_is_a_full_document(self):
        from core.webapp_api import render_page
        html = render_page()
        self.assertTrue(html.startswith("<!doctype html>"))
        for piece in ('charset="utf-8"', 'name="viewport"', "[hidden]{display:none!important}", "<title>Mira</title>"):
            self.assertIn(piece, html)

    def test_question_from_app_shows_thinking(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch
        from core import mira, richmsg, webapp_api
        shown = []

        class Spy:
            def __init__(self, bot, chat, thread, text, timer=False):
                shown.append((chat, text, timer))
            async def __aenter__(self):
                return self
            async def __aexit__(self, *exc):
                return False

        bot = MagicMock(send_message=AsyncMock())
        with patch.object(richmsg, "thinking", Spy), patch.object(mira, "assistant_context", AsyncMock(return_value="ctx")), \
                patch.object(mira, "think", AsyncMock(return_value=("ответ", [], []))), patch.object(mira, "dialog_add", AsyncMock()):
            asyncio.run(webapp_api._ask(bot, None, MagicMock(owner_user_id=7), None, "вопрос"))
        self.assertEqual(shown, [(7, "🔥 Думаю", True)])

    def test_rules(self):
        from core.webapp_api import check_init_data as ok
        self.assertTrue(ok(self._init(), "123:abc", 1, now=1100))
        self.assertFalse(ok(self._init(), "123:abc", 2, now=1100))             # чужой аккаунт
        self.assertFalse(ok(self._init(token="9:z"), "123:abc", 1, now=1100))  # чужая подпись
        self.assertFalse(ok(self._init(), "123:abc", 1, now=1000 + 90000))     # протухла
        tampered = self._init().replace("%22id%22%3A+1", "%22id%22%3A+2")
        self.assertFalse(ok(tampered, "123:abc", 1, now=1100))
        for bad in ("", "hash=1", "a=b&&&", None):
            self.assertFalse(ok(bad, "123:abc", 1, now=1100))


class TestSkillsConnectors(unittest.TestCase):
    """v4.73: скиллы ставятся, скилл по расписанию идёт поручением, подключения слушаются выключателя."""

    def _settings(self, **kw):
        from types import SimpleNamespace
        base = dict(todoist_api_token="t", google_calendar_id="c", email_accounts="", github_token="", vault_repo="", youtube_api_key=None,
                    youtube_channel_id=None, twitch_client_id=None, twitch_client_secret=None, owner_user_id=1, anthropic_monthly_budget=0)
        return SimpleNamespace(**{**base, **kw})

    def _with_db(self, fn):
        import aiosqlite
        from unittest import mock
        store = {}

        async def get_setting(db, key):
            return store.get(key)

        async def set_setting(db, key, value):
            store[key] = value

        async def run():
            async with aiosqlite.connect(":memory:") as db:
                with mock.patch("db.get_setting", get_setting), mock.patch("db.set_setting", set_setting):
                    await fn(db)
        asyncio.run(run())

    def test_install_with_schedule_and_remove(self):
        from core import routines, skills

        async def go(db):
            s = self._settings()
            note = await skills.install(db, s, "meeting", {"every": "daily", "time": "09:00"})
            self.assertIn("поставила", note)
            async with db.execute("SELECT kind, text FROM routines") as cur:
                self.assertEqual(await cur.fetchall(), [("order", "skill:meeting")])
            row = next(r for r in await skills.catalog(db, s) if r["id"] == "meeting")
            self.assertTrue(row["installed"])
            self.assertIn("09:00", row["when"])
            await skills.remove(db, "meeting")
            self.assertIn("нет", await routines.list_text(db))
            self.assertIn("подготовь", (await skills.prompt_of(db, "meeting")).lower())
        self._with_db(go)

    def test_found_by_name(self):
        from core import skills

        async def go(db):
            self.assertIn("поставила", await skills.install(db, self._settings(), "Подготовка к встрече"))
            self.assertIn("Подготовка к встрече [meeting]", await skills.installed_line(db))
            self.assertTrue(await skills.prompt_of(db, "подготовка к встрече"))
        self._with_db(go)

    def test_needs_connector(self):
        from core import skills

        async def go(db):
            self.assertIn("подключить", await skills.install(db, self._settings(), "github_week"))
            ok = self._settings(github_token="x")
            self.assertIn("поставила", await skills.install(db, ok, "github_week"))
        self._with_db(go)

    def test_create_custom(self):
        from core import skills

        async def go(db):
            s = self._settings()
            with self.assertRaises(ValueError):
                await skills.create(db, s, {"name": "Пусто"})
            self.assertIn("поставила", await skills.create(db, s, {"name": "Клипы недели", "prompt": "Собери клипы", "needs": ["нечто"]}))
            row = next(r for r in await skills.catalog(db, s) if r["name"] == "Клипы недели")
            self.assertEqual((row["kind"], row["needs"]), ("custom", []))
            self.assertIn("Клипы", await skills.installed_line(db))
            await skills.remove(db, row["id"])
            self.assertFalse(any(r["name"] == "Клипы недели" for r in await skills.catalog(db, s)))
        self._with_db(go)

    def test_routine_runs_skill_prompt(self):
        from unittest import mock
        from core import routines, skills

        async def go(db):
            s = self._settings()
            s.anthropic_model_smart = "x"
            await skills.install(db, s, "meeting")
            seen = {}

            async def think(client, settings, ctx, text, **kw):
                seen.update(text=text, read_only=kw.get("read_only"))
                return "готово", [], []

            with mock.patch("core.mira.think", think), mock.patch("core.mira.assistant_context", mock.AsyncMock(return_value="c")), \
                 mock.patch("core.mira.dialog_add", mock.AsyncMock()), mock.patch("db.is_over_budget", mock.AsyncMock(return_value=False)):
                await routines._run_order(mock.AsyncMock(), db, s, None, "skill:meeting")
            self.assertTrue(seen["read_only"])
            self.assertIn("встрече", seen["text"])
        self._with_db(go)

    def test_connector_toggle_blocks_tool(self):
        from core import connectors, mira_tools

        async def go(db):
            s = self._settings(github_token="x")
            self.assertTrue(await connectors.enabled(db, s, "github"))
            self.assertTrue(await connectors.toggle(db, s, "github", False))
            self.assertIn("выключено", await mira_tools.run("github", {"action": "repos"}, message=None, db=db, settings=s, client=None, dry=False))
            self.assertFalse(await connectors.toggle(db, s, "telegram", False))    # основу не выключить
            self.assertFalse(await connectors.toggle(db, s, "discord", True))      # в планах
            self.assertIn("не подключено", await mira_tools.run("vault", {"action": "board"}, message=None, db=db, settings=s, client=None, dry=False))
        self._with_db(go)

    def test_vault_path_cannot_escape(self):
        from core.mira_tools import _clean_path
        self.assertEqual(_clean_path("/02-projects/a.md"), "02-projects/a.md")
        with self.assertRaises(ValueError):
            _clean_path("../secret")

    def test_vault_search_matches_all_words_in_path(self):
        from core.mira_tools import _match_paths
        paths = ["02-projects/TG-SORTER/HANDOFF.md", "03-sessions/HANDOFF.md", "04-content/Контент-доска.md"]
        self.assertEqual(_match_paths(paths, "handoff sorter"), ["02-projects/TG-SORTER/HANDOFF.md"])
        self.assertEqual(_match_paths(paths, "контент ДОСКА"), ["04-content/Контент-доска.md"])
        self.assertEqual(_match_paths(paths, ""), [])

    def test_read_tools_have_no_writes(self):
        from core import mira
        names = {t["name"] for t in mira.READ_TOOLS}
        self.assertTrue({"mail", "web_search", "reels", "vault", "github"} <= names)
        self.assertFalse(names & mira.WRITE_TOOLS)


class TestHumanMessages(unittest.TestCase):
    """v4.74: сообщения Миры без служебных строк, денег и английских ярлыков."""

    def test_task_card(self):
        from core.actions import task_card
        text = task_card(["Принести брелочек"], "nika", "FRIEND", "завтра 14:00")
        self.assertIn("Принести брелочек", text)
        self.assertIn("📅 завтра 14:00", text)
        for junk in ("$", "кредит", "Todoist", "личное", "💸"):
            self.assertNotIn(junk, text)
        self.assertIn("Не уверена", task_card(["А"], "x", "UNCLEAR"))
        many = task_card(["А", "Б"], "x", "BUSINESS")
        self.assertTrue("• <b>А</b>" in many and "2 дела" in many)
        self.assertTrue(text.startswith("📌 Записала:"))


class TestExecutorCheck(unittest.TestCase):
    """v4.74: чужое дело не становится задачей молча, сбой проверки задачу не теряет."""

    def test_failure_keeps_task(self):
        from unittest import mock
        from core import actions

        async def run():
            with mock.patch("core.actions.llm.create", mock.AsyncMock(side_effect=RuntimeError("500"))):
                self.assertEqual(await actions._executor_of(None, "тред", "дело"), "owner")
        asyncio.run(run())

    def test_card_once_per_task(self):
        from unittest import mock
        from core import actions
        store = {}

        async def get_setting(db, key):
            return store.get(key)

        async def set_setting(db, key, value):
            store[key] = value

        async def run():
            send = mock.AsyncMock()
            with mock.patch("db.get_setting", get_setting), mock.patch("db.set_setting", set_setting), mock.patch("core.topics.send_owner", send):
                for _ in range(2):
                    await actions._offer_not_mine(None, None, 1, "nika", 5, "FRIEND", {"title": "Принести брелочек"})
            send.assert_awaited_once()
            self.assertIn("брелочек", send.await_args.args[3])
            saved = [v for k, v in store.items() if k.startswith("notmine:") and "seen" not in k]
            self.assertEqual(len(saved), 1)
            self.assertIn("nika", saved[0])
        asyncio.run(run())


class TestTodoistRead(unittest.TestCase):
    """v4.75: Мира видит дела недели, а бэклоги и списки только по просьбе."""

    def _task(self, title, group="day", due="", deadline="", project="Personal", **kw):
        base = dict(id=title, title=title, desc="", due=due, deadline=deadline, project=project, section="", group=group, sub=False, urgent=False)
        return {**base, **kw}

    def test_groups(self):
        from core import todoist_read as T
        self.assertEqual(T.group_of("inbox", "inbox"), "bot")
        self.assertEqual(T.group_of("6hF332Cpfq5prf3P", "inbox"), "lists")
        self.assertEqual(T.group_of("6gppV96WfH8w5fQG", "inbox"), "ondemand")
        self.assertEqual(T.group_of("6CrgCR8c6cWF47fM", "inbox"), "day")

    def test_upcoming_only_day_group(self):
        from datetime import date
        from core import todoist_read as T
        today = date(2026, 11, 1)
        tasks = [self._task("платёж", due="2026-11-05"), self._task("просрочка", due="2026-10-20"),
                 self._task("далеко", due="2026-12-01"), self._task("без срока"),
                 self._task("дедлайн", deadline="2026-11-02"), self._task("подзадача", due="2026-11-02", sub=True),
                 self._task("сайт", group="ondemand", due="2026-11-02"), self._task("аниме", group="lists", due="2026-11-02")]
        self.assertEqual([t["title"] for t in T.upcoming(tasks, today)], ["просрочка", "дедлайн", "платёж"])
        self.assertIn("просрочено с 20.10", T.line(tasks[1], today))

    def test_project_lookup_and_render(self):
        from datetime import date
        from core import todoist_read as T
        tasks = [self._task("Блич", group="lists", project="🌌Что я смотрю", section="В планах"),
                 self._task("своё", group="bot", project="📱 Telegram Входящие")]
        rows = T.project_rows(tasks, "что я смотрю")
        self.assertEqual(len(rows), 1)
        self.assertIn("В планах", T.render(rows, date(2026, 11, 1)))
        self.assertEqual(T.project_rows(tasks, "telegram"), [])
        many = [self._task(str(i), group="lists") for i in range(50)]
        self.assertIn("и ещё 10", T.render(many, date(2026, 11, 1)))

    def test_failure_keeps_last_snapshot(self):
        from types import SimpleNamespace
        from unittest import mock
        from core import todoist_read as T
        T._cache.update(at=0.0, data=[self._task("старое")])

        async def run():
            with mock.patch("core.todoist_read._all", mock.AsyncMock(side_effect=RuntimeError("500"))):
                return await T.fetch(SimpleNamespace(todoist_api_token="t", todoist_project_id="x"))
        rows = asyncio.run(run())
        T._cache.update(at=0.0, data=None)
        self.assertEqual(rows[0]["title"], "старое")


class TestThreads(unittest.TestCase):
    """v4.76: разговор тредов раздельный, а память общая; служебные темы тредами не считаются."""

    def _run(self, fn):
        import aiosqlite

        async def run():
            async with aiosqlite.connect(":memory:") as db:
                await fn(db)
        asyncio.run(run())

    def _bot(self, tid=77):
        from types import SimpleNamespace
        from unittest import mock
        bot = mock.AsyncMock()
        bot.create_forum_topic.return_value = SimpleNamespace(message_thread_id=tid)
        return bot

    def test_create_and_key(self):
        from core import threads as T

        async def go(db):
            bot = self._bot()
            self.assertIn("создала", await T.create(bot, db, 1, "💼 Работа", "Здесь работаем"))
            self.assertEqual(await T.key_of(db, 77), 77)
            self.assertEqual(await T.key_of(db, 670726), 0)     # служебная тема
            self.assertEqual(await T.key_of(db, None), 0)
            self.assertIn("уже есть", await T.create(bot, db, 1, "💼 работа"))
            bot.create_forum_topic.assert_awaited_once()
            with self.assertRaises(ValueError):
                await T.create(bot, db, 1, "  ")
        self._run(go)

    def test_deleted_topic_is_recreated(self):
        from core import threads as T

        async def go(db):
            bot = self._bot(77)
            await T.create(bot, db, 1, "Учёба")
            bot = self._bot(88)
            bot.send_message.side_effect = [RuntimeError("thread not found"), None]
            self.assertIn("создала", await T.create(bot, db, 1, "Учёба"))
            self.assertEqual([r[0] for r in await T.rows(db)], [88])
        self._run(go)

    def test_dialog_separate_and_bridge(self):
        from core import mira, threads as T

        async def go(db):
            await T.create(self._bot(), db, 1, "💼 Работа")
            await mira.dialog_add(db, "owner", "смета для Тимура", thread=77)
            await mira.dialog_add(db, "owner", "купи молоко")
            self.assertEqual([r[2] for r in await mira.dialog_rows(db, thread=77)], ["смета для Тимура"])
            self.assertEqual([r[2] for r in await mira.dialog_rows(db, thread=0)], ["купи молоко"])
            self.assertEqual(len(await mira.dialog_rows(db)), 2)        # сводка дня видит всё
            self.assertIn("Работа: «смета", await mira.other_threads_text(db, 0))
            self.assertIn("общий чат: «купи", await mira.other_threads_text(db, 77))
        self._run(go)

    def test_old_table_gets_column(self):
        from core import mira

        async def go(db):
            await db.execute("CREATE TABLE dialog (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, role TEXT, text TEXT, tools_json TEXT DEFAULT '[]')")
            await db.execute("INSERT INTO dialog(ts, role, text) VALUES (?, 'owner', 'старое')", (__import__("time").time(),))
            await db.commit()
            await mira.ensure_dialog_table(db)
            self.assertEqual([r[2] for r in await mira.dialog_rows(db, thread=0)], ["старое"])
        self._run(go)


class TestGroupSafety(unittest.TestCase):
    """v4.77: Мира в группе отвечает только владельцу, в разрешённой группе, без его данных."""

    def _msg(self, text, user_id, chat_id=-100, mention=True):
        from types import SimpleNamespace
        from unittest import mock
        ent = [SimpleNamespace(type="mention", extract_from=lambda t: "@Sort38_bot")] if mention else []
        return SimpleNamespace(text=text, caption=None, entities=ent, caption_entities=None, reply_to_message=None,
                               chat=SimpleNamespace(id=chat_id, type="supergroup", title="Группа"),
                               from_user=SimpleNamespace(id=user_id, username="u", first_name="Имя"),
                               reply=mock.AsyncMock(return_value=SimpleNamespace(text="ответ")))

    def _go(self, fn):
        import aiosqlite
        from types import SimpleNamespace
        from unittest import mock
        store = {}

        async def get_setting(db, key):
            return store.get(key)

        async def set_setting(db, key, value):
            store[key] = value

        async def run():
            async with aiosqlite.connect(":memory:") as db:
                settings = SimpleNamespace(owner_user_id=1, anthropic_model_smart="m", anthropic_model="h", anthropic_monthly_budget=0)
                bot = mock.AsyncMock()
                bot.id = 9
                bot.get_me.return_value = SimpleNamespace(username="Sort38_bot")
                create = mock.AsyncMock(return_value=SimpleNamespace(content=[SimpleNamespace(type="text", text="ответ")], usage=None, stop_reason="end_turn"))
                with mock.patch("db.get_setting", get_setting), mock.patch("db.set_setting", set_setting), \
                     mock.patch("core.group_mira.llm.create", create), mock.patch("core.group_mira.record", mock.AsyncMock()), \
                     mock.patch("core.topics.send_owner", mock.AsyncMock()) as card, \
                     mock.patch("handlers.groups._get_cached_usernames", mock.AsyncMock(return_value=("Sort38_bot", "me"))), \
                     mock.patch("handlers.groups.process_action_items", mock.AsyncMock(return_value=0)):
                    await fn(db, settings, bot, create, card)
        asyncio.run(run())

    def test_unapproved_group_gets_no_answer(self):
        from handlers.groups import handle_group_message

        async def go(db, s, bot, create, card):
            m = self._msg("@Sort38_bot привет", 1)
            await handle_group_message(m, bot, db, s, None)
            m.reply.assert_not_awaited()
            create.assert_not_awaited()
            card.assert_awaited_once()
        self._go(go)

    def test_only_owner_triggers_answer(self):
        from core import group_mira
        from handlers.groups import handle_group_message

        async def go(db, s, bot, create, card):
            await group_mira.allow(db, -100, "Группа")
            stranger = self._msg("@Sort38_bot какие у него долги?", 2)
            await handle_group_message(stranger, bot, db, s, None)
            stranger.reply.assert_not_awaited()
            create.assert_not_awaited()
            owner = self._msg("@Sort38_bot привет", 1)
            await handle_group_message(owner, bot, db, s, None)
            owner.reply.assert_awaited_once()
            kw = create.await_args.kwargs
            self.assertNotIn("tools", kw)
            self.assertIn("нет доступа", kw["system"])
        self._go(go)

    def test_daily_limit(self):
        from datetime import date
        from core import group_mira as G

        async def go(db, s, bot, create, card):
            await G.allow(db, -100, "Группа")
            day = date(2026, 11, 1)
            for _ in range(G.DAILY_LIMIT):
                self.assertEqual(await G.reply_text(db, s, None, -100, "да", "", day), "ответ")
            self.assertIsNone(await G.reply_text(db, s, None, -100, "да", "", day))
        self._go(go)

    def test_log_only_allowed_and_forget(self):
        from core import group_mira as G

        async def go(db, s, bot, create, card):
            await G.log_message(db, -5, "Аня", "не сохранять")
            await G.allow(db, -100, "Группа")
            await G.log_message(db, -100, "Аня", "сохранить")
            self.assertIn("сохранить", await G._lines(db, -100, 0, 10))
            self.assertEqual(await G._lines(db, -5, 0, 10), "")
            await G.forget(db, -100)
            self.assertFalse(await G.is_allowed(db, -100))
            self.assertEqual(await G._lines(db, -100, 0, 10), "")
        self._go(go)

    def test_stranger_adding_bot_makes_it_leave(self):
        from types import SimpleNamespace
        from handlers.groups import bot_added

        async def go(db, s, bot, create, card):
            event = SimpleNamespace(chat=SimpleNamespace(id=-7, type="group", title="Чужая"), from_user=SimpleNamespace(id=2))
            await bot_added(event, bot, db, s)
            bot.leave_chat.assert_awaited_once_with(-7)
        self._go(go)


class TestHabits(unittest.TestCase):
    def test_streak(self):
        from datetime import date
        from core.habits import streak
        today = date(2026, 11, 3)
        self.assertEqual(streak({"2026-11-03", "2026-11-02", "2026-10-31"}, today), 2)
        self.assertEqual(streak({"2026-11-02", "2026-11-01"}, today), 2)   # сегодня ещё можно успеть
        self.assertEqual(streak({"2026-11-01"}, today), 0)

    def test_flow(self):
        import aiosqlite
        from datetime import date
        from core import habits as H

        async def run():
            async with aiosqlite.connect(":memory:") as db:
                today = date(2026, 11, 1)
                self.assertIn("нет", await H.done(db, "чтение", today))
                await H.add(db, "Чтение 20 минут")
                self.assertIn("серия 1", await H.done(db, "чтение", today))
                self.assertIn("сегодня да", await H.report(db, today))
                await H.remove(db, "чтение")
                self.assertIn("пока нет", await H.report(db, today))
        asyncio.run(run())


class TestMoneyToolsAreReadOnly(unittest.TestCase):
    def test_ton_address_only_public(self):
        import aiosqlite
        from unittest import mock
        from core.mira_tools import set_ton_address

        async def run():
            with mock.patch("db.set_setting", mock.AsyncMock()) as save:
                self.assertIn("не похоже", await set_ton_address(None, "word1 word2 word3 секретная фраза"))
                save.assert_not_awaited()
                self.assertIn("запомнила", await set_ton_address(None, "EQCD39VS5jcptHL8vMjEXrzGaRcCVYto7HUn4bpAOg8xqB2N"))
                save.assert_awaited_once()
        asyncio.run(run())

    def test_no_transfer_tools(self):
        from core import mira
        names = " ".join(t["name"] for t in mira.TOOLS)
        for word in ("send", "transfer", "swap", "trade"):
            self.assertNotIn(word, names)


class TestThinkingLabel(unittest.TestCase):
    def test_label(self):
        from core.richmsg import thinking_label
        self.assertEqual(thinking_label("🔥 Думаю", 0.4), "🔥 Думаю 0с")
        self.assertEqual(thinking_label("🔥 Думаю", 7.9), "🔥 Думаю 7с")


class TestContentBoard(unittest.TestCase):
    """v4.78: карточки двигаются как у @vaultsn38bot, стрим и клипы спрашиваются один раз."""

    BOARD = """## 💡 Идея
- [ ] идея

## 🎯 Стрим недели

- [ ] 🇷🇺 Russian horror [[сценарии/a|📝 Сценарий]] · 📅 2026-10-25
- [ ] 🎙 Backrooms [[сценарии/b|📝 Сценарий]]

## 🔴 Отстримлено



## ✅ Опубликовано

"""

    def test_move(self):
        from datetime import date
        from core import content_board as B
        day = date(2026, 11, 1)
        card = B.lane_cards(self.BOARD, "w")[0]
        self.assertEqual(B.card_title(card), "🇷🇺 Russian horror 📝 Сценарий".replace(" 📝 Сценарий", " 📝 Сценарий"))
        text, title = B.move(self.BOARD, B.card_key(card), "s", day)
        self.assertIn("Russian", title)
        self.assertEqual(len(B.lane_cards(text, "w")), 1)
        moved = B.lane_cards(text, "s")
        self.assertEqual(len(moved), 1)
        self.assertIn("🔴 2026-11-01", moved[0])
        self.assertNotIn("📅", moved[0])
        text2, _ = B.move(text, B.card_key(moved[0]), "p", date(2026, 11, 3))
        self.assertIn("✅ 2026-11-03", B.lane_cards(text2, "p")[0])
        self.assertEqual(B.lane_cards(text2, "s"), [])
        self.assertIsNone(B.move(self.BOARD, "00000000", "s", day)[0])

    def test_find_and_add(self):
        from datetime import date
        from core import content_board as B
        day = date(2026, 11, 5)
        key = B.find(self.BOARD, "это был russian horror")
        self.assertEqual(key, B.card_key(B.lane_cards(self.BOARD, "w")[0]))
        self.assertIsNone(B.find(self.BOARD, "игра Photomaly"))
        text, title = B.add(self.BOARD, "Photomaly", "s", day)
        self.assertEqual(title, "Photomaly")
        self.assertEqual(B.lane_cards(text, "s"), ["- [ ] Photomaly · 🔴 2026-11-05"])
        self.assertEqual(B.card_key(B.lane_cards(text, "s")[0]), __import__("hashlib").sha1(b"Photomaly").hexdigest()[:8])

    def test_mark_from_mira(self):
        from datetime import date
        from types import SimpleNamespace
        from unittest import mock
        from core import content_board as B
        store, saved = {B.ASKED: '{"title": "x", "ts": 0}'}, {}

        async def set_setting(db, key, value):
            store[key] = value

        async def get_setting(db, key):
            return store.get(key)

        async def fake_write(settings, lane, change):
            new, title = change(self.BOARD)
            saved["text"] = new
            return title if new else ""

        async def go():
            s = SimpleNamespace()
            with mock.patch("db.set_setting", set_setting), mock.patch("db.get_setting", get_setting), \
                 mock.patch.object(B, "_write", fake_write):
                self.assertEqual(await B.asked(None, 100), "x")
                out = await B.mark(None, s, "Photomaly", "s", date(2026, 11, 5), 100)
                self.assertIn("Photomaly", out)
                self.assertIn("Photomaly", B.lane_cards(saved["text"], "s")[0])
                self.assertEqual(store[B.ASKED], "")
                self.assertTrue(any(k.startswith("boardclips:") for k in store))
                self.assertEqual(await B.asked(None, 100), "")
                self.assertIn("нет", await B.mark(None, s, "Photomaly", "p", date(2026, 11, 5), 100))
        asyncio.run(go())

    def _db(self, fn):
        import aiosqlite

        async def run():
            async with aiosqlite.connect(":memory:") as db:
                await db.execute("CREATE TABLE social_stats (ts REAL, platform TEXT, entity TEXT, metric TEXT, value REAL)")
                await db.execute("CREATE TABLE social_items (platform TEXT, item_id TEXT, title TEXT)")
                await db.execute("CREATE TABLE settings_kv (key TEXT PRIMARY KEY, value TEXT)")
                await db.commit()
                await fn(db)
        asyncio.run(run())

    def test_stream_end_detection(self):
        from core import content_board as B

        async def go(db):
            now = 1_000_000.0
            self.assertIsNone(await B._ended_stream(db, now))
            await db.execute("INSERT INTO social_stats VALUES (?, 'twitch', 'stream:1', 'viewers', 5)", (now - 60,))
            await db.execute("INSERT INTO social_items VALUES ('twitch', 'stream:1', 'Хоррор')")
            await db.commit()
            self.assertIsNone(await B._ended_stream(db, now))                 # ещё идёт
            self.assertEqual(await B._ended_stream(db, now + 1000), ("stream:1", "Хоррор"))
            self.assertIsNone(await B._ended_stream(db, now + 8 * 3600))      # слишком давно
        self._db(go)

    def test_clips_ask_limit(self):
        from types import SimpleNamespace
        from unittest import mock
        from core import content_board as B
        store = {}

        async def get_setting(db, key):
            return store.get(key)

        async def set_setting(db, key, value):
            store[key] = value
            await db.execute("INSERT OR REPLACE INTO settings_kv VALUES (?,?)", (key, value))
            await db.commit()

        async def go(db):
            s = SimpleNamespace(owner_user_id=1)
            with mock.patch("db.get_setting", get_setting), mock.patch("db.set_setting", set_setting), \
                 mock.patch("core.topics.send_owner", mock.AsyncMock()) as send:
                await B.plan_clips(db, "abc", "Хоррор", 0)
                await B._ask_clips(None, db, s, 1000)
                send.assert_not_awaited()                                     # рано
                now = 0
                for _ in range(B.MAX_CLIP_ASKS + 2):
                    now += (B.CLIPS_AFTER_DAYS + 1) * 86400
                    await B._ask_clips(None, db, s, now)
                self.assertEqual(send.await_count, B.MAX_CLIP_ASKS)
        self._db(go)


class TestAutonomy(unittest.TestCase):
    def _db(self, fn):
        import aiosqlite

        async def run():
            async with aiosqlite.connect(":memory:") as db:
                await db.execute("CREATE TABLE settings_kv (key TEXT PRIMARY KEY, value TEXT)")
                await db.commit()
                await fn(db)
        asyncio.run(run())

    def test_switch_is_on_until_turned_off(self):
        from core import autonomy

        async def go(db):
            self.assertTrue(await autonomy.enabled(db, "digest"))
            rows = await autonomy.toggle(db, "digest", False)
            self.assertFalse(await autonomy.enabled(db, "digest"))
            self.assertFalse(next(r for r in rows if r["id"] == "digest")["on"])
            self.assertTrue(await autonomy.enabled(db, "board"))
            with self.assertRaises(ValueError):
                await autonomy.toggle(db, "чужое", True)
        self._db(go)

    def test_remembered_keeps_only_dated_facts_newest_first(self):
        import json
        from core.autonomy import remembered
        raw = json.dumps({"sections": {"other": "старая запись; [30.10] любит кофе; [31.10] съёмки вечером"}}, ensure_ascii=False)
        self.assertEqual(remembered(raw), [{"d": "31.10", "t": "съёмки вечером"}, {"d": "30.10", "t": "любит кофе"}])
        self.assertEqual(remembered(None), [])
        self.assertEqual(remembered("не json"), [])

    def test_stranger_group_gets_no_channel_draft(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        from core import channel, group_mira

        async def go(db):
            message = MagicMock(text="новый пост", caption=None)
            with patch.object(group_mira, "is_allowed", AsyncMock(return_value=False)), patch.object(channel, "draft", AsyncMock()) as draft:
                await channel.on_post(message, None, db, MagicMock(), None)
            draft.assert_not_awaited()
        self._db(go)

    def test_comment_is_published_once_and_only_by_button(self):
        import json
        from unittest.mock import AsyncMock, MagicMock
        from core import channel
        from db import set_setting

        async def go(db):
            bot = MagicMock(send_message=AsyncMock())
            await set_setting(db, "chcomment:a", json.dumps({"chat": -5, "reply": 9, "text": "привет"}))
            await set_setting(db, "chcomment:b", json.dumps({"chat": -5, "reply": 9, "text": "нет"}))
            self.assertEqual(await channel.decide(bot, db, "b", False), "Не публикую")
            bot.send_message.assert_not_awaited()
            self.assertEqual(await channel.decide(bot, db, "a", True), "Опубликовала")
            self.assertEqual(await channel.decide(bot, db, "a", True), "Уже решено")
            bot.send_message.assert_awaited_once_with(-5, "привет", reply_to_message_id=9, parse_mode=None)
        self._db(go)

    def test_video_note_post_is_transcribed_for_draft(self):
        from types import SimpleNamespace as NS
        from unittest.mock import AsyncMock, patch
        from core import channel

        def msg(**kw):
            base = dict(text=None, caption=None, video_note=None, voice=None, audio=None, video=None, photo=None)
            base.update(kw)
            return NS(**base)

        async def go(db):
            with patch("core.transcribe.transcribe_voice", AsyncMock(return_value="завтра стрим в восемь")) as tr:
                body, heard = await channel.post_text(msg(video_note=NS(file_id="vn1")), None, db)
                self.assertEqual(body, "[кружок, речь автора]\nзавтра стрим в восемь")
                self.assertEqual(heard, "кружок: «завтра стрим в восемь»")
                tr.assert_awaited_once()
                body, _ = await channel.post_text(msg(caption="анонс", video=NS(file_id="v1")), None, db)
                self.assertEqual(body, "анонс\n\n[видео, речь автора]\nзавтра стрим в восемь")
            with patch("core.transcribe.transcribe_voice", AsyncMock(return_value=None)):
                self.assertEqual(await channel.post_text(msg(video_note=NS(file_id="vn2")), None, db), ("", ""))
            self.assertEqual(await channel.post_text(msg(photo=[1]), None, db), ("", ""))
            self.assertEqual(await channel.post_text(msg(text="просто текст"), None, db), ("просто текст", ""))
        self._db(go)


class TestReelAnalysis(unittest.TestCase):
    def test_text_has_analysis_and_player_link(self):
        from core.mira_tools import reel_text
        text = reel_text({"id": "abc12", "owner": "clay", "plays": 4600000, "likes": None}, {"summary": "Лепка брелоков", "text_hook": "Отправь половинке", "funnel": ""})
        self.assertIn("@clay: 4600000 просмотров", text)
        self.assertIn("Суть: Лепка брелоков", text)
        self.assertIn("Текстовый хук: Отправь половинке", text)
        self.assertNotIn("Воронка", text)
        self.assertIn("https://reels.sncont.com/#reel=abc12", text)

    def test_only_instagram_links(self):
        from core.mira_tools import _reels
        self.assertIn("нужна ссылка", asyncio.run(_reels({"action": "analyse", "url": "https://evil.example/x"})))

    def test_new_skills_are_in_catalog(self):
        from core import skills
        ids = {s["id"] for s in skills.CATALOG}
        self.assertTrue({"youtube_ideas", "reel_adapt", "booking"} <= ids)


class TestCommitmentCleanup(unittest.TestCase):
    def _db(self, fn):
        import aiosqlite

        async def run():
            async with aiosqlite.connect(":memory:") as db:
                await db.execute("CREATE TABLE task_sync (id INTEGER PRIMARY KEY, todoist_task_id TEXT, user_id INTEGER, task_title TEXT, synced INTEGER DEFAULT 0, closed_at TEXT, notion_status TEXT)")
                await db.execute("CREATE TABLE contact_profiles (user_id INTEGER PRIMARY KEY, commitments TEXT)")
                await db.execute("INSERT INTO task_sync (id, todoist_task_id, user_id, task_title) VALUES (1, 'td1', 7, 'Закинуть 150 за закуски на посиделке у Ванька'), (2, 'td2', 7, '❓ Заказать стекло для гриля')")
                await db.execute("INSERT INTO contact_profiles VALUES (7, 'Закинуть 150 за закуски на посиделке у Ванька (обещал 11.10); заказать стекло для гриля; вернуть долг 1500')")
                await db.commit()
                await fn(db)
        asyncio.run(run())

    async def _text(self, db):
        async with db.execute("SELECT commitments FROM contact_profiles WHERE user_id=7") as cur:
            return (await cur.fetchone())[0]

    def test_without_done_removes_only_matching_items(self):
        from db import without_done
        text = "купить Нике ванильный мист; позвонить Ивану"
        self.assertEqual(without_done(text, ["купить Нике ванильный мист"]), "позвонить Ивану")
        self.assertEqual(without_done(text, ["Сдать курсовую работу"]), text)
        self.assertEqual(without_done("", ["что угодно"]), "")
        self.assertEqual(without_done(text, []), text)

    def test_closing_by_id_clears_promise_and_keeps_others(self):
        from db import mark_sync_done_by_id

        async def go(db):
            await mark_sync_done_by_id(db, 1)
            text = await self._text(db)
            self.assertNotIn("Закинуть 150", text)
            self.assertIn("стекло", text)
            self.assertIn("долг 1500", text)
        self._db(go)

    def test_closing_by_todoist_id_clears_promise(self):
        from db import mark_sync_done

        async def go(db):
            await mark_sync_done(db, "td2")
            self.assertNotIn("стекло", await self._text(db))
        self._db(go)

    def test_closing_survives_missing_profile_table(self):
        import aiosqlite
        from db import mark_sync_done_by_id

        async def run():
            async with aiosqlite.connect(":memory:") as db:
                await db.execute("CREATE TABLE task_sync (id INTEGER PRIMARY KEY, todoist_task_id TEXT, user_id INTEGER, task_title TEXT, synced INTEGER DEFAULT 0, closed_at TEXT, notion_status TEXT)")
                await db.execute("INSERT INTO task_sync (id, user_id, task_title) VALUES (1, 7, 'дело')")
                await mark_sync_done_by_id(db, 1)
                async with db.execute("SELECT synced FROM task_sync") as cur:
                    self.assertEqual((await cur.fetchone())[0], 1)
        asyncio.run(run())

    def test_reply_tasks_are_recognised(self):
        from handlers.business import _REPLY_TASK_RE
        self.assertTrue(_REPLY_TASK_RE.match("Ответить бабушке на вопрос про отдых"))
        self.assertTrue(_REPLY_TASK_RE.match("❓ Написать Нике"))
        self.assertIsNone(_REPLY_TASK_RE.match("Сдать курсовую работу"))


class TestVidIQ(unittest.TestCase):
    """v4.97: vidIQ только из белого списка, дневной лимит, ошибки не съедают лимит."""

    def test_cap_and_channel_me(self):
        from datetime import date
        from types import SimpleNamespace
        from unittest import mock
        from core import vidiq
        store, calls = {}, []

        async def get_setting(db, key):
            return store.get(key)

        async def set_setting(db, key, value):
            store[key] = value

        async def fake_call(settings, name, params):
            calls.append((name, params))
            return "vidIQ ошибка: bad" if params.get("bad") else "ok"

        async def go():
            s = SimpleNamespace(vidiq_api_key="k", youtube_channel_id="UCme")
            day = date(2026, 11, 5)
            with mock.patch("db.get_setting", get_setting), mock.patch("db.set_setting", set_setting), \
                 mock.patch.object(vidiq, "_call", fake_call):
                self.assertIn("неизвестное", await vidiq.tool({"action": "generate_thumbnail"}, None, s, day))
                self.assertEqual(await vidiq.tool({"action": "channel_stats", "params": {"channelId": "me"}}, None, s, day), "ok")
                self.assertEqual(calls[-1], ("vidiq_channel_stats", {"channelId": "UCme"}))
                await vidiq.tool({"action": "outliers", "params": {"bad": 1}}, None, s, day)   # ошибка не считается
                for _ in range(vidiq.DAILY_CAP - 1):
                    await vidiq.tool({"action": "trending", "params": {"videoFormat": "long"}}, None, s, day)
                self.assertIn("лимит", await vidiq.tool({"action": "outliers"}, None, s, day))
                self.assertEqual(await vidiq.tool({"action": "balance"}, None, s, day), "ok")   # баланс бесплатный
        asyncio.run(go())

    def test_parse_sse(self):
        from core import vidiq
        self.assertEqual(vidiq._parse('event: message\ndata: {"result": {"x": 1}}\n'), {"result": {"x": 1}})
        self.assertEqual(vidiq._parse('{"result": 2}'), {"result": 2})


class TestMiraAck(unittest.TestCase):
    """v4.97: «договор», «ок», «👍» — согласие без модели, «да» и вопросы идут в модель."""

    def test_ack(self):
        from core.mira import ack_reply
        self.assertEqual(ack_reply("Договор"), "Договорились 🤝")
        self.assertEqual(ack_reply("ок!"), "👌")
        self.assertEqual(ack_reply("👍"), "👌")
        self.assertEqual(ack_reply("спс)"), "Обращайся 🙌")
        for text in ("да", "договор с отцом где", "ок, а что завтра?", "найди договор"):
            self.assertEqual(ack_reply(text), "", text)

    def test_ack_skipped_after_question(self):
        import aiosqlite
        from core import mira

        async def go():
            async with aiosqlite.connect(":memory:") as db:
                await mira.ensure_dialog_table(db)
                await mira.dialog_add(db, "mira", "Через пару дней спрошу про клипы.")
                self.assertFalse(await mira._asked_last(db))
                await mira.dialog_add(db, "mira", "Перенести на завтра?")
                self.assertTrue(await mira._asked_last(db))
        asyncio.run(go())


class TestMarket(unittest.IsolatedAsyncioTestCase):
    """v4.100: финансы как у второй Миры: CoinGecko (CoinMarketCap по ключу), новости Perplexity с запасным поиском, блок в сводке."""

    async def asyncSetUp(self):
        import aiosqlite
        self.db = await aiosqlite.connect(":memory:")
        await self.db.execute("CREATE TABLE settings_kv (key TEXT PRIMARY KEY, value TEXT)")

    async def asyncTearDown(self):
        await self.db.close()

    def test_lines_format(self):
        from core import market
        self.assertEqual(market._stock_line("S&P 500", 7353.73, -4.356), "S&P 500 7 354 (−4,4%)")
        self.assertEqual(market._stock_line("Золото", 4043.1, 2.9), "Золото $4 043,10 (+2,9%)")
        self.assertEqual(market._stock_line("Доходность 10Y", 4.886, -1.8), "Доходность 10Y 4,89%")
        self.assertEqual(market._stock_line("Доллар", 81.23, 0.4), "Доллар 81,2 ₽ (+0,4%)")
        self.assertEqual(market._money(2.71e12), "$2,71 трлн")
        self.assertEqual(market._money(80512.4), "$80 512")

    async def test_coins_fall_back_to_coingecko(self):
        from unittest import mock
        from core import market
        settings = mock.MagicMock(coinmarketcap_api_key="k")
        calls = []

        async def fake(url, headers=None):
            calls.append(url)
            if "coinmarketcap" in url:
                raise RuntimeError("401")
            return {"bitcoin": {"usd": 80500, "usd_24h_change": 4.0, "usd_market_cap": 1.6e12}}
        with mock.patch.object(market, "_get", fake):
            rows = await market.coins(settings, ["btc"])
        self.assertEqual(rows[0]["sym"], "BTC")
        self.assertEqual(rows[0]["price"], 80500)
        self.assertIn("coingecko", calls[-1])
        self.assertIn("за сутки +4,0%", await self._crypto_text(market, fake, settings))

    async def _crypto_text(self, market, fake, settings):
        from unittest import mock
        with mock.patch.object(market, "_get", fake):
            return await market.crypto_text(settings, ["btc"])

    async def test_news_falls_back_to_web_search(self):
        from unittest import mock
        from core import market, mira_tools
        settings = mock.MagicMock(perplexity_api_key="k")
        with mock.patch("aiohttp.ClientSession.post", side_effect=RuntimeError("402")), \
             mock.patch.object(mira_tools, "_web", mock.AsyncMock(return_value="веб-поиск")) as web:
            self.assertEqual(await market.news(self.db, settings, None), "веб-поиск")
        web.assert_awaited_once()
        self.assertNotIn("лонг", web.await_args.args[0]["query"])

    async def test_morning_respects_switches_and_digest_shows_block(self):
        from datetime import date
        from unittest import mock
        from core import connectors, market
        from core.digest import _rich_digest
        settings = mock.MagicMock()
        with mock.patch.object(connectors, "enabled", mock.AsyncMock(return_value=False)):
            self.assertEqual(await market.morning(self.db, settings, None), ([], ""))
        doc = _rich_digest(date(2026, 11, 6), {}, overdue=[], due_today=[], due_tomorrow=[], bd_today=[], bd_tmr=[], pair_lines=[],
                           commits=[], pending=[], cal_events=[], task_names=set(), conflict_lines=[], rain_lines=[], waits=[],
                           silent=[], todoist_lines=[], social_lines=[], sub_lines=[], tip="",
                           market_lines=["BTC $80 512 (+4,0%)"], market_news="1. S&P 500 упал на 4%\n2. Нефть дешевеет")
        self.assertIn("<h4>📈 Рынок</h4>", doc)
        self.assertIn("<li>BTC $80 512 (+4,0%)</li>", doc)
        self.assertIn("📰 Новости рынка", doc)
        self.assertIn("<li>1. S&amp;P 500 упал на 4%</li>", doc)

    def test_connectors_registered(self):
        from core import connectors, mira_tools
        ids = {c["id"]: c["name"] for c in connectors.CONNECTORS}
        self.assertEqual((ids["crypto"], ids["ton"], ids["finnews"]), ("Рынок криптовалют", "TON Wallet", "Perplexity Finance"))
        self.assertEqual(mira_tools.NAMES["market_news"], "finnews")


class TestDayWidget(unittest.IsolatedAsyncioTestCase):
    """v4.106: виджет «Мой день» (Bot API 10.4) и маскировка номеров карт."""

    async def asyncSetUp(self):
        import tempfile, aiosqlite
        from db import init_db
        self.tmp = tempfile.TemporaryDirectory()
        path = os.path.join(self.tmp.name, "t.db")
        await init_db(path)
        self.db = await aiosqlite.connect(path)
        self.db.row_factory = aiosqlite.Row

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    async def _task(self, title, due, synced=0):
        cur = await self.db.execute("INSERT INTO task_sync (task_title, user_id, due_date, synced) VALUES (?, 0, ?, ?)",
                                    (title, due, synced))
        await self.db.commit()
        return cur.lastrowid

    def test_html_fits_profile(self):
        import zlib
        from core import daywidget
        raw, packed = daywidget.html_sizes()
        self.assertLessEqual(raw, 32768)
        self.assertLessEqual(packed, 4096)
        for bad in ("innerHTML", "fetch(", "src=", "<input", "@font-face"):
            self.assertNotIn(bad, daywidget.HTML)
        self.assertIn("W.request('s')", daywidget.HTML)   # свежие данные при открытии чата

    def test_mask_cards(self):
        from core.redact import mask_cards
        self.assertEqual(mask_cards("карта 4111 1111 1111 1111!"), "карта [карта ••1111]!")
        self.assertEqual(mask_cards("4111-1111-1111-1111"), "[карта ••1111]")
        keep = "тел +7 916 123-45-67, заказ 1234567890123, 4276 3800 1234 5678"
        self.assertEqual(mask_cards(keep), keep)   # телефон и номера без проверки по Луну не трогаем
        self.assertEqual(mask_cards("👍 5500 0000 0000 0004", [{"type": "bank_card_number", "offset": 3, "length": 19}]),
                         "👍 [карта ••0004]")
        self.assertIsNone(mask_cards(None))

    async def test_vault_add_masks_cards(self):
        from db import vault_add
        await vault_add(self.db, 7, "u", "in", "кидай на 4111111111111111")
        async with self.db.execute("SELECT text FROM conversations") as cur:
            self.assertEqual((await cur.fetchone())[0], "кидай на [карта ••1111]")

    async def test_build_state(self):
        from core import daywidget
        from core.util import today_local
        t = today_local()
        y, tm = (t - timedelta(days=1)).isoformat(), (t + timedelta(days=1)).isoformat()
        a = await self._task("Позвонить в банк", t.isoformat() + "T15:00:00")
        b = await self._task("Сдать лабу", y)
        await self._task("Завтрашнее", tm)
        c = await self._task("Купить молоко", t.isoformat(), synced=1)
        state, ids = await daywidget.build_state(self.db, prev_ids=[c])
        self.assertEqual(ids, [b, a, c])                       # просроченные, сегодня, закрытые
        self.assertEqual([x[2] for x in state["t"]], [2, 0, 1])
        self.assertIn("15:00", state["t"][1][3])
        self.assertTrue(state["t"][0][3].startswith("просрочено"))
        for i in range(40):
            await self._task(f"Очень длинная задача номер {i} про созвон с подрядчиком по смете", t.isoformat())
        state, ids = await daywidget.build_state(self.db)
        import json
        packed = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode()
        self.assertLessEqual(len(packed), 1000)
        self.assertGreater(state["m"], 0)
        self.assertEqual(len(state["t"]) + state["m"], len(ids))

    async def test_fresh_widget_counts_closed_today(self):
        from core import daywidget
        from core.util import today_local
        from db import mark_sync_done_by_id
        t = today_local()
        a = await self._task("Открытая", t.isoformat())
        b = await self._task("Закрыта сегодня", t.isoformat())
        await mark_sync_done_by_id(self.db, b)
        c = await self._task("Закрыта давно", t.isoformat(), synced=1)
        await self.db.execute("UPDATE task_sync SET closed_at = '2026-01-01 10:00:00' WHERE id = ?", (c,))
        d = await self._task("Будущая, закрыта сегодня", (t + timedelta(days=3)).isoformat())
        await mark_sync_done_by_id(self.db, d)
        state, ids = await daywidget.build_state(self.db)   # свежий /day без прошлого виджета
        self.assertEqual(ids, [a, b])
        self.assertEqual([x[2] for x in state["t"]], [0, 1])

    async def test_query_closes_and_checks_owner(self):
        from unittest import mock
        from types import SimpleNamespace
        from core import daywidget
        from core.util import today_local
        sid = await self._task("Позвонить в банк", today_local().isoformat())
        settings = SimpleNamespace(owner_user_id=1, bot_token="x", todoist_api_token="")
        calls = []

        async def fake(token, method, payload):
            calls.append((method, payload))
            return {"ok": True, "result": {"message_id": 50}}
        from core import widgets
        with mock.patch.object(widgets, "call", fake):
            ok, _ = await daywidget.send(self.db, settings, 1)
            self.assertTrue(ok)
            self.assertTrue(calls[0][1]["allow_queries"])
            await daywidget.middleware(None, SimpleNamespace(model_extra={"widget_query": {
                "id": "q1", "from": {"id": 999}, "chat": {"id": 1}, "message_id": 50, "data": f"d:{sid}"}}),
                {"db": self.db, "settings": settings})
            self.assertEqual(calls[-1], ("answerWidgetQuery", {"widget_query_id": "q1", "data": "Недоступно"}))
            await daywidget.middleware(None, SimpleNamespace(model_extra={"widget_query": {
                "id": "q2", "from": {"id": 1}, "chat": {"id": 1}, "message_id": 50, "data": f"d:{sid}"}}),
                {"db": self.db, "settings": settings})
        async with self.db.execute("SELECT synced FROM task_sync WHERE id = ?", (sid,)) as cur:
            self.assertEqual((await cur.fetchone())[0], 1)
        methods = [m for m, _ in calls]
        self.assertIn(("answerWidgetQuery", {"widget_query_id": "q2", "data": "Готово ✓"}), calls)
        self.assertEqual(methods[-1], "editMessageWidgetData")   # карточка перерисована
        self.assertIn(f"[{sid},", calls[-1][1]["chat_data"])

    async def test_middleware_passes_other_updates(self):
        from types import SimpleNamespace
        from core import daywidget
        seen = []

        async def handler(event, data):
            seen.append(event)
            return "handled"
        ev = SimpleNamespace(model_extra={})
        self.assertEqual(await daywidget.middleware(handler, ev, {}), "handled")
        self.assertEqual(seen, [ev])

    def test_wired(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        main = open(os.path.join(root, "main.py"), encoding="utf-8").read()
        self.assertIn('"widget_query"', main)
        self.assertIn("dp.update.outer_middleware(_widgets.middleware)", main)
        self.assertIn('Command("day", "день")', open(os.path.join(root, "handlers/commands.py"), encoding="utf-8").read())


class TestWidgets(unittest.IsolatedAsyncioTestCase):
    """v4.107: виджеты «Привычки», «Черновик», «Встреча», «Рынок» и общий разбор нажатий."""

    async def asyncSetUp(self):
        import tempfile, aiosqlite
        from types import SimpleNamespace
        from db import init_db
        self.tmp = tempfile.TemporaryDirectory()
        path = os.path.join(self.tmp.name, "t.db")
        await init_db(path)
        self.db = await aiosqlite.connect(path)
        self.db.row_factory = aiosqlite.Row
        self.settings = SimpleNamespace(owner_user_id=1, bot_token="x", todoist_api_token="",
                                        anthropic_model_smart="m", google_service_account_path="sa",
                                        google_calendar_id="cal", timezone="Europe/Moscow")
        self.calls = []

        async def fake(token, method, payload):
            self.calls.append((method, payload))
            return {"ok": True, "result": {"message_id": 77}}
        from unittest import mock
        from core import widgets
        self._p = mock.patch.object(widgets, "call", fake)
        self._p.start()

    async def asyncTearDown(self):
        self._p.stop()
        await self.db.close()
        self.tmp.cleanup()

    async def _tap(self, data, uid=1, mid=77):
        from core import widgets
        await widgets.middleware(None, __import__("types").SimpleNamespace(model_extra={"widget_query": {
            "id": "q", "from": {"id": uid}, "chat": {"id": 1}, "message_id": mid, "data": data}}),
            {"db": self.db, "settings": self.settings, "bot": getattr(self, "bot", None), "anthropic_client": None})
        return [p["data"] for m, p in self.calls if m == "answerWidgetQuery"][-1]

    def test_all_fit_profile(self):
        from core import widgets
        from core.widgets import habits, drafts, meeting, market
        for mod in (habits, drafts, meeting, market):
            raw, packed = widgets.sizes(mod.HTML)
            self.assertLessEqual(raw, 32768)
            self.assertLessEqual(packed, 4096)
            for bad in ("fetch(", "src=", "<input", "@font-face"):
                self.assertNotIn(bad, mod.HTML)

    async def test_owner_only_and_routing(self):
        self.assertEqual(await self._tap("hb:x:1:0101", uid=999), "Недоступно")
        self.assertEqual(await self._tap("hb:x:5:0101"), "Этот день уже вне недели")

    async def test_habits_toggle(self):
        from core import habits
        from core.util import today_local
        from core.widgets import habits as wh
        await habits.add(self.db, "Зарядка")
        ok, _ = await wh.send(self.db, self.settings, 1)
        self.assertTrue(ok)
        today = today_local()
        self.assertEqual(await self._tap(f"hb:x:1:{today:%m%d}"), "Зарядка: сегодня отмечено")
        rows = await habits.week(self.db, today)
        self.assertEqual((rows[0][2], rows[0][3]), (1, "0000001"))
        self.assertEqual(self.calls[-2][0], "editMessageWidgetData")   # карточка перерисована до ответа
        self.assertEqual(await self._tap(f"hb:x:1:{today:%m%d}"), "Зарядка: отметку за сегодня сняла")
        self.assertEqual((await habits.week(self.db, today))[0][3], "0000000")

    async def test_drafts_widget(self):
        from types import SimpleNamespace
        from core import drafts
        from core.widgets import drafts as wd
        drafts._pending["tok1"] = {"bc_id": "bc", "chat_id": 5, "text": "Да, к семи буду", "username": "vladik",
                                   "user_id": 5, "incoming": "к семи сможешь?", "ts": 1e12}
        self.assertTrue(await wd.offer(1, "tok1"))
        self.assertEqual(self.calls[0][0], "sendWidget")
        sent = []

        async def send_message(**kw):
            sent.append(kw)
        self.bot = SimpleNamespace(send_message=send_message)
        self.assertEqual(await self._tap("df:o:tok1"), "Отправила @vladik")
        self.assertEqual(sent[0]["text"], "Да, к семи буду")
        self.assertIn('"st":"s"', self.calls[-2][1]["chat_data"])
        self.assertEqual(await self._tap("df:o:tok1"), "Черновик устарел (бот перезапускался)")   # второй раз не шлёт
        self.assertEqual(len(sent), 1)
        drafts._pending["big"] = {"text": "очень длинно " * 120, "username": "u", "incoming": ""}
        self.assertFalse(await wd.offer(1, "big"))   # не влез — старая карточка
        drafts._pending.pop("big", None)

    async def test_meeting_shift(self):
        from unittest import mock
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from core.widgets import meeting
        sid = (await self.db.execute("INSERT INTO task_sync (task_title, user_id, due_date, calendar_event_id) "
                                     "VALUES ('Встреча', 0, '2026-11-09T18:00', 'ev1')")).lastrowid
        await self.db.commit()
        start = datetime(2026, 11, 9, 18, 0, tzinfo=ZoneInfo("Europe/Moscow"))
        self.assertTrue(await meeting.offer(self.db, self.settings, 1, "ev1", "Встреча с Владом", start, "Влад"))

        async def shift(sa, cal, eid, minutes):
            self.assertEqual((eid, minutes), ("ev1", 15))
            return "2026-11-09T18:15:00+03:00"
        with mock.patch("core.calendar.shift_event", shift):
            self.assertEqual(await self._tap("mt:m:15"), "Сдвинула на 18:15")
        async with self.db.execute("SELECT due_date FROM task_sync WHERE id = ?", (sid,)) as cur:
            self.assertEqual((await cur.fetchone())[0], "2026-11-09T18:15")
        edit = [p for m, p in self.calls if m == "editMessageWidgetData"][-1]
        self.assertIn(str(int(start.timestamp()) + 900), edit["chat_data"])

    async def test_market_state(self):
        from unittest import mock
        from core.widgets import market

        async def coin(gid, days):
            return [100.0, 90.0, 110.0]

        async def stock(sym, period):
            if sym == "CL=F":
                raise RuntimeError("yahoo 500")
            return [50.0, 55.0]
        with mock.patch.object(market, "_coin", coin), mock.patch.object(market, "_stock", stock):
            st = await market.build_state("7")
        names = [r[0] for r in st["r"]]
        self.assertNotIn("Нефть", names)   # упавший источник просто пропускаем
        self.assertEqual(st["r"][0][:3], ["BTC", "$110,0", 10.0])
        self.assertEqual(st["r"][0][3], "i0z")
        self.assertEqual(market._fmt("Доллар", 82.437), "82,44 ₽")
        self.assertEqual(len(market._spark(list(range(100)))), 20)


    async def test_refusal_cached_and_text_fallbacks(self):
        """v4.110: 13.11 Telegram запретил виджеты ботам — отказ запоминаем на 3 ч и шлём то же текстом."""
        from unittest import mock
        from core import widgets
        from core.widgets import market, habits
        self._p.stop()                       # здесь нужна настоящая call с подменённым _post
        posts = []

        async def refuse(token, method, payload):
            posts.append(method)
            if method == "sendWidget":
                return {"ok": False, "description": "Bad Request: INPUT_MEDIA_WIDGET_NOT_ALLOWED"}
            return {"ok": True, "result": {"message_id": 91}}
        widgets._BLOCK["until"] = 0.0
        try:
            with mock.patch.object(widgets, "_post", refuse):
                r1 = await widgets.call("t", "sendWidget", {})
                r2 = await widgets.call("t", "sendWidget", {})
                self.assertTrue(widgets.refused(r1["description"]) and widgets.refused(r2["description"]))
                self.assertEqual(posts.count("sendWidget"), 1)           # второй раз Telegram не дёргаем
                self.assertTrue(widgets.blocked())
                await widgets.call("t", "sendMessage", {})               # остальные методы ходят как обычно
                self.assertEqual(posts.count("sendMessage"), 1)
            widgets._BLOCK["until"] = 0.0
            with mock.patch.object(widgets, "_post", mock.AsyncMock(return_value={"ok": True, "result": {}})):
                await widgets.call("t", "sendWidget", {})
            self.assertFalse(widgets.blocked())
        finally:
            widgets._BLOCK["until"] = 0.0
            self._p.start()
        state = {"p": "1", "u": "10:00", "r": [["BTC", "$74 822", -3.8, "ab"], ["Золото", "$4 128", 3.0, "ba"]]}
        with mock.patch.object(market, "build_state", mock.AsyncMock(return_value=state)):
            t = await market.text()
        self.assertIn("🔴 BTC: $74 822  −3,8%", t)
        self.assertIn("🟢 Золото: $4 128  +3,0%", t)
        self.assertIn("добавь привычку", await habits.text(self.db))

    async def test_meeting_text_card_when_refused(self):
        """v4.110: виджет встречи не пустили — та же карточка текстом с кнопками «+15 мин / +1 ч»."""
        import time
        from datetime import datetime
        from unittest import mock
        from core import widgets
        from core.widgets import meeting
        self._p.stop()
        sent = []

        async def fake(token, method, payload):
            sent.append((method, payload))
            if method == "sendWidget":
                return {"ok": False, "description": "Bad Request: INPUT_MEDIA_WIDGET_NOT_ALLOWED"}
            return {"ok": True, "result": {"message_id": 88}}
        widgets._BLOCK["until"] = 0.0
        try:
            with mock.patch.object(widgets, "_post", fake):
                start = datetime.fromtimestamp(time.time() + 27 * 60)
                self.assertTrue(await meeting.offer(self.db, self.settings, 1, "ev1", "Визит в стоматологию", start, "Клиника"))
        finally:
            widgets._BLOCK["until"] = 0.0
            self._p.start()
        msg = [p for m, p in sent if m == "sendMessage"][0]
        self.assertIn("Визит в стоматологию", msg["text"])
        self.assertIn("через 2", msg["text"])
        kb = [p for m, p in sent if m == "editMessageReplyMarkup"][0]["reply_markup"]["inline_keyboard"][0]
        self.assertEqual([b["callback_data"] for b in kb], ["mtb:15:88", "mtb:60:88"])
        self.assertEqual((await widgets.get_json(self.db, "mt:msg:88"))["e"], "ev1")
        with mock.patch("core.calendar.shift_event", mock.AsyncMock(return_value="2026-11-17T19:00:00+03:00")):
            note, text = await meeting.on_button(self.db, self.settings, 88, 15)
        self.assertEqual(note, "Сдвинула на 19:00")
        self.assertIn("в 19:00", text)
        self.assertEqual(await meeting.on_button(self.db, self.settings, 99, 15), ("Не нашла встречу", None))

    def test_app_button_and_market_screen(self):
        """v4.112: текст в чате с кнопкой на экран мини-приложения; экран «Рынок» и ссылки ?tab= / ?open=."""
        from types import SimpleNamespace
        from handlers.commands import _app_kb
        kb = _app_kb(SimpleNamespace(webapp_url="https://mira.sncont.com/"), "tab=den")
        self.assertEqual(kb.inline_keyboard[0][0].web_app.url, "https://mira.sncont.com/?tab=den")
        self.assertIsNone(_app_kb(SimpleNamespace(webapp_url=""), "open=market"))
        from core.webapp_api import PAGE
        html = PAGE.read_text(encoding="utf-8")
        for part in ('data-open="market"', "api(`market?p=${p}`)", 'QS.get("tab")', 'QS.get("open")', "data-mk="):
            self.assertIn(part, html)

    async def test_market_api_cached(self):
        """v4.112: api/market отдаёт котировки периода и держит их 10 минут; чужой период — ошибка."""
        from unittest import mock
        from types import SimpleNamespace
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        from core import webapp_api
        from core.widgets import market
        app = web.Application()
        st = SimpleNamespace(owner_user_id=1, bot_token="1:x", webapp_url="", timezone="Europe/Moscow")
        webapp_api.setup(app, None, self.db, st, None)
        calls = []

        async def fake(p):
            calls.append(p)
            return {"p": p, "u": "10:00", "r": [["BTC", "$1", 1.0, "ab"]]}
        with mock.patch.object(market, "build_state", fake), \
                mock.patch.object(webapp_api, "check_init_data", lambda *a, **k: True):
            async with TestClient(TestServer(app)) as c:
                h = {"X-Init-Data": "x"}
                r1 = await c.get("/webapp/api/market?p=7", headers=h)
                body = await r1.json()
                r2 = await c.get("/webapp/api/market?p=7", headers=h)
                r3 = await c.get("/webapp/api/market?p=99", headers=h)
        self.assertEqual(r1.status, 200)
        self.assertEqual(body["r"][0][0], "BTC")
        self.assertEqual(r2.status, 200)
        self.assertEqual(calls, ["7"])                     # второй раз из кэша
        self.assertEqual(r3.status, 400)

    async def test_partial_registry(self):
        """v4.107.1: цикл дайджеста импортирует только habits — остальные виджеты всё равно ловят нажатия."""
        from core import widgets
        from core.widgets import habits
        saved = dict(widgets._KINDS)
        widgets._KINDS.clear()
        widgets.register("hb", habits.on_query)
        try:
            self.assertEqual(await self._tap("mt:m:15"), "Не нашла встречу")
            self.assertNotEqual(await self._tap("df:n:nope"), "Не поняла нажатие")
        finally:
            widgets._KINDS.clear()
            widgets._KINDS.update(saved)


class TestMeetingPrepLayout(unittest.TestCase):
    """v4.113 (Bot API 10.5): подготовка к встрече — без пустой строки заголовка, память и переписка в одном раскрывающемся блоке."""

    def test_table_without_empty_header(self):
        from core.richmsg import table
        self.assertNotIn("<th>", table([], [["👤 Кто", "Елена"]]))
        self.assertNotIn("<th>", table(["", ""], [["a", "b"]]))
        self.assertIn("<th>Раздел</th>", table(["Раздел", "Зап."], [["a", "b"]]))

    def test_prep_docs(self):
        from core.digest import prep_docs
        prof = {"name": "Елена", "username": "Albion_OOO", "intent": "напоминания о записях",
                "summary": "Рассылочный аккаунт клиники <b>", "commitments": "подтвердить визит"}
        ctx = "[2026-11-17] in: Могу предложить в четверг в 18:30\n[2026-11-17] out: да давайте\n"
        new, old = prep_docs("Визит в стоматологию", 1795000000, 27, "18:30", prof, ctx)
        self.assertNotIn("<th>", new)
        self.assertIn("<blockquote expandable>", new)
        self.assertIn("<li>[2026-11-17] out: да давайте</li>", new)
        self.assertIn("⚠️ Ты обещал: подтвердить визит", new.split("<blockquote expandable>")[0])   # обещание на виду
        self.assertIn("&lt;b&gt;", new)                                    # текст контакта экранирован
        self.assertIn("<details><summary>💬 Последние сообщения</summary>", old)
        n2, o2 = prep_docs("X", 1, 5, "10:00", None, "")
        self.assertIn("контакт не распознан", n2)
        self.assertEqual(n2, o2)
