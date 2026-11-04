"""ARES CODE — системные метрики для HUD (psutil)."""
import os
import time

import psutil

from core import config

_started = time.time()
psutil.cpu_percent(interval=None)  # первичный замер, чтобы следующий вернул реальное значение


def snapshot() -> dict:
    vm = psutil.virtual_memory()
    du = psutil.disk_usage(str(config.WORKSPACE))
    try:
        load = [round(x, 2) for x in os.getloadavg()]
    except (AttributeError, OSError):
        load = []
    return {
        "cpu": psutil.cpu_percent(interval=None),
        "ram_pct": vm.percent,
        "ram_used": vm.used,
        "ram_total": vm.total,
        "disk_pct": du.percent,
        "disk_used": du.used,
        "disk_total": du.total,
        "procs": len(psutil.pids()),
        "uptime_s": int(time.time() - _started),
        "load": load,
    }
