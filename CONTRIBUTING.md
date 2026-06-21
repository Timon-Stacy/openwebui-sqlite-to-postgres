# Contributing

**Contributions are welcome and encouraged** — issues, pull requests, documentation, and real-world migration reports are all appreciated. If this tool saved you a headache, sharing what worked (or didn't) on your data helps the next person. You don't need to ask permission to open an issue or a PR; just go for it. 🙌

This is a small, focused project (two files, no framework), so it's easy to dive in.

## Ways to contribute

- 🐛 **Report bugs** — open an issue with your Open WebUI version, PostgreSQL version, and the error output.
- 🧩 **Support newer Open WebUI tables/columns** — when Open WebUI adds tables, update `TABLE_ORDER` (and `TABLE_DEPS` if the table has foreign keys) in `migrate_sqlite_to_pg.py`.
- ✅ **Report success/failure** — tell us which Open WebUI + PostgreSQL versions you migrated, and roughly how big the database was.
- 📖 **Improve the docs** — clarifications, fixes, and migration notes are genuinely valuable.

## Development setup

```bash
git clone <your-fork-url>
cd openwebui-sqlite-migration
pip install -r requirements.txt
```

Both entry points are plain scripts:

- `migrate_sqlite_to_pg.py` — the migration engine (Python + psycopg2).
- `openwebui-migrate.sh` — the orchestrator (Bash).

See the **Architecture** section in the [README](README.md#-architecture-for-contributors) (and [`CLAUDE.md`](CLAUDE.md)) for a map of the code.

## Testing your change

The data path is easiest to test against a throwaway PostgreSQL:

```bash
# 1. A disposable Postgres
docker run -d --name test-pg -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=openwebui -p 5433:5432 postgres:16-alpine

# 2. Create the schema the way Open WebUI would — the simplest faithful way is
#    to point a real Open WebUI at it once (see the README quick start), or for a
#    quick unit-style check, create just the table(s) you're touching.

# 3. Run the migrator against a small SQLite file
SQLITE_DB_PATH=path/to/test.db \
DATABASE_URL=postgresql://postgres:postgres@localhost:5433/openwebui \
  python3 migrate_sqlite_to_pg.py --dry-run     # preview the plan
  python3 migrate_sqlite_to_pg.py --truncate    # run it
  python3 migrate_sqlite_to_pg.py --validate    # compare row counts

# 4. Clean up
docker rm -f test-pg
```

The full end-to-end stack (PostgreSQL + Open WebUI + migrator) is available via
`docker compose` — see the README's Docker quick start.

Before opening a PR, please sanity-check:

```bash
bash -n openwebui-migrate.sh          # shell parses
python3 -m py_compile migrate_sqlite_to_pg.py   # python compiles
```

## Submitting changes

1. Fork the repo and create a branch (`git checkout -b fix/your-thing`).
2. Make your change. Keep it focused; match the surrounding style.
3. Run the sanity checks above.
4. Open a pull request with a clear description of **what** changed and **why**. If it fixes an issue, link it.

Clear commit messages are appreciated — a short imperative subject line (e.g.
`fix: handle NULL in prompt.content`) and a sentence or two of context.

## Scope

This tool is **specific to Open WebUI's schema** — the table ordering and recent-data filters encode knowledge of its data model. General-purpose SQLite→PostgreSQL features are out of scope, but the data-only, schema-introspecting approach is reusable if you want to fork it for another app.

## Questions

Not sure about something? Open an issue and ask — questions are welcome too.

Thanks for helping out! ❤️
