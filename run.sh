#!/usr/bin/env bash
# ARES CODE — запуск одним процессом (API + бот + фоновые задачи)
set -e
cd "$(dirname "$0")"
if [ ! -f .env ]; then
  echo "[ARES] Нет .env — скопируй .env.example в .env и заполни ключи."
  exit 1
fi
exec python3 api.py
