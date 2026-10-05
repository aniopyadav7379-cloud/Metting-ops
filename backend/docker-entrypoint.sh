#!/bin/sh
# Backend container entrypoint.
#
# Alembic is the canonical source of schema truth. Run migrations BEFORE
# starting uvicorn so the app never serves traffic against a schema that
# was raced ahead by SQLAlchemy's Base.metadata.create_all (which silently
# creates tables WITHOUT the FK / index / default additions a real
# alembic revision would apply). If alembic fails, exit non-zero so the
# container's restart policy surfaces the problem instead of silently
# running on a partial schema.
set -e

echo "[entrypoint] Running alembic upgrade head..."
alembic upgrade head || { echo "[entrypoint] Alembic migration failed"; exit 1; }
echo "[entrypoint] Alembic upgrade head complete"

echo "[entrypoint] Starting uvicorn..."
exec python -m uvicorn main:app --host 0.0.0.0 --port 9050
