---
name: tg-diagnose
description: >-
  Read-only расследование на проде бота @Sort38_bot (TG-SORTER). ОБЯЗАТЕЛЬНО используй,
  когда владелец спрашивает «почему бот так сделал / не сделал», «почему задача создалась/
  закрылась/не создалась», «посмотри по переписке», «проверь на проде», или нужно понять
  поведение по контакту/задаче/роли/дайджесту. Кодирует безопасный (mode=ro) способ поднять
  данные одним python-скриптом, шпаргалку по схеме БД, обход зависаний ssh и приём «репро»
  для воспроизведения решений LLM (извлечение/авто-закрытие). Чтение — всегда безопасно.
---

# tg-diagnose — read-only разбор прода

Цель — понять, **почему** бот повёл себя так, на фактах из прод-БД, ничего не меняя.

## Как поднимать данные (надёжно)
Пиши ОДИН python-скрипт, заливай и запускай как root (read-only, ничего не пишет):
```bash
scp /tmp/diag.py root@<server-ip>:/tmp/diag.py
ssh <server-ip> "cd /opt/tg-business-sorter && ./venv/bin/python -u /tmp/diag.py 2>&1 | grep -v 'INFO\|DEBUG'"
```
- Подключение **read-only**: `aiosqlite.connect(f"file:{settings.db_path}?mode=ro", uri=True)`.
- Всегда `python -u` (небуферизованно) — иначе вывод по ssh «застревает» до выхода и кажется, что висит.
- ssh часто уходит в фон → **жди уведомление о завершении, не перечитывай пустой output**. `unittest|tail` и иные пайпы могут зависнуть — избегай, гоняй одиночный python.

## Шпаргалка по схеме (data/bot.db, WAL)
- **conversations**: `user_id, username, direction('in'/'out'), text, media_type, created_at, category`. Контекст: `db.vault_get_context(db, uid, limit=N, since_hours=48)` — отдаёт строки с префиксом `[ГГГГ-ММ-ДД]`.
- **task_sync**: `id, todoist_task_id, notion_page_id, synced(0=открыта), task_title, user_id, closed_at, topic, notion_status, calendar_event_id, due_date`. Открытые задачи: `db.get_open_tasks_for_user(db, uid)` (отдаёт `due_date`).
- **contact_profiles**: `user_id, username, name, relationship(=роль), summary, facts, commitments, notion_contact_id, message_count, photo_*`. Роли: `db.get_distinct_roles`, `db.get_contacts_by_role`.
- **pending_analysis**: `user_id, dirty(1=ждёт разбора), last_activity` — армится входящими; исходящие владельца армят через `_OWNER_COMMIT_RE` в business.py.
- Прочее: `message_log`, `saved_links`, `notification_log`, `digest_state`, `settings_kv`, `task_feedback`.

Резолв контакта: `db.get_contact_by_username(db, uname)` или по `user_id`. Username владельца в БД — `"me"` (id 111222333).

## Приём «репро» — воспроизвести решение LLM (доказать причину, не гадать)
Самый сильный ход: скормить РЕАЛЬНУЮ переписку в ту же функцию и посмотреть вердикт.
- Извлечение задач: `from core.actions import extract_action_items` → `await extract_action_items(client, settings.anthropic_model_smart, uname, "BUSINESS", ctx)`. Сравни контекст БЕЗ/С датами, полный (профиль+шапка) vs голый.
- Авто-закрытие: `_check_tasks_done_batch(client, model, open_tasks, conversation)` — какие задачи судья счёл выполненными.
Это показывает, дело в извлечении/судье или в триггере (settle не армился).

## Логи
`journalctl -u tg-sorter --since '3 hours ago' --no-pager | grep -iE 'Auto-closed|Settle|Extracted|Skipped|Registry|ERROR' | tail`. Пусто по фигуранту = settle/действие просто не запускалось (часто корень — триггер, а не логика).

## Куда писать скрипты
В `/tmp` (локально создать → scp). Не оставлять мусор в репо. Любые ПРАВКИ — это уже tg-prod-fix (с подтверждением владельца), не сюда.
