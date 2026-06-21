<div align="center">

# Open WebUI · SQLite → PostgreSQL Migrator

**Move your [Open WebUI](https://github.com/open-webui/open-webui) database from SQLite to PostgreSQL — safely, selectively, and without fighting Alembic.**

[![Python](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-13%2B-336791.svg)](https://www.postgresql.org/)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED.svg)](https://www.docker.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](#-license)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](#-contributing)

</div>

---

## ✨ Why this exists

Open WebUI supports PostgreSQL out of the box — but it has **no built-in path to move your existing SQLite data over**. The community workarounds (`pgloader`, hand-rolled dumps) tend to break on the same wall:

> Open WebUI's schema is managed by **Alembic**. If a migration tool also creates the schema, Alembic later tries to "fix" columns it didn't make — e.g. `extract(epoch from created_at)` on a column that's *already* a `BIGINT` — and the app fails to start.

This tool takes the opposite approach and sidesteps the conflict entirely:

> **Let Open WebUI create the schema. We only copy the data in.**

You point a fresh Open WebUI at PostgreSQL once (it builds every table and index via Alembic), stop it, and run this tool to stream your SQLite rows into that schema. No DDL fights, no type guesswork — the live PostgreSQL schema is the single source of truth.

---

## 🚀 Features

- 🎯 **Data-only by default** — Open WebUI owns the schema; this tool just fills it. Zero Alembic conflicts.
- 🧠 **Schema-aware coercion** — reads the *live* PostgreSQL column types and nullability to convert SQLite values correctly: `0/1 → boolean`, text → `JSONB`, `NULL → ''` for `NOT NULL` columns, null-byte stripping.
- 🗂️ **Selective migration** — `KEEP_TABLES` migrates only the tables you list (plus their foreign-key dependencies, resolved automatically).
- ⏱️ **Incremental / recent-only** — `RECENT_DAYS=7` copies only the last week of chats and everything attached to them (messages, tags, files). Perfect for a staged cutover.
- 📊 **Live progress + ETA** — per-table progress bars, rows/sec, and time estimates, all on `stderr` so logs stay clean.
- ⚡ **Fast** — cursor streaming (no `OFFSET` re-scans), `COPY ... FROM STDIN`, memory-mapped SQLite reads, and FK enforcement disabled during load.
- 🐳 **Docker or host-native** — run it however you like; one `DATABASE_URL` configures both.
- 🔎 **Dry-run & validation** — preview the plan instantly, and compare row counts table-by-table after.

---

## 🧭 How it works

```mermaid
flowchart LR
    A[(SQLite<br/>webui.db)] -->|stream rows| M{{migrator}}
    S[Open WebUI<br/>first run on PG] -.->|creates schema<br/>via Alembic| P[(PostgreSQL)]
    M -->|COPY FROM STDIN| P
    M -.reads column types<br/>& nullability.-> P
```

1. **Open WebUI builds the schema** — start a fresh Open WebUI instance pointed at your empty PostgreSQL database. It runs Alembic and creates every table/index. Then stop it.
2. **The migrator copies data** — it introspects the PostgreSQL schema, reads your SQLite tables in foreign-key-safe order, normalizes each value to match the real target type, and bulk-loads via `COPY`.

---

## 📋 Prerequisites

- An existing Open WebUI **SQLite** database (`webui.db`).
- A **PostgreSQL** database whose schema was created by Open WebUI (start Open WebUI once with `DATABASE_URL` pointing at it, then stop it).
- **Docker** *or* Python 3.8+ with `pip`.

> [!IMPORTANT]
> **Stop Open WebUI before migrating.** The tool writes into the same database Open WebUI uses. A running instance holds locks and can interleave writes.

---

## 1️⃣ Step one — let Open WebUI create the schema (do this first!)

> [!CAUTION]
> **This tool does not create the database schema — Open WebUI does.** Before running any migration command, you **must** point a fresh Open WebUI at your target PostgreSQL and let it build the tables. Skip this and the migrator has nothing to copy into.

Point Open WebUI at PostgreSQL via its `DATABASE_URL`, **start it once**, and let it finish booting. On that first run it executes its Alembic migrations and creates every table and index. Then **stop it**.

```bash
# Standalone example — your own Open WebUI:
docker run --rm \
  -e DATABASE_URL=postgresql://user:pass@your-postgres:5432/openwebui \
  -p 3000:8080 ghcr.io/open-webui/open-webui:main
# Open http://localhost:3000 once so it finishes the migrations, then Ctrl-C / stop it.
```

Using the bundled stack? `docker compose up -d postgres open-webui` does exactly this for you (see [Quick start](#-quick-start-docker)).

Only **after** Open WebUI has created the schema (and is stopped) do you run this migrator.

---

## ⚡ Quick start (Docker)

The bundled `docker-compose.yml` is a **complete stack** — PostgreSQL, Open WebUI, and the migrator — so the whole data-only flow runs end to end:

```bash
# 0. Put your existing webui.db next to this file, then configure
cp .env.example .env

# 1. Bring up PostgreSQL + Open WebUI. Open WebUI builds the schema via Alembic.
docker compose up -d postgres open-webui      # Open WebUI on http://localhost:3000

# 2. Once it's up, STOP Open WebUI so it doesn't hold the database
docker compose stop open-webui

# 3. Copy your SQLite data into the schema Open WebUI just created.
#    TRUNCATE_TARGET=1 clears the rows Open WebUI seeds on first boot (e.g. `config`)
#    so the COPY doesn't hit a duplicate-key error.
docker compose build migrator
docker compose --profile tools run --rm migrator check-pg
docker compose --profile tools run --rm -e SKIP_BACKUP=1 -e TRUNCATE_TARGET=1 migrator migrate-python
docker compose --profile tools run --rm migrator validate

# 4. Bring Open WebUI back up — now running on your migrated data
docker compose up -d open-webui
```

Your `webui.db` and working directory are mounted into the migrator at `/data`.

> [!TIP]
> Migrating into your **own** PostgreSQL instead of the bundled one? Set `DATABASE_URL` in `.env`
> and skip the `open-webui` service — just make sure that database's schema was created by Open WebUI first.

## ⚡ Quick start (host-native)

```bash
pip install -r requirements.txt
cp .env.example .env          # edit it

./openwebui-migrate.sh check-pg
./openwebui-migrate.sh backup            # safe copy (or set SKIP_BACKUP=1 to skip)
./openwebui-migrate.sh migrate-python
./openwebui-migrate.sh validate
```

### Or call the Python script directly

No shell wrapper, no `.env` — just two environment variables:

```bash
SQLITE_DB_PATH=webui.db \
DATABASE_URL=postgresql://user:pass@host:5432/openwebui \
  python3 migrate_sqlite_to_pg.py --dry-run     # preview, instant
  python3 migrate_sqlite_to_pg.py               # migrate
  python3 migrate_sqlite_to_pg.py --validate    # compare row counts
```

---

## ⚙️ Configuration

A single connection string drives everything. Copy `.env.example` → `.env` and set:

| Variable          | Required | Default | Description |
|-------------------|:--------:|---------|-------------|
| `DATABASE_URL`    | ✅ | — | Target PostgreSQL, e.g. `postgresql://user:pass@host:5432/openwebui` |
| `SOURCE_SQLITE`   | ✅ | — | Path to your source `webui.db` (used by the shell wrapper) |
| `KEEP_TABLES`     |   | *(all)* | Comma-separated allowlist — migrate only these tables (+ their FK deps) |
| `RECENT_DAYS`     |   | `0` | If set, copy only the last *N* days of chats and attached rows (`0` = everything) |
| `BATCH_SIZE`      |   | `5000` | Rows fetched per batch while streaming |
| `TRUNCATE_TARGET` |   | `0` | `1` clears the target tables before copying (see [Safety](#-safety--gotchas)) |
| `SKIP_BACKUP`     |   | `0` | `1` reads `SOURCE_SQLITE` directly instead of making a backup copy first |
| `INTEGRITY_CHECK` |   | `0` | `1` runs `PRAGMA integrity_check` first (slow on multi-GB DBs) |

> The shell wrapper reads `SOURCE_SQLITE`; if you call `migrate_sqlite_to_pg.py` directly, set **`SQLITE_DB_PATH`** to the file instead.

---

## 🧰 Commands

Run via `./openwebui-migrate.sh <command>` or `docker compose --profile tools run --rm migrator <command>`.

| Command | What it does |
|---------|--------------|
| `check-pg` | Verify PostgreSQL is reachable |
| `migrate-python` / `data-only` | **Copy data** into Open WebUI's existing schema *(the main command)* |
| `validate` | Compare SQLite vs PostgreSQL row counts, table by table |
| `inspect` | SQLite integrity check + row counts |
| `backup` | Make a safe, WAL-checkpointed copy of the SQLite DB first |
| `create-schema` | *(optional)* Build the PG schema from SQLite, if you're **not** letting Open WebUI do it |

Python flags (when calling the script directly): `--dry-run`, `--validate`, `--truncate`, `--sqlite-counts`, `--postgres-counts`, `--create-schema`, `--with-schema`.

---

## 🎯 Selective & incremental migration

Two features make staged cutovers easy.

**Migrate only some tables** — dependencies are pulled in automatically, so you never hit a foreign-key error:

```bash
KEEP_TABLES=chat,user,file ./openwebui-migrate.sh migrate-python
# chat_file, chat_message, etc. are added automatically as needed
```

**Migrate only recent data** — copy just the last week of activity. `chat` is filtered by timestamp; messages, tags, and files are filtered to match those chats:

```bash
RECENT_DAYS=7 ./openwebui-migrate.sh migrate-python
```

Combine them freely. Great for "migrate the recent stuff now, backfill later."

---

## 🛡️ Safety & gotchas

> [!WARNING]
> **By default this tool appends.** `COPY` does not truncate, and PostgreSQL still enforces primary keys — so loading into a table that already has rows fails on a duplicate key. Open WebUI seeds a few rows (e.g. `config`) when it builds the schema, so in practice you'll want **`TRUNCATE_TARGET=1`** (or the `--truncate` flag), which clears exactly the target tables first — no `CASCADE`, no identity reset, indexes untouched. Re-running a migration also needs this.

- **Read-only on the source.** SQLite is opened `mode=ro`; your `webui.db` is never modified. Use `backup` if you want a checkpointed copy to work from.
- **Stop Open WebUI first** (locks + concurrent writes).
- **Validate after.** `validate` flags any table whose counts don't line up.
- **Foreign keys during load.** Enforcement is disabled (`session_replication_role = replica`) so orphaned child rows (whose parent was already deleted in SQLite) still copy instead of aborting the run.

---

## 🏗️ Architecture (for contributors)

Two files, no framework:

- **`migrate_sqlite_to_pg.py`** — the engine.
  - `sqlite_table_list()` — topological sort via `TABLE_ORDER` + `TABLE_DEPS` for FK-safe ordering.
  - `pg_column_info()` — reads `information_schema` for each target table's types **and nullability**.
  - `migrate_table()` — streams rows with a server-side cursor and bulk-loads via `COPY ... FROM STDIN (FORMAT csv)`; per-column coercion is derived from the live PG type.
  - `build_where_clause()` — implements the `RECENT_DAYS` filters (direct, via `chat_id`, and via `chat_file → chat`).
  - `expand_keep_tables()` — closes `KEEP_TABLES` over its FK dependencies.
- **`openwebui-migrate.sh`** — orchestrator. Parses `DATABASE_URL`, runs preconditions, and delegates to the Python engine.

Adding support for a new table usually means adding it to `TABLE_ORDER` (and `TABLE_DEPS` if it has foreign keys). Type handling is automatic — it follows the PostgreSQL schema.

---

## 🩺 Troubleshooting

<details>
<summary><strong>Open WebUI won't start after migration: <code>extract(epoch from created_at)</code> error</strong></summary>

This means the schema was created by something other than Open WebUI. Use the **data-only** flow: drop the database, let a fresh Open WebUI build the schema via Alembic, stop it, then run `migrate-python`.
</details>

<details>
<summary><strong>It seems to hang after "Proceed?"</strong></summary>

On large databases the per-table `COUNT(*)` (used for the ETA) can take a while; you'll see a `counting rows…` marker first. If you piped output through `tee` and see nothing, ensure you're on the latest version — the script runs unbuffered (`python3 -u`).
</details>

<details>
<summary><strong><code>duplicate key value violates unique constraint</code></strong></summary>

The target table already contains rows (Open WebUI seeds some on first boot, or a previous run inserted them). Re-run with **`TRUNCATE_TARGET=1`** (or `--truncate`) to clear the target tables first. See [Safety & gotchas](#-safety--gotchas).
</details>

---

## 🗺️ Scope

**This is built for Open WebUI's schema specifically** — the table ordering and recent-data filters encode knowledge of Open WebUI's data model. It is not a general-purpose SQLite→PostgreSQL converter, though the data-only, schema-introspecting approach is reusable.

---

## 🤝 Contributing

**Contributions are welcome and encouraged** — no need to ask first. Issues, PRs, docs, and real-world migration reports are all appreciated. Helpful contributions:

- Support for newer Open WebUI tables/columns (update `TABLE_ORDER` / `TABLE_DEPS`).
- Tested results against more PostgreSQL versions.
- Docs and real-world migration notes.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for development setup, how to test, and the PR process.

---

## 📄 License

Released under the **MIT License**. See [`LICENSE`](LICENSE).

---

<div align="center">

Made for the Open WebUI community. If this saved you a migration headache, consider leaving a ⭐.

</div>
