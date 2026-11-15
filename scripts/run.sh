#!/usr/bin/env bash
# Обёртка для cron (Linux и macOS): папка проекта, venv, PATH для `claude`, лог, защита от параллельного запуска.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
mkdir -p logs
LOCK=logs/.lock.d
if ! mkdir "$LOCK" 2>/dev/null; then
  # Лок старше 3 часов считаем зависшим (например, после перезагрузки).
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +180 2>/dev/null)" ]; then
    rmdir "$LOCK" && mkdir "$LOCK"
  else
    echo "$(date '+%F %T') уже запущено, пропускаю" >> logs/cron.log
    exit 0
  fi
fi
trap 'rmdir "$LOCK"' EXIT
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3
echo "=== $(date '+%F %T') $* ===" >> logs/cron.log
"$PY" -m chess_trainer "$@" >> logs/cron.log 2>&1
