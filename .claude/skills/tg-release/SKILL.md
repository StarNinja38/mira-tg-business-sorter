---
name: tg-release
description: >-
  Полный релиз/сохранение сессии TG-SORTER по чеклисту CLAUDE.md. ОБЯЗАТЕЛЬНО используй, когда
  владелец говорит «сохрани сессию», «выкати версию», «обнови доки», «заканчиваем на сегодня»,
  или когда фича/фикс готовы и их надо довести до конца: деплой → CHANGELOG → доки (repo↔vault)
  → PDF → заметка сессии → HANDOFF → git push обоих репо + тег → синк vault. Держит версии,
  пути и формат коммитов, чтобы ничего не забыть и не разъехались зеркала документации.
---

# tg-release — релиз и сохранение сессии

Полный чеклист после готовой правки. Пропускать пункты нельзя — иначе доки/зеркала разъезжаются.

## 0. Версия
Patch (`vX.Y.Z`) — фикс; minor (`vX.Y`) — фича. Текущую смотри в CHANGELOG (последняя сверху).

## 1. Деплой
Сначала **tg-deploy** (залить + smoke + рестарт + подтвердить `ready`). Если без него — здесь нечего релизить.

## 2. CHANGELOG (vault)
`~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Claude(Second Brain)/02-projects/TG-SORTER/CHANGELOG.md` — добавить запись **сверху**: `## [vX.Y] — ГГГГ-ММ-ДД — заголовок` + разделы Добавлено/Изменено/Исправлено.

## 3. Доки (2 зеркала — держать синхронными!)
- Репо: `~/Downloads/TG-sort/docs/` · Vault: `…/Claude(Second Brain)/Telegram/Docs/`
- Обновить релевантное: `03 — Функции` / `04 — Команды` / `02 — Архитектура` / `05 — Интеграции` / `06 — Деплой`; **новая команда → ещё и в `main.py` bot_commands**.
- Запись в `08 — Журнал разработки.md` (новый Этап).
- **PDF** (тёмный канон, вшитые шрифты):
  ```bash
  DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib ~/.tgsorter/pdfvenv/bin/python scripts/build_pdf_design.py vX.Y
  ```
  Затем `cp docs/TG-Sorter-Документация-vX.Y.pdf "…/Telegram/Docs/"` и удалить старый PDF в vault.
- Синк правок repo→vault: `cp "docs/<файл>.md" "…/Telegram/Docs/<файл>.md"` (или наоборот, если правил vault). Сверять `diff`.

## 4. Заметка сессии (если значимо)
`…/03-sessions/ГГГГ-ММ/сессия ГГГГ-ММ-ДД <тема>.md` (frontmatter: tags/project: TG-SORTER/date/type: session). Грабли — отдельной таблицей. Строку в `03-sessions/index.md` сверху.

## 5. HANDOFF
`02-projects/TG-SORTER/HANDOFF.md` — новый блок «Последняя сессия» + «Следующие шаги».

## 6. Git
**Код-репо** (`~/Downloads/TG-sort`):
```bash
git add -A && git commit -q -m "<тип>: описание

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>" && git push -q origin main
git tag vX.Y && git push -q origin vX.Y   # при новой версии
```
**Vault** (`~/Desktop/Claude (Second Brain)` → симлинк на `Claude(Second Brain)`): коммить ТОЛЬКО файлы TG-SORTER (CHANGELOG/HANDOFF/заметка/index), не подмешивать чужие (SN-CONTENT/_carryover). Push отклонён (параллельная сессия) → `git pull --rebase -q && git push -q`.

## 7. Vault sync (контент Telegram/)
Папка `Telegram/` — отдельный git-репо (tg-vault), его коммитит+пушит launchd (раз в 15 мин). Принудительно: `/opt/homebrew/bin/python3 ~/.tgsorter/sync_vault.py`. CRM-пульт/контакты/доки в `Telegram/` уходят им — вручную не пушить.

## Проверка в конце
- Код-репо: `git status -s` чисто, `git log origin/main..HEAD` пусто (0 unpushed), нужный тег запушен.
- second-brain: только TG-SORTER закоммичено, 0 unpushed.
- Прод: `systemctl is-active` + `ready` в логах.

## Не смешивать
TG-SORTER ≠ SN-CONTENT (общий лишь сервер). Не путать их CHANGELOG/HANDOFF/репозитории.
