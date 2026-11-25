---
name: tg-deploy
description: >-
  Деплой изменений бота @Sort38_bot (TG-SORTER) на прод и проверка, что он поднялся.
  ОБЯЗАТЕЛЬНО используй этот скилл всякий раз, когда нужно залить правку кода на сервер,
  перезапустить бота, «задеплой», «выкати», «перезапусти тг-сортер», или после редактирования
  любого файла в core/, handlers/, db.py, main.py, config.py этого проекта — чтобы не забыть
  ни один файл, прогнать smoke правильным способом и убедиться по логам, что бот в строю.
  Не для полного релиза с документацией (для этого — tg-release).
---

# tg-deploy — деплой + проверка бота

Прод: `/opt/tg-business-sorter` на `<server-ip>`, systemd-юнит **`tg-sorter`**, пользователь `tgsorter`, venv `./venv/bin/python`. Владелец id `111222333`.

## Порядок (не пропускать шаги)

1. **Синтаксис локально** — `python3 -m py_compile <изменённые .py>`. Дёшево ловит опечатки до сервера.

2. **Залить ВСЕ изменённые файлы** (rsync разрешён без подтверждения):
   ```bash
   rsync -avz core/actions.py root@<server-ip>:/opt/tg-business-sorter/core/actions.py
   ```
   ⚠️ Грабля дня: если ты добавил функцию в `db.py` ПОСЛЕ предыдущего деплоя, а потом залил только `commands.py` — на проде её нет, команда упадёт `ImportError`. **Сверь, что задеплоены все файлы, которые менялись в сессии**, а не только последний.

3. **Import-check** — быстрый и надёжный (ssh не виснет на нём):
   ```bash
   ssh <server-ip> "cd /opt/tg-business-sorter && ./venv/bin/python -c 'import core.actions, db, handlers.commands; print(\"import OK\")'"
   ```

4. **Smoke-тесты.** ⚠️ НЕ запускай `./venv/bin/python -m unittest tests.test_core | tail` — этот пайп **виснет** по ssh (unittest пишет в stderr, пайп не закрывается). Вместо этого — программный раннер одним python-вызовом:
   ```bash
   ssh <server-ip> "/opt/tg-business-sorter/venv/bin/python -u /tmp/run_smoke.py 2>&1 | tail -2"
   ```
   Если `/tmp/run_smoke.py` нет — создай (`scripts/run_smoke.py` в этом скилле) и `scp`. Ждёшь строку `SMOKE: ran=N failures=0 errors=0 ok=True`.

5. **Eval-гейт (ТОЛЬКО если менялся `core/classifier.py` или `EXTRACT_SYSTEM`/`DONE_*`/`PROFILE` промпты):**
   ```bash
   ssh <server-ip> "cd /opt/tg-business-sorter && ./venv/bin/python scripts/eval_extraction.py 3 2>&1 | tail -12"
   ```
   Должно быть **14/14**. Этот корпус — голый короткий тред, настоящий вход бота он не видит, поэтому при правке извлечения ещё `scripts/eval_real_threads.py 5` (тот же запуск): сверять с базой в его шапке, новых падений быть не должно. Оба стоят денег (Sonnet) — гонять только при правке извлечения/классификации.

6. **Рестарт + подтверждение** (рестарт = действие с подтверждением владельца, но в рамках одобренной задачи деплой его включает):
   ```bash
   ssh <server-ip> "systemctl restart tg-sorter && sleep 4 && systemctl is-active tg-sorter"
   ```
   Затем убедись, что бот реально поднялся (а не только `active`):
   ```bash
   ssh <server-ip> "journalctl -u tg-sorter --since '40 sec ago' --no-pager | grep -iE 'ready|error|traceback' | tail -5"
   ```
   Ждёшь `Bot @Sort38_bot ... ready`.

7. **После деплоя — прогони РЕАЛЬНУЮ команду/путь, который менял**, а не только import. Грабля дня: `/roles` дважды падал (сначала недозалитый `db.py`, потом не-импортнутый `html`), хотя `import` проходил. Если менял хендлер — вызови его на проде (или попроси владельца нажать), либо смоделируй вызов в read-only скрипте.

## Гигиена фоновых ssh
ssh-команды часто уходят в фон (харнес уведомит о завершении). **Жди уведомление, не опрашивай пустой output-файл** повторными чтениями. Особо: `unittest | tail`, длинные `journalctl | grep` — могут зависнуть; гоняй одиночными python-вызовами с `-u`.

## Что НЕ делать
- Не коммить/не пушить в рамках tg-deploy (это делает tg-release). tg-deploy — только сервер.
- Не запускать eval, если извлечение/классификация не менялись (деньги).
