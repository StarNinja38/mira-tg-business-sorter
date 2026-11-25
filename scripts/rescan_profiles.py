#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Пере-скан профилей контактов по НОВЫМ правилам (полная память v1.8.4 + имя v1.8.3).
Для каждого контакта читает ВСЮ историю переписки и одним вызовом смарт-модели
пересобирает: relationship, intent, topics, summary, name, facts (память), commitments.
Затем апсертит профиль и синкает в Notion Контакты.

Запуск на сервере:
    cd /opt/tg-business-sorter && sudo -u tgsorter ./venv/bin/python scripts/rescan_profiles.py
Опц.: один контакт → добавить user_id аргументом.
"""
import asyncio
import json
import os
import sys

# корень проекта в path (скрипт лежит в scripts/, а config/core — в корне)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiosqlite
from anthropic import AsyncAnthropic

from config import settings
from core.util import loads_loose
from core.vault import PROFILE_SYSTEM, PROFILE_USER, _sync_profile_to_notion
from db import get_contact_profile, upsert_contact_profile

MAX_MSGS = 160          # сколько последних сообщений истории отдаём модели
MAX_CHARS = 9000        # потолок длины истории в промпте


async def history(conn, user_id: int, username: str) -> str:
    rows = await (await conn.execute(
        "SELECT direction, text, created_at FROM conversations "
        "WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (user_id, MAX_MSGS),
    )).fetchall()
    lines = []
    for direction, text, ts in reversed(rows):
        who = "Ты" if direction == "out" else f"@{username}"
        t = (text or "").replace("\n", " ").strip()
        if t:
            lines.append(f"{who}: {t}")
    blob = "\n".join(lines)
    return blob[-MAX_CHARS:] if len(blob) > MAX_CHARS else blob


async def rescan_one(conn, client, profile: dict) -> str:
    user_id = profile["user_id"]
    username = profile.get("username") or f"id{user_id}"
    convo = await history(conn, user_id, username)
    if not convo:
        return f"@{username}: нет истории — пропуск"

    prompt = PROFILE_USER.format(
        username=username,
        current_summary=profile.get("summary") or "Нет данных",
        current_facts=profile.get("facts") or "—",
        current_commitments=profile.get("commitments") or "—",
        conversation=convo,
        category=profile.get("relationship") or "FRIEND",
        text="(полный пере-скан всей истории — собери максимально подробную память)",
    )
    resp = await client.messages.create(
        model=settings.anthropic_model_smart, max_tokens=900,
        system=PROFILE_SYSTEM, messages=[{"role": "user", "content": prompt}],
    )
    data = loads_loose(resp.content[0].text)

    fields = dict(
        relationship=data.get("relationship", "unknown"),
        topics=json.dumps(data.get("topics", []), ensure_ascii=False),
        intent=data.get("intent", ""),
        summary=data.get("summary", ""),
        facts=(data.get("facts") or "")[:1500],
        commitments=(data.get("commitments") or "")[:600],
    )
    if data.get("name"):
        fields["name"] = data["name"]
    await upsert_contact_profile(conn, user_id, username, increment=False, **fields)

    # синк в Notion Контакты
    updated = await get_contact_profile(conn, user_id)
    if updated and settings.notion_token and settings.notion_contacts_id:
        try:
            await _sync_profile_to_notion(
                settings.notion_token, settings.notion_contacts_id,
                user_id, username, updated, (updated or {}).get("notion_contact_id"), conn,
            )
        except Exception as e:
            print(f"   notion sync skip @{username}: {e}")

    nm = fields.get("name") or "—"
    return f"@{username}: {fields['relationship']} · имя={nm} · facts={len(fields['facts'])}c"


async def main():
    only = int(sys.argv[1]) if len(sys.argv) > 1 else None
    conn = await aiosqlite.connect(settings.db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA busy_timeout=5000")
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    q = "SELECT * FROM contact_profiles"
    params = ()
    if only:
        q += " WHERE user_id = ?"; params = (only,)
    rows = await (await conn.execute(q, params)).fetchall()
    profiles = [dict(r) for r in rows]
    print(f"Пере-скан {len(profiles)} контакт(ов) по новым правилам (память v1.8.4)…\n")

    for p in profiles:
        try:
            print("  •", await rescan_one(conn, client, p))
        except Exception as e:
            print(f"  ✗ @{p.get('username')}: {e}")
    await conn.close()
    print("\nГотово.")


if __name__ == "__main__":
    asyncio.run(main())
