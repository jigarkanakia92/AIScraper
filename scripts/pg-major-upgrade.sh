#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Carry AIScraper's PostgreSQL rows across a major version bump (e.g. 15 -> 16).
#
# PostgreSQL cannot read a data directory written by an older major version:
#
#   FATAL:  database files are incompatible with server
#   DETAIL: The data directory was initialized by PostgreSQL version 15,
#           which is not compatible with this version 16.x.
#
# docker-compose.yml gives each major version its own volume (-> pgdata16), so
# the new server always starts clean. This script copies your existing rows
# into that fresh cluster with dump & restore. The old volume is left intact.
#
# Usage:
#   bash scripts/pg-major-upgrade.sh
#   OLD_VOLUME=myproj_pgdata bash scripts/pg-major-upgrade.sh
#
# Environment:
#   OLD_VOLUME     data volume to read (default: auto-detected <project>_pgdata)
#   BACKUP_DIR     where the .sql dump is written (default: ./pg_upgrade_backup)
#   FORCE=1        restore even if the target database already has tables
#
# Requires: docker with the compose plugin. The stack is stopped and restarted.
# ---------------------------------------------------------------------------
set -euo pipefail

cd "$(dirname "$0")/.."

TMP_CONTAINER="aiscraper-pg-upgrade"
NEW_SERVICE="postgres-db"
BACKUP_DIR="${BACKUP_DIR:-pg_upgrade_backup}"

die() { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
say() { printf '\033[36m==>\033[0m %s\n' "$*"; }
ok()  { printf '\033[32m  ✓\033[0m %s\n' "$*"; }

command -v docker >/dev/null 2>&1 || die "docker is not on PATH"
docker compose version >/dev/null 2>&1 || die "the 'docker compose' plugin is required"

# --- credentials: .env wins, then compose defaults ------------------------
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi
DB_USER="${DB_USER:-aiscraper}"
DB_NAME="${DB_NAME:-aiscraper}"
DB_PASSWORD="${DB_PASSWORD:-aiscraper}"

# --- locate the old volume -------------------------------------------------
if [ -z "${OLD_VOLUME:-}" ]; then
    project="${COMPOSE_PROJECT_NAME:-$(basename "$PWD")}"
    project="$(printf '%s' "$project" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
    if docker volume inspect "${project}_pgdata" >/dev/null 2>&1; then
        OLD_VOLUME="${project}_pgdata"
    else
        # Fall back to any *_pgdata volume (covers a renamed project directory).
        OLD_VOLUME="$(docker volume ls --format '{{.Name}}' | grep -E '_pgdata$' | head -n1 || true)"
    fi
fi

[ -n "${OLD_VOLUME:-}" ] || die "no old 'pgdata' volume found — nothing to migrate. If you don't need the old rows, just run: docker compose up -d --build"
docker volume inspect "$OLD_VOLUME" >/dev/null 2>&1 || die "volume '$OLD_VOLUME' does not exist"

# --- which PostgreSQL wrote it? -------------------------------------------
old_pg="$(docker run --rm -v "${OLD_VOLUME}:/v:ro" alpine:3 sh -c 'cat /v/PG_VERSION 2>/dev/null || true' | tr -d '[:space:]')"
[ -n "$old_pg" ] || die "could not read PG_VERSION from volume '$OLD_VOLUME' (is it a PostgreSQL data volume?)"

new_pg="$(sed -n 's/.*image: *postgres:\([0-9]\+\).*/\1/p' docker-compose.yml | head -n1)"
[ -n "$new_pg" ] || die "could not determine the target PostgreSQL version from docker-compose.yml"

say "volume ${OLD_VOLUME} holds PostgreSQL ${old_pg}; compose targets PostgreSQL ${new_pg}"
[ "$old_pg" = "$new_pg" ] && die "versions already match — you do not need this script"

mkdir -p "$BACKUP_DIR"
stamp="$(date +%Y%m%d-%H%M%S)"
dumpfile="${BACKUP_DIR}/${DB_NAME}-pg${old_pg}-${stamp}.sql"

cleanup() { docker rm -f "$TMP_CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# --- 1. stop the stack -----------------------------------------------------
say "stopping the stack (volumes are kept) ..."
docker compose down --remove-orphans >/dev/null
ok "stopped"

# --- 2. read the old data with a matching server ---------------------------
say "starting a temporary postgres:${old_pg}-alpine on the old volume ..."
cleanup
docker run -d --name "$TMP_CONTAINER" \
    -e POSTGRES_USER="$DB_USER" \
    -e POSTGRES_PASSWORD="$DB_PASSWORD" \
    -e POSTGRES_DB="$DB_NAME" \
    -v "${OLD_VOLUME}:/var/lib/postgresql/data" \
    "postgres:${old_pg}-alpine" >/dev/null

printf '  waiting for PostgreSQL %s to accept connections ' "$old_pg"
for _ in $(seq 1 60); do
    if docker exec "$TMP_CONTAINER" pg_isready -q -U "$DB_USER" >/dev/null 2>&1; then
        printf '\n'; ok "ready"; break
    fi
    printf '.'; sleep 1
done
docker exec "$TMP_CONTAINER" pg_isready -q -U "$DB_USER" >/dev/null 2>&1 \
    || { docker logs --tail 30 "$TMP_CONTAINER" || true; die "temporary server never became ready"; }

say "dumping database '${DB_NAME}' ..."
docker exec "$TMP_CONTAINER" pg_dump \
    -U "$DB_USER" -d "$DB_NAME" \
    --no-owner --no-privileges --clean --if-exists \
    > "$dumpfile"
[ -s "$dumpfile" ] || die "dump is empty — aborting before touching anything else"
ok "$(wc -l < "$dumpfile" | tr -d ' ') lines -> $dumpfile"

cleanup
ok "temporary server removed (your ${old_pg} data is untouched)"

# --- 3. bring up the new cluster ------------------------------------------
say "starting postgres:${new_pg}-alpine on a fresh volume ..."
docker compose up -d "$NEW_SERVICE" >/dev/null

printf '  waiting for PostgreSQL %s to accept connections ' "$new_pg"
for _ in $(seq 1 90); do
    if docker compose exec -T "$NEW_SERVICE" pg_isready -q -U "$DB_USER" >/dev/null 2>&1; then
        printf '\n'; ok "ready"; break
    fi
    printf '.'; sleep 1
done
docker compose exec -T "$NEW_SERVICE" pg_isready -q -U "$DB_USER" >/dev/null 2>&1 \
    || { docker compose logs --tail 30 "$NEW_SERVICE" || true; die "new server never became ready"; }

# --- 4. restore ------------------------------------------------------------
tables="$(docker compose exec -T "$NEW_SERVICE" psql -U "$DB_USER" -d "$DB_NAME" -tAc \
    "select count(*) from information_schema.tables where table_schema='public'" | tr -d '[:space:]')"
if [ "$tables" != "0" ] && [ "${FORCE:-0}" != "1" ]; then
    die "target database '${DB_NAME}' already has ${tables} table(s); refusing to overwrite. Re-run with FORCE=1 if you are sure."
fi

say "restoring into the fresh ${new_pg} cluster ..."
docker compose exec -T "$NEW_SERVICE" psql -U "$DB_USER" -d "$DB_NAME" \
    -v ON_ERROR_STOP=1 --quiet < "$dumpfile" >/dev/null
ok "restored"

rows="$(docker compose exec -T "$NEW_SERVICE" psql -U "$DB_USER" -d "$DB_NAME" -tAc \
    "select coalesce(sum(n_live_tup),0) from pg_stat_user_tables" | tr -d '[:space:]')"
ok "rows now in '${DB_NAME}': ${rows}"

cat <<EOF

Done. Next steps:
  1. Start everything (Alembic re-checks the schema, then the scheduler runs):
       docker compose up -d --build
  2. Once you are happy, reclaim the old volume's disk space:
       docker volume rm ${OLD_VOLUME}
  Backup kept at: ${dumpfile}
EOF
