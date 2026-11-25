---
name: tg-prod-fix
description: >-
  Санкционированная правка прод-БД бота @Sort38_bot (TG-SORTER): исправить/создать/закрыть/
  перенести задачу, поправить роль/имя/факты/фото контакта, почистить обещания. ОБЯЗАТЕЛЬНО
  используй, когда владелец просит «почини в БД», «закрой/создай/перенеси задачу», «верни/переоткрой задачу»
  (бот закрыл преждевременно), «поменяй роль»,
  «исправь контакт», или когда диагностика (tg-diagnose) нашла, что нужно поправить данные на проде.
  Кодирует модель прав (правки — только с явным «да» владельца), запуск под tgsorter, штатные
  функции вместо сырого SQL, идемпотентность/дедуп и верификацию до/после, чтобы Notion/vault не разъехались.
---

# tg-prod-fix — правка прод-БД (с подтверждением)

Прод-правки **разрешены** владельцем (CLAUDE.md «оба пути»), но требуют **явного «да»** на каждую (модель прав: read/деплой — без спроса, любые правки — с подтверждением). Если «да» ещё не прозвучало — спроси, покажи что именно поменяешь.

## Как запускать
Скрипт в `/tmp` (локально создать → `scp`), запуск **под tgsorter**, чтобы БД осталась за ним:
```bash
scp /tmp/fix.py root@<server-ip>:/tmp/fix.py
ssh <server-ip> "sudo -u tgsorter /opt/tg-business-sorter/venv/bin/python -u /tmp/fix.py 2>&1 | grep -v 'DEBUG'"
```
Обычная (RW) connection: `aiosqlite.connect(settings.db_path)`. Всегда `python -u`, ждать уведомление.

## Через ШТАТНЫЕ функции (не сырой SQL) — чтобы синк не сломался
- **Контакт (имя/факты/роль/фото):** `db.upsert_contact_profile(db, uid, username, increment=False, **fields)` → затем `core.vault._sync_profile_to_notion(...)`. Так Notion/vault остаются синхронными, и enrich не откатит.
- **Создать задачу:** `core.actions._create_everywhere(task={title,due_date,topic,needs_clarification,description}, username, user_id, category, original_text, telegram_link, todoist_token, notion_token, notion_database_id, bot=None, db, owner_id)` → вернёт `(todoist_id, notion_id, cal_id)` → `db.save_task_sync(db, td, no, user_id, title, topic, due_date=_normalize_due(due))` → `db.set_calendar_event_id(db, sid, cal)` если есть время.
- **Закрыть задачу:** `core.sync.complete_todoist_task` + `core.sync.update_notion_status(token, page_id)` (Готово) + `db.mark_sync_done_by_id(db, id)`. (Если страница Notion архивна — update вернёт 400, это ок: архив в дайджест не попадает.) Календарное событие удалить: `core.calendar.delete_calendar_event`.
- **Вернуть (переоткрыть) задачу** — частый кейс: бот закрыл преждевременно, владелец просит вернуть. Штатно (НЕ сырой SQL — иначе Todoist/Notion разъедутся):
  ```python
  from core.actions import _reopen_todoist            # POST /tasks/{id}/reopen
  from core.sync import update_notion_status          # status="В работе" (НЕ Готово)
  from db import reopen_task_sync_by_id               # synced=0, closed_at=NULL, notion_status='В работе'
  # row = SELECT * FROM task_sync WHERE id=<id>
  if row['todoist_task_id']:  await _reopen_todoist(s.todoist_api_token, row['todoist_task_id'])
  if row['notion_page_id']:   await update_notion_status(s.notion_token, row['notion_page_id'], status="В работе")
  await reopen_task_sync_by_id(db, <id>)              # by row id — работает и для partial (todoist_id=NULL)
  ```
  **Календарь не трогать:** `auto_close` событие НЕ удаляет (только Todoist/Notion/synced) → `calendar_event_id` цел, событие на месте. Поэтому при реопене календарь не восстанавливаем.
  Найти id: `SELECT id,task_title,synced,closed_at FROM task_sync WHERE task_title LIKE '%…%' ORDER BY id DESC` (закрытая = `synced=1`, есть `closed_at`).
- **Перенести задачу:** `core.actions.update_todoist_task` + `update_notion_due_date` + `update_task_calendar(db, task, title, _normalize_due(due))` (сохрани ВРЕМЯ, если было). `db.set_task_due`.
- **Роль/группа:** `upsert_contact_profile(relationship=...)` — роль свободная, бот её ведёт сам; для Notion поля «Роль» — PATCH select (авто-создаёт опцию).
- **Notion напрямую** (заголовок/Username/Роль): PATCH `/v1/pages/{id}` (через бот-токен) или Notion-MCP. Select-имя без запятых.

## Идемпотентность / дедуп (из CLAUDE.md — соблюдать!)
- **Search-before-create:** перед POST искать существующее по стабильному ключу (user_id, сохранённый notion_id); нашёл → update, нет → create.
- **Backstop задач:** `db.task_exists_recently(db, uid, title, due_date=...)` — учитывает день (разные дни = разные задачи).
- **Один механизм** на сущность, не плодить параллельные пути. Сохранять внешний id сразу.

## Верификация до/после (обязательно)
Печатай строку ДО и ПОСЛЕ правки (читать безопасно). Это бэкап-в-логе и доказательство, что поменялось именно то.

## Владелец
Владелец id `111222333`, username в БД `"me"` (в Notion отображается реальным `@StarNinja38` через `settings.owner_username`). Не менять локальный ключ «me» без нужды — на него завязана логика.

## После правок данных
Если менялись роли/контакты, отображаемые в vault — триггерни синк (`/opt/homebrew/bin/python3 ~/.tgsorter/sync_vault.py`) или дождись launchd. Код при этом обычно не трогаем (это не релиз) — если трогали код, дальше tg-release.
