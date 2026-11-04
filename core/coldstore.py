"""ARES CODE — холодное хранение: пул почтовых ящиков (mail.ru).

Стратегия ротации (выбранная): ROUND-ROBIN по порядковому номеру архива.
  индекс ящика = (seq - 1) % N, где seq — монотонный номер архива в SQLite,
  при недоступности ящика попытка со следующего (health-aware failover).
Почему round-robin, а не «заполнять по порядку»:
  1) квота ящиков расходуется равномерно, без «дно ящика 1 переполнено»;
  2) не нужно ничего вычислять при выборе — реестр archive→mailbox и так в SQLite;
  3) добавить/убрать ящик = дописать переменную .env, ничего не перестраивается.

Загрузка: SMTP само-письмом с zip-вложением (больше лимита письма — части part i/M).
Чтение: IMAP по теме ARES::<проект>::<sha8>, сборка частей, сверка sha256.
"""
import email
import email.message
import imaplib
import re
import smtplib
import time
from pathlib import Path

from core import config, db

SUBJ_PREFIX = "ARES::"


def mailboxes():
    return config.coldstore_mailboxes()


def pick_index(seq: int, n: int) -> int:
    return (int(seq) - 1) % n if n else 0


def _imap(login: str, pwd: str):
    M = imaplib.IMAP4_SSL(config.COLDSTORE_IMAP_HOST, config.COLDSTORE_IMAP_PORT)
    M.login(login, pwd)
    M.select("INBOX", readonly=True)
    return M


def status() -> list:
    """Здоровье каждого ящика + сколько архивов уже лежит (IMAP-поиск по теме)."""
    out = []
    for login, pwd in mailboxes():
        item = {"login": _mask(login), "ok": False}
        try:
            M = _imap(login, pwd)
            typ, data = M.search(None, "SUBJECT", f'"{SUBJ_PREFIX}"')
            item["archives"] = len((data[0] or b"").split())
            M.logout()
            item["ok"] = True
        except Exception as e:
            item["err"] = f"{type(e).__name__}: {str(e)[:120]}"
        out.append(item)
    return out


def _mask(login: str) -> str:
    name, _, dom = login.partition("@")
    if len(name) <= 2:
        return f"{name[0]}***@{dom}" if name else login
    return f"{name[0]}{name[1]}***@{dom}"


def upload_archive(arch: dict) -> dict:
    """Отправить архив в пул ящиков (round-robin + failover). arch — строка из db."""
    mbs = mailboxes()
    if not mbs:
        return {"ok": False, "error": "COLDSTORE_EMAIL_*_LOGIN/PASS не настроены в .env"}
    path = Path(arch["path"])
    if not path.exists():
        return {"ok": False, "error": "файл архива не найден"}

    data = path.read_bytes()
    limit = max(1, config.COLDSTORE_MAX_MAIL_MB) * 1024 * 1024
    parts = [data[i:i + limit] for i in range(0, len(data), limit)] or [b""]
    subject_base = f"{SUBJ_PREFIX}{arch['project']}::{arch['sha256'][:12]}"

    start = pick_index(arch["seq"], len(mbs))
    last_err = None
    for k in range(len(mbs)):
        login, pwd = mbs[(start + k) % len(mbs)]
        try:
            with smtplib.SMTP_SSL(config.COLDSTORE_SMTP_HOST, config.COLDSTORE_SMTP_PORT, timeout=40) as s:
                s.login(login, pwd)
                for i, chunk in enumerate(parts, 1):
                    msg = email.message.EmailMessage()
                    msg["From"] = login
                    msg["To"] = login
                    msg["Subject"] = f"{subject_base}::part{i}/{len(parts)}" if len(parts) > 1 else subject_base
                    msg.set_content(
                        "ARES CODE cold archive\n"
                        f"project: {arch['project']}\n"
                        f"sha256: {arch['sha256']}\n"
                        f"part: {i}/{len(parts)}\n"
                        f"date: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    )
                    msg.add_attachment(chunk, maintype="application", subtype="zip",
                                       filename=f"{arch['project']}.zip.part{i}")
                    s.send_message(msg)
            db.set_cold(arch["id"], login, subject_base, len(parts))
            return {"ok": True, "mailbox": _mask(login), "parts": len(parts), "subject": subject_base}
        except Exception as e:
            last_err = e
    return {"ok": False, "error": f"все ящики недоступны, последняя ошибка: {last_err}"}


def pull_archive(arch_id: int) -> Path:
    """Достать архив из холодного хранилища обратно в data/archives (со сверкой sha256)."""
    row = db.get_archive(arch_id)
    if not row:
        raise ValueError("архив не найден в реестре")
    if not row["cold"]:
        raise ValueError("архив не отмечен как холодный")
    creds = dict(mailboxes())
    if row["mailbox"] not in creds:
        raise ValueError(f"в .env нет пароля для ящика {row['mailbox']}")

    M = _imap(row["mailbox"], creds[row["mailbox"]])
    try:
        typ, data = M.search(None, "SUBJECT", f'"{row["message_id"]}"')
        ids = data[0].split() if data and data[0] else []
        found = []
        for i in ids:
            typ, md = M.fetch(i, "(RFC822)")
            raw = md[0][1]
            m = email.message_from_bytes(raw)
            att = None
            for p in m.walk():
                if p.get_content_maintype() == "application" and p.get_payload(decode=True):
                    att = p.get_payload(decode=True)
                    break
            found.append((m.get("Subject", ""), att))
    finally:
        try:
            M.logout()
        except Exception:
            pass

    def partno(t):
        mm = re.search(r"part(\d+)/(\d+)", t[0] or "")
        return int(mm.group(1)) if mm else 1

    found.sort(key=partno)
    need = int(row["parts"] or 1)
    if len(found) < need:
        raise ValueError(f"в ящике найдено {len(found)} из {need} частей")
    blob = b"".join((a or b"") for _, a in found[:need])

    import hashlib
    if hashlib.sha256(blob).hexdigest() != row["sha256"]:
        raise ValueError("sha256 не совпал — архив повреждён при пересылке")
    out = Path(row["path"])
    out.write_bytes(blob)
    return out
