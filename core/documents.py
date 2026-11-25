"""
Document text extraction — через MarkItDown (Microsoft) с фолбэком на pypdf/python-docx.
Поддержка: PDF, Word, Excel, PowerPoint, CSV, HTML, EPUB, JSON, XML, RTF, ODT, TXT/MD.
Файл качается из Telegram, текст извлекается → анализ на задачи. Всё локально (приватность).
"""
import asyncio
import tempfile
from pathlib import Path
from typing import Optional

from loguru import logger

MAX_CHARS = 8000  # cap extracted text to keep LLM costs sane

# MarkItDown берёт на себя офисные/структурные форматы (богатый текст + таблицы)
MARKITDOWN_EXT = {
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
    ".csv", ".html", ".htm", ".epub", ".json", ".xml", ".rtf", ".odt",
}
# Простой текст читаем напрямую; PDF/DOCX имеют легаси-фолбэк
PLAIN_EXT = {".txt", ".md"}
SUPPORTED_EXT = MARKITDOWN_EXT | PLAIN_EXT


def _extract_markitdown(path: str) -> str:
    """Главный путь: любой документ → Markdown (сохраняет заголовки/таблицы → LLM точнее)."""
    from markitdown import MarkItDown
    md = MarkItDown()
    result = md.convert(path)
    return (getattr(result, "text_content", "") or "")


def _extract_pdf(path: str) -> str:  # легаси-фолбэк
    from pypdf import PdfReader
    reader = PdfReader(path)
    parts = []
    for page in reader.pages[:30]:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(parts)


def _extract_docx(path: str) -> str:  # легаси-фолбэк
    import docx
    doc = docx.Document(path)
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def _extract_txt(path: str) -> str:
    for enc in ("utf-8", "cp1251", "latin-1"):
        try:
            return Path(path).read_text(encoding=enc)
        except Exception:
            continue
    return ""


async def extract_document_text(bot, document) -> Optional[tuple[str, str]]:
    """Скачать документ из Telegram и извлечь текст. Возвращает (имя_файла, текст) или None."""
    file_name = document.file_name or "document"
    ext = Path(file_name).suffix.lower()

    if ext not in SUPPORTED_EXT:
        logger.info(f"Document type {ext} not supported for text extraction")
        return None

    if document.file_size and document.file_size > 20 * 1024 * 1024:
        logger.info(f"Document {file_name} too large ({document.file_size} bytes)")
        return None

    tmp_path = None
    try:
        file_info = await bot.get_file(document.file_id)
        file_bytes = await bot.download_file(file_info.file_path)
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(file_bytes.read())
            tmp_path = tmp.name

        loop = asyncio.get_running_loop()
        text = ""

        if ext in PLAIN_EXT:
            text = await loop.run_in_executor(None, _extract_txt, tmp_path)
        else:
            # Основной путь — MarkItDown (широкий охват + структура)
            try:
                text = await loop.run_in_executor(None, _extract_markitdown, tmp_path)
            except Exception as exc:
                logger.warning(f"MarkItDown failed for {file_name}: {exc} — пробую легаси")
            # Фолбэк для PDF/DOCX, если MarkItDown недоступен/пуст
            if not (text or "").strip():
                if ext == ".pdf":
                    text = await loop.run_in_executor(None, _extract_pdf, tmp_path)
                elif ext in (".docx", ".doc"):
                    try:
                        text = await loop.run_in_executor(None, _extract_docx, tmp_path)
                    except Exception:
                        pass

        text = (text or "").strip()[:MAX_CHARS]
        if text:
            logger.info(f"Extracted {len(text)} chars from {file_name} ({ext})")
            return file_name, text
        logger.info(f"No text extracted from {file_name}")
        return None

    except Exception as exc:
        logger.error(f"Document extraction failed for {file_name}: {exc}")
        return None
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
