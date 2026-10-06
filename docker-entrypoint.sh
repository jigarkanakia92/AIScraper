#!/bin/sh
# Entrypoint: wait for PostgreSQL, run Alembic migrations, then start the app.
#
# IMPORTANT: this file must stay LF-only. With CRLF line endings the shebang
# becomes "#!/bin/sh\r", the kernel cannot find that interpreter, and the
# container dies with:
#   [dumb-init] /usr/local/bin/docker-entrypoint.sh: No such file or directory
# `.gitattributes` pins LF for *.sh and the Dockerfile strips stray CRs at
# build time, so the file works no matter which OS checked it out.
set -eu

DB_HOST="${DB_HOST:-postgres-db}"
DB_PORT="${DB_PORT:-5432}"

echo "[entrypoint] Waiting for PostgreSQL at ${DB_HOST}:${DB_PORT} ..."
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

if [ "${SKIP_MIGRATIONS:-0}" = "1" ]; then
    echo "[entrypoint] SKIP_MIGRATIONS=1 - not running Alembic migrations"
else
    echo "[entrypoint] Running Alembic migrations ..."
    alembic upgrade head
fi

echo "[entrypoint] Starting: $*"
exec "$@"
