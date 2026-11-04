"""ARES CODE — локальное горячее хранение: zip + passport.json + sha256."""
import hashlib
import json
import os
import time
import zipfile
from pathlib import Path

from core import config, db

EXCLUDE_DIRS = {"__pycache__", "node_modules", ".git", ".venv", "venv", ".cache", ".tmp", ".trash", ".archives"}


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def make_archive(project: str) -> dict:
    project = "".join(c for c in project if c.isalnum() or c in "-_") or "default"
    src = (config.WORKSPACE / project).resolve()
    if not src.is_dir():
        raise FileNotFoundError(f"проекта нет: workspace/{project}")

    ts = time.strftime("%Y%m%d_%H%M%S")
    zip_path = config.ARCHIVE_DIR / f"{project}_{ts}.zip"
    files = []
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, names in os.walk(src):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            for n in sorted(names):
                if n == ".env":  # секреты в архивы не попадают
                    continue
                full = Path(root) / n
                rel = full.relative_to(src)
                try:
                    z.write(full, arcname=str(rel))
                except OSError:
                    continue
                files.append({"path": str(rel), "size": full.stat().st_size})

    passport = {
        "ares_version": "1.0",
        "project": project,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "file_count": len(files),
        "total_size": sum(f["size"] for f in files),
        "files": files,
    }
    with zipfile.ZipFile(zip_path, "a", zipfile.ZIP_DEFLATED) as z:
        z.writestr("passport.json", json.dumps(passport, ensure_ascii=False, indent=2))

    sha = sha256_file(zip_path)
    size = zip_path.stat().st_size
    row = db.add_archive(project, str(zip_path), sha, size)
    return dict(row)


def restore_archive(row) -> dict:
    """Распаковать архив row (локальный файл) в workspace."""
    src = Path(row["path"])
    if not src.exists():
        raise FileNotFoundError("файл архива не найден (возможно, только в холодном хранилище)")
    dest = config.WORKSPACE / row["project"]
    if dest.exists():
        dest = config.WORKSPACE / f"{row['project']}__restore_{time.strftime('%Y%m%d_%H%M%S')}"
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    with zipfile.ZipFile(src) as z:
        for member in z.namelist():
            if member == "passport.json":
                continue
            # защита от zip-slip
            target = (dest / member).resolve()
            if not str(target).startswith(str(dest.resolve())):
                continue
            z.extract(member, dest)
            n += 1
    return {"restored_to": str(dest.relative_to(config.WORKSPACE)), "files": n}
