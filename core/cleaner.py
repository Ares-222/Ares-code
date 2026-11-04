"""ARES CODE — уборщик workspace.

* ежедневный прогон в CLEANUP_HOUR (локальное время сервера) или внепланово при диске > DISK_CLEAN_AT;
* сначала dry-run (что бы убралось), затем перенос неактивных проектов в workspace/.trash;
* .trash чистится: записи старше TRASH_TTL_H (24ч) удаляются физически;
* никогда не трогает: активный проект, .env, базы данных (data/*), служебные каталоги.
"""
import asyncio
import shutil
import time

from core import config, resources

SKIP_NAMES = {".trash", ".tmp", ".env", "ares.db", ".archives"}
DB_EXT = (".db", ".sqlite", ".sqlite3")


def _is_protected(path, active_project: str | None) -> bool:
    if path.name in SKIP_NAMES or path.name.startswith("."):
        return True
    if active_project and path.name == active_project:
        return True
    if path.suffix.lower() in DB_EXT:
        return True
    return False


async def cleanup(dry_run: bool = True, active_project: str | None = None) -> dict:
    active = active_project or db_active()
    candidates, moved = [], []
    for child in config.WORKSPACE.iterdir():
        if _is_protected(child, active):
            continue
        candidates.append(child)
    if dry_run:
        return {"dry_run": True, "would_move": sorted(c.name for c in candidates),
                "protected": active or "(нет)"}
    stamp = time.strftime("%Y%m%d_%H%M%S")
    config.TRASH_DIR.mkdir(parents=True, exist_ok=True)
    for c in candidates:
        try:
            shutil.move(str(c), str(config.TRASH_DIR / f"{stamp}_{c.name}"))
            moved.append(c.name)
        except OSError:
            continue
    return {"dry_run": False, "moved": moved, "protected": active or "(нет)"}


def db_active() -> str | None:
    from core import db
    try:
        return db.kv_get("active_project")
    except Exception:
        return None


def purge_trash(ttl_h: int | None = None) -> list:
    ttl = (ttl_h or config.TRASH_TTL_H) * 3600
    now = time.time()
    removed = []
    for child in config.TRASH_DIR.iterdir():
        try:
            if now - child.stat().st_mtime > ttl:
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
                removed.append(child.name)
        except OSError:
            continue
    return removed


async def loop(notify=None):
    """Фоновый цикл: проверка каждые 5 минут; уборка по часу или по диску; ежечасный purge .trash."""
    done_day, last_disk_run, last_purge = "", 0.0, time.time()
    while True:
        try:
            snap = resources.snapshot()
            hour = time.localtime().tm_hour
            today = time.strftime("%Y-%m-%d")
            disk_over = (snap["disk_pct"] / 100.0) >= config.DISK_CLEAN_AT
            hour_now = hour == config.CLEANUP_HOUR and done_day != today
            disk_now = disk_over and (time.time() - last_disk_run > 1800)
            if hour_now or disk_now:
                res = await cleanup(dry_run=False)
                removed = purge_trash()
                if hour_now:
                    done_day = today
                if disk_now:
                    last_disk_run = time.time()
                if notify and (res.get("moved") or removed):
                    msg = ("🧹 ARES: уборка\n" +
                           (f"в .trash: {', '.join(res['moved']) or '—'}\n" if res.get("moved") else "") +
                           (f"удалено из .trash: {', '.join(removed) or '—'}" if removed else "")).strip()
                    try:
                        await notify(msg)
                    except Exception:
                        pass
            if time.time() - last_purge > 3600:
                purge_trash()
                last_purge = time.time()
        except Exception:
            pass
        await asyncio.sleep(300)
