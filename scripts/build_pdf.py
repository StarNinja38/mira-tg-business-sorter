#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TG-SORTER — генератор PDF-документации (премиум-дизайн).
Читает docs/*.md и рендерит в красивый PDF: обложка с градиентом, секции-бейджи,
каллауты, тёмные кодоблоки, зебра-таблицы, футер с номерами страниц.

Запуск:  python3 scripts/build_pdf.py [version]
"""
import os
import re
import sys
from datetime import date

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, Table, TableStyle,
    KeepTogether, PageBreak, NextPageTemplate, Flowable, ListFlowable, ListItem,
)

# ── палитра ──────────────────────────────────────────────────────────────────
INK      = colors.HexColor("#0F172A")   # slate-900
INK2     = colors.HexColor("#1E293B")   # slate-800
MUTED    = colors.HexColor("#475569")   # slate-600
FAINT    = colors.HexColor("#94A3B8")   # slate-400
LINE     = colors.HexColor("#E2E8F0")   # slate-200
BG       = colors.HexColor("#F8FAFC")   # slate-50
CARD     = colors.HexColor("#F1F5F9")   # slate-100
PRIMARY  = colors.HexColor("#2563EB")   # blue-600
PRIMARY2 = colors.HexColor("#0EA5E9")   # sky-500
ACCENT   = colors.HexColor("#06B6D4")   # cyan-500
GREEN    = colors.HexColor("#16A34A")
AMBER    = colors.HexColor("#D97706")
RED      = colors.HexColor("#DC2626")
VIOLET   = colors.HexColor("#7C3AED")
CODE_BG  = colors.HexColor("#0B1220")
CODE_FG  = colors.HexColor("#E2E8F0")
CODE_CM  = colors.HexColor("#64748B")

PAGE_W, PAGE_H = A4
MARGIN = 18 * mm
CONTENT_W = PAGE_W - 2 * MARGIN

DOCS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs")
VERSION = sys.argv[1] if len(sys.argv) > 1 else "v1.8.1"
TODAY = date.today().strftime("%d.%m.%Y")

CHAPTERS = [
    ("01 — Обзор.md",                "Обзор",                 "Зачем нужен бот и как он работает", PRIMARY),
    ("02 — Архитектура.md",          "Архитектура",           "Из чего собран и как устроен внутри", VIOLET),
    ("03 — Функции.md",              "Функции",               "Что бот умеет — по подсистемам",     ACCENT),
    ("04 — Команды.md",              "Команды",               "Управление ботом из Telegram",       PRIMARY2),
    ("05 — Интеграции.md",           "Интеграции",            "Todoist · Notion · Calendar · Vault", GREEN),
    ("06 — Деплой и обслуживание.md","Деплой и обслуживание", "Сервер, обновления, бэкапы",          AMBER),
    ("07 — Решение проблем.md",      "Решение проблем",       "Типичные сбои и как чинить",          RED),
]

# ── шрифты ───────────────────────────────────────────────────────────────────
def register_fonts():
    base = "/System/Library/Fonts/Supplemental"
    pdfmetrics.registerFont(TTFont("Sans",   f"{base}/Arial.ttf"))
    pdfmetrics.registerFont(TTFont("Sans-B", f"{base}/Arial Bold.ttf"))
    pdfmetrics.registerFont(TTFont("Sans-I", f"{base}/Arial Italic.ttf"))
    pdfmetrics.registerFont(TTFont("Sans-BI", f"{base}/Arial Bold Italic.ttf"))
    # моноширинный — пробуем Menlo (есть кириллица), иначе Arial
    mono = "Sans"
    for path, idx in [("/System/Library/Fonts/Menlo.ttc", 0)]:
        try:
            pdfmetrics.registerFont(TTFont("Mono", path, subfontIndex=idx))
            mono = "Mono"
            break
        except Exception:
            pass
    if mono == "Sans":
        pdfmetrics.registerFont(TTFont("Mono", f"{base}/Courier New.ttf")) if os.path.exists(f"{base}/Courier New.ttf") else None
    from reportlab.pdfbase.pdfmetrics import registerFontFamily
    registerFontFamily("Sans", normal="Sans", bold="Sans-B", italic="Sans-I", boldItalic="Sans-BI")

# ── эмодзи / очистка ─────────────────────────────────────────────────────────
_EMOJI = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U00002600-\U000027BF" "\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF" "\U00002B00-\U00002BFF" "\U0000FE00-\U0000FE0F"
    "\U0000200D" "\U000023E0-\U000023FF" "\U00002190-\U000021FF" "]+",
    flags=re.UNICODE,
)
def strip_emoji(s: str) -> str:
    s = _EMOJI.sub("", s)
    return re.sub(r"[ \t]{2,}", " ", s).strip()

def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def inline(s: str) -> str:
    """Markdown inline → reportlab markup. Эмодзи убираем."""
    s = strip_emoji(s)
    # вытащим `code`, чтобы не экранировать внутри
    codes = []
    def _grab(m):
        codes.append(m.group(1)); return f"\x00{len(codes)-1}\x00"
    s = re.sub(r"`([^`]+)`", _grab, s)
    s = esc(s)
    s = re.sub(r"\*\*([^*]+)\*\*", r'<font name="Sans-B">\1</font>', s)
    s = re.sub(r"(?<![\w*])\*([^*]+)\*(?![\w*])", r'<font name="Sans-I">\1</font>', s)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<font color="#2563EB">\1</font>', s)
    def _put(m):
        c = esc(codes[int(m.group(1))])
        return f'<font name="Mono" size="8.5" color="#0B3D91" backColor="#EEF2F7"> {c} </font>'
    s = re.sub(r"\x00(\d+)\x00", _put, s)
    return s

# ── стили ────────────────────────────────────────────────────────────────────
def styles():
    ss = getSampleStyleSheet()
    S = {}
    S["h1"] = ParagraphStyle("h1", fontName="Sans-B", fontSize=20, leading=24, textColor=INK, spaceBefore=2, spaceAfter=2)
    S["h2"] = ParagraphStyle("h2", fontName="Sans-B", fontSize=14, leading=18, textColor=INK, spaceBefore=14, spaceAfter=6)
    S["h3"] = ParagraphStyle("h3", fontName="Sans-B", fontSize=11.5, leading=15, textColor=PRIMARY, spaceBefore=10, spaceAfter=3)
    S["body"] = ParagraphStyle("body", fontName="Sans", fontSize=10, leading=15, textColor=INK2, spaceAfter=6, alignment=TA_LEFT)
    S["li"] = ParagraphStyle("li", parent=S["body"], spaceAfter=3, leftIndent=2)
    S["cap"] = ParagraphStyle("cap", fontName="Sans", fontSize=8.5, leading=11, textColor=FAINT)
    S["th"] = ParagraphStyle("th", fontName="Sans-B", fontSize=9, leading=12, textColor=colors.white)
    S["td"] = ParagraphStyle("td", fontName="Sans", fontSize=9, leading=12.5, textColor=INK2)
    S["code"] = ParagraphStyle("code", fontName="Mono", fontSize=8.3, leading=12.5, textColor=CODE_FG)
    S["quote"] = ParagraphStyle("quote", fontName="Sans-I", fontSize=9.5, leading=14, textColor=MUTED, leftIndent=8)
    S["toc"] = ParagraphStyle("toc", fontName="Sans", fontSize=11, leading=16, textColor=INK2)
    return S

# ── флоуэблы ─────────────────────────────────────────────────────────────────
class HR(Flowable):
    def __init__(self, w=CONTENT_W, color=LINE, thick=0.8, pad=4):
        super().__init__(); self.w=w; self.color=color; self.thick=thick; self.pad=pad
    def wrap(self, *a): return (self.w, self.pad*2)
    def draw(self):
        self.canv.setStrokeColor(self.color); self.canv.setLineWidth(self.thick)
        self.canv.line(0, self.pad, self.w, self.pad)

class SectionHeader(Flowable):
    """Бейдж с номером + заголовок + подзаголовок + цветная линия."""
    def __init__(self, num, title, subtitle, color):
        super().__init__(); self.num=num; self.title=title; self.subtitle=subtitle; self.color=color
    def wrap(self, w, h): self.w=CONTENT_W; return (CONTENT_W, 30*mm)
    def draw(self):
        c = self.canv; H = 30*mm
        # бейдж-кружок
        r = 9*mm
        cy = H - r - 2
        c.setFillColor(self.color); c.circle(r, cy, r, fill=1, stroke=0)
        c.setFillColor(colors.white); c.setFont("Sans-B", 15)
        c.drawCentredString(r, cy - 5.4, self.num)
        # заголовок
        tx = 2*r + 6*mm
        c.setFillColor(INK); c.setFont("Sans-B", 22)
        c.drawString(tx, cy + 1, self.title)
        c.setFillColor(MUTED); c.setFont("Sans", 10.5)
        c.drawString(tx, cy - 13, strip_emoji(self.subtitle))
        # нижняя акцентная полоса: цветной сегмент + тонкая линия на всю ширину
        y = 3
        c.setStrokeColor(LINE); c.setLineWidth(0.8)
        c.line(0, y + 1, CONTENT_W, y + 1)
        c.setFillColor(self.color)
        c.roundRect(0, y, 46*mm, 2.6, 1.3, fill=1, stroke=0)

class Callout(Flowable):
    """Каллаут: скруглённый бокс с акцентной левой полосой."""
    PAL = {"info": (PRIMARY, colors.HexColor("#EFF6FF")),
           "ok":   (GREEN,   colors.HexColor("#F0FDF4")),
           "warn": (AMBER,   colors.HexColor("#FFFBEB")),
           "stop": (RED,     colors.HexColor("#FEF2F2")),
           "rule": (VIOLET,  colors.HexColor("#F5F3FF"))}
    def __init__(self, flows, kind="info"):
        super().__init__(); self.flows=flows; self.kind=kind
    def wrap(self, w, h):
        self.w = CONTENT_W
        self.iw = self.w - 14*mm
        self.hh = 0
        for f in self.flows:
            _, fh = f.wrap(self.iw, 10_000); self.hh += fh
        self.H = self.hh + 9*mm
        return (self.w, self.H)
    def draw(self):
        c = self.canv; bar, bg = self.PAL.get(self.kind, self.PAL["info"])
        c.setFillColor(bg); c.roundRect(0, 0, self.w, self.H, 4*mm, fill=1, stroke=0)
        c.setFillColor(bar); c.roundRect(0, 0, 3.2*mm, self.H, 1.4*mm, fill=1, stroke=0)
        y = self.H - 4.5*mm
        for f in self.flows:
            _, fh = f.wrap(self.iw, 10_000)
            f.drawOn(c, 9*mm, y - fh)
            y -= fh

class CodeBlock(Flowable):
    """Тёмный кодоблок со скруглением."""
    def __init__(self, lines, S):
        super().__init__(); self.lines=lines; self.S=S
    def wrap(self, w, h):
        self.w = CONTENT_W
        self.lh = 12.5
        self.H = len(self.lines) * self.lh + 9*mm
        return (self.w, self.H)
    def draw(self):
        c = self.canv
        c.setFillColor(CODE_BG); c.roundRect(0, 0, self.w, self.H, 3.5*mm, fill=1, stroke=0)
        # «светофор» точки
        for i, col in enumerate([colors.HexColor("#FF5F56"), colors.HexColor("#FFBD2E"), colors.HexColor("#27C93F")]):
            c.setFillColor(col); c.circle(6*mm + i*4.2*mm, self.H - 5*mm, 1.3*mm, fill=1, stroke=0)
        y = self.H - 9*mm
        for ln in self.lines:
            stripped = ln.strip()
            is_cm = stripped.startswith("#") or stripped.startswith("//")
            c.setFont("Mono", 8.3)
            c.setFillColor(CODE_CM if is_cm else CODE_FG)
            c.drawString(5*mm, y, ln[:118])
            y -= self.lh

# ── парсер markdown → флоуэблы ───────────────────────────────────────────────
def parse_md(text, S):
    out = []
    lines = text.split("\n")
    i = 0
    # пропустить frontmatter
    if lines and lines[0].strip() == "---":
        i = 1
        while i < len(lines) and lines[i].strip() != "---":
            i += 1
        i += 1
    n = len(lines)
    buf = []
    def flush():
        if buf:
            out.append(Paragraph(inline(" ".join(buf)), S["body"]))
            buf.clear()
    while i < n:
        ln = lines[i]; st = ln.strip()
        # код-блок
        if st.startswith("```"):
            flush(); i += 1; code = []
            while i < n and not lines[i].strip().startswith("```"):
                code.append(lines[i]); i += 1
            i += 1
            if code:
                out.append(Spacer(1, 3)); out.append(CodeBlock(code, S)); out.append(Spacer(1, 5))
            continue
        # таблица
        if st.startswith("|") and i + 1 < n and re.match(r"^\|[\s:\-|]+\|?\s*$", lines[i+1].strip()):
            flush(); rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(lines[i].strip()); i += 1
            out.append(build_table(rows, S)); out.append(Spacer(1, 6)); continue
        # заголовки
        m = re.match(r"^(#{1,6})\s+(.*)$", st)
        if m:
            flush(); lvl = len(m.group(1)); title = m.group(2)
            if lvl == 1:
                i += 1; continue  # H1 файла — заменяем своим SectionHeader
            out.append(Paragraph(inline(title), S["h2"] if lvl == 2 else S["h3"]))
            i += 1; continue
        # hr
        if st in ("---", "***", "___"):
            flush(); out.append(Spacer(1, 2)); out.append(HR()); out.append(Spacer(1, 2)); i += 1; continue
        # блок-кавычка → callout info
        if st.startswith(">"):
            flush(); q = []
            while i < n and lines[i].strip().startswith(">"):
                q.append(lines[i].strip()[1:].strip()); i += 1
            kind = "rule" if any(w in q[0] for w in ("ПРАВИЛО", "ВАЖНО", "Важно")) else "info"
            out.append(Spacer(1, 2))
            out.append(Callout([Paragraph(inline(" ".join(q)), S["body"])], kind))
            out.append(Spacer(1, 5)); continue
        # списки (маркированные/нумерованные)
        if re.match(r"^\s*([-*]|\d+\.)\s+", ln):
            flush(); items = []; ordered = bool(re.match(r"^\s*\d+\.", ln))
            while i < n and re.match(r"^\s*([-*]|\d+\.)\s+", lines[i]):
                txt = re.sub(r"^\s*([-*]|\d+\.)\s+", "", lines[i])
                items.append(ListItem(Paragraph(inline(txt), S["li"]),
                                      value=None, leftIndent=10,
                                      bulletColor=PRIMARY))
                i += 1
            out.append(ListFlowable(
                items, bulletType="1" if ordered else "bullet",
                bulletFontName="Sans-B", bulletColor=PRIMARY,
                bulletFontSize=8 if not ordered else 9,
                leftIndent=14, bulletDedent=8, spaceBefore=2, spaceAfter=6,
            ))
            continue
        # пустая строка
        if not st:
            flush(); i += 1; continue
        # обычный текст
        buf.append(st); i += 1
    flush()
    return out

def build_table(rows, S):
    def cells(r): return [c.strip() for c in r.strip().strip("|").split("|")]
    header = cells(rows[0])
    body = [cells(r) for r in rows[2:]]
    data = [[Paragraph(inline(h), S["th"]) for h in header]]
    for r in body:
        r = (r + [""] * len(header))[:len(header)]
        data.append([Paragraph(inline(c), S["td"]) for c in r])
    ncol = len(header)
    cw = CONTENT_W / ncol
    # первая колонка чуть шире, если столбцов мало
    widths = [cw] * ncol
    if ncol >= 2:
        widths[0] = cw * 1.25
        rest = (CONTENT_W - widths[0]) / (ncol - 1)
        widths = [widths[0]] + [rest] * (ncol - 1)
    t = Table(data, colWidths=widths, repeatRows=1)
    ts = [
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, 0), 0, INK),
        ("LINEBELOW", (0, 1), (-1, -1), 0.5, LINE),
        ("ROUNDEDCORNERS", [4, 4, 0, 0]),
    ]
    for ri in range(1, len(data)):
        if ri % 2 == 0:
            ts.append(("BACKGROUND", (0, ri), (-1, ri), BG))
    t.setStyle(TableStyle(ts))
    return t

# ── обложка + футер ──────────────────────────────────────────────────────────
def draw_cover(c, doc):
    c.saveState()
    # фон-градиент (тёмно-синий → индиго)
    c.linearGradient(0, 0, PAGE_W, PAGE_H,
                     [colors.HexColor("#0B1220"), colors.HexColor("#172554"), colors.HexColor("#1E3A8A")],
                     (0, 0.55, 1), extend=True)
    c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    # декоративные круги
    c.setFillColor(colors.HexColor("#2563EB")); c.setFillAlpha(0.18)
    c.circle(PAGE_W - 20*mm, PAGE_H - 30*mm, 60*mm, fill=1, stroke=0)
    c.setFillColor(colors.HexColor("#06B6D4")); c.setFillAlpha(0.14)
    c.circle(25*mm, 35*mm, 45*mm, fill=1, stroke=0)
    c.setFillAlpha(1)
    # верхняя метка
    c.setFillColor(colors.HexColor("#7DD3FC")); c.setFont("Sans-B", 11)
    c.drawString(MARGIN, PAGE_H - 32*mm, "TELEGRAM BUSINESS SORTER")
    c.setStrokeColor(colors.HexColor("#38BDF8")); c.setLineWidth(2)
    c.line(MARGIN, PAGE_H - 35*mm, MARGIN + 52*mm, PAGE_H - 35*mm)
    # тайтл
    c.setFillColor(colors.white); c.setFont("Sans-B", 58)
    c.drawString(MARGIN, PAGE_H/2 + 22*mm, "TG-SORTER")
    c.setFillColor(colors.HexColor("#CBD5E1")); c.setFont("Sans", 15)
    c.drawString(MARGIN, PAGE_H/2 + 10*mm, "Документация бота @Sort38_bot")
    c.setFillColor(colors.HexColor("#94A3B8")); c.setFont("Sans", 11)
    c.drawString(MARGIN, PAGE_H/2 + 1*mm,
                 "Сортировка входящих · задачи · память · интеграции")
    # чип версии
    chip_y = PAGE_H/2 - 16*mm
    c.setFillColor(colors.HexColor("#2563EB"))
    c.roundRect(MARGIN, chip_y, 42*mm, 10*mm, 3*mm, fill=1, stroke=0)
    c.setFillColor(colors.white); c.setFont("Sans-B", 12)
    c.drawCentredString(MARGIN + 21*mm, chip_y + 3, f"ВЕРСИЯ {VERSION}")
    c.setFillColor(colors.HexColor("#94A3B8")); c.setFont("Sans", 11)
    c.drawString(MARGIN + 48*mm, chip_y + 3, TODAY)
    # подвал обложки
    c.setFillColor(colors.HexColor("#64748B")); c.setFont("Sans", 9)
    c.drawString(MARGIN, 20*mm, "Python · aiogram 3 · Claude (Haiku + Sonnet) · SQLite")
    c.drawRightString(PAGE_W - MARGIN, 20*mm, "Приватный проект · StarNinja38")
    c.restoreState()

def draw_footer(c, doc):
    c.saveState()
    pg = c.getPageNumber()
    c.setStrokeColor(LINE); c.setLineWidth(0.6)
    c.line(MARGIN, 13*mm, PAGE_W - MARGIN, 13*mm)
    c.setFillColor(FAINT); c.setFont("Sans", 8)
    c.drawString(MARGIN, 8.5*mm, f"TG-SORTER · Документация {VERSION}")
    c.drawRightString(PAGE_W - MARGIN, 8.5*mm, f"{pg - 1}")
    c.setFillColor(PRIMARY); c.setFont("Sans-B", 8)
    c.drawCentredString(PAGE_W/2, 8.5*mm, "@Sort38_bot")
    c.restoreState()

# ── TOC ──────────────────────────────────────────────────────────────────────
def build_toc(S):
    flows = [Spacer(1, 6),
             Paragraph("Содержание", S["h1"]),
             HR(color=PRIMARY, thick=2, pad=5), Spacer(1, 8)]
    rows = []
    for idx, (_, title, sub, color) in enumerate(CHAPTERS, 1):
        badge = Table([[Paragraph(f'<font color="white" name="Sans-B">{idx:02d}</font>', S["td"])]],
                      colWidths=[9*mm], rowHeights=[9*mm])
        badge.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), color),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("ROUNDEDCORNERS", [4, 4, 4, 4]),
            ("LEFTPADDING", (0,0),(-1,-1),0),("RIGHTPADDING", (0,0),(-1,-1),0),
        ]))
        txt = Paragraph(f'<font name="Sans-B" size="12" color="#0F172A">{title}</font><br/>'
                        f'<font name="Sans" size="9" color="#64748B">{strip_emoji(sub)}</font>', S["toc"])
        rows.append([badge, txt])
    t = Table(rows, colWidths=[12*mm, CONTENT_W - 12*mm])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, LINE),
        ("LEFTPADDING", (1, 0), (1, -1), 8),
    ]))
    flows.append(t)
    # что нового
    flows += [Spacer(1, 14),
              Callout([Paragraph(f'<font name="Sans-B" color="#0F172A">Что нового в {VERSION}</font>', S["body"]),
                       Paragraph("Многослойная защита от дублей задач (семантический судья при создании + "
                                 "ночная зачистка) · память = полная память контакта (характер, привычки, "
                                 "образ жизни) · статус «Новая → В работе» на канбан-доске Notion · отказ "
                                 "от встречи не создаёт задачу · экономия LLM на болтовне · папка «Вложения» "
                                 "с авто-переносом · приватный бэкап-репозиторий vault.", S["body"])], "ok")]
    return flows

# ── сборка ───────────────────────────────────────────────────────────────────
def main():
    register_fonts()
    S = styles()
    out_path = os.path.join(OUT_DIR, f"TG-Sorter-Документация-{VERSION}.pdf")

    doc = BaseDocTemplate(
        out_path, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN, bottomMargin=20*mm,
        title=f"TG-SORTER — Документация {VERSION}", author="StarNinja38",
    )
    frame = Frame(MARGIN, 18*mm, CONTENT_W, PAGE_H - MARGIN - 18*mm, id="body")
    doc.addPageTemplates([
        PageTemplate(id="cover", frames=[frame], onPage=draw_cover),
        PageTemplate(id="body", frames=[frame], onPage=draw_footer),
    ])

    story = [NextPageTemplate("body"), PageBreak()]   # стр.1 — обложка (canvas), далее — body
    story += build_toc(S)                       # стр.2 — содержание
    for idx, (fname, title, sub, color) in enumerate(CHAPTERS, 1):
        story.append(PageBreak())
        story.append(SectionHeader(f"{idx:02d}", title, sub, color))
        story.append(Spacer(1, 6))
        path = os.path.join(DOCS, fname)
        with open(path, encoding="utf-8") as f:
            story += parse_md(f.read(), S)

    doc.build(story)
    print(f"OK → {out_path}")
    print(f"size: {os.path.getsize(out_path)/1024:.0f} KB")

    # Авто-очистка: оставляем только что собранный PDF, прошлые версии удаляем
    import glob
    for old in glob.glob(os.path.join(OUT_DIR, "TG-Sorter-Документация-*.pdf")):
        if os.path.abspath(old) != os.path.abspath(out_path):
            os.remove(old)
            print(f"removed old: {os.path.basename(old)}")

if __name__ == "__main__":
    main()
