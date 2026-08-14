#!/usr/bin/env sh
# Точка входа всех контейнеров проекта.
# Миграции накатываются до запуска процесса, чтобы код никогда не стартовал
# на схеме старее себя.
set -e

ROLE="${1:-bot}"

wait_for_db() {
  echo "[entrypoint] ждём postgres..."
  for i in $(seq 1 60); do
    if python -c "
import asyncio, sys
from sqlalchemy import text
from app.db.session import get_engine

async def check():
    async with get_engine().connect() as c:
        await c.execute(text('SELECT 1'))

try:
    asyncio.run(check())
except Exception:
    sys.exit(1)
" 2>/dev/null; then
      echo "[entrypoint] postgres доступен"
      return 0
    fi
    sleep 1
  done
  echo "[entrypoint] ОШИБКА: postgres не поднялся за 60 секунд" >&2
  exit 1
}

run_migrations() {
  echo "[entrypoint] текущая ревизия базы:"
  alembic current
  echo "[entrypoint] накатываем миграции (alembic upgrade head)..."
  alembic upgrade head
  echo "[entrypoint] ревизия после наката:"
  alembic current
}

case "$ROLE" in
  bot)
    wait_for_db
    # Миграции гоняет только бот: два процесса одновременно поднимут гонку блокировок.
    run_migrations
    if [ "${ENV:-dev}" = "dev" ]; then
      exec watchfiles --filter python "python -m app.bot.main" app
    else
      exec python -m app.bot.main
    fi
    ;;
  api)
    wait_for_db
    if [ "${ENV:-dev}" = "dev" ]; then
      exec uvicorn app.api.main:app --host "${API_HOST:-0.0.0.0}" --port "${API_PORT:-8080}" --reload --reload-dir app
    else
      exec uvicorn app.api.main:app --host "${API_HOST:-0.0.0.0}" --port "${API_PORT:-8080}"
    fi
    ;;
  worker)
    wait_for_db
    if [ "${ENV:-dev}" = "dev" ]; then
      exec watchfiles --filter python "arq app.worker.main.WorkerSettings" app
    else
      exec arq app.worker.main.WorkerSettings
    fi
    ;;
  migrate)
    wait_for_db
    run_migrations
    ;;
  *)
    exec "$@"
    ;;
esac
