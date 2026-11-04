"""ARES CODE — песочница-исполнитель команд.

Белый список бинарёв, чистое окружение (без секретов), rlimit по памяти/CPU/файлам,
таймаут с убийством группы процессов, ограничение числа параллельных воркеров,
работа только внутри /workspace/<проект>, вывод — «хвост, а не середина».
"""
import asyncio
import os
import shlex
import signal
import subprocess
import resource
import time
from pathlib import Path

from core import config

# Ядро по ТЗ + безопасные утилиты, нужные агенту в CODE-режиме.
WHITELIST = {
    "python3", "python", "pip", "pip3", "node", "npm", "npx", "git", "curl",
    "ffmpeg", "zip", "unzip", "sqlite3", "ls", "cat", "head", "tail", "grep",
    "find", "echo", "printf", "mkdir", "touch", "cp", "mv", "rm", "rmdir",
    "pwd", "wc", "sed", "awk", "tar", "jq", "diff", "sort", "uniq", "stat",
    "du", "df", "chmod", "true", "false",
}

# Подстроки, которые запрещены в команде даже внутри белого списка
FORBIDDEN_SUBSTR = (".env", "id_rsa", "/.ssh", "ares.db", "/proc/", "/sys/")


class ExecResult(dict):
    @property
    def exit(self):
        return self.get("exit")


# не больше EXEC_WORKERS одновременных процессов (в т.ч. фоновые воркеры агента)
_sem = asyncio.Semaphore(max(1, config.EXEC_WORKERS))

_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": str(config.WORKSPACE),
    "LANG": "C.UTF-8",
    "PYTHONUNBUFFERED": "1",
    "TMPDIR": str(config.WORKSPACE / ".tmp"),
}


def _preexec_limits():
    mem = max(64, config.EXEC_MEM_MB) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
    cpu = max(5, config.EXEC_TIMEOUT + 10)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024 * 1024, 256 * 1024 * 1024))
    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
    except (ValueError, OSError):
        pass


def _reject(cmd: str, reason: str) -> ExecResult:
    return ExecResult(cmd=cmd, argv=[], exit=126, out="", err=reason,
                      duration_ms=0, timeout=False, rejected=True)


def _guard_paths(argv, project_dir: Path):
    """Абсолютные пути в аргументах — только внутри проекта или его рабочих каталогов."""
    allowed_roots = [str(project_dir), str(config.WORKSPACE / ".tmp"), str(config.TRASH_DIR), str(config.ARCHIVE_DIR)]
    for a in argv[1:]:
        if a.startswith("/"):
            real = os.path.realpath(a)
            if not any(real == r or real.startswith(r.rstrip("/") + "/") for r in allowed_roots):
                raise PermissionError(f"путь вне песочницы: {a}")


async def run(cmd, project: str = "default", timeout: int | None = None) -> ExecResult:
    """Выполнить команду в песочнице. cmd — строка или список аргументов."""
    t0 = time.time()
    if isinstance(cmd, str):
        cmd_s = cmd.strip()
        try:
            argv = shlex.split(cmd_s)
        except ValueError as e:
            return _reject(cmd_s, f"не удалось разобрать команду: {e}")
    else:
        argv = [str(x) for x in cmd]
        cmd_s = " ".join(argv)

    if not argv:
        return _reject(cmd_s, "пустая команда")

    project = "".join(c for c in project if c.isalnum() or c in "-_") or "default"
    project_dir = (config.WORKSPACE / project).resolve()
    project_dir.mkdir(parents=True, exist_ok=True)
    (config.WORKSPACE / ".tmp").mkdir(parents=True, exist_ok=True)

    # --- проверки безопасности ---
    binbase = os.path.basename(argv[0])
    if binbase not in WHITELIST:
        return _reject(cmd_s, f"запрещено: '{binbase}' нет в белом списке исполнителя")
    joined = " ".join(argv)
    for bad in FORBIDDEN_SUBSTR:
        if bad in joined:
            return _reject(cmd_s, f"запрещено: доступ к '{bad}' из песочницы закрыт")
    try:
        _guard_paths(argv, project_dir)
    except PermissionError as e:
        return _reject(cmd_s, str(e))

    tmo = int(timeout or config.EXEC_TIMEOUT)
    timeouted = False

    async with _sem:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(project_dir), env=_ENV,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                start_new_session=True, preexec_fn=_preexec_limits,
            )
        except FileNotFoundError as e:
            return _reject(cmd_s, f"бинарник не найден: {e}")

        out_task = asyncio.create_task(proc.stdout.read())
        err_task = asyncio.create_task(proc.stderr.read())
        try:
            await asyncio.wait_for(proc.wait(), timeout=tmo)
        except asyncio.TimeoutError:
            timeouted = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)  # start_new_session => pgid == pid
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        try:
            out_b, err_b = await asyncio.wait_for(asyncio.gather(out_task, err_task), timeout=15)
        except asyncio.TimeoutError:
            out_task.cancel(); err_task.cancel()
            out_b, err_b = b"", b""

    code = proc.returncode if proc.returncode is not None else (124 if timeouted else -1)
    if timeouted:
        code = 124

    out = (out_b or b"").decode("utf-8", "replace")
    err = (err_b or b"").decode("utf-8", "replace")

    # хвосты: exit==0 — EXEC_OUT_MAX_LINES, при ошибке — до 100 строк; середину не режем
    out = _tail(out, 100 if (code != 0 or timeouted) else config.EXEC_OUT_MAX_LINES)
    err = _tail(err, 400)

    return ExecResult(
        cmd=cmd_s, argv=argv, exit=code, out=out.strip(), err=err.strip(),
        duration_ms=int((time.time() - t0) * 1000), timeout=timeouted, rejected=False,
    )


def _tail(text: str, n: int) -> str:
    lines = text.splitlines()
    if len(lines) > n:
        return "\n".join(lines[-n:]) + f"\n…(показан хвост {n} из {len(lines)} строк)"
    return text


def filter_result(res: ExecResult, max_lines: int | None = None) -> str:
    """Компактное представление результата для наблюдения агента (ECONOMY — 10 строк)."""
    n = int(max_lines or config.EXEC_OUT_MAX_LINES)
    parts = [f"$ {res['cmd']}  → exit {res['exit']} ({res['duration_ms']} ms)"]
    if res.get("rejected"):
        parts.append(f"ОТКАЗ: {res['err']}")
        return "\n".join(parts)
    if res["out"]:
        lines = res["out"].splitlines()
        if len(lines) > n:
            lines = lines[-n:] + [f"…(хвост {n} из {len(lines)})"]
        parts.append("\n".join(lines))
    if res["err"]:
        parts.append("STDERR: " + res["err"][:800])
    if res.get("timeout"):
        parts.append("ПРИЧИНА: превышен таймаут")
    return "\n".join(parts)
