# CLAUDE.md — TG-SORTER (Telegram Business Sorter)

Бот @Sort38_bot. Python/aiogram/Claude Haiku. Деплой: `/opt/tg-business-sorter` на `<old-server-ip>`, systemd `tg-sorter`.

> ⚠️ **ОТДЕЛЬНЫЙ ПРОЕКТ.** Не связан с SN-CONTENT. Общий только сервер. Никогда не смешивать документацию, changelog, HANDOFF, репозитории этих двух проектов.

---

## 🧊 ПРАВИЛО: feature-freeze (P2 council-аудита, с 2026-06-22)
По итогам `/council`: бот переинженерен для 1 пользователя, темп багов = хрупкость, реальная цена — внимание, отнятое у стриминга. **До стабилизации НЕ добавлять новые фичи.** Разрешено только:
- **P0-класс**: баги, теряющие/портящие данные; фиксы регрессий.
- **Структурные упрощения** из бэклога аудита (убирают сложность, не добавляют).
Перед ЛЮБОЙ новой фичей — спросить владельца «это точно не подождёт?» + напомнить meta-вопрос (хобби/R&D vs секретарь; само-тест: `git log` бота vs SN-CONTENT за месяц). **Не лечить переусложнение новыми абстракциями.** Заморозка снимается только явным решением владельца.

## 🟢 ПРАВИЛО: создание сущностей — один источник, идемпотентно

Чтобы не было дублей и пропаж, любое создание во внешних системах подчиняется правилу:

| Сущность | Единственный источник | Дедуп-ключ | Идемпотентность |
|----------|----------------------|------------|-----------------|
| **Задача** (Todoist+Notion) | `core/actions.process_action_items` (из `tasks_loop` по затиханию) | topic-match (`_check_update_existing`) + **жёсткий backstop** `task_exists_recently` (нормализ. название, 24ч) | проверка перед созданием; реестр = `task_sync`; частичное создание логируется (`Partial task creation`) |
| **Контакт** (Notion) | `core/vault._sync_profile_to_notion` (из `enrich_profile`) | `notion_contact_id` + search by Username | search-before-create, сохраняет id; **реконсиляция** `sync._reconcile_contacts` дотягивает пропавшие (fix fire-and-forget) |
| **Ссылка** | `db.save_links` | `(user_id, url)` — SELECT перед INSERT | один URL от человека не сохраняется дважды |
| **Импорт Notion→Todoist** | `sync._import_manual_notion_tasks` | `notion_page_tracked` (по page_id) + `title_tracked_in_sync` (по названию) | двойная защита от повторного импорта |
| **Событие** (Google Calendar) | `core/calendar.create_calendar_event` (из `_create_everywhere` + fallback `update_task_calendar`) | **тот же день** + якорь: контакт (`id=<user_id>` в описании) ИЛИ нормализ. название (без `❓`/эмодзи) | search-before-create: ищет среди событий **бота** (`creator == сервис-аккаунт`, ручные владельца не трогает) → нашёл `patch` (перенос/обновл.), нет → `insert`; `event_id` в `task_sync.calendar_event_id` |
| **Архив** (Notion) | `core/digest._push_archive_to_notion` | одна страница на прогон | — |

**Принципы (соблюдать при любом новом создании):**
1. **Один механизм** на сущность — не плодить параллельные пути создания.
2. **Search-before-create** — перед POST искать существующее (по стабильному ключу), нашёл → update, нет → create.
3. **Стабильный ключ** — якорь дедупа = неизменяемое (`user_id`, сохранённый `notion_id`); username вторичен (может смениться).
4. **Сохранять внешний id** в локальную БД сразу после создания (`notion_contact_id`, `task_sync`).
5. **Дедуп-ключ** обязателен (username / topic-match / sync row / `(user_id,url)`).
6. **Backstop** — кроме семантического (LLM topic-match) всегда иметь жёсткий ключ-проверку (нормализ. название), т.к. LLM-дедуп ненадёжен.
7. **Реконсиляция** — фоновый цикл дотягивает то, что упало при первом создании (fire-and-forget → not-forgotten).
8. **Логировать создание и частичные сбои** — не молча (реестр-лог `Registry:` / `Partial task creation`).
9. Никогда не создавать из `handlers/` напрямую в обход профильной функции.

---

## 🟢 ПРАВКА ПРОД-БД — РАЗРЕШЕНА (оба пути)

Владелец санкционировал **оба** способа правки данных контактов/профилей:
1. **Команды бота** — `/setname @user Имя`, `/addfact @user текст` (бот сам пишет БД + синк Notion). Удобно владельцу.
2. **Прямая правка прод-БД** (Claude по ssh) — РАЗРЕШЕНА. Это НЕ запрещено.

Требования к прямой правке (чтобы не разъехалось):
- **Через штатные функции бота** где возможно (`upsert_contact_profile` + `_sync_profile_to_notion`), а не сырой SQL — тогда Notion/vault остаются синхронными и enrich не откатит.
- **Бэкап/верификация**: вывести строку ДО и ПОСЛЕ; чтение всегда безопасно.
- Запуск: `cd /opt/tg-business-sorter && PYTHONPATH=/opt/tg-business-sorter ./venv/bin/python /tmp/script.py`.
- Соблюдать идемпотентность/дедуп из правил выше; не плодить сущности.

### Админ-CLI `scripts/admin_ops.py` (v3.5.1) — закрытый набор операций
Запуск: `ssh <old-server-ip> /opt/tg-business-sorter/venv/bin/python /opt/tg-business-sorter/scripts/admin_ops.py <cmd>`. Скрипт сам делает chdir в `/opt/tg-business-sorter`.
- **Чтение (безопасно):** `read <@user|id>` · `health` · `logs [N]`.
- **Правки (меняют прод-БД):** `setname <@user> <Имя>` · `addfact <@user> <текст>` · `fixrole <@user> <роль>` · `contact_memory` (пересбор `/me`). Запускать как `sudo -u tgsorter …`, чтобы БД осталась за tgsorter.

### Модель прав Claude Code (settings.local.json) — владелец настроил
- **Без подтверждения:** `admin_ops.py read|health|logs` + `rsync`/`scp` (чтение/диагностика/деплой/заливка файлов).
- **С подтверждением (Yes владельца):** любые правки (`admin_ops.py setname/addfact/fixrole/contact_memory`, скрипты в `/tmp`), рестарт, произвольный `ssh`.
- Принцип: **читаю/деплою сам, любые изменения данных — только с подтверждением.** Allow-правила узкие (read-подкоманды по точному префиксу), не blanket-root.

---

## 🔴 ПРАВИЛО: после ЛЮБОГО изменения — обновить документацию

При изменении кода или поведения бота **обязательно** пройти весь чеклист. Не пропускать.

### 1. Деплой на сервер
```bash
rsync -avz <файл> root@<old-server-ip>:/opt/tg-business-sorter/<путь>
ssh <old-server-ip> "systemctl restart tg-sorter && sleep 4 && journalctl -u tg-sorter -n 5"
```
Убедиться что бот поднялся без ошибок.

### 2. CHANGELOG (история версий)
Файл: `~/Desktop/Claude (Second Brain)/02-projects/TG-SORTER/CHANGELOG.md`
- Добавить запись сверху: `## [vX.Y] — ГГГГ-ММ-ДД` с разделами Добавлено/Изменено/Исправлено
- Поднять версию (patch — фикс, minor — фича)

### 3. Документация (2 зеркала — держать синхронными!)
- **Vault:** `~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Claude(Second Brain)/Telegram/Docs/` (контейнер Obsidian iCloud, синк на Mac/iPhone/iPad)
- **Репо:** `~/Downloads/TG-sort/docs/`
- Обновить релевантный файл: 03-Функции / 04-Команды / 02-Архитектура / 05-Интеграции / 06-Деплой
- Новая команда → добавить в `04 — Команды.md` И в меню бота (`main.py` bot_commands)
- Добавить запись в `08 — Журнал разработки.md` (новый этап)
- **PDF — НЕ каждую версию** (P3c council: доки не должны быть отдельным проектом; 9 МБ PDF каждый патч раздувал vault/iCloud и тормозил мобайл). Собирать только на **мажорный релиз (vX.0)** или **по явному запросу владельца**. Патч/minor — `CHANGELOG` + `HANDOFF` достаточно. Когда собираешь (WeasyPrint, вшитые шрифты):
  - **ОСНОВНОЙ — тёмный техно** (Linear/Vercel, акцент Telegram-blue #2AABEE): `DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib ~/.tgsorter/pdfvenv/bin/python scripts/build_pdf_design.py vX.Y` (канон, чистит старые `*-vX.Y.pdf`)
  - запасной светлый (Stripe-like): `… scripts/build_pdf_web.py vX.Y` (тот же выходной файл — перезапишет канон)
  - reportlab-`build_pdf.py` — легаси
- После: `cp ~/Library/Mobile\ Documents/iCloud~md~obsidian/Documents/Claude\(Second\ Brain\)/Telegram/Docs/*.md ~/Downloads/TG-sort/docs/`

### 4. HANDOFF (статус проекта)
Файл: `02-projects/TG-SORTER/HANDOFF.md`
- Обновить «Последняя сессия» + «Следующие шаги» + лог обновлений

### 5. Заметка сессии (если значимая работа)
- Создать `03-sessions/ГГГГ-ММ/сессия ГГГГ-ММ-ДД <тема>.md` по формату
- Добавить строку в `03-sessions/index.md` (проект: TG-SORTER)
- Грабли вынести в отдельную таблицу

### 6. Git (код + репо-доки)
```bash
cd ~/Downloads/TG-sort
git add -A && git commit -m "<тип>: описание

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
git push origin main
git tag vX.Y && git push origin vX.Y   # при новой версии
```

### 7. Vault git push
```bash
cd ~/Desktop/Claude\ \(Second\ Brain\) && git add -A && git commit -m "tg-sorter: описание" && git push
```

---

## Где что (быстрая карта)

| Что | Где |
|-----|-----|
| Код | `~/Downloads/TG-sort` (этот репо) |
| Деплой | `/opt/tg-business-sorter` на сервере |
| Конфиг | `/opt/tg-business-sorter/.env` (секреты, НЕ в git) |
| БД | `/opt/tg-business-sorter/data/bot.db` (WAL) |
| Память (vault) | `~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Claude(Second Brain)/Telegram/` (контейнер Obsidian iCloud → синк Mac/iPhone/iPad). Пишет `~/.tgsorter/sync_vault.py` по launchd (**Homebrew** python `/opt/homebrew/bin/python3` + FDA на его Python.app). `~/TelegramVault` — старый бэкап |
| Документация | `docs/` (репо) + `Telegram/Docs/` (vault) |
| Проект в Obsidian | `02-projects/TG-SORTER/` (README/HANDOFF/CHANGELOG) |
| GitHub (код) | `StarNinja38/tg-business-sorter` (приватный) |
| GitHub (vault-бэкап) | `StarNinja38/tg-vault` (приватный) — резерв папки `Telegram/` (контакты/переписка/вложения/архив). Git-репо лежит прямо в папке `Telegram/`. **Актуализация авто:** `sync_vault.push_vault_repo()` коммитит+пушит в конце каждого синка (launchd, раз в 15 мин), только при изменениях |

## Тесты
- Smoke (чистые функции): `cd /opt/tg-business-sorter && ./venv/bin/python -m unittest tests.test_core -v`
- **Eval-харнес извлечения** (перед ЛЮБЫМ изменением `EXTRACT_SYSTEM`!): `./venv/bin/python scripts/eval_extraction.py 3` — регресс-корпус реальных кейсов (встреча/отказ/смолток/мем), majority-of-N. Должно быть 14/14. Плюс `scripts/eval_real_threads.py 5` — настоящие переписки на точном вводе бота (id из прод-БД), база в шапке скрипта. Стоит немного денег (Sonnet).

## Что НЕ делать
- ❌ Не смешивать с SN-CONTENT (разные проекты, общий лишь сервер)
- ❌ Не коммитить `.env`, `gcal-service.json`, `data/`, `logs/`
- ❌ Не ставить `@`/`#` в начало названий задач Todoist (парсятся как метка/проект)
- ❌ Не оставлять документацию несинхронной между `docs/` и `Telegram/Docs/`
- ❌ Приватные данные людей (`Telegram/Contacts,Conversations,Archive,Вложения`) — только в репо `tg-vault`. Не подмешивать в репо кода и не убирать из `.gitignore` в `second-brain`. Бэкап актуализируется автоматически (`push_vault_repo` в синке) — вручную пушить не нужно
- ❌ **НЕ трогать роли/типы контактов** (эмёрджентная LLM-кластеризация в `enrich_profile`: `relationship`/`contact_type`, `known_roles`/`known_types`). Владелец ценит фичу, она работает. Не «упрощать» в ручные теги, не рефакторить (council P3b — осознанно пропущен). Чинить только если реально сломается.

## Архитектура (кратко)
`main.py` → роутеры (`handlers/business,commands,groups,callbacks`) + фоновые циклы (`core/sync,digest`).
`core/`: classifier (правила+Claude), actions (задачи→Todoist/Notion/Calendar), vault (память), transcribe (голос), documents (PDF), calendar (Google).
БД-таблицы: whitelist, message_log, conversations, contact_profiles, task_sync, saved_links, notification_log, digest_state, settings_kv.
