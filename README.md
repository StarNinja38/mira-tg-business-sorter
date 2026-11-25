# Mira \ tg-business-sorter — a personal AI assistant on Telegram

[Русская версия](README.ru.md)

Mira is a single-owner AI assistant that runs as a Telegram bot ([@Sort38_bot](https://t.me/Sort38_bot)). It started in June 2026 as a sorter for Telegram Business messages and grew into an assistant I use every day: it reads my business chats, turns what people ask into tasks, remembers who is who, writes reply drafts in my style, and moderates my Twitch and YouTube stream chats.

This repository is a public snapshot of the production code. Real data, the server address, my personal IDs and unfinished parts (voice calls, a private dashboard, Obsidian sync) are removed. The bot itself only answers its owner, so there is no public demo.

## What it does

- **Inbox triage.** Every incoming Telegram Business message goes through cheap regex rules first, then Claude Haiku. The result is SPAM, BUSINESS, FRIEND or UNCLEAR, and each category has its own action.
- **Tasks from conversations.** When a chat goes quiet for 10 minutes, a "settle" pass reads the thread with Claude Sonnet and extracts tasks and promises. They go to Todoist and Google Calendar with deduplication. Later passes close tasks that are done and reopen ones that come back.
- **Contact memory.** For each person Mira keeps structured memory: facts in sections, a timeline, writing style and role. It is merged in diff mode (add or retire entries), indexed with local embeddings and fed into prompts as a relevant slice instead of the whole history.
- **Agent chat.** In a private chat Mira runs a tool-calling loop over tasks, calendar, memory, mail, web search, Twitch, YouTube and more. A catalog of ready-made skills and scheduled routines sits on top.
- **Reply drafts** in my own style, learned from my outgoing messages.
- **Stream tools.** When a stream goes live, Mira prepares an announcement card (rendered with Pillow) and posts it to the channel on one tap. While the stream runs she keeps the post up to date, and at the end she turns it into a "recording" card with links to the VODs. She also moderates Twitch and YouTube chats and answers mentions.
- **Mini app.** A Telegram Web App (`webapp/index.html`) with the day view, people, skills, connections and settings.

## LLM engineering notes

- **Model routing.** Haiku 5.5 handles classification, yes/no judges and moderation; Sonnet 5.5 handles extraction, memory and the agent. Model names come from config.
- **Verdict after reason.** Haiku runs without extended thinking, so every JSON answer puts the reason field before the verdict.
- **Cost control.** A monthly budget guard, rules before LLM calls, and batching of judge calls.
- **Idempotency.** Search-before-create against stable keys and a backstop check by title and day, so retries never create duplicate tasks.
- **Privacy.** Card numbers are masked before anything is stored (`core/redact.py`, Luhn check); optional PII redaction before cloud calls.
- **Evals.** `eval_extraction.py` is a regression set of anonymized cases for task extraction, run before every prompt change. `mira_eval.py` runs 15 dialogue scenarios against the agent with a Haiku judge and a rubric. `eval_done.py` and `eval_executor.py` check the "is it done" and "who does it" judges, and `eval_real_threads.py` replays real threads from the database (read-only; no conversation text is stored in the repo).
- **Built with Claude Code.** `CLAUDE.md` and `.claude/skills/` are the actual working agreements and runbooks (deploy, read-only diagnosis, safe production fixes, release) the coding agent follows on this project.

## Stack

Python 3 (tested on 3.14), aiogram 3, Anthropic SDK, SQLite (WAL) via aiosqlite, aiohttp for the mini app API, fastembed for local embeddings, faster-whisper for voice messages, Pillow for cards. Integrations: Todoist, Google Calendar, Notion (being retired), Twitch, YouTube, Gmail. Runs on a small Ubuntu VPS under systemd.

About **30,000 lines** of Python, **306 unit tests**.

## Layout

```
main.py            entry point: routers and background loops
config.py          settings from .env (pydantic-settings)
db.py              SQLite schema and queries
handlers/          business chats, owner chat, commands, callbacks, groups
core/
  classifier.py    rules + Haiku classification
  actions.py       task extraction, close/reopen judges, Todoist/Calendar
  tasks_loop.py    settle loop (runs when a chat goes quiet)
  contact_memory.py  structured memory per contact, merge, retrieval
  vault.py         profile enrichment and memory updates
  mira.py          agent loop; mira_tools.py — its tools
  skills.py        skill catalog; routines.py — scheduled routines
  stream_report.py, stream_card.py   stream announcement lifecycle
  widgets/         in-chat widgets (day, habits, drafts, meeting, market)
webapp/            Telegram mini app
scripts/           evals, admin CLI, maintenance
docs/              full documentation (in Russian)
tests/test_core.py
```

## Running it

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # BOT_TOKEN, OWNER_USER_ID, ANTHROPIC_API_KEY are required
python main.py            # creates data/bot.db on first start
```

Create the bot with [@BotFather](https://t.me/BotFather) and enable Business Mode, then connect it in Telegram: Settings → Telegram Business → Chatbots. Your user id can be found with [@userinfobot](https://t.me/userinfobot).

Tests:

```bash
BOT_TOKEN=1:x OWNER_USER_ID=1 ANTHROPIC_API_KEY=test python -m unittest tests.test_core
```

The detailed docs in `docs/` are in Russian: architecture, features, commands, integrations, deployment and the development log.
