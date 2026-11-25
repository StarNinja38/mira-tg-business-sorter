#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
admin_ops.py — закрытый набор админ-операций бота для запуска по ssh.

РАЗДЕЛЕНИЕ (важно для прав доступа Claude Code):
  ЧТЕНИЕ  (read / health / logs)              — безопасно, разрешено без подтверждения владельца.
  ПРАВКИ  (setname / addfact / fixrole / contact_memory) — МЕНЯЮТ прод-БД → запускать только с Yes владельца
            (их НЕ добавляют в allow-rules; запускать как: sudo -u tgsorter … чтобы БД осталась за tgsorter).

Запуск (read-форма, разрешённая):
  /opt/tg-business-sorter/venv/bin/python /opt/tg-business-sorter/scripts/admin_ops.py read @user
"""
import os
import sys
import asyncio
import subprocess

os.chdir("/opt/tg-business-sorter")
sys.path.insert(0, "/opt/tg-business-sorter")

import aiosqlite  # noqa: E402
from config import settings  # noqa: E402

DB = settings.db_path


def _usage():
    print("commands:")
    print("  read <@user|id>            — показать память (чтение)")
    print("  health                     — статус сервиса + ошибки за 24ч")
    print("  logs [N]                   — последние N строк журнала")
    print("  setname <@user> <Имя>      — [ПРАВКА] исправить имя")
    print("  addfact <@user> <текст>    — [ПРАВКА] дописать факт")
    print("  fixrole <@user> <роль>     — [ПРАВКА] сменить тип/роль")
    print("  contact_memory                    — [ПРАВКА] пересобрать /me владельца")


async def _profile(db, key):
    from db import get_contact_by_username, get_contact_profile
    if str(key).lstrip("-").isdigit():
        return await get_contact_profile(db, int(key))
    return await get_contact_by_username(db, str(key).lstrip("@"))


# ── ЧТЕНИЕ ──────────────────────────────────────────────────────────────────
async def cmd_read(args):
    if not args:
        print("нужен @user или id"); return
    db = await aiosqlite.connect(f"file:{DB}?mode=ro", uri=True)  # read-only
    db.row_factory = aiosqlite.Row
    p = await _profile(db, args[0])
    if not p:
        print("нет такого контакта")
    else:
        p = dict(p)
        for k in ("user_id", "username", "name", "relationship", "message_count", "photo_file_id"):
            print(f"{k}: {p.get(k)}")
        print("summary:", p.get("summary"))
        print("facts:", p.get("facts"))
    await db.close()


def cmd_health(args):
    act = subprocess.run(["systemctl", "is-active", "tg-sorter"], capture_output=True, text=True).stdout.strip()
    err = subprocess.run(
        "journalctl -u tg-sorter --since '24 hours ago' --no-pager | grep -ciE 'ERROR|CRITICAL|Traceback' || true",
        shell=True, capture_output=True, text=True).stdout.strip()
    print("service:", act)
    print("errors_24h:", err)


def cmd_logs(args):
    n = args[0] if args else "20"
    out = subprocess.run(["journalctl", "-u", "tg-sorter", "-n", str(n), "--no-pager"],
                         capture_output=True, text=True).stdout
    print(out)


# ── ПРАВКИ (требуют подтверждения владельца) ────────────────────────────────
async def _save(db, uid, uname, **fields):
    from db import upsert_contact_profile, get_contact_profile
    from core.vault import _sync_profile_to_notion
    cur = dict(await get_contact_profile(db, uid) or {})
    await upsert_contact_profile(db, uid, uname or cur.get("username") or "",
                                 increment=False, last_contact=cur.get("last_contact"), **fields)
    new = dict(await get_contact_profile(db, uid))
    if settings.notion_token and settings.notion_contacts_id:
        try:
            await _sync_profile_to_notion(settings.notion_token, settings.notion_contacts_id,
                                          uid, new.get("username") or "", new,
                                          new.get("notion_contact_id"), db)
            print("notion: ok")
        except Exception as e:
            print("notion fail:", e)


async def cmd_setname(args):
    db = await aiosqlite.connect(DB); db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA busy_timeout=5000")
    p = await _profile(db, args[0])
    if not p:
        print("нет контакта"); return
    p = dict(p); name = " ".join(args[1:]).splitlines()[0].strip()
    if not name:
        print("нужно имя"); return
    await _save(db, p["user_id"], p.get("username"), name=name)
    print("name →", name); await db.close()


async def cmd_addfact(args):
    db = await aiosqlite.connect(DB); db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA busy_timeout=5000")
    p = await _profile(db, args[0])
    if not p:
        print("нет контакта"); return
    p = dict(p); fact = " ".join(args[1:]).splitlines()[0].strip()
    if not fact:
        print("нужен текст"); return
    ex = (p.get("facts") or "").strip()
    merged = ((ex + "; " + fact) if ex else fact)[:1600]
    await _save(db, p["user_id"], p.get("username"), facts=merged)
    print("факт добавлен"); await db.close()


async def cmd_fixrole(args):
    db = await aiosqlite.connect(DB); db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA busy_timeout=5000")
    p = await _profile(db, args[0])
    if not p:
        print("нет контакта"); return
    p = dict(p); rel = (args[1] if len(args) > 1 else "").strip()
    if not rel:
        print("нужна роль"); return
    await _save(db, p["user_id"], p.get("username"), relationship=rel)
    print("роль →", rel); await db.close()


async def cmd_contact_memory(args):
    from anthropic import AsyncAnthropic
    from core.vault import enrich_owner_profile
    db = await aiosqlite.connect(DB); db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA busy_timeout=5000")
    cli = AsyncAnthropic(api_key=settings.anthropic_api_key)
    ok = await enrich_owner_profile(cli, settings.anthropic_model_smart, db)
    print("contact_memory rebuilt:", ok); await db.close()


async def cmd_avatars(args):
    """[ПРАВКА] Сверить аватары с Telegram: поставить новым, обновить изменившиеся.
    Ручные (/setphoto) и подтверждённые фото не трогает."""
    from aiogram import Bot
    from core.photos import refresh_all_avatars
    db = await aiosqlite.connect(DB); db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA busy_timeout=5000")
    bot = Bot(settings.bot_token)
    res = await refresh_all_avatars(bot, db, settings)
    await bot.session.close(); await db.close()
    print(f"avatars: новых {res['new']}, обновлено {res['changed']}, без изменений {res['unchanged']}, "
          f"ручных/подтв {res['manual']}, ошибок {res['err']}")


def main():
    if len(sys.argv) < 2:
        _usage(); return
    cmd, args = sys.argv[1], sys.argv[2:]
    table = {
        "read": lambda: asyncio.run(cmd_read(args)),
        "health": lambda: cmd_health(args),
        "logs": lambda: cmd_logs(args),
        "setname": lambda: asyncio.run(cmd_setname(args)),
        "addfact": lambda: asyncio.run(cmd_addfact(args)),
        "fixrole": lambda: asyncio.run(cmd_fixrole(args)),
        "contact_memory": lambda: asyncio.run(cmd_contact_memory(args)),
        "avatars": lambda: asyncio.run(cmd_avatars(args)),
    }
    fn = table.get(cmd)
    if fn:
        fn()
    else:
        _usage()


if __name__ == "__main__":
    main()
