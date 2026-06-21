#!/usr/bin/env python3
"""
Open WebUI SQLite → PostgreSQL migrator (data-only).

Open WebUI owns the PostgreSQL schema (it builds the tables via Alembic on first
run). This tool reads that live schema and streams the SQLite data into it via
COPY — no DDL, no Alembic, no third-party migration packages.

Dependencies:  psycopg2-binary  (pip install -r requirements.txt)

Environment:
  SQLITE_DB_PATH   path to the source webui.db
  DATABASE_URL     postgresql://user:pass@host:port/dbname  (target, schema exists)
  BATCH_SIZE       rows per batch (default 5000)
  KEEP_TABLES      comma-separated allowlist (+ FK deps); empty = all tables
  RECENT_DAYS      copy only the last N days of chats and attached rows (0 = all)

Commands (via openwebui-migrate.sh or direct):
  (default)         copy data into the existing PostgreSQL schema
  --dry-run         show the plan (tables to copy), no reads or writes
  --validate        compare row counts between SQLite and PostgreSQL
  --sqlite-counts   show SQLite row counts (no PG needed)
  --postgres-counts show PostgreSQL row counts
  --create-schema   (optional) build the schema in PostgreSQL from SQLite yourself
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sqlite3
import sys
import time
from io import StringIO
from typing import Dict, Iterable, List, Optional, Set, Tuple

psycopg2 = None  # lazy import


def require_psycopg2():
    global psycopg2
    if psycopg2 is None:
        try:
            import psycopg2 as _p
        except ImportError as exc:
            raise ImportError("Run: pip install -r requirements.txt") from exc
        psycopg2 = _p
    return psycopg2


# ---------------------------------------------------------------------------
# Table ordering (foreign key safe)
# ---------------------------------------------------------------------------

TABLE_ORDER: List[str] = [
    "config",
    "user",
    "knowledge",
    "file",
    "auth",
    "memory",
    "tag",
    "folder",
    "chat",
    "chat_message",
    "chatidtag",
    "function",
    "tool",
    "model",
    "prompt",
    "prompt_history",
    "document",
    "channel",
    "message",
    "message_reaction",
    "channel_member",
    "channel_webhook",
    "oauth_session",
    "group",
    "group_member",
    "api_key",
    "feedback",
    "note",
    "skill",
    "access_grant",
    "automation",
    "automation_run",
    "calendar",
    "calendar_event",
    "calendar_event_attendee",
    "shared_chat",
    "pinned_note",
    "knowledge_directory",
    "chat_file",
    "channel_file",
    "knowledge_file",
]

TABLE_DEPS: Dict[str, List[str]] = {
    "chat_file":           ["chat", "file"],
    "channel_file":        ["channel", "file", "message"],
    "knowledge_file":      ["knowledge", "file", "knowledge_directory"],
    "knowledge_directory": ["knowledge"],
    "api_key":             ["user"],
    "oauth_session":       ["user"],
    "group_member":        ["group", "user"],
    "channel_webhook":     ["channel"],
    "channel_member":      ["channel"],
    "chat_message":        ["chat"],
    "message_reaction":    ["message"],
    "shared_chat":         ["chat"],
    "pinned_note":         ["note"],
}

SKIP_SQLITE_TABLES = {"alembic_version", "migratehistory", "sqlite_sequence"}

# Tables that get a created_at >= cutoff filter when RECENT_DAYS is set
RECENT_CHAT_DIRECT = {"chat"}
# Tables that filter via chat_id subquery
RECENT_CHAT_VIA_ID = {"chat_message", "chatidtag", "chat_file", "shared_chat"}
# Tables that filter via file_id → chat_file → chat subquery
RECENT_CHAT_VIA_FILE = {"file"}
RESERVED_PG        = {"user", "group", "order", "table", "select", "function"}

# ---------------------------------------------------------------------------
# Column type overrides: (table, column) → PostgreSQL type
# Derived from Open WebUI's SQLAlchemy models + community migration notes.
# ---------------------------------------------------------------------------

COL_TYPE_OVERRIDES: Dict[Tuple[str, str], str] = {
    # booleans stored as INTEGER in SQLite
    ("auth",     "active"):    "BOOLEAN",
    ("chat",     "archived"):  "BOOLEAN",
    ("function", "is_active"): "BOOLEAN",
    ("function", "is_global"): "BOOLEAN",
    # timestamps stored as epoch ms (bigint) — keep as bigint, not timestamptz
    ("chat",     "created_at"): "BIGINT",
    ("chat",     "updated_at"): "BIGINT",
    # JSONB columns
    ("user",     "info"):        "JSONB",
    ("user",     "settings"):    "JSONB",
    ("user",     "permissions"): "JSONB",
    ("chat",     "meta"):        "JSONB",
    ("function", "meta"):        "JSONB",
    ("function", "spec"):        "JSONB",
    ("tool",     "meta"):        "JSONB",
    ("tool",     "spec"):        "JSONB",
    ("model",    "meta"):        "JSONB",
    ("model",    "params"):      "JSONB",
    ("model",    "access_control"): "JSONB",
    ("config",   "data"):        "JSONB",
    ("group",    "data"):        "JSONB",
    ("group",    "meta"):        "JSONB",
    ("group",    "permissions"): "JSONB",
    ("group",    "user_ids"):    "JSONB",
    ("channel",  "data"):        "JSONB",
    ("channel",  "access_control"): "JSONB",
    ("message",  "data"):        "JSONB",
    ("message",  "meta"):        "JSONB",
    ("feedback", "data"):        "JSONB",
    ("feedback", "meta"):        "JSONB",
    ("knowledge","data"):        "JSONB",
    ("knowledge","access_control"): "JSONB",
    ("memory",   "meta"):        "JSONB",
    ("file",     "meta"):        "JSONB",
    ("prompt",   "command"):     "VARCHAR(255)",
    # config / document ids are integer in PG schema
    ("config",   "id"):      "INTEGER",
    ("config",   "version"): "INTEGER",
    ("document", "id"):      "INTEGER",
}

# Columns that must not be NULL in PG even if NULL in SQLite
NOT_NULL_DEFAULTS: Dict[Tuple[str, str], str] = {
    ("prompt", "content"):    "''",
    ("group",  "description"):"''",
}

# SQLite affinity → default PostgreSQL type
SQLITE_TO_PG: Dict[str, str] = {
    "INTEGER": "BIGINT",
    "INT":     "BIGINT",
    "REAL":    "DOUBLE PRECISION",
    "NUMERIC": "DOUBLE PRECISION",
    "TEXT":    "TEXT",
    "BLOB":    "BYTEA",
    "":        "TEXT",
}

# Data-load value coercion is driven by the live PG schema (see migrate_table),
# not hard-coded column lists — Open WebUI owns the schema.

COPY_NULL = "\\N"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def pg_ident(name: str) -> str:
    return f'"{name}"' if name.lower() in RESERVED_PG else name


def env(name: str, default: Optional[str] = None, required: bool = False) -> str:
    val = os.getenv(name, default)
    if required and not val:
        sys.exit(f"Missing environment variable: {name}")
    return val  # type: ignore[return-value]


def build_pg_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        sys.exit("Missing DATABASE_URL environment variable")
    return url


def sqlite_table_list(conn: sqlite3.Connection) -> List[str]:
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    available = {r[0] for r in cur.fetchall()} - SKIP_SQLITE_TABLES
    ordered: List[str] = []
    done: Set[str] = set()
    for _ in range(len(available) + 2):
        if not (available - done):
            break
        progress = False
        for t in TABLE_ORDER:
            if t in available and t not in done:
                if all(d in done for d in TABLE_DEPS.get(t, [])):
                    ordered.append(t)
                    done.add(t)
                    progress = True
        if not progress:
            ordered.extend(sorted(available - done))
            break
    return ordered


def expand_keep_tables(keep: Set[str]) -> Set[str]:
    """Expand a set of tables to include all transitive FK dependencies."""
    expanded = set(keep)
    changed = True
    while changed:
        changed = False
        for t in list(expanded):
            for dep in TABLE_DEPS.get(t, []):
                if dep not in expanded:
                    expanded.add(dep)
                    changed = True
    return expanded


def pg_table_set(conn) -> Set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE'"
        )
        return {r[0] for r in cur.fetchall()}


def pg_column_info(conn, table: str) -> Dict[str, Tuple[str, bool]]:
    """Return {column: (data_type, is_nullable)} for a PG table."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=%s",
            (table,),
        )
        return {name: (dtype, nullable == "YES") for name, dtype, nullable in cur.fetchall()}


def build_where_clause(table: str, cutoff_ms: int) -> Tuple[str, tuple]:
    """Return (where_sql, params) for tables that need recent-data filtering."""
    if table in RECENT_CHAT_DIRECT:
        return "created_at >= ?", (cutoff_ms,)
    if table in RECENT_CHAT_VIA_ID:
        return ('chat_id IN (SELECT id FROM "chat" WHERE created_at >= ?)', (cutoff_ms,))
    if table in RECENT_CHAT_VIA_FILE:
        return (
            'id IN (SELECT file_id FROM "chat_file"'
            ' WHERE chat_id IN (SELECT id FROM "chat" WHERE created_at >= ?))',
            (cutoff_ms,),
        )
    return "", ()


def sqlite_pragma_columns(conn: sqlite3.Connection, table: str):
    """Returns list of (cid, name, type, notnull, dflt_value, pk)."""
    return conn.execute(f'PRAGMA table_info("{table}")').fetchall()


def sqlite_pragma_indexes(conn: sqlite3.Connection, table: str):
    return conn.execute(f'PRAGMA index_list("{table}")').fetchall()


def sqlite_index_columns(conn: sqlite3.Connection, index_name: str) -> List[str]:
    rows = conn.execute(f'PRAGMA index_info("{index_name}")').fetchall()
    return [r[2] for r in rows]  # r[2] = column name


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------

def sqlite_aff_to_pg(sqlite_type: str) -> str:
    t = sqlite_type.upper().split("(")[0].strip()
    return SQLITE_TO_PG.get(t, "TEXT")


def create_schema(sqlite_conn: sqlite3.Connection, pg_conn, verbose: bool = True) -> int:
    """
    CREATE TABLE … IF NOT EXISTS for every SQLite table, with proper PG types.
    Also creates indexes found in SQLite.
    Returns number of tables created.
    """
    tables = sqlite_table_list(sqlite_conn)
    existing = pg_table_set(pg_conn)
    created = 0

    for table in tables:
        if table in existing:
            if verbose:
                print(f"  {table}: already exists")
            continue

        cols = sqlite_pragma_columns(sqlite_conn, table)
        col_defs = []
        pk_cols = [c[1] for c in cols if c[5]]  # pk flag

        for c in cols:
            _cid, name, sqlite_type, notnull, default, is_pk = c
            # Determine PG type
            pg_type = COL_TYPE_OVERRIDES.get((table, name)) \
                   or sqlite_aff_to_pg(sqlite_type or "")

            # VARCHAR from SQLite TEXT when we know the column is short
            if pg_type == "TEXT" and sqlite_type and "CHAR" in sqlite_type.upper():
                pg_type = "VARCHAR(255)"

            col_def = f"  {pg_ident(name)} {pg_type}"

            # NOT NULL
            not_null_default = NOT_NULL_DEFAULTS.get((table, name))
            if not_null_default:
                col_def += f" NOT NULL DEFAULT {not_null_default}"
            elif notnull and default is not None:
                col_def += f" NOT NULL DEFAULT {default}"
            elif notnull:
                col_def += " NOT NULL"

            col_defs.append(col_def)

        # Add PRIMARY KEY constraint
        if len(pk_cols) == 1:
            pk_col = pk_cols[0]
            pk_type = COL_TYPE_OVERRIDES.get((table, pk_col)) \
                   or sqlite_aff_to_pg(
                          next((c[2] for c in cols if c[1] == pk_col), "") or ""
                      )
            # Use SERIAL for integer single-column PKs unless overridden
            if pk_type in ("BIGINT", "INTEGER", "INT") and \
               not COL_TYPE_OVERRIDES.get((table, pk_col)):
                # Replace col def with SERIAL
                col_defs = [
                    d.replace(
                        f"  {pg_ident(pk_col)} {pk_type}",
                        f"  {pg_ident(pk_col)} BIGSERIAL",
                    ) if d.startswith(f"  {pg_ident(pk_col)} ") else d
                    for d in col_defs
                ]
            col_defs.append(f"  PRIMARY KEY ({pg_ident(pk_col)})")
        elif len(pk_cols) > 1:
            pks = ", ".join(pg_ident(p) for p in pk_cols)
            col_defs.append(f"  PRIMARY KEY ({pks})")

        ddl = (
            f"CREATE TABLE IF NOT EXISTS {pg_ident(table)} (\n"
            + ",\n".join(col_defs)
            + "\n)"
        )

        with pg_conn.cursor() as cur:
            cur.execute(ddl)
        pg_conn.commit()

        # Indexes
        for idx in sqlite_pragma_indexes(sqlite_conn, table):
            idx_name = idx[1]
            is_unique = bool(idx[2])
            idx_cols = sqlite_index_columns(sqlite_conn, idx_name)
            if not idx_cols:
                continue
            safe_idx_name = f"{table}_{idx_name}"[:63]
            cols_sql = ", ".join(pg_ident(c) for c in idx_cols)
            unique_kw = "UNIQUE " if is_unique else ""
            try:
                with pg_conn.cursor() as cur:
                    cur.execute(
                        f"CREATE {unique_kw}INDEX IF NOT EXISTS "
                        f"{safe_idx_name} ON {pg_ident(table)} ({cols_sql})"
                    )
                pg_conn.commit()
            except Exception as exc:
                pg_conn.rollback()
                if verbose:
                    print(f"    warn: index {safe_idx_name}: {exc}")

        if verbose:
            print(f"  {table}: created ({len(cols)} columns)")
        created += 1

    return created


# ---------------------------------------------------------------------------
# Data migration helpers
# ---------------------------------------------------------------------------

def row_counts_sqlite(conn: sqlite3.Connection) -> Dict[str, int]:
    counts = {}
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        try:
            counts[name] = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        except sqlite3.Error:
            counts[name] = -1
    return counts


def row_counts_pg(conn) -> Dict[str, int]:
    counts = {}
    for table in sorted(pg_table_set(conn)):
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM {pg_ident(table)}")
                counts[table] = cur.fetchone()[0]
        except Exception:
            counts[table] = -1
    return counts


class CopyStream:
    def __init__(self, row_iter: Iterable[tuple]):
        self._iter = iter(row_iter)
        self._buf = ""
        self._done = False

    def _line(self) -> str:
        try:
            row = next(self._iter)
        except StopIteration:
            self._done = True
            return ""
        out = StringIO()
        writer = csv.writer(out, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        writer.writerow([COPY_NULL if v is None else v for v in row])
        return out.getvalue()

    def read(self, size: int = 262144) -> str:
        if self._done and not self._buf:
            return ""
        while len(self._buf) < size and not self._done:
            self._buf += self._line()
        chunk, self._buf = self._buf[:size], self._buf[size:]
        return chunk


def stream_rows(
    conn: sqlite3.Connection,
    table: str,
    columns: List[str],
    batch: int,
    where_extra: str = "",
    params_extra: tuple = (),
) -> Iterable[tuple]:
    col_sql = ", ".join(f'"{c}"' for c in columns)
    where_clause = f"WHERE ({where_extra})" if where_extra else ""
    base = f'SELECT {col_sql} FROM "{table}" {where_clause}'

    # Cursor-based streaming: no OFFSET re-scan on every batch
    cur = conn.execute(base, params_extra)
    while True:
        rows = cur.fetchmany(batch)
        if not rows:
            break
        yield from rows


def count_rows_sqlite(
    conn: sqlite3.Connection,
    table: str,
    where_extra: str = "",
    params_extra: tuple = (),
) -> int:
    where = f"WHERE ({where_extra})" if where_extra else ""
    return conn.execute(f'SELECT COUNT(*) FROM "{table}" {where}', params_extra).fetchone()[0]


class Progress:
    """Per-table progress bar written to stderr so stdout can be piped/logged cleanly."""

    _W = 25  # bar fill width

    def __init__(self, total: int, label: str, idx: int, n_tables: int):
        self.total   = total
        self.label   = label
        self.prefix  = f"[{idx}/{n_tables}]"
        self.t0      = time.time()
        self._last   = 0.0
        self._tty    = sys.stderr.isatty()

    def update(self, n: int) -> None:
        if not self._tty:
            return
        now = time.time()
        if now - self._last < 0.15:
            return
        self._last = now
        self._render(n)

    def finish(self, n: int, orphaned: int = 0) -> None:
        if self._tty:
            cols = shutil.get_terminal_size((120, 24)).columns
            sys.stderr.write(f"\r{' ' * cols}\r")
            sys.stderr.flush()
        elapsed = max(time.time() - self.t0, 1e-3)
        rps  = int(n / elapsed)
        skip = f"  ({orphaned} orphaned skipped)" if orphaned else ""
        print(f"  {self.prefix} {self.label}  {n:,} rows  {elapsed:.1f}s  ({rps:,}/s){skip}",
              flush=True)

    def _render(self, n: int) -> None:
        elapsed = max(time.time() - self.t0, 1e-3)
        rps = int(n / elapsed) if n else 0
        if self.total:
            frac   = min(n / self.total, 1.0)
            filled = int(frac * self._W)
            bar    = "█" * filled + "░" * (self._W - filled)
            pct    = int(frac * 100)
            eta    = ""
            if rps > 0 and n < self.total:
                rem    = int((self.total - n) / rps)
                h, r   = divmod(rem, 3600)
                m, s   = divmod(r, 60)
                eta = f" ETA {h:02d}:{m:02d}:{s:02d}" if h else f" ETA {m:02d}:{s:02d}"
            line = (
                f"  {self.prefix} {self.label}  "
                f"[{bar}] {pct:3d}%  {n:,}/{self.total:,}  {rps:,}/s{eta}"
            )
        else:
            line = f"  {self.prefix} {self.label}  {n:,} rows  {rps:,}/s"
        cols = shutil.get_terminal_size((120, 24)).columns
        sys.stderr.write(f"\r{line:<{cols}}")
        sys.stderr.flush()


def migrate_table(
    sqlite_conn: sqlite3.Connection,
    pg_conn,
    table: str,
    batch: int,
    where_extra: str = "",
    params_extra: tuple = (),
    table_idx: int = 1,
    table_total: int = 1,
) -> int:
    """Stream table data into PostgreSQL via COPY. Returns rows copied.
    The PG schema (owned by Open WebUI) is authoritative for column types."""
    pg_cols = pg_column_info(pg_conn, table)
    if not pg_cols:
        print(f"  SKIP {table}: not in PostgreSQL")
        return 0

    all_cols = [r[1] for r in sqlite_pragma_columns(sqlite_conn, table)]
    columns = [c for c in all_cols if c in pg_cols]
    if not columns:
        print(f"  SKIP {table}: no matching columns")
        return 0

    label = table + (" [recent]" if where_extra else "")
    # COUNT(*) on a recent-filter subquery can take minutes on a multi-GB DB.
    # Print a marker first so this phase isn't a silent stall.
    prefix = f"[{table_idx}/{table_total}]"
    sys.stderr.write(f"  {prefix} {label}: counting rows…\r")
    sys.stderr.flush()
    t_count = time.time()
    total = count_rows_sqlite(sqlite_conn, table, where_extra, params_extra)
    if time.time() - t_count > 2:
        print(f"  {prefix} {label}: {total:,} rows to copy "
              f"(counted in {time.time() - t_count:.0f}s)", flush=True)
    progress = Progress(total, label, table_idx, table_total)
    progress.update(0)

    # Per-column normalizer kind from the real PG type + nullability (hot-loop dict-free).
    # NOT NULL columns get NULL coerced to a safe empty value so COPY won't abort.
    col_kinds: List[str] = []  # bool | json | json_nn | text_nn | ""
    for col in columns:
        pg_t, nullable = pg_cols[col]
        if pg_t == "boolean":
            col_kinds.append("bool")
        elif pg_t in ("jsonb", "json"):
            col_kinds.append("json" if nullable else "json_nn")
        elif not nullable and pg_t in ("text", "character varying", "character"):
            col_kinds.append("text_nn")
        else:
            col_kinds.append("")

    copied = 0

    def normalized_iter():
        nonlocal copied
        for raw in stream_rows(sqlite_conn, table, columns, batch,
                               where_extra, params_extra):
            copied += 1
            progress.update(copied)
            out = []
            for v, kind in zip(raw, col_kinds):
                if v is None:
                    if kind == "text_nn":
                        out.append("")
                    elif kind == "json_nn":
                        out.append("{}")
                    else:
                        out.append(None)
                elif kind == "bool":
                    out.append("t" if v in (1, True, "1", "true", "t") else "f")
                elif kind in ("json", "json_nn"):
                    if isinstance(v, (dict, list)):
                        out.append(json.dumps(v))
                    elif isinstance(v, str):
                        try:
                            json.loads(v)
                            out.append(v)
                        except json.JSONDecodeError:
                            out.append("{}")
                    else:
                        out.append("{}")
                elif isinstance(v, bytes):
                    out.append(v.decode("utf-8", errors="replace"))
                elif isinstance(v, str) and "\x00" in v:
                    out.append(v.replace("\x00", ""))
                else:
                    out.append(v)
            yield tuple(out)

    col_list = ", ".join(pg_ident(c) for c in columns)
    with pg_conn.cursor() as cur:
        cur.copy_expert(
            f"COPY {pg_ident(table)} ({col_list}) FROM STDIN "
            f"WITH (FORMAT csv, NULL '{COPY_NULL}')",
            CopyStream(normalized_iter()),
        )
    pg_conn.commit()

    progress.finish(copied)
    return copied


def reset_sequences(pg_conn) -> None:
    with pg_conn.cursor() as cur:
        cur.execute("""
            DO $$
            DECLARE r RECORD;
            BEGIN
              FOR r IN
                SELECT c.relname AS tbl, a.attname AS col
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_attribute a ON a.attrelid = c.oid
                WHERE c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped
                  AND n.nspname = 'public'
                  AND pg_get_serial_sequence(quote_ident(c.relname), a.attname) IS NOT NULL
              LOOP
                EXECUTE format(
                  'SELECT setval(pg_get_serial_sequence(%L,%L), COALESCE((SELECT MAX(%I) FROM %I),1))',
                  r.tbl, r.col, r.col, r.tbl
                );
              END LOOP;
            END $$;
        """)
    pg_conn.commit()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Open WebUI SQLite → PostgreSQL migrator"
    )
    parser.add_argument("--create-schema", action="store_true",
                        help="Create PG tables from SQLite schema (idempotent, no data)")
    parser.add_argument("--with-schema", action="store_true",
                        help="Create PG schema before copying data (default is data-only)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Schema diff + table list, no data read or written (instant)")
    parser.add_argument("--sqlite-counts",   action="store_true",
                        help="Show SQLite row counts (slow on large DBs)")
    parser.add_argument("--postgres-counts", action="store_true")
    parser.add_argument("--validate",        action="store_true",
                        help="Compare row counts SQLite vs PostgreSQL (slow on large DBs)")
    parser.add_argument("--truncate", action="store_true",
                        default=env("TRUNCATE_TARGET", "0") == "1",
                        help="Clear (TRUNCATE) the target tables before copying. Off by "
                             "default; needed when the tables already hold rows (e.g. Open "
                             "WebUI seeded `config` on first boot).")
    args = parser.parse_args()

    sqlite_path = env("SQLITE_DB_PATH", required=True)
    batch = int(env("BATCH_SIZE", "5000"))

    if not os.path.isfile(sqlite_path):
        sys.exit(f"SQLite file not found: {sqlite_path}")

    sqlite_conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True, timeout=120)
    sqlite_conn.row_factory = sqlite3.Row
    sqlite_conn.execute("PRAGMA cache_size = -65536")    # 64 MB page cache
    sqlite_conn.execute("PRAGMA mmap_size = 4294967296") # 4 GB memory-mapped I/O
    sqlite_conn.execute("PRAGMA temp_store = MEMORY")

    # Integrity check is opt-in: it reads every page (minutes on a multi-GB DB).
    # Set INTEGRITY_CHECK=1 to enable. Off by default for data-only copies.
    if env("INTEGRITY_CHECK", "0") == "1" and not (args.dry_run or args.create_schema):
        print("Integrity check (this reads every page — slow on large DBs)...", flush=True)
        integrity = sqlite_conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            sys.exit(f"SQLite integrity check failed: {integrity}")
        print("Integrity check passed.", flush=True)

    if args.sqlite_counts:
        print("Counting rows (SELECT COUNT(*) per table — slow on large DBs)...")
        for table, count in sorted(row_counts_sqlite(sqlite_conn).items()):
            print(f"{table}\t{count}")
        sqlite_conn.close()
        return

    # --- Connect to PostgreSQL ---
    pg_url = build_pg_url()
    pg_conn = None
    try:
        pg = require_psycopg2()
        pg_conn = pg.connect(pg_url)
        pg_conn.autocommit = False
    except (ImportError, Exception) as exc:
        err = str(exc)
        if "does not exist" in err and "database" in err.lower():
            sys.exit(
                f"Database missing: {err}\n"
                "Run: docker compose --profile tools run --rm migrator create-db"
            )
        sys.exit(f"PostgreSQL connection failed: {exc}")

    if args.postgres_counts:
        print("Counting rows in PostgreSQL...")
        for table, count in sorted(row_counts_pg(pg_conn).items()):
            print(f"{table}\t{count}")
        pg_conn.close()
        return

    if args.validate:
        print("Validating (SELECT COUNT(*) on all tables — slow on large DBs)...")
        sq = row_counts_sqlite(sqlite_conn)
        pq = row_counts_pg(pg_conn)
        keep_tables = {t.strip() for t in env("KEEP_TABLES", "").split(",") if t.strip()}
        all_tables = sorted(set(sq) | set(pq))
        ok = True
        print(f"{'table':<25} {'sqlite':>12} {'postgres':>12}  status")
        print("-" * 57)
        for t in all_tables:
            if t in SKIP_SQLITE_TABLES:
                continue
            if keep_tables and t not in keep_tables:
                continue
            s, p = sq.get(t, 0), pq.get(t, 0)
            status = ("pg only"       if t not in sq else
                      "MISSING in pg" if t not in pq else
                      "ok"            if s == p    else
                      "MISMATCH")
            if status not in ("ok", "pg only"):
                ok = False
            print(f"{t:<25} {s:>12,} {p:>12,}  {status}")
        sqlite_conn.close()
        pg_conn.close()
        sys.exit(0 if ok else 1)

    # --- Schema: always create/verify first (reads only sqlite_master — instant) ---
    tables      = sqlite_table_list(sqlite_conn)
    pg_existing = pg_table_set(pg_conn)

    if args.dry_run:
        # Instant: only reads sqlite_master + information_schema, zero table scans
        to_create = [t for t in tables if t not in pg_existing]
        existing  = [t for t in tables if t in pg_existing]
        print(f"SQLite:  {sqlite_path}")
        print(f"PG:      {pg_url.split('@', 1)[-1]}")
        print(f"Tables:  {len(tables)} in SQLite")
        print(f"  Already in PG : {len(existing)}")
        if to_create:
            print(f"  Will CREATE   : {len(to_create)}")
            for t in to_create:
                print(f"    + {t}")
        else:
            print(f"  All tables exist in PG — ready to copy data")
        print("\nDry run complete (no data read or written).")
        sqlite_conn.close()
        pg_conn.close()
        return

    if args.with_schema:
        print("=== Schema ===")
        created = create_schema(sqlite_conn, pg_conn, verbose=True)
        pg_existing = pg_table_set(pg_conn)
        print(f"  {created} tables created\n")
    elif args.create_schema:
        created = create_schema(sqlite_conn, pg_conn, verbose=True)
        pg_existing = pg_table_set(pg_conn)
        print(f"  {created} tables created")
        sqlite_conn.close()
        pg_conn.close()
        print("Done (schema only).")
        return
    else:
        print("=== Schema ===  (skipped — using existing PG schema)\n")

    # --- Apply KEEP_TABLES: data only for selected tables ---
    keep_tables = {t.strip() for t in env("KEEP_TABLES", "").split(",") if t.strip()}
    if keep_tables:
        expanded = expand_keep_tables(keep_tables)
        auto_added = expanded - keep_tables
        if auto_added:
            print(f"  Auto-including FK dependencies: {', '.join(sorted(auto_added))}")
        data_tables = [t for t in tables if t in expanded]
        print(f"  Data copy: {', '.join(data_tables)}\n")
    else:
        data_tables = tables

    # --- Recent-data filter for chat tables ---
    recent_days = int(env("RECENT_DAYS", "0"))
    cutoff_ms = int((time.time() - recent_days * 86400) * 1000) if recent_days else 0
    if recent_days:
        filtered = RECENT_CHAT_DIRECT | RECENT_CHAT_VIA_ID | RECENT_CHAT_VIA_FILE
        print(f"  Recent filter: last {recent_days} days for {', '.join(sorted(filtered))}\n")

    # Orphaned rows (child whose parent was deleted) load fine because the COPY
    # runs with session_replication_role = replica, which disables FK enforcement.
    # No pre-scan needed — PRAGMA foreign_key_check would full-scan the whole DB.

    # --- Copy data ---
    print(f"\n=== Copy data ===")
    print(f"SQLite:     {sqlite_path}")
    print(f"PostgreSQL: {pg_url.split('@', 1)[-1]}")
    print(f"Tables:     {len(data_tables)}  batch_size: {batch}\n")

    migratable = [t for t in data_tables if t in pg_existing]
    for t in data_tables:
        if t not in pg_existing:
            print(f"  SKIP {t}: not in PostgreSQL")

    # FK enforcement is disabled for the load so orphaned children still copy.
    with pg_conn.cursor() as cur:
        cur.execute("SET session_replication_role = replica")
        cur.execute("SET synchronous_commit = off")
    pg_conn.commit()

    if args.truncate:
        # Opt-in: clear exactly the target tables (one statement, no CASCADE so
        # unlisted tables are untouched; no RESTART IDENTITY; lock_timeout turns a
        # lock held by a running Open WebUI into a clear error instead of a hang).
        print("Clearing target tables (--truncate)...", flush=True)
        table_list = ", ".join(pg_ident(t) for t in migratable)
        try:
            with pg_conn.cursor() as cur:
                cur.execute("SET lock_timeout = '15s'")
                cur.execute(f"TRUNCATE TABLE {table_list}")
            pg_conn.commit()
        except Exception as exc:
            pg_conn.rollback()
            msg = str(exc).lower()
            if "lock" in msg or "timeout" in msg:
                sys.exit(f"\nCould not lock tables to clear (15s): {exc}\n"
                         "Is Open WebUI still connected? Stop it and re-run.")
            if "foreign key" in msg or "referenced" in msg:
                sys.exit(f"\nA table outside the migration set references one of these: {exc}\n"
                         "Add it to KEEP_TABLES so it's cleared too.")
            raise
    else:
        # Default: append. Existing rows stay; COPY into a non-empty table fails on
        # duplicate primary keys. Use --truncate (or TRUNCATE_TARGET=1) to clear first.
        print("Appending data (existing rows kept; use --truncate to clear first).", flush=True)

    total_rows = 0
    t_start = time.time()
    try:
        for idx, table in enumerate(migratable, 1):
            where_extra, params_extra = ("", ())
            if cutoff_ms:
                where_extra, params_extra = build_where_clause(table, cutoff_ms)
            total_rows += migrate_table(
                sqlite_conn, pg_conn, table,
                batch,
                where_extra, params_extra,
                table_idx=idx, table_total=len(migratable),
            )

        with pg_conn.cursor() as cur:
            cur.execute("SET session_replication_role = origin")
        pg_conn.commit()

        print(f"\nResetting sequences...")
        reset_sequences(pg_conn)

    except Exception:
        pg_conn.rollback()
        raise
    finally:
        sqlite_conn.close()
        pg_conn.close()

    elapsed = time.time() - t_start
    print(f"\nMigration complete — {total_rows:,} rows in {elapsed:.0f}s")


if __name__ == "__main__":
    main()
