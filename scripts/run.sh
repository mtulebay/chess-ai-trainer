#!/usr/bin/env bash
# Обёртка для cron: правильная папка, venv, лог, защита от параллельного запуска.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
exec 9>logs/.lock
flock -n 9 || { echo "$(date -Is) уже запущено, пропускаю" >> logs/cron.log; exit 0; }
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3
echo "=== $(date -Is) $* ===" >> logs/cron.log
"$PY" -m chess_trainer "$@" >> logs/cron.log 2>&1
