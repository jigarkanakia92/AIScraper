#!/bin/sh
# Entrypoint: wait for DB, run migrations, then start the app.
set -e

echo "[entrypoint] Waiting for PostgreSQL at ${DB_HOST:-postgres-db}:${DB_PORT:-5432}…"
python - <<'PY'
import os, socket, time
host = os.environ.get("DB_HOST", "postgres-db")
port = int(os.environ.get("DB_PORT", "5432"))
deadline = time.monotonic() + 90
while time.monotonic() < deadline:
    try:
        with socket.create_connection((host, port), timeout=2):
            print("[entrypoint] DB port is open")
            break
    except OSError:
        time.sleep(2)
else:
    raise SystemExit("[entrypoint] Timed out waiting for DB")
PY

echo "[entrypoint] Running Alembic migrations…"
alembic upgrade head

echo "[entrypoint] Starting app…"
exec "$@"
