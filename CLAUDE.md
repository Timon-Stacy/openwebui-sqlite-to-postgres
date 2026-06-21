# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A self-contained toolkit to copy an Open WebUI **SQLite** database into **PostgreSQL**. It is **data-only**: Open WebUI itself creates the PostgreSQL schema (via Alembic on first run); this tool only streams the data in. This avoids the Alembic conflicts that schema-creating migrators hit (e.g. `extract(epoch from created_at)` on an already-`BIGINT` column).

## Running the migrator

**Docker:**
```bash
cp .env.example .env                                           # edit DATABASE_URL + SOURCE_SQLITE
docker compose build migrator
docker compose --profile tools run --rm migrator check-pg
docker compose --profile tools run --rm migrator backup        # or set SKIP_BACKUP=1
docker compose --profile tools run --rm migrator migrate-python
docker compose --profile tools run --rm migrator validate
```

**Host-native:**
```bash
cp .env.example .env
pip install -r requirements.txt
./openwebui-migrate.sh check-pg
./openwebui-migrate.sh backup        # or SKIP_BACKUP=1
./openwebui-migrate.sh migrate-python
./openwebui-migrate.sh validate
```

**Python script directly (no shell, no .env):**
```bash
SQLITE_DB_PATH=webui.db DATABASE_URL=postgresql://... \
  python3 migrate_sqlite_to_pg.py --dry-run        # instant plan, no data touched
  python3 migrate_sqlite_to_pg.py                  # migrate (data-only)
  python3 migrate_sqlite_to_pg.py --validate       # compare row counts
```

## Environment / config

A single `DATABASE_URL` drives everything; the shell parses it internally into host/port/user/password/database for `psql`. Copy `.env.example` to `.env`. Key variables:

- `DATABASE_URL` — target PostgreSQL (schema already created by Open WebUI)
- `SOURCE_SQLITE` — source `webui.db` (shell wrapper). Direct-Python uses `SQLITE_DB_PATH`.
- `KEEP_TABLES=a,b,c` — migrate only these tables; their FK dependencies are auto-included (`expand_keep_tables`).
- `RECENT_DAYS=N` — copy only the last N days of chats and attached rows.
- `SKIP_BACKUP=1` — read `SOURCE_SQLITE` directly instead of making a backup copy (also skips the integrity check). Safe because Open WebUI must be stopped anyway.
- `INTEGRITY_CHECK=1` — opt in to `PRAGMA integrity_check` (slow on multi-GB DBs; off by default).

## Architecture

**`openwebui-migrate.sh`** is a pure orchestrator — validates preconditions, sets exports, delegates to `migrate_sqlite_to_pg.py`. Runs Python with `-u` (unbuffered) so progress shows through `tee`. Canonical flow:

```
check-pg → backup → migrate-python → validate
```

`create-schema` / `--with-schema` remain as an **optional** escape hatch for when you are *not* letting Open WebUI build the schema; they are not part of the default path.

**`migrate_sqlite_to_pg.py`** is the core engine. The live PostgreSQL schema is the source of truth for types:

- `sqlite_table_list()` — topological sort using `TABLE_ORDER` + `TABLE_DEPS` for FK-safe ordering.
- `pg_column_info()` — reads `information_schema` for each target table's column types **and** nullability.
- `migrate_table()` — streams rows with a server-side cursor (`fetchmany`, no `OFFSET` re-scan) and bulk-loads via `COPY … FROM STDIN (FORMAT csv)` using `CopyStream`. Per-column value coercion (`bool`, `json`, NULL→`''`/`'{}'` for NOT NULL) is derived from the live PG type. Does **not** truncate — data is appended.
- `build_where_clause()` — implements `RECENT_DAYS` filters (direct on `chat`, via `chat_id` subquery, and via `chat_file → chat` for `file`).
- `reset_sequences()` — advances identity/serial sequences to `MAX(id)` after load.

Loads run with `SET session_replication_role = replica` (FK triggers off, so orphaned children still copy) and `SET synchronous_commit = off`.

**Key constants when adding tables/columns:**

| Constant | Purpose |
|---|---|
| `TABLE_ORDER` | Explicit insert order for FK safety |
| `TABLE_DEPS` | Per-table FK dependency list (topological sort + `KEEP_TABLES` expansion) |
| `RECENT_CHAT_DIRECT` / `RECENT_CHAT_VIA_ID` / `RECENT_CHAT_VIA_FILE` | Which tables the `RECENT_DAYS` filter applies to, and how |
| `SKIP_SQLITE_TABLES` | Tables never migrated (`alembic_version`, `migratehistory`, `sqlite_sequence`) |

Type handling is automatic (it follows the PG schema), so most new tables only need an entry in `TABLE_ORDER` (and `TABLE_DEPS` if they have foreign keys).

## Important behaviors

- **Target tables must be empty.** COPY appends; `session_replication_role=replica` disables FK triggers but **not** primary-key enforcement, so loading into a non-empty table fails on duplicate keys.
- **Open WebUI must be stopped** during migration — it shares the target database.
- The source SQLite is opened `mode=ro` and never modified.

## Docker topology

`docker-compose.yml` is a self-contained stack on a compose-managed network (`ainet`):
- `postgres` — PostgreSQL (`postgres:16-alpine`, overridable via `$POSTGRES_IMAGE`), db `openwebui`, host port `5433`.
- `open-webui` — `ghcr.io/open-webui/open-webui:main` pointed at `postgres`. Start it once to let Alembic build the schema, then **stop it before migrating**. Port `${OPENWEBUI_PORT:-3000}:8080`.
- `migrator` — profile `tools`, mounts `.:/data`, entrypoint is the **mounted** `/data/openwebui-migrate.sh` so edits apply without rebuilding. `DATABASE_URL` defaults to the bundled `postgres`, overridable via `.env`.

For a migration into an existing PostgreSQL, set `DATABASE_URL` in `.env` and skip the `open-webui` service.
