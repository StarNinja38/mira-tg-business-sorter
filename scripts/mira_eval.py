"""
v4.44 Ф5: eval Мира-чата — 15 диалоговых сценариев из плана, dry-run (без карточек и записей),
Haiku-судья по рубрике + проверка ожидаемых инструментов. Гейт для правок MIRA_SYSTEM.
Запуск на проде:  cd /opt/tg-business-sorter && .venv/bin/python scripts/mira_eval.py [--verbose]
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, "/opt/tg-business-sorter")
sys.path.insert(0, ".")
if os.environ.get("MIRA_CODE"):   # проверяемая копия кода раньше прода и cwd, иначе сравнение «старая/новая» гоняет одно и то же
    sys.path.insert(0, os.environ["MIRA_CODE"])

SCENARIOS = [
    # (сообщение, ожидается ЛЮБОЙ из инструментов (пусто = не важно), запрещённые, критерий). {C1}/{C2} — живые контакты из БД.
    ("где я работаю?", set(), {"create_task", "propose_task"}, "ответ по памяти владельца (работа/поиск работы); свежие датированные факты важнее старых"),
    ("что у меня с {C1}?", set(), {"create_task"}, "по памяти/переписке с {C1}"),
    ("а завтра?", set(), set(), "понимает как продолжение (план на завтра / уточнение предыдущей темы)"),
    ("я не устроился, завтра собеседование в Альфа-Банк в 11", {"remember"}, set(), "поддерживает по-человечески, запоминает факт; собеседование либо предлагает записать, либо переносит уже существующую задачу на 11:00"),
    ("напомни в 18 позвонить маме", {"create_task"}, {"propose_task"}, "создаёт задачу на ближайшее 18:00 (сегодня; если 18:00 уже прошло — завтра)"),
    ("напоминай пить воду каждые 2 часа", {"routine"}, {"create_task", "propose_task"}, "ставит регулярное напоминание, а не задачу"),
    ("каждое утро смотри мой календарь и планируй день", {"routine"}, {"create_task"}, "ставит поручение на каждое утро"),
    ("перенеси урок английского на четверг", {"reschedule_task"}, set(), "перенос задачи про урок; если задачи нет — сказать об этом"),
    ("что говорил {C1} про деньги?", {"search_memory"}, {"create_task"}, "ищет в переписке; цитаты или честное «не нашла»"),
    ("когда я свободен на час в среду?", {"find_slots"}, set(), "перечисляет СВОБОДНЫЕ окна среды (не занятые)"),
    ("что ответить {C2} на «ты как, жив?»", {"draft_message"}, set(), "черновик ответа {C2} в стиле владельца, отправка по кнопке"),
    ("{C1} переехала к маме", {"remember"}, {"create_task", "propose_task"}, "запоминает факт о контакте {C1}"),
    ("у {C2} др 12 мая", {"set_birthday"}, set(), "подтверждает, что запомнила др"),
    ("напиши {C2}, что задержусь на 15 минут", {"draft_message"}, set(), "черновик {C2}, отправка по кнопке"),
    ("забей", set(), {"create_task", "propose_task", "remember"}, "просто подтверждает отказ, ничего не создаёт"),
    ("сегодня надо забрать посылку и вечером созвон с {C2} в 20", {"propose_task", "create_task"}, set(), "видит ДВА дела, предлагает/записывает оба"),
    ("найди грузинский ресторан рядом с Курской", {"browse", "web_search"}, {"create_task", "propose_task", "web_form"}, "ищет через браузер или поиск; рестораны без данных не выдумывает (в dry-run результатов нет, честно говорит, что поищет)"),
    ("что по ссылке https://example.com/menu ?", {"browse"}, {"create_task", "propose_task"}, "открывает ссылку браузером, не пересказывает её содержимое без данных"),
    ("договор", set(), {"search_memory", "todoist", "create_task", "propose_task", "remember"}, "короткая реплика: отвечает 1–2 предложениями (согласие или переспрос, что найти), не ищет в переписке и задачах"),
    ("ты кто и что умеешь?", set(), {"create_task", "propose_task", "remember"}, "представляется Мирой (женский род), кратко (≤6 предложений) перечисляет умения"),
]

JUDGE = """Ты — судья качества ответа ассистентки Миры. Дано: ПОЛНАЯ картина мира, которую видела Мира (память, расписание пар, задачи, календарь, цитаты переписки, память диалога), сообщение владельца, ответ, вызванные инструменты и что они вернули, критерий.
Правила: всё, что есть в картине мира или в результатах инструментов, — факт, а не выдумка (проверяй по обоим текстам, прежде чем писать «выдумала»); инструменты не обязательны, если данные уже в картине; эмодзи и обращение «босс» — норма (по имени/«Миш» — ошибка стиля); если названное время сегодня уже прошло, «завтра» — верно; одна короткая инициатива в конце («кстати, …») — разрешена; в dry-run черновики и карточки не показываются, фраза «черновик готов/покажу карточку» — норма.
Провал = не выполнен критерий, или явная выдумка ВНЕ картины мира, или грубый стиль (мужской род о себе, >8 предложений, markdown-разметка).
Ответь ТОЛЬКО JSON одной строкой, сначала причина, потом вердикт: {"why": "до 20 слов", "pass": true|false}"""


async def main():
    verbose = "--verbose" in sys.argv
    import aiosqlite
    from anthropic import AsyncAnthropic
    from config import settings
    from core import mira, llm
    from core.util import loads_loose
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    db = await aiosqlite.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    db.row_factory = aiosqlite.Row
    passed = 0
    # живые контакты: два самых активных с русским/любым именем — чтобы сценарии не зависели от выдуманных имён
    rows = await (await db.execute("SELECT name, username FROM contact_profiles WHERE user_id != ? AND user_id != 0 "
                                   "AND COALESCE(name,'') != '' AND COALESCE(relationship,'') NOT IN ('спамер') "
                                   "ORDER BY message_count DESC LIMIT 2", (settings.owner_user_id,))).fetchall()
    names = [r[0].split()[0] for r in rows] + ["Ника", "Ваня"]
    C1, C2 = names[0], names[1]
    print(f"контакты для сценариев: C1={C1} C2={C2}")
    print(f"{'#':>2} {'ok':2} {'tools':<34} сообщение")
    lat = []
    for i, (msg, want, forbid, crit) in enumerate(SCENARIOS, 1):
        msg = msg.replace("{C1}", C1).replace("{C2}", C2); crit = crit.replace("{C1}", C1).replace("{C2}", C2)
        try:
            ctx = await mira.assistant_context(db, settings, msg)
            _t0 = time.monotonic()
            trace = []
            reply, used, _ = await mira.think(client, settings, ctx, msg, db=db, dry=True, trace=trace)
            lat.append(time.monotonic() - _t0)
            used_s = set(used)
            tool_ok = (not want or bool(want & used_s)) and not (forbid & used_s)
            j = await llm.create(client, model=os.environ.get("JUDGE_MODEL") or settings.anthropic_model_smart, max_tokens=300, system=JUDGE,   # судья фиксируется отдельно — сравнение моделей честнее
                                 messages=[{"role": "user", "content": f"КАРТИНА МИРА:\n{ctx[:9000]}\n\nРЕЗУЛЬТАТЫ ИНСТРУМЕНТОВ:\n"
                                            + ("\n\n".join(f"[{n}]\n{o[:2500]}" for n, o in trace) or "-")
                                            + f"\n\nСообщение: {msg}\nОтвет Миры: {reply}\nИнструменты: {used or '-'}\nКритерий: {crit}"}])
            jraw = llm.text_of(j)
            try:
                jd = loads_loose(jraw) or {}
            except Exception:
                jd = {"pass": '"pass": true' in jraw, "why": jraw[:120]}
            ok = bool(tool_ok and jd.get("pass"))
            passed += ok
            print(f"{i:>2} {'✅' if ok else '❌'} {','.join(used) or '-':<34} {msg}")
            if verbose or not ok:
                print(f"     → {reply[:300]}\n     судья: {jd.get('why', '?')}{'' if tool_ok else f' | tools: want {sorted(want)} forbid {sorted(forbid)}'}")
        except Exception as exc:
            print(f"{i:>2} 💥 {type(exc).__name__}: {exc} — {msg}")
    print(f"\nИТОГ: {passed}/{len(SCENARIOS)}  модель={settings.anthropic_model_smart}  "
          f"ответ Миры: медиана {sorted(lat)[len(lat)//2]:.1f} с, макс {max(lat):.1f} с" if lat else f"\nИТОГ: {passed}/{len(SCENARIOS)}")
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
