#!/bin/sh
# Backend container entrypoint.

set -e

echo "[entrypoint] Running alembic upgrade head..."
alembic -c /app/alembic.ini upgrade head || {
    echo "[entrypoint] Alembic migration failed"
    exit 1
}
echo "[entrypoint] Alembic upgrade head complete"

echo "[entrypoint] Starting uvicorn..."
exec python -m uvicorn main:app --host 0.0.0.0 --port "${PORT:-9050}"
