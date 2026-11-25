#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Премиум-PDF документации TG-SORTER: docs/*.md → HTML → PDF через WeasyPrint.
Почему weasyprint (а не reportlab/Chrome): полное вшивание шрифтов через @font-face
(в reportlab-PDF утекал не-вшитый Type1 Helvetica → «съезжание» в мобильных
просмотрщиках), а Chrome headless падал «Printing failed» на полной вёрстке.

Запуск:  /usr/bin/python3 scripts/build_pdf_web.py [version]
Зависимости: brew install pango; /usr/bin/python3 -m pip install --user weasyprint markdown
"""
import os
import re
import sys
from datetime import date

import markdown

DOCS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs")
OUT_DIR = DOCS
VERSION = sys.argv[1] if len(sys.argv) > 1 else "v2.4"
TODAY = date.today().strftime("%d.%m.%Y")

# Шрифты: системные семейства через fontconfig (fc-list их видит, кириллица полная,
# weasyprint вшивает сабсеты сам). @font-face с file:// на macOS ломал кириллицу
# (pango не подхватывал и фолбэчил её на широкий шрифт → «разреженные» буквы).

CHAPTERS = [
    ("01 — Обзор.md",                 "Обзор",            "Зачем нужен бот и как он работает",   "#2563EB"),
    ("02 — Архитектура.md",           "Архитектура",      "Из чего собран и как устроен внутри", "#7C3AED"),
    ("03 — Функции.md",               "Функции",          "Что бот умеет — по подсистемам",      "#0891B2"),
    ("04 — Команды.md",               "Команды",          "Управление ботом из Telegram",        "#0EA5E9"),
    ("05 — Интеграции.md",            "Интеграции",       "Todoist · Notion · Calendar · Vault", "#16A34A"),
    ("06 — Деплой и обслуживание.md", "Деплой",           "Развёртывание и сопровождение",       "#D97706"),
    ("07 — Решение проблем.md",       "Решение проблем",  "Диагностика и типовые неполадки",     "#DC2626"),
    ("08 — Журнал разработки.md",     "Журнал",           "Хронология версий и решений",         "#475569"),
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
    """Obsidian `> [!type] Title` → <div class=callout>."""
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
    html = re.sub(r"^\s*<h1>.*?</h1>", "", html, count=1, flags=re.DOTALL)  # шапка главы уже есть
    return style_callouts(html)


CSS = """
@page {
  size: A4; margin: 16mm 14mm 18mm 14mm;
  @bottom-left  { content: "TG-SORTER · Документация __VER__"; font-family: Arial; font-size: 8pt; color: #94A3B8; }
  @bottom-right { content: counter(page); font-family: Arial; font-size: 8pt; color: #94A3B8; }
}
@page cover { margin: 0; @bottom-left { content: none; } @bottom-right { content: none; } }

* { box-sizing: border-box; }
body { font-family: Arial, sans-serif; color: #1E293B; font-size: 10pt; line-height: 1.6; margin: 0; }

/* COVER */
.cover { page: cover; width: 210mm; height: 296mm;
  background: linear-gradient(135deg, #1E3A8A 0%, #2563EB 45%, #0EA5E9 80%, #06B6D4 100%);
  color: #fff; page-break-after: always; position: relative; }
.cover-inner { position: absolute; left: 24mm; top: 95mm; right: 24mm; }
.cover .eyebrow { font-size: 11pt; letter-spacing: 6px; text-transform: uppercase; opacity: .85; font-weight: bold; }
.cover h1 { font-size: 40pt; line-height: 1.05; margin: 14px 0 10px; font-weight: bold; }
.cover .sub { font-size: 13.5pt; opacity: .94; line-height: 1.45; max-width: 150mm; }
.cover .badges { margin-top: 24px; }
.badge { display: inline-block; background: rgba(255,255,255,.18); border: 1px solid rgba(255,255,255,.4);
  border-radius: 999px; padding: 6px 16px; font-size: 10.5pt; font-weight: bold; margin-right: 10px; }
.cover .foot { position: absolute; left: 24mm; bottom: 22mm; font-size: 9.5pt; opacity: .8; }

/* TOC */
.toc { page-break-after: always; }
.toc h2 { font-size: 21pt; color: #0F172A; margin: 4mm 0 6mm; }
table.toct { width: 100%; border-collapse: collapse; }
table.toct td { border-bottom: 1px solid #E2E8F0; padding: 10px 6px; vertical-align: middle; }
.toc-num { width: 13mm; }
.toc-num span { display: inline-block; width: 11mm; height: 11mm; border-radius: 4mm; color: #fff;
  font-weight: bold; font-size: 12pt; text-align: center; line-height: 11mm; }
.toc-title { font-weight: bold; color: #0F172A; font-size: 12pt; }
.toc-sub { color: #64748B; font-size: 9.5pt; }

/* CHAPTER */
.chapter { page-break-before: always; }
table.chhead { width: 100%; border-collapse: collapse; margin-bottom: 6mm;
  border-bottom: 2.5px solid #E2E8F0; }
table.chhead td { vertical-align: middle; padding-bottom: 5mm; border: none; }
.ch-num { width: 18mm; }
.ch-num span { display: inline-block; width: 15mm; height: 15mm; border-radius: 5mm; color: #fff;
  font-weight: bold; font-size: 20pt; text-align: center; line-height: 15mm; }
.ch-title { font-size: 23pt; font-weight: bold; color: #0F172A; }
.ch-sub { color: #64748B; font-size: 10.5pt; }

.body h1 { font-size: 16pt; color: #0F172A; margin: 8mm 0 3mm; }
.body h2 { font-size: 14.5pt; color: #0F172A; margin: 8mm 0 2.5mm; padding-left: 10px;
  border-left: 4px solid var(--accent, #2563EB); }
.body h3 { font-size: 11.5pt; color: #1E293B; margin: 5mm 0 2mm; }
.body p { margin: 0 0 2.6mm; }
.body ul, .body ol { margin: 0 0 2.6mm; padding-left: 20px; }
.body li { margin: 1mm 0; }
strong { color: #0F172A; }
a { color: #2563EB; text-decoration: none; }

code { font-family: "Courier New", monospace; font-size: 8.4pt; background: #EEF2F7; color: #0B3B8F;
  padding: 0.5px 4px; border-radius: 4px; }
pre { font-family: "Courier New", monospace; background: #0B1220; color: #E5EAF2; border-radius: 9px; padding: 11px 13px;
  font-size: 8pt; line-height: 1.5; margin: 3mm 0; white-space: pre-wrap; word-wrap: break-word; }
pre code { background: none; color: #E5EAF2; padding: 0; font-size: 8pt; }

table { border-collapse: collapse; width: 100%; margin: 3mm 0; font-size: 8.8pt; }
th { background: var(--accent, #2563EB); color: #fff; text-align: left; padding: 6px 9px;
  font-size: 8.6pt; border: 1px solid var(--accent, #2563EB); }
td { padding: 5.5px 9px; border: 1px solid #E2E8F0; vertical-align: top; word-wrap: break-word; }
tbody tr:nth-child(even) td { background: #F8FAFC; }

blockquote { margin: 3mm 0; padding: 7px 14px; border-left: 4px solid #94A3B8; background: #F8FAFC;
  color: #475569; }
.callout { margin: 3mm 0; padding: 9px 13px; border-radius: 8px; border: 1px solid; }
.callout-title { font-weight: bold; margin-bottom: 2px; font-size: 9.6pt; }
.callout p { margin: 1.3mm 0 0; }
.callout-info, .callout-note { background: #EFF6FF; border-color: #BFDBFE; color: #1E3A8A; }
.callout-tip, .callout-success { background: #ECFDF5; border-color: #A7F3D0; color: #065F46; }
.callout-warning, .callout-important { background: #FFFBEB; border-color: #FDE68A; color: #92400E; }
.callout-danger { background: #FEF2F2; border-color: #FECACA; color: #991B1B; }
.callout-question { background: #F5F3FF; border-color: #DDD6FE; color: #5B21B6; }
hr { border: none; border-top: 1px solid #E2E8F0; margin: 5mm 0; }
""".replace("__VER__", VERSION)


def build_html() -> str:
    toc_rows, chapters = [], []
    for i, (fname, title, sub, accent) in enumerate(CHAPTERS, 1):
        path = os.path.join(DOCS, fname)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            body = render_md(f.read())
        num = f"{i:02d}"
        toc_rows.append(
            f'<tr><td class="toc-num"><span style="background:{accent}">{num}</span></td>'
            f'<td><div class="toc-title">{title}</div><div class="toc-sub">{sub}</div></td></tr>')
        chapters.append(
            f'<section class="chapter" style="--accent:{accent}">'
            f'<table class="chhead"><tr><td class="ch-num"><span style="background:{accent}">{num}</span></td>'
            f'<td><div class="ch-title">{title}</div><div class="ch-sub">{sub}</div></td></tr></table>'
            f'<div class="body">{body}</div></section>')
    cover = f"""<div class="cover"><div class="cover-inner">
<div class="eyebrow">Telegram Business Sorter</div>
<h1>Документация<br>бота @Sort38_bot</h1>
<div class="sub">Сортировщик входящих · CRM · личный ассистент. Классификация, задачи, память, умные ответы, vault-пульты.</div>
<div class="badges"><span class="badge">{VERSION}</span><span class="badge">{TODAY}</span></div>
</div><div class="foot">Python · aiogram3 · Claude (Haiku + Sonnet) · Whisper · RAG · Obsidian</div></div>"""
    toc = f'<div class="toc"><h2>Содержание</h2><table class="toct">{"".join(toc_rows)}</table></div>'
    return (f"<!doctype html><html lang=ru><head><meta charset=utf-8></head>"
            f"<body>{cover}{toc}{''.join(chapters)}</body></html>")


def main() -> int:
    from weasyprint import HTML, CSS as WCSS
    out_pdf = os.path.join(OUT_DIR, f"TG-Sorter-Документация-{VERSION}.pdf")
    HTML(string=build_html(), base_url=DOCS).write_pdf(out_pdf, stylesheets=[WCSS(string=CSS)])
    import glob
    for old in glob.glob(os.path.join(OUT_DIR, "TG-Sorter-Документация-*.pdf")):
        if os.path.abspath(old) != os.path.abspath(out_pdf):
            os.remove(old)
            print(f"removed old: {os.path.basename(old)}")
    print(f"OK → {out_pdf}\nsize: {os.path.getsize(out_pdf)//1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
