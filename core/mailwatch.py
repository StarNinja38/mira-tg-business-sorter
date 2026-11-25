"""
v4.28 Почта: Мира читает новые письма (IMAP, Gmail App Password) и сообщает владельцу.
v4.36 (B4) переработка надёжности:
- курсор UID двигается ТОЛЬКО после успешной доставки карточки (раньше — до: сбой сети = письмо потеряно);
- «ядовитое» письмо (таймаут/битые заголовки) не стопорит ящик — пропускается с warning;
- письма обрабатываются ПО ВОЗРАСТАНИЮ, первые 10 за проход (раньше — только 10 последних, старшие терялись);
- большие письма (>300 КБ) — только заголовки, без тела;
- UIDVALIDITY отслеживается: смена → baseline заново (иначе тихая потеря);
- сетевые/IMAP-ошибки → warning + алерт владельцу (кулдаун 2 ч), а не debug.

Принципы: только ЧТЕНИЕ (BODY.PEEK, readonly); анти-шум — Промо/Соцсети мимо; ноль LLM.
"""
import email
import imaplib
import re
from email.header import decode_header

from loguru import logger

_SKIP_LABELS = ("CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL")
_BIG_MAIL = 300_000          # байт: больше — читаем только заголовки
_PER_PASS = 10


def _dec(s) -> str:
    if not s:
        return ""
    out = ""
    try:
        for p, enc in decode_header(s):
            out += p.decode(enc or "utf-8", "replace") if isinstance(p, bytes) else str(p)
    except Exception:
        out = str(s)
    return out.strip()


def _snippet(msg) -> str:
    """Первые ~250 символов текстовой части."""
    try:
        parts = msg.walk() if msg.is_multipart() else [msg]
        for p in parts:
            if p.get_content_type() == "text/plain":
                body = p.get_payload(decode=True) or b""
                txt = body.decode(p.get_content_charset() or "utf-8", "replace")
                return re.sub(r"\s+", " ", txt).strip()[:250]
    except Exception:
        pass
    return ""


def parse_accounts(raw: str) -> list[tuple]:
    """EMAIL_ACCOUNTS='user1@gmail.com:pass1, user2@gmail.com:pass2' → [(user, pass), …].
    App-пароли Gmail содержат пробелы — допускаем их внутри пароля (сплит по ПЕРВОМУ ':')."""
    out = []
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if ":" in chunk:
            u, p = chunk.split(":", 1)
            if u.strip() and p.strip():
                out.append((u.strip(), p.strip()))
    return out


def _uidvalidity(M) -> str:
    try:
        v = M.response("UIDVALIDITY")[1]
        return (v[0].decode() if v and v[0] else "") or ""
    except Exception:
        return ""


def fetch_new(host: str, user: str, pwd: str, last_uid: int, important_only: bool = True,
              last_uidv: str = "") -> dict:
    """Блокирующая (через to_thread). → {"uidv": str, "baseline": bool, "scanned": [uid…] по возрастанию,
    "msgs": [{"uid","from","subject","snippet","important"}]}.
    scanned — все просмотренные uid (в т.ч. пропущенные как промо/неважные): по ним двигается курсор.
    important_only: только письма с Gmail-меткой \\Important (их ML важности, без нашего LLM)."""
    M = imaplib.IMAP4_SSL(host, timeout=45)   # v4.64: 25 с не хватало — таймауты по всем ящикам
    try:
        M.login(user, pwd)
        M.select("INBOX", readonly=True)
        uidv = _uidvalidity(M)
        typ, data = M.uid("search", None, "ALL" if not last_uid else f"UID {last_uid + 1}:*")
        uids = sorted(int(u) for u in (data[0].split() if data and data[0] else []))
        if not last_uid or (last_uidv and uidv and uidv != last_uidv):
            # baseline (первый запуск ИЛИ ящик пересобран — UIDVALIDITY сменился): хвост без уведомлений
            return {"uidv": uidv, "baseline": True, "scanned": [uids[-1]] if uids else [], "msgs": []}
        uids = [u for u in uids if u > last_uid][:_PER_PASS]     # по возрастанию, порциями
        scanned, out = [], []
        for uid in uids:
            try:
                typ, d = M.uid("fetch", str(uid), "(X-GM-LABELS RFC822.SIZE)")
                meta = d[0].decode("utf-8", "replace") if d and isinstance(d[0], bytes) else str(d[0] if d else "")
                scanned.append(uid)
                if any(l in meta for l in _SKIP_LABELS):
                    continue                            # промо/соцсети — мимо всегда
                important = "\\Important" in meta
                if important_only and not important:
                    continue
                m_size = re.search(r"RFC822\.SIZE (\d+)", meta)
                big = bool(m_size and int(m_size.group(1)) > _BIG_MAIL)
                typ, d = M.uid("fetch", str(uid), "(BODY.PEEK[HEADER])" if big else "(BODY.PEEK[])")
                if not d or not d[0] or not isinstance(d[0], tuple):
                    continue
                m = email.message_from_bytes(d[0][1])
                out.append({"uid": uid, "from": _dec(m.get("From"))[:120],
                            "subject": _dec(m.get("Subject"))[:150] or "(без темы)",
                            "snippet": "" if big else _snippet(m), "important": important})
            except Exception as exc:
                # ядовитое письмо — пропускаем, курсор всё равно продвинется дальше него
                logger.warning(f"Mailwatch [{user}] uid={uid} skip: {type(exc).__name__}: {exc}")
                scanned.append(uid)
        return {"uidv": uidv, "baseline": False, "scanned": scanned, "msgs": out}
    finally:
        try:
            M.logout()
        except Exception:
            pass


def fetch_recent(host: str, user: str, pwd: str, limit: int = 8, query: str = "") -> list[dict]:
    """v4.73 Блокирующая (через to_thread): последние письма или поиск Gmail, только чтение."""
    M = imaplib.IMAP4_SSL(host, timeout=45)
    try:
        M.login(user, pwd)
        M.select("INBOX", readonly=True)
        if query:
            M._encoding = "utf-8"
            M.literal = query.encode("utf-8")
            typ, data = M.uid("search", "CHARSET", "UTF-8", "X-GM-RAW")
        else:
            typ, data = M.uid("search", None, "ALL")
        uids = sorted(int(u) for u in (data[0].split() if data and data[0] else []))[-limit:]
        out = []
        for uid in reversed(uids):
            typ, d = M.uid("fetch", str(uid), "(X-GM-LABELS RFC822.SIZE)")
            meta = d[0].decode("utf-8", "replace") if d and isinstance(d[0], bytes) else str(d[0] if d else "")
            if any(l in meta for l in _SKIP_LABELS):
                continue
            size = re.search(r"RFC822\.SIZE (\d+)", meta)
            big = bool(size and int(size.group(1)) > _BIG_MAIL)
            typ, d = M.uid("fetch", str(uid), "(BODY.PEEK[HEADER])" if big else "(BODY.PEEK[])")
            if not d or not d[0] or not isinstance(d[0], tuple):
                continue
            m = email.message_from_bytes(d[0][1])
            out.append({"date": _dec(m.get("Date"))[:22], "from": _dec(m.get("From"))[:120],
                        "subject": _dec(m.get("Subject"))[:150] or "(без темы)", "snippet": "" if big else _snippet(m)})
        return out
    finally:
        try:
            M.logout()
        except Exception:
            pass


MAIL_ALERT_AFTER_S = 3600   # v4.64: тревожить, только если ящик не отвечает час подряд


async def _mail_fail(bot, db, owner_id: int, user: str, reason: str) -> None:
    """Временный сбой опроса. 13.10.2026: 19 тревог за 5 дней на единичные таймауты Gmail, почта при этом
    читалась следующим проходом — шум, приучающий игнорировать тревоги бота. Тревога — только после часа подряд."""
    import time as _t
    from db import get_setting, set_setting
    try:
        first = float(await get_setting(db, f"mailfail:{user}") or 0)
        now = _t.time()
        if not first:
            await set_setting(db, f"mailfail:{user}", str(now))
            return
        if now - first < MAIL_ALERT_AFTER_S or await get_setting(db, f"mailalerted:{user}"):
            return
        from core.alerts import alert_owner
        await alert_owner(bot, db, owner_id, "apifail:mail",
                          f"📧 Почта [{user}]: не отвечает больше часа ({reason}). Письма пока не приходят.", cooldown_minutes=180)
        await set_setting(db, f"mailalerted:{user}", "1")
    except Exception as exc:
        logger.debug(f"mail fail bookkeeping skip: {exc}")


async def _mail_ok(bot, db, owner_id: int, user: str) -> None:
    """Ящик снова ответил: сбросить счётчик; если была тревога — коротко сообщить, что почта вернулась."""
    from db import get_setting, set_setting
    try:
        if await get_setting(db, f"mailfail:{user}"):
            await set_setting(db, f"mailfail:{user}", "")
        if await get_setting(db, f"mailalerted:{user}"):
            await set_setting(db, f"mailalerted:{user}", "")
            from core.topics import send_owner
            await send_owner(bot, owner_id, "urgent", f"✅ Почта [{user}] снова работает")
    except Exception as exc:
        logger.debug(f"mail ok bookkeeping skip: {exc}")


async def poll_mail(bot, owner_id: int, db, settings) -> None:
    """Один проход по ВСЕМ ящикам (EMAIL_ACCOUNTS): важные новые письма → карточки в 📧 Почта.
    Курсор = последний uid, ДО которого всё либо доставлено, либо осознанно пропущено."""
    import asyncio
    import html as _html
    accounts = parse_accounts(getattr(settings, "email_accounts", "") or "")
    if not accounts:
        return
    from db import get_setting, set_setting
    from core.topics import send_owner
    for user, pwd in accounts:
        key, vkey = f"mail_uid:{user}", f"mail_uidv:{user}"
        try:
            last_uid = int(await get_setting(db, key) or 0)
            last_uidv = await get_setting(db, vkey) or ""
            res = await asyncio.to_thread(
                fetch_new, settings.email_imap_host, user, pwd, last_uid,
                getattr(settings, "email_important_only", True), last_uidv)
            await _mail_ok(bot, db, owner_id, user)   # v4.64: ящик ответил — сбрасываем счётчик сбоев
            if res.get("uidv") and res["uidv"] != last_uidv:
                await set_setting(db, vkey, res["uidv"])
            if res["baseline"]:
                if res["scanned"]:
                    await set_setting(db, key, str(res["scanned"][-1]))
                    logger.info(f"Mailwatch [{user}]: baseline uid={res['scanned'][-1]}")
                continue
            msgs = sorted(res["msgs"], key=lambda m: m["uid"])
            delivered_upto = last_uid
            failed = False
            for m in msgs[:5]:
                card = (f"📧 <b>{_html.escape(m['from'])}</b> → {_html.escape(user.split('@')[0])}\n"
                        f"<b>{_html.escape(m['subject'])}</b>")
                if m.get("snippet"):
                    card += f"\n<i>{_html.escape(m['snippet'])}</i>"
                try:
                    await send_owner(bot, owner_id, "email", card, parse_mode="HTML")
                    delivered_upto = m["uid"]
                except Exception as exc:
                    logger.warning(f"Mailwatch [{user}] deliver uid={m['uid']} failed: {exc} — курсор не двигаем")
                    failed = True
                    break
            if failed:
                # доставлено до delivered_upto — остальное (в т.ч. неважные до него) придёт следующим проходом
                if delivered_upto > last_uid:
                    await set_setting(db, key, str(delivered_upto))
                continue
            if len(msgs) > 5:   # остальные важные — следующим проходом (курсор ставим до 5-го)
                await set_setting(db, key, str(msgs[4]["uid"]))
            elif res["scanned"]:
                await set_setting(db, key, str(res["scanned"][-1]))
            if msgs:
                logger.info(f"Mailwatch [{user}]: {min(len(msgs), 5)} важных писем доставлено")
        except imaplib.IMAP4.error as exc:
            logger.warning(f"Mailwatch [{user}] auth/IMAP error: {exc}")
            try:
                from core.alerts import alert_owner
                await alert_owner(bot, db, owner_id, "apifail:mail",
                                  f"📧 Почта [{user}]: IMAP не пустил ({str(exc)[:80]}). Проверь app-пароль.", cooldown_minutes=120)
            except Exception:
                pass
        except Exception as exc:
            logger.warning(f"Mailwatch [{user}] error: {type(exc).__name__}: {exc}")
            await _mail_fail(bot, db, owner_id, user, type(exc).__name__)
