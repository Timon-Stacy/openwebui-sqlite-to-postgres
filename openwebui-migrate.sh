#!/usr/bin/env bash
# Open WebUI: SQLite → PostgreSQL migration helper (data-only).
# Open WebUI owns the PostgreSQL schema; this tool copies data into it.
#
# Usage:
#   ./openwebui-migrate.sh check-pg        # verify PostgreSQL is reachable + schema present
#   ./openwebui-migrate.sh backup          # safe copy of the SQLite DB (or SKIP_BACKUP=1)
#   ./openwebui-migrate.sh inspect         # integrity + row counts
#   ./openwebui-migrate.sh migrate-python  # copy data into Open WebUI's existing schema
#   ./openwebui-migrate.sh data-only       # same thing (canonical name)
#   ./openwebui-migrate.sh validate        # compare row counts
#   ./openwebui-migrate.sh create-db       # CREATE DATABASE (before Open WebUI builds the schema)
#   ./openwebui-migrate.sh disk-check      # free space estimate for the backup step
#   ./openwebui-migrate.sh create-schema   # (optional) build the PG schema from SQLite yourself
#
# Docker:
#   docker compose build migrator
#   docker compose --profile tools run --rm migrator <command>
#
# Configure via .env next to this script (see .env.example).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="${WORK_DIR:-$SCRIPT_DIR/work}"
LOG_DIR="${LOG_DIR:-$WORK_DIR/logs}"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"

# --- Source SQLite (production path — READ ONLY for backup) ---
SOURCE_SQLITE="${SOURCE_SQLITE:-/path/to/webui.db}"

# --- Working copy (all migration experiments use this, never production) ---
CLEAN_SQLITE="${CLEAN_SQLITE:-$WORK_DIR/webui-clean.db}"
BACKUP_SQLITE="${BACKUP_SQLITE:-$WORK_DIR/webui-orig-${TIMESTAMP}.db}"

# --- PostgreSQL target (lab / staging) ---
DATABASE_URL="${DATABASE_URL:-}"

# --- Migration tuning ---
BATCH_SIZE="${BATCH_SIZE:-5000}"

# Comma-separated table names to migrate (all others are skipped)
KEEP_TABLES="${KEEP_TABLES:-}"

# Days of history to keep for chat tables (0 = all history)
RECENT_DAYS="${RECENT_DAYS:-0}"

# Clear target tables before copying (needed if they already hold rows)
TRUNCATE_TARGET="${TRUNCATE_TARGET:-0}"

# Optional: route psql via `docker exec <name>` instead of TCP (leave empty for TCP)
PG_CONTAINER="${PG_CONTAINER:-}"
NON_INTERACTIVE="${NON_INTERACTIVE:-0}"

# Load local overrides (.env on host; in Docker also /data/.env from mounted project dir)
if [[ -f "$SCRIPT_DIR/.env" ]]; then
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/.env"
fi
if [[ -f "/data/.env" ]]; then
  # shellcheck disable=SC1091
  source "/data/.env"
fi
if [[ -f /.dockerenv && -f "$SCRIPT_DIR/.env.docker" ]]; then
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/.env.docker"
fi

# Parse DATABASE_URL into internal PG_* vars used by psql helpers
parse_pg_url() {
  [[ -n "$DATABASE_URL" ]] || die "DATABASE_URL is not set. Copy .env.example to .env and configure it."
  local rest="${DATABASE_URL#postgresql://}"
  rest="${rest#postgres://}"
  local userinfo="${rest%%@*}"
  local hostpart="${rest#*@}"
  PG_USER="${userinfo%%:*}"
  PG_PASSWORD="${userinfo#*:}"
  local hostport="${hostpart%%/*}"
  PG_DATABASE="${hostpart#*/}"
  PG_DATABASE="${PG_DATABASE%%\?*}"  # strip query params
  if [[ "$hostport" == *:* ]]; then
    PG_HOST="${hostport%%:*}"
    PG_PORT="${hostport#*:}"
  else
    PG_HOST="$hostport"
    PG_PORT="5432"
  fi
}
parse_pg_url

# Inside migrator container: always TCP to PG_HOST (never docker exec from inside docker)
if [[ -f /.dockerenv ]]; then
  PG_CONTAINER=""
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${CYAN}[$(date +%H:%M:%S)]${NC} $*"; }
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC} $*"; }
die()  { echo -e "${RED}ERROR:${NC} $*" >&2; exit 1; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

mkdir_work() {
  mkdir -p "$WORK_DIR" "$LOG_DIR"
}

sqlite_cmd() {
  if [[ -n "${SQLITE3_BIN:-}" && -x "$SQLITE3_BIN" ]]; then
    echo "$SQLITE3_BIN"
  elif command -v sqlite3 >/dev/null 2>&1; then
    echo sqlite3
  else
    die "sqlite3 not found. Install sqlite3 or set SQLITE3_BIN."
  fi
}

psql_exec() {
  local sql="$1"
  if [[ -n "$PG_CONTAINER" ]]; then
    docker exec -e PGPASSWORD="$PG_PASSWORD" -i "$PG_CONTAINER" \
      psql -h 127.0.0.1 -U "$PG_USER" -d "$PG_DATABASE" -v ON_ERROR_STOP=1 -c "$sql"
  else
    PGPASSWORD="$PG_PASSWORD" psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DATABASE" -v ON_ERROR_STOP=1 -c "$sql"
  fi
}

psql_file() {
  local file="$1"
  if [[ -n "$PG_CONTAINER" ]]; then
    docker exec -e PGPASSWORD="$PG_PASSWORD" -i "$PG_CONTAINER" \
      psql -h 127.0.0.1 -U "$PG_USER" -d "$PG_DATABASE" -v ON_ERROR_STOP=1 -f - <"$file"
  else
    PGPASSWORD="$PG_PASSWORD" psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DATABASE" -v ON_ERROR_STOP=1 -f "$file"
  fi
}

psql_admin_exec() {
  local sql="$1"
  if [[ -n "$PG_CONTAINER" ]]; then
    docker exec -e PGPASSWORD="$PG_PASSWORD" -i "$PG_CONTAINER" \
      psql -h 127.0.0.1 -U "$PG_USER" -d postgres -v ON_ERROR_STOP=1 -c "$sql"
  else
    PGPASSWORD="$PG_PASSWORD" psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d postgres -v ON_ERROR_STOP=1 -c "$sql"
  fi
}

# Machine-readable query (no headers / row-count lines — avoids grep false positives)
psql_admin_scalar() {
  local sql="$1"
  if [[ -n "$PG_CONTAINER" ]]; then
    docker exec -e PGPASSWORD="$PG_PASSWORD" -i "$PG_CONTAINER" \
      psql -h 127.0.0.1 -U "$PG_USER" -d postgres -tA -v ON_ERROR_STOP=1 -c "$sql"
  else
    PGPASSWORD="$PG_PASSWORD" psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d postgres -tA -v ON_ERROR_STOP=1 -c "$sql"
  fi
}

psql_scalar() {
  local sql="$1"
  if [[ -n "$PG_CONTAINER" ]]; then
    docker exec -e PGPASSWORD="$PG_PASSWORD" -i "$PG_CONTAINER" \
      psql -h 127.0.0.1 -U "$PG_USER" -d "$PG_DATABASE" -tA -v ON_ERROR_STOP=1 -c "$sql"
  else
    PGPASSWORD="$PG_PASSWORD" psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DATABASE" -tA -v ON_ERROR_STOP=1 -c "$sql"
  fi
}

require_source() {
  [[ -f "$SOURCE_SQLITE" ]] || die "SOURCE_SQLITE not found: $SOURCE_SQLITE (set in .env)"
}

require_clean_copy() {
  # SKIP_BACKUP=1 → use SOURCE_SQLITE directly (experiments only, not production)
  if [[ "${SKIP_BACKUP:-0}" == "1" ]]; then
    require_source
    CLEAN_SQLITE="$SOURCE_SQLITE"
    # Also skip the full integrity check — it reads every page of a large DB
    export SKIP_INTEGRITY_CHECK=1
    warn "SKIP_BACKUP=1: using source db directly — safe only if Open WebUI is stopped"
    return 0
  fi
  [[ -f "$CLEAN_SQLITE" ]] || die "Clean SQLite copy missing.
  Run backup: $0 backup
  Or skip it: SKIP_BACKUP=1 $0 <command>"
  local sqlite tables
  sqlite="$(sqlite_cmd)"
  tables=$("$sqlite" "$CLEAN_SQLITE" "SELECT count(*) FROM sqlite_master WHERE type='table';" 2>/dev/null || echo "0")
  if [[ "$tables" == "0" ]]; then
    die "Clean copy exists but has no tables: $CLEAN_SQLITE
Run backup first:  $0 backup
Or skip it:        SKIP_BACKUP=1 $0 migrate-python"
  fi
  log "Clean copy: $CLEAN_SQLITE ($tables tables)"
}

pg_isready_check() {
  local db="${1:-$PG_DATABASE}"
  if [[ -n "$PG_CONTAINER" ]]; then
    docker exec "$PG_CONTAINER" pg_isready -h 127.0.0.1 -U "$PG_USER" -d "$db" >/dev/null 2>&1
  else
    need_cmd pg_isready
    PGPASSWORD="$PG_PASSWORD" pg_isready -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$db" >/dev/null 2>&1
  fi
}

database_exists() {
  [[ "$(psql_admin_scalar "SELECT 1 FROM pg_database WHERE datname = '${PG_DATABASE}'")" == "1" ]]
}

require_pg_server() {
  pg_isready_check postgres \
    || die "PostgreSQL server not ready (host=${PG_HOST}:${PG_PORT})"
}

require_pg() {
  require_pg_server
  database_exists \
    || die "Database '${PG_DATABASE}' does not exist on ${PG_HOST}:${PG_PORT}. Run: $0 create-db"
}

confirm_or_die() {
  local msg="$1"
  if [[ "$NON_INTERACTIVE" == "1" ]]; then
    log "NON_INTERACTIVE=1 — proceeding: $msg"
    return 0
  fi
  read -r -p "$msg [y/N] " confirm
  [[ "$confirm" =~ ^[Yy]$ ]] || die "Aborted by user"
}

cmd_disk_check() {
  require_source
  mkdir_work
  local sqlite_dir free_kb db_kb need_kb free_h need_h
  sqlite_dir="$(dirname "$SOURCE_SQLITE")"
  db_kb=$(du -k "$SOURCE_SQLITE" | cut -f1)
  need_kb=$((db_kb * 3))
  free_kb=$(df -k "$WORK_DIR" | awk 'NR==2 {print $4}')
  free_h=$(du -h "$WORK_DIR" 2>/dev/null | cut -f1 || echo "?")
  need_h=$(echo "$need_kb" | awk '{printf "%.1fG", $1/1024/1024}')

  log "SQLite size: $(du -h "$SOURCE_SQLITE" | cut -f1) ($(basename "$SOURCE_SQLITE"))"
  log "Recommend ≥3× SQLite size free in WORK_DIR for raw copy + clean rebuild"
  log "WORK_DIR=$WORK_DIR (free: $(df -h "$WORK_DIR" | awk 'NR==2 {print $4}'), need ~${need_h})"
  log "SQLite folder: $sqlite_dir (free: $(df -h "$sqlite_dir" | awk 'NR==2 {print $4}'))"

  if [[ "$free_kb" -lt "$need_kb" ]]; then
    die "Not enough free space in WORK_DIR. Need ~${need_h}, have $(df -h "$WORK_DIR" | awk 'NR==2 {print $4}')"
  fi
  ok "Disk space looks sufficient for backup step"
}

cmd_check_pg() {
  log "Checking PostgreSQL at ${PG_HOST}:${PG_PORT}..."
  if [[ -n "$PG_CONTAINER" ]]; then
    docker ps --filter "name=^${PG_CONTAINER}$" --format '{{.Names}} {{.Status}}' \
      || warn "Container $PG_CONTAINER not running"
  fi
  if pg_isready_check postgres; then
    ok "server is accepting connections"
  else
    die "Cannot reach PostgreSQL. Start compose: docker compose up -d postgres"
  fi
  if database_exists; then
    ok "database '$PG_DATABASE' exists"
  else
    warn "database '$PG_DATABASE' not found — run: $0 create-db"
    return 0
  fi
  local tables
  tables=$(psql_scalar "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'" 2>/dev/null || echo "?")
  if [[ "$tables" == "0" ]]; then
    warn "schema is empty — start Open WebUI once with this DATABASE_URL so it builds the tables, then stop it"
  else
    ok "schema present ($tables tables) — ready for data-only migration"
  fi
}

cmd_create_db() {
  require_pg_server
  log "Target: ${PG_HOST}:${PG_PORT} (PG_CONTAINER=${PG_CONTAINER:-<tcp>})"
  log "Creating database '$PG_DATABASE' if missing..."

  if database_exists; then
    ok "Database '$PG_DATABASE' already exists"
  else
    psql_admin_exec "CREATE DATABASE \"${PG_DATABASE}\";"
    if database_exists; then
      ok "Database '$PG_DATABASE' created and verified"
    else
      die "CREATE DATABASE appeared to run but '${PG_DATABASE}' is still missing — check PG_HOST/PG_CONTAINER"
    fi
  fi

  echo ""
  echo "DATABASE_URL=$DATABASE_URL"
}

# Safe backup — never copy .db while WAL may be ahead of main file.
# Rebuild via .dump into a new file.
cmd_backup() {
  require_source
  mkdir_work
  local sqlite
  sqlite="$(sqlite_cmd)"

  log "Backing up production SQLite (read-only dump, excludes stale WAL-only data)"
  log "Source: $SOURCE_SQLITE"
  log "Backup archive: $BACKUP_SQLITE"
  log "Clean working copy: $CLEAN_SQLITE"

  cp -a "$SOURCE_SQLITE" "$BACKUP_SQLITE"
  ok "Raw file copy saved to $BACKUP_SQLITE"

  rm -f "$CLEAN_SQLITE"
  log "Rebuilding consistent database via sqlite3 .dump (this can take a while on large DBs)..."

  "$sqlite" "file:${SOURCE_SQLITE}?mode=ro" ".dump" | "$sqlite" "$CLEAN_SQLITE"

  log "Running integrity check on clean copy..."
  local result
  result=$("$sqlite" "$CLEAN_SQLITE" "PRAGMA integrity_check;")
  [[ "$result" == "ok" ]] || die "Integrity check failed: $result"
  ok "Integrity check passed"

  local size_orig size_clean
  size_orig=$(du -h "$BACKUP_SQLITE" | cut -f1)
  size_clean=$(du -h "$CLEAN_SQLITE" | cut -f1)
  ok "Backup complete — orig: $size_orig, clean: $size_clean"
  warn "All migration steps must use CLEAN_SQLITE=$CLEAN_SQLITE, not production."
}

# Step 2: Inspect the working copy
cmd_inspect() {
  require_clean_copy
  local sqlite
  sqlite="$(sqlite_cmd)"

  log "SQLite file: $CLEAN_SQLITE"
  "$sqlite" "$CLEAN_SQLITE" "SELECT sqlite_version() AS sqlite_version;"

  log "Integrity + quick check"
  "$sqlite" "$CLEAN_SQLITE" "PRAGMA integrity_check;"
  "$sqlite" "$CLEAN_SQLITE" "PRAGMA quick_check;"

  log "Foreign key violations (orphaned chat_file/knowledge_file rows are common)"
  "$sqlite" "$CLEAN_SQLITE" "PRAGMA foreign_key_check;" || true

  log "Row counts per table"
  "$sqlite" "$CLEAN_SQLITE" "
    SELECT 'SELECT ''' || name || ''' AS table_name, COUNT(*) AS rows FROM \"' || name || '\";' 
    FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';
  " | "$sqlite" "$CLEAN_SQLITE" -header -column
}

# Step 4a: Python migration (our migrate_sqlite_to_pg.py)
migrate_python_bin() {
  need_cmd python3
  local py="$SCRIPT_DIR/migrate_sqlite_to_pg.py"
  [[ -f "$py" ]] || die "Missing $py"
  if ! python3 -c "import psycopg2" 2>/dev/null; then
    if [[ -f /.dockerenv ]]; then
      die "psycopg2 missing in migrator image — rebuild: docker compose build migrator"
    fi
    log "Installing psycopg2-binary..."
    python3 -m pip install -q -r "$SCRIPT_DIR/requirements.txt"
  fi
  python3 -u "$py" "$@"
}

cmd_create_schema() {
  require_clean_copy
  require_pg_server
  if ! database_exists; then
    warn "Database '$PG_DATABASE' missing — creating it now"
    cmd_create_db
  fi
  export SQLITE_DB_PATH="$CLEAN_SQLITE"
  export DATABASE_URL
  export KEEP_TABLES
  [[ -n "$KEEP_TABLES" ]] && log "Keeping only tables: $KEEP_TABLES"
  log "Creating PostgreSQL schema from SQLite..."
  migrate_python_bin --create-schema
  ok "Schema ready"
}

# Data-only migration: schema/DB already exist (created by Open WebUI on first run).
# This is the canonical migration path; `migrate-python` is an alias for it.
cmd_data_only() {
  require_clean_copy
  require_pg

  export SQLITE_DB_PATH="$CLEAN_SQLITE"
  export DATABASE_URL
  export BATCH_SIZE="${BATCH_SIZE:-5000}"
  export KEEP_TABLES
  export RECENT_DAYS
  export TRUNCATE_TARGET

  mkdir_work
  local logfile="$LOG_DIR/data-only-${TIMESTAMP}.log"
  local masked_url="${DATABASE_URL//:${PG_PASSWORD}@/:***@}"
  log "Data-only migration (schema owned by Open WebUI) → $logfile"
  log "SQLITE_DB_PATH=$SQLITE_DB_PATH"
  log "DATABASE_URL=$masked_url"
  [[ -n "$KEEP_TABLES" ]]  && log "Keeping only tables: $KEEP_TABLES"
  [[ "$RECENT_DAYS" != "0" ]] && log "Recent filter: last ${RECENT_DAYS} days for chat tables"
  [[ "$TRUNCATE_TARGET" == "1" ]] && log "Will TRUNCATE target tables before copying"

  confirm_or_die "Proceed with data-only migration?"
  migrate_python_bin | tee "$logfile"

  ok "Data migration finished. Run: $0 validate"
}

# Step 6: Validate row counts
cmd_validate() {
  require_clean_copy
  require_pg

  export SQLITE_DB_PATH="$CLEAN_SQLITE"
  export DATABASE_URL
  export KEEP_TABLES

  migrate_python_bin --validate
}

cmd_help() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  echo ""
  echo "Environment / .env variables (see .env.example):"
  echo "  DATABASE_URL        target PostgreSQL connection string"
  echo "  SOURCE_SQLITE       path to your webui.db"
  echo "  WORK_DIR            working dir for the backup copy + logs"
  echo "  KEEP_TABLES         migrate only these tables (+ FK deps)"
  echo "  RECENT_DAYS         migrate only the last N days of chats"
  echo "  BATCH_SIZE          rows per streaming batch (default 5000)"
  echo "  SKIP_BACKUP=1       read SOURCE_SQLITE directly (Open WebUI must be stopped)"
  echo "  NON_INTERACTIVE=1   don't prompt for confirmation"
}

main() {
  local cmd="${1:-help}"
  shift || true
  case "$cmd" in
    check-pg)         cmd_check_pg "$@" ;;
    backup)           cmd_backup "$@" ;;
    inspect)          cmd_inspect "$@" ;;
    migrate-python)   cmd_data_only "$@" ;;   # alias — migration is data-only
    data-only)        cmd_data_only "$@" ;;
    validate)         cmd_validate "$@" ;;
    create-db)        cmd_create_db "$@" ;;
    disk-check)       cmd_disk_check "$@" ;;
    create-schema)    cmd_create_schema "$@" ;;
    help|-h|--help)   cmd_help ;;
    *) die "Unknown command: $cmd. Run: $0 help" ;;
  esac
}

main "$@"
