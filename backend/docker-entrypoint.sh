#!/bin/sh

set -e

echo "[entrypoint] Checking database..."

TABLE_COUNT=$(python - <<'PY'
from database.database import engine
from sqlalchemy import inspect

print(len(inspect(engine).get_table_names()))
PY
)

if [ "$TABLE_COUNT" = "0" ]; then
    echo "[entrypoint] Fresh database detected."
    echo "[entrypoint] Enabling PostgreSQL extensions..."

    python - <<'PY'
from database.database import engine
from sqlalchemy import text

with engine.begin() as connection:
    connection.execute(text("CREATE EXTENSION IF NOT EXISTS citext"))

print("[entrypoint] PostgreSQL extensions enabled.")
PY

    echo "[entrypoint] Creating current application schema..."

    python - <<'PY'
import database.models  # noqa: F401
import database.models_rooms  # noqa: F401
import auth.models  # noqa: F401
import models.agent_system  # noqa: F401

from database.database import engine, Base

Base.metadata.create_all(bind=engine)

print("[entrypoint] Current application schema created.")
PY

    echo "[entrypoint] Stamping schema at Alembic head..."
    alembic -c /app/alembic.ini stamp head
else
    echo "[entrypoint] Existing database detected."
    echo "[entrypoint] Running Alembic migrations..."
    alembic -c /app/alembic.ini upgrade head
fi

echo "[entrypoint] Database initialization complete."
echo "[entrypoint] Starting uvicorn..."

exec python -m uvicorn main:app --host 0.0.0.0 --port "${PORT:-9050}"