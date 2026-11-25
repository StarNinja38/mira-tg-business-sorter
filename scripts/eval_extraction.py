#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Eval-харнес извлечения задач (v2.9). Регресс-корпус РЕАЛЬНЫХ кейсов из практики:
прогоняет EXTRACT на каждом N раз, голосует большинством, сверяет с ожиданием
(должна быть задача / не должно). Ловит регрессии промпта ДО деплоя — извлечение
LLM-ное и недетерминированное, поэтому majority-of-N, а не один прогон.

Запуск на сервере:  cd /opt/tg-business-sorter && ./venv/bin/python scripts/eval_extraction.py [runs]
Стоит немного денег (N×кейсов вызовов Sonnet) — гонять перед изменением EXTRACT_SYSTEM.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anthropic import AsyncAnthropic
from config import settings
from core.actions import extract_action_items

RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 3

# (название, категория, тред, ожидание: True=должна быть задача / False=не должно)
CASES = [
    ("Неформальная встреча + согласие", "FRIEND",
     "@x: можем завтра встретиться\n@x: и подумать в какую поиграть\nТы: Да", True),
    ("Согласованный созвон без времени", "BUSINESS",
     "@x: давай созвонимся завтра обсудить проект\nТы: давай", True),
    ("Конкретная просьба с дедлайном", "BUSINESS",
     "@x: скинь договор до пятницы пожалуйста\nТы: ок сделаю", True),
    ("Обещание владельца помочь", "FRIEND",
     "@x: поможешь с курсовой?\nТы: да помогу на выходных", True),
    ("Отказ от встречи (несовпадение)", "FRIEND",
     "@x: поехали к 10:20 вместе\nТы: не, я сам раньше поеду", False),
    ("Завершённый смолток", "FRIEND",
     "@x: как сдал экзамен?\nТы: сдал, который сегодняшний\n@x: молодец!", False),
    ("Вопрос задал сам владелец", "FRIEND",
     "Ты: а во сколько у тебя завтра пара?\n@x: в 9", False),
    ("Пересланный мем", "FRIEND",
     "@x: [переслано из канала] смотри какой прикол про котов", False),
    # v4.68: сиюминутное по ходу живой переписки — не задача (26.10 такие засоряли Todoist)
    ("Сиюминутное: заказ еды на сейчас", "FRIEND",
     "@x: я голодная\nТы: щас закажу нам поесть\nТы: через 10 мин привезут\n@x: ура", False),
    # …но дело на завтра среди живой переписки — задача (страховка от перегиба правила)
    ("Дело на завтра в живой переписке", "FRIEND",
     "@x: ты где\nТы: щас выхожу\n@x: не забудь завтра собрать вещи в поездку и документы\nТы: да соберу завтра утром", True),
    # v4.69: просьба владельца к собеседнику — дело собеседника (29–30.10 бот заводил такое на владельца)
    ("Просьба владельца: написать и записать кружок", "FRIEND",
     "Ты: Засыпаю на ходу\nТы: Ты напиши как в отеле будешь\nТы: И кружок запиши\nТы: Спокойной ночи милая ❤️\n@x: Хорошо\n@x: Спокойной ночи милый❤️", False),
    ("Просьба владельца: завтра покажи", "FRIEND",
     "@x: утром творог поела\n@x: что я тебе покажу если всё съела\nТы: Завтра покажи\nТы: Надо кушать нормально\n@x: Хорошо", False),
    # …но если владелец в том же сообщении берёт своё действие — задача (страховка от перегиба)
    ("Просьба + своё действие владельца", "BUSINESS",
     "Ты: скинь договор до пятницы, я подпишу и отправлю обратно\n@x: ок, скину", True),
    ("Просьба собеседника к владельцу остаётся задачей", "FRIEND",
     "@x: завтра скинь мне адрес и напиши когда выезжаешь\nТы: ок", True),
]


async def run_case(client, thread, category):
    tasks = await extract_action_items(client, settings.anthropic_model_smart, "x", category, thread)
    return len(tasks) > 0


async def main():
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    print(f"Eval извлечения: {len(CASES)} кейсов × {RUNS} прогонов\n")
    passed = 0
    for name, cat, thread, expect in CASES:
        votes = []
        for _ in range(RUNS):
            try:
                votes.append(await run_case(client, thread, cat))
            except Exception as exc:
                votes.append(None)
        yes = sum(1 for v in votes if v is True)
        verdict = yes > RUNS / 2  # majority «есть задача»
        ok = (verdict == expect)
        passed += ok
        mark = "✅" if ok else "❌"
        exp = "задача" if expect else "НЕ задача"
        got = "задача" if verdict else "НЕ задача"
        print(f"{mark} {name}: ждём «{exp}», получили «{got}» ({yes}/{RUNS} за задачу)")
    print(f"\nИТОГ: {passed}/{len(CASES)} кейсов прошли")
    return 0 if passed == len(CASES) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
