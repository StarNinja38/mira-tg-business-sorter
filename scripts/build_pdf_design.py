#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Премиум-PDF документации TG-SORTER — ТЁМНЫЙ ТЕХНО стиль (Linear/Vercel),
акцент Telegram-blue #2AABEE. docs/*.md → HTML → PDF через WeasyPrint.

«Claude design»-вариант: тёмный фон, радиальное свечение на обложке, акцентные
номера глав, тёмные панели кода/таблиц, цветные callout'ы. Светлый генератор —
build_pdf_web.py (fallback). Оба вшивают шрифты (системные семейства, кириллица ок).

Запуск: DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib ~/.tgsorter/pdfvenv/bin/python \\
        scripts/build_pdf_design.py [version]
"""
import os
import re
import sys
import glob
from datetime import date

import markdown

DOCS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs")
OUT_DIR = DOCS
VERSION = sys.argv[1] if len(sys.argv) > 1 else "v2.8"
TODAY = date.today().strftime("%d.%m.%Y")

ACCENT = "#2AABEE"   # Telegram blue
ACCENT2 = "#5DC0F5"  # lighter for gradients/glow

CHAPTERS = [
    ("01 — Обзор.md",                 "Обзор",            "Зачем нужен бот и как он работает"),
    ("02 — Архитектура.md",           "Архитектура",      "Из чего собран и как устроен внутри"),
    ("03 — Функции.md",               "Функции",          "Что бот умеет — по подсистемам"),
    ("04 — Команды.md",               "Команды",          "Управление ботом из Telegram"),
    ("05 — Интеграции.md",            "Интеграции",       "Todoist · Notion · Calendar · Vault"),
    ("06 — Деплой и обслуживание.md", "Деплой",           "Развёртывание и сопровождение"),
    ("07 — Решение проблем.md",       "Решение проблем",  "Диагностика и типовые неполадки"),
    ("08 — Журнал разработки.md",     "Журнал",           "Хронология версий и решений"),
]

CALLOUT_ICON = {"info": "ℹ️", "tip": "💡", "warning": "⚠️", "note": "📝",
                "danger": "🛑", "important": "❗", "success": "✅", "question": "❓"}


def strip_frontmatter(md: str) -> str:
    if md.startswith("---"):
        end = md.find("\n---", 3)
        if end != -1:
            return md[end + 4:].lstrip("\n")
    return md


def style_callouts(html: str) -> str:
    def repl(m):
        body = m.group(1)
        cm = re.match(r"\s*<p>\s*\[!(\w+)\]\s*(.*?)(</p>|<br\s*/?>)", body, re.IGNORECASE | re.DOTALL)
        if not cm:
            return m.group(0)
        ctype = cm.group(1).lower()
        title = cm.group(2).strip()
        icon = CALLOUT_ICON.get(ctype, "💬")
        rest = body[cm.end():]
        head = f'<div class="callout-title">{icon} {title}</div>'
        inner = f"<p>{rest}" if rest.strip() and not rest.lstrip().startswith("<p>") else rest
        return f'<div class="callout callout-{ctype}">{head}{inner}</div>'
    return re.sub(r"<blockquote>(.*?)</blockquote>", repl, html, flags=re.DOTALL)


def render_md(md: str) -> str:
    html = markdown.markdown(strip_frontmatter(md),
                             extensions=["tables", "fenced_code", "sane_lists"])
    html = re.sub(r"^\s*<h1>.*?</h1>", "", html, count=1, flags=re.DOTALL)
    return style_callouts(html)


CSS = """
@page {
  size: A4; margin: 16mm 14mm 18mm 14mm; background: #0D1117;
  @bottom-left  { content: "TG-SORTER · __VER__"; font-family: Arial; font-size: 8pt; color: #8B949E; }
  @bottom-right { content: counter(page); font-family: "Menlo","Courier New",monospace; font-size: 8.5pt; color: #2AABEE; }
}
@page cover { margin: 0; background: #0D1117; @bottom-left { content: none; } @bottom-right { content: none; } }

* { box-sizing: border-box; }
body { font-family: Arial, sans-serif; color: #C9D1D9; font-size: 10pt; line-height: 1.62; margin: 0;
  background: #0D1117; }

/* ===== COVER ===== */
.cover { page: cover; width: 210mm; height: 296mm; position: relative; page-break-after: always;
  background:
    radial-gradient(circle at 28% 32%, rgba(42,171,238,0.34) 0%, rgba(42,171,238,0.0) 42%),
    radial-gradient(circle at 82% 78%, rgba(93,192,245,0.20) 0%, rgba(93,192,245,0.0) 45%),
    #0D1117;
  color: #E6EDF3; }
.cover-inner { position: absolute; left: 24mm; top: 88mm; right: 22mm; }
.cover .eyebrow { font-family: "Menlo","Courier New",monospace; font-size: 10.5pt; letter-spacing: 4px;
  text-transform: uppercase; color: #2AABEE; font-weight: bold; }
.cover h1 { font-size: 42pt; line-height: 1.04; margin: 16px 0 12px; font-weight: bold; color: #fff;
  letter-spacing: -0.5px; }
.cover h1 .accent { color: #2AABEE; }
.cover .sub { font-size: 13pt; color: #8B949E; line-height: 1.5; max-width: 150mm; }
.cover .badges { margin-top: 26px; }
.badge { display: inline-block; background: rgba(42,171,238,0.12); border: 1px solid #2AABEE;
  border-radius: 7px; padding: 7px 17px; font-size: 10.5pt; font-weight: bold; margin-right: 10px;
  color: #5DC0F5; font-family: "Menlo","Courier New",monospace; }
.cover .rule { margin-top: 30px; width: 64mm; height: 3px;
  background: linear-gradient(90deg, #2AABEE 0%, rgba(42,171,238,0) 100%); }
.cover .foot { position: absolute; left: 24mm; bottom: 22mm; right: 24mm; font-size: 9pt; color: #6E7681;
  font-family: "Menlo","Courier New",monospace; border-top: 1px solid #21262D; padding-top: 10px; }

/* ===== TOC ===== */
.toc { page-break-after: always; padding-top: 6mm; }
.toc h2 { font-size: 22pt; color: #fff; margin: 0 0 8mm; font-weight: bold; }
.toc h2 .accent { color: #2AABEE; }
table.toct { width: 100%; border-collapse: collapse; }
table.toct td { border-bottom: 1px solid #21262D; padding: 11px 6px; vertical-align: middle; }
.toc-num { width: 14mm; }
.toc-num span { display: inline-block; width: 11mm; height: 11mm; border-radius: 8px;
  background: linear-gradient(160deg, #2AABEE, #1B8FD0); color: #fff;
  font-weight: bold; font-size: 12pt; text-align: center; line-height: 11mm;
  font-family: "Menlo","Courier New",monospace; }
.toc-title { font-weight: bold; color: #E6EDF3; font-size: 12.5pt; }
.toc-sub { color: #8B949E; font-size: 9.5pt; }

/* ===== CHAPTER ===== */
.chapter { page-break-before: always; }
table.chhead { width: 100%; border-collapse: collapse; margin-bottom: 7mm;
  border-bottom: 1px solid #21262D; }
table.chhead td { vertical-align: middle; padding-bottom: 5mm; border: none; }
.ch-num { width: 19mm; }
.ch-num span { display: inline-block; width: 15mm; height: 15mm; border-radius: 11px;
  background: linear-gradient(160deg, #2AABEE, #1B8FD0); color: #fff;
  font-weight: bold; font-size: 19pt; text-align: center; line-height: 15mm;
  font-family: "Menlo","Courier New",monospace; }
.ch-title { font-size: 24pt; font-weight: bold; color: #fff; letter-spacing: -0.3px; }
.ch-sub { color: #8B949E; font-size: 10.5pt; }

.body h1 { font-size: 16pt; color: #fff; margin: 8mm 0 3mm; }
.body h2 { font-size: 14.5pt; color: #E6EDF3; margin: 8mm 0 2.5mm; padding-left: 11px;
  border-left: 3px solid #2AABEE; }
.body h3 { font-size: 11.5pt; color: #5DC0F5; margin: 5mm 0 2mm; }
.body p { margin: 0 0 2.6mm; }
.body ul, .body ol { margin: 0 0 2.6mm; padding-left: 20px; }
.body li { margin: 1mm 0; }
.body li::marker { color: #2AABEE; }
strong { color: #fff; }
a { color: #2AABEE; text-decoration: none; }

code { font-family: "Menlo","Courier New",monospace; font-size: 8.4pt; background: #161B22; color: #5DC0F5;
  padding: 1px 5px; border-radius: 5px; border: 1px solid #21262D; }
pre { font-family: "Menlo","Courier New",monospace; background: #161B22; color: #C9D1D9;
  border: 1px solid #21262D; border-left: 3px solid #2AABEE; border-radius: 9px; padding: 12px 14px;
  font-size: 8pt; line-height: 1.55; margin: 3.5mm 0; white-space: pre-wrap; word-wrap: break-word; }
pre code { background: none; color: #C9D1D9; padding: 0; border: none; font-size: 8pt; }

table { border-collapse: collapse; width: 100%; margin: 3.5mm 0; font-size: 8.8pt;
  border: 1px solid #21262D; border-radius: 8px; overflow: hidden; }
th { background: linear-gradient(160deg, #2AABEE, #1B8FD0); color: #fff; text-align: left;
  padding: 7px 10px; font-size: 8.6pt; border: none; }
td { padding: 6px 10px; border-top: 1px solid #21262D; color: #C9D1D9; vertical-align: top; word-wrap: break-word; }
tbody tr:nth-child(odd) td { background: #131820; }
tbody tr:nth-child(even) td { background: #0F141B; }

blockquote { margin: 3mm 0; padding: 8px 14px; border-left: 3px solid #6E7681; background: #131820;
  color: #8B949E; border-radius: 0 6px 6px 0; }
.callout { margin: 3.5mm 0; padding: 10px 14px; border-radius: 8px; border: 1px solid #21262D;
  border-left-width: 3px; background: #131820; }
.callout-title { font-weight: bold; margin-bottom: 2px; font-size: 9.7pt; color: #E6EDF3; }
.callout p { margin: 1.3mm 0 0; color: #C9D1D9; }
.callout-info, .callout-note    { border-left-color: #2AABEE; }
.callout-tip, .callout-success  { border-left-color: #2EA043; }
.callout-warning, .callout-important { border-left-color: #D29922; }
.callout-danger { border-left-color: #F85149; }
.callout-question { border-left-color: #A371F7; }
hr { border: none; border-top: 1px solid #21262D; margin: 5mm 0; }
""".replace("__VER__", VERSION)


def build_html() -> str:
    toc_rows, chapters = [], []
    for i, (fname, title, sub) in enumerate(CHAPTERS, 1):
        path = os.path.join(DOCS, fname)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            body = render_md(f.read())
        num = f"{i:02d}"
        toc_rows.append(
            f'<tr><td class="toc-num"><span>{num}</span></td>'
            f'<td><div class="toc-title">{title}</div><div class="toc-sub">{sub}</div></td></tr>')
        chapters.append(
            f'<section class="chapter">'
            f'<table class="chhead"><tr><td class="ch-num"><span>{num}</span></td>'
            f'<td><div class="ch-title">{title}</div><div class="ch-sub">{sub}</div></td></tr></table>'
            f'<div class="body">{body}</div></section>')
    cover = f"""<div class="cover"><div class="cover-inner">
<div class="eyebrow">Telegram Business Sorter</div>
<h1>Документация<br>бота <span class="accent">@Sort38_bot</span></h1>
<div class="sub">Сортировщик входящих · CRM · личный ассистент. Классификация, задачи, память,
умные самообучающиеся ответы, топики и rich-вёрстка Bot API 10.1.</div>
<div class="badges"><span class="badge">{VERSION}</span><span class="badge">{TODAY}</span></div>
<div class="rule"></div>
</div><div class="foot">Python · aiogram 3.28 (Bot API 10.0) · Claude (Haiku + Sonnet) · Whisper · RAG · Obsidian</div></div>"""
    toc = f'<div class="toc"><h2>Со<span class="accent">держание</span></h2><table class="toct">{"".join(toc_rows)}</table></div>'
    return (f"<!doctype html><html lang=ru><head><meta charset=utf-8></head>"
            f"<body>{cover}{toc}{''.join(chapters)}</body></html>")


def main() -> int:
    from weasyprint import HTML, CSS as WCSS
    # Тёмный техно — КАНОНИЧЕСКИЙ стиль документации (основное имя, чистит старые).
    out_pdf = os.path.join(OUT_DIR, f"TG-Sorter-Документация-{VERSION}.pdf")
    HTML(string=build_html(), base_url=DOCS).write_pdf(out_pdf, stylesheets=[WCSS(string=CSS)])
    for old in glob.glob(os.path.join(OUT_DIR, "TG-Sorter-Документация-*.pdf")):
        if os.path.abspath(old) != os.path.abspath(out_pdf):
            os.remove(old)
            print(f"removed old: {os.path.basename(old)}")
    print(f"OK → {out_pdf}\nsize: {os.path.getsize(out_pdf)//1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
