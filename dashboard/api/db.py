"""SQLite layer for the dashboard (Phase C.1).

Single-file SQLite database at `settings.runs_db_path` (default
`data/dashboard.db`). Uses **stdlib `sqlite3`** (sync) rather than aiosqlite -
FastAPI runs sync route handlers in a threadpool, the dashboard is local-first
single-machine, and WAL mode + `busy_timeout` give us enough concurrency
without the operational complexity of an async driver.

**Database-level PRAGMAs** (set ONCE in `init_db()`, persist on disk):
  - `journal_mode=WAL` - readers don't block the writer. Stored in the
    DB header; subsequent connections inherit it automatically.
  - `synchronous=NORMAL` - paired with WAL: durable across crashes,
    not durable across full power loss. Acceptable for a dev tool.

**Per-connection PRAGMAs** (set in every `connect()` call, NOT persisted):
  - `busy_timeout=5000` - wait up to 5s rather than failing immediately
    when another connection holds the write lock.
  - `foreign_keys=ON` - SQLite default is OFF *per connection*; we want
    FK enforcement if any future migration adds them.

The split matters: a future maintainer who deletes the WAL line in
`init_db` would silently regress to rollback-journal mode, but the
test pinning `journal_mode=='wal'` would catch it. Conversely, dropping
the per-connection `busy_timeout` would cause sporadic SQLITE_BUSY
failures under load - also caught by `test_pragmas_applied_per_connection`.

**Migrations:** `PRAGMA user_version`-driven. `MIGRATIONS` is an ordered
list of callables `(conn) -> None`; running `apply_migrations(conn)` brings
the schema from `user_version=N` to `len(MIGRATIONS)`. Each migration is
idempotent within its own version (uses `CREATE TABLE IF NOT EXISTS` etc.)
so partial application during a crash is recoverable. No Alembic - overkill
for one schema file.

**Why no ORM:** the schema has one table (`runs`). SQLAlchemy's value
proposition (relationships, query builder, multi-DB) doesn't apply here.
Raw SQL with `sqlite3.Row` is more honest about what we're doing.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator

from .lifecycle import (
    ACTIVE_STATUSES_TUPLE,
    NON_DELETABLE_STATUSES_TUPLE,
    RunStatus,
    RUN_STATUSES_TUPLE,
    TERMINAL_STATUSES_TUPLE,
    transition_sources_for,
)


# Mirrors `Manifest.Kind` (test_ui/common/manifest.py) - keeping these in
# sync is enforced by `test_dashboard_db.py::test_run_kinds_match_manifest`.
RUN_KINDS_TUPLE: tuple[str, ...] = ("baseline", "current", "comparator", "report")

# Dashboard lifecycle vocabulary is defined in `dashboard/api/lifecycle.py`
# and re-exported here for compatibility with existing imports/tests.

# How a row got into the table.
RUN_SOURCES_TUPLE: tuple[str, ...] = ("dashboard", "discovered", "cli")


# Strict literal pattern for the CHECK-constraint IN-lists below. We
# string-interpolate values directly into the CREATE TABLE SQL because
# CHECK constraints don't support bound parameters, so we defend at the
# helper level: every value must be a lowercase identifier with no
# punctuation. A future maintainer who appends a value containing a
# quote / space / paren gets an AssertionError at import time, not a
# subtly-broken DDL.
_CHECK_LITERAL_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def _quote_compile_time_literals(values: tuple[str, ...]) -> str:
    """Render a CHECK-constraint IN-list from a tuple of compile-time literals.

    SAFE only when `values` is a module-level constant tuple of strings
    matching `_CHECK_LITERAL_PATTERN` (lowercase identifiers). Asserts
    that constraint so a future tuple containing whitespace, an apostrophe,
    or other SQL metacharacters trips at load time rather than producing
    broken DDL silently.
    """
    for v in values:
        if not _CHECK_LITERAL_PATTERN.fullmatch(v):
            raise AssertionError(
                f"CHECK-list value {v!r} is not a lowercase identifier; "
                "this helper is only safe for compile-time literals."
            )
    return ", ".join(f"'{v}'" for v in values)


# IMPORTANT for migration authors: do NOT use `conn.executescript()` here.
# executescript() implicitly issues a COMMIT before running its body, which
# would cancel the explicit BEGIN that `apply_migrations` wraps each migration
# in - leaving the user_version bump outside any transaction. Use one
# `conn.execute(...)` per statement instead.
def _migration_001_initial(conn: sqlite3.Connection) -> None:
    """Initial schema: the `runs` table + its indexes.

    `args_json` and `command_json` are stored as TEXT (JSON-serialized)
    rather than as separate columns because they're opaque to the DB -
    we never WHERE / GROUP BY their contents. JSON1 functions can be
    added later if a query genuinely needs to introspect them.

    `pid_start_time` is the leader-PID's start time from /proc/<pid>/stat
    field 22 (Linux). Stored to defeat PID recycling: at restart-recovery
    time we re-read /proc and refuse to SIGTERM a PGID whose leader's
    start-time has changed (different process, same number).
    """
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS runs (
          id              INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id          TEXT NOT NULL UNIQUE,
          kind            TEXT NOT NULL CHECK (kind IN ({_quote_compile_time_literals(RUN_KINDS_TUPLE)})),
          status          TEXT NOT NULL CHECK (status IN ({_quote_compile_time_literals(RUN_STATUSES_TUPLE)})),
          created_at      TEXT NOT NULL,
          started_at      TEXT,
          finished_at     TEXT,
          date_dir        TEXT,
          args_json       TEXT NOT NULL,
          command_json    TEXT NOT NULL,
          exit_code       INTEGER,
          error           TEXT,
          pid             INTEGER,
          pgid            INTEGER,
          pid_start_time  TEXT,
          source          TEXT NOT NULL DEFAULT 'dashboard'
                          CHECK (source IN ({_quote_compile_time_literals(RUN_SOURCES_TUPLE)}))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_runs_created   ON runs(created_at DESC)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_run_id    ON runs(run_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_runs_kind_date ON runs(kind, date_dir)"
    )


# Ordered list. Append-only - never reorder, never delete.
# `apply_migrations` runs each callable whose 1-based index > current user_version.
MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    _migration_001_initial,
]


_RETENTION_DATE_FORMAT = "%d-%m-%Y"
_RETENTION_DATETIME_FORMAT = "%d-%m-%Y %H:%M:%S"
_RETENTION_DEFAULT_SOURCES: tuple[str, ...] = ("dashboard", "cli")


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection. Per-connection PRAGMAs are applied; database-level
    PRAGMAs (WAL, synchronous) are NOT - those are set once in `init_db`.

    Caller is responsible for closing. Use `connection_scope()` for the
    common request-scoped pattern. Each call opens a fresh connection -
    SQLite handles the contention via WAL + `busy_timeout`.

    Does NOT mkdir the parent - `init_db` does that once at startup. A
    `connect()` call against a path whose parent doesn't exist will raise
    `sqlite3.OperationalError`, which is the right behavior: it surfaces
    a config bug instead of silently creating directory debris in the
    operator's filesystem (e.g. via /api/health probing a typo'd path).
    """
    conn = sqlite3.connect(
        db_path,
        # `isolation_level=None` makes the driver autocommit; we still get
        # transactional behavior by issuing BEGIN/COMMIT explicitly when we
        # want it. The default (driver-implicit transactions on DML) is more
        # surprising than helpful in a request/response context.
        isolation_level=None,
        # `check_same_thread=False` so a connection opened in one thread can
        # be used in another (FastAPI's threadpool may move the request).
        # Safe because we open one connection per request (no sharing).
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    # `busy_timeout` and `foreign_keys` are per-connection in SQLite - they
    # MUST be set on every fresh connection or they revert to the (bad)
    # defaults: SQLITE_BUSY immediate failure and FK enforcement off.
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def connection_scope(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open + close a connection. Use as a FastAPI dependency or in scripts."""
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_user_version(conn: sqlite3.Connection) -> int:
    """Read the schema version (`PRAGMA user_version`). 0 = fresh DB."""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def set_user_version(conn: sqlite3.Connection, version: int) -> None:
    """Write the schema version. Inlined into the SQL because PRAGMAs don't
    accept bound parameters in SQLite. Safe - `version` is an int we control."""
    if not isinstance(version, int) or version < 0:
        raise ValueError(f"version must be a non-negative int, got {version!r}")
    conn.execute(f"PRAGMA user_version = {version}")


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Bring the schema to the latest version. Returns the new user_version.

    Each migration runs in its own transaction so a crash mid-migration
    leaves the DB at the previous version (safe to re-run). The
    user_version bump is part of the same transaction as the schema
    change so the two can never disagree.
    """
    current = get_user_version(conn)
    target = len(MIGRATIONS)
    if current > target:
        # The DB was written by a newer codebase. Don't pretend we know what
        # to do - fail loudly so the operator notices the version mismatch.
        raise RuntimeError(
            f"DB user_version={current} is ahead of code (max={target}). "
            f"Downgrade the DB or upgrade the codebase."
        )
    for i in range(current, target):
        migration = MIGRATIONS[i]
        new_version = i + 1
        conn.execute("BEGIN")
        try:
            migration(conn)
            set_user_version(conn, new_version)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return target


def init_db(db_path: Path) -> None:
    """Open + set database-level PRAGMAs + migrate. Idempotent.

    Creates the parent directory (one-time, at startup) so the operator
    doesn't have to mkdir `data/` themselves. After this point, `connect()`
    refuses to materialize directories - see its docstring for why.

    Database-level PRAGMAs (`journal_mode`, `synchronous`) are set here
    rather than in `connect` because they're persisted in the DB header -
    setting them on every connection would be redundant work, and the
    `journal_mode=WAL` switch can silently fail if there's an active
    transaction on another connection. Doing it once at startup, before
    any other connection exists, sidesteps both issues.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connection_scope(db_path) as conn:
        # `journal_mode` returns the active mode (or the new mode on success).
        # We assert success rather than trust it - a misconfigured filesystem
        # (e.g. NFS without mandatory locking) silently keeps the DB in
        # rollback-journal mode, and we'd rather know at startup than under
        # concurrent load.
        active = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if active.lower() != "wal":
            raise RuntimeError(
                f"failed to enable WAL on {db_path} (active mode={active!r}). "
                "Check filesystem support (NFS without locking won't work)."
            )
        conn.execute("PRAGMA synchronous=NORMAL")
        apply_migrations(conn)


# --------------------------------------------------------------------------- #
# Row helpers - keep SQL out of route handlers.                              #
# --------------------------------------------------------------------------- #


def insert_discovered_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    kind: str,
    status: RunStatus,
    created_at: str,
    started_at: str | None,
    finished_at: str | None,
    date_dir: str | None,
) -> int | None:
    """INSERT a `source='discovered'` row. Returns the new id, or None on
    UNIQUE conflict (run_id already known - caller should treat as "already
    synced" rather than as an error).

    Discovered rows have empty args/command JSON because they were never
    spawned by the dashboard - the actual command lives in the manifest's
    on-disk run record (`data/runs/<run_id>.run.json`) if it exists.
    """
    try:
        cursor = conn.execute(
            """
            INSERT INTO runs (
                run_id, kind, status, created_at, started_at, finished_at,
                date_dir, args_json, command_json, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'discovered')
            """,
            (
                run_id,
                kind,
                status,
                created_at,
                started_at,
                finished_at,
                date_dir,
                json.dumps({}),
                json.dumps([]),
            ),
        )
        return int(cursor.lastrowid) if cursor.lastrowid else None
    except sqlite3.IntegrityError as e:
        # Duplicate run_id - already in the table from a prior sync or from
        # the dashboard itself. Not an error; just skip.
        if "UNIQUE constraint failed: runs.run_id" in str(e):
            return None
        raise


def list_runs(
    conn: sqlite3.Connection,
    *,
    kind: str | None = None,
    status: RunStatus | None = None,
    date_dir: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[sqlite3.Row], int]:
    """Return (rows, total). `total` counts the filtered set, not the page.

    `date_dir` filters to a specific DD-MM-YYYY string - added so the
    Reports page can request `?kind=report&date_dir=01-01-2099` and get
    every report run for that date in one query, without the 500-row
    client-side filter that round-1 review caught (CRITICAL #1).
    """
    where: list[str] = []
    params: list[object] = []
    if kind is not None:
        where.append("kind = ?")
        params.append(kind)
    if status is not None:
        where.append("status = ?")
        params.append(status)
    if date_dir is not None:
        where.append("date_dir = ?")
        params.append(date_dir)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    total = int(
        conn.execute(f"SELECT COUNT(*) FROM runs{where_sql}", params).fetchone()[0]
    )
    rows = conn.execute(
        f"SELECT * FROM runs{where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return rows, total


def get_run(conn: sqlite3.Connection, db_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs WHERE id = ?", (db_id,)).fetchone()


# --------------------------------------------------------------------------- #
# Job-runner row helpers (Phase C.1 second slice).                           #
# --------------------------------------------------------------------------- #


def insert_pending_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    kind: str,
    args: dict,
    command: list[str],
    date_dir: str,
    created_at: str,
    source: str = "dashboard",
) -> int:
    """INSERT a new pending row and return its `id`.

    Pending = "row exists, subprocess not yet spawned". The runner spawns,
    then race-safely promotes to `running` via `mark_running`. Both halves
    must be paired - a row left in `pending` after process-startup failure
    is a bug (the runner should mark it `failed` instead).

    `command` is the full argv that WILL be exec'd (including `python`);
    stored so retry can re-spawn with identical args without re-deriving.
    `args` is the parsed request body that produced `command`; stored so
    the dashboard UI can show "what was asked" vs "what was run".
    """
    cursor = conn.execute(
        """
        INSERT INTO runs (
            run_id, kind, status, created_at, date_dir,
            args_json, command_json, source
        ) VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            kind,
            created_at,
            date_dir,
            json.dumps(args),
            json.dumps(command),
            source,
        ),
    )
    if cursor.lastrowid is None:  # pragma: no cover - sqlite always sets it
        raise RuntimeError("INSERT succeeded but lastrowid is None")
    return int(cursor.lastrowid)


def mark_running(
    conn: sqlite3.Connection,
    *,
    db_id: int,
    pid: int,
    pgid: int,
    pid_start_time: str | None,
    started_at: str,
) -> bool:
    """Promote pending → running. Returns True iff the UPDATE actually
    changed a row.

    The `WHERE status='pending'` guard makes this race-safe: if `_watch`
    has already observed the subprocess exit (and marked the row terminal)
    before this UPDATE lands, the UPDATE is a no-op rather than rolling
    back the terminal status. The caller can use the return value to
    detect the race and skip its own follow-up work.

    `pid_start_time` may be None on platforms where /proc isn't readable
    (macOS); restart-recovery then falls back to "trust the PGID is ours
    if it's still alive" - a weaker guarantee but acceptable on dev
    machines, which is the only place macOS hits this code.
    """
    cursor = conn.execute(
        """
        UPDATE runs
        SET status = 'running', pid = ?, pgid = ?, pid_start_time = ?, started_at = ?
        WHERE id = ? AND status = 'pending'
        """,
        (pid, pgid, pid_start_time, started_at, db_id),
    )
    return cursor.rowcount > 0


def mark_terminal(
    conn: sqlite3.Connection,
    *,
    db_id: int,
    status: RunStatus,
    finished_at: str,
    exit_code: int | None,
    error: str | None = None,
) -> bool:
    """Set the terminal status. Race-safe: refuses to overwrite an existing
    terminal status. Returns True iff the row was updated.

    `status` MUST be terminal. Allowed source statuses are derived from
    the lifecycle transition matrix (`dashboard/api/lifecycle.py`) so the
    semantics stay centralized.
    """
    if status not in TERMINAL_STATUSES_TUPLE:
        raise ValueError(
            "mark_terminal status must be one of "
            f"{TERMINAL_STATUSES_TUPLE}, got {status!r}"
        )
    allowed_from = transition_sources_for(status)  # pending/running for terminals
    placeholders = ", ".join("?" * len(allowed_from))
    cursor = conn.execute(
        f"""
        UPDATE runs
        SET status = ?, finished_at = ?, exit_code = ?, error = ?
        WHERE id = ? AND status IN ({placeholders})
        """,
        (status, finished_at, exit_code, error, db_id, *allowed_from),
    )
    return cursor.rowcount > 0


def find_active_runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return rows in `pending` or `running` state.

    Used at startup to identify orphaned runs from the previous dashboard
    instance - `_watch` tasks die when the process exits, so any row left
    non-terminal at startup was either (a) abandoned by a crash or (b)
    abandoned by a clean shutdown that didn't wait for the subprocess.
    Recovery treats both the same way: PGID-verify, kill if ours, mark
    interrupted.
    """
    placeholders = ", ".join("?" * len(ACTIVE_STATUSES_TUPLE))
    return conn.execute(
        f"SELECT * FROM runs WHERE status IN ({placeholders})",
        ACTIVE_STATUSES_TUPLE,
    ).fetchall()


def find_active_run_for_kind_date(
    conn: sqlite3.Connection, *, kind: str, date_dir: str
) -> sqlite3.Row | None:
    """Return the in-flight (pending/running) row for this kind+date, if any.

    Used by `POST /api/runs` to enforce the per-(kind, date) idempotency
    rule from the plan: a second request returns 409, not a duplicate
    spawn that would race the first for the .tmp dir.
    """
    placeholders = ", ".join("?" * len(ACTIVE_STATUSES_TUPLE))
    query = f"""
        SELECT * FROM runs
        WHERE kind = ? AND date_dir = ? AND status IN ({placeholders})
        LIMIT 1
    """
    return conn.execute(
        query,
        (kind, date_dir, *ACTIVE_STATUSES_TUPLE),
    ).fetchone()


class RunNotDeletable(ValueError):
    """The run is in a state that disallows deletion (pending/running)."""


def delete_run(conn: sqlite3.Connection, db_id: int) -> sqlite3.Row | None:
    """Remove a single run row. Returns the deleted row (for the caller
    to do on-disk cleanup), or None if no row exists with `db_id`.

    Raises `RunNotDeletable` if the row is `pending` or `running` -
    refusing to delete an in-flight run is the operator-friendly choice
    (the alternative is killing the subprocess + cleaning up; that's a
    separate cancel route, not a delete).

    DB-level only: caller is responsible for `data/<kind>/<date>/<run_id>/`
    and log-file cleanup. Splitting that out keeps this function pure
    SQL and tests-without-tmpfile-side-effects.
    """
    row = get_run(conn, db_id)
    if row is None:
        return None
    if row["status"] in NON_DELETABLE_STATUSES_TUPLE:
        raise RunNotDeletable(
            f"run id={db_id} is {row['status']!r}; refusing to delete an "
            "in-flight run. Wait for it to terminate first."
        )
    conn.execute("DELETE FROM runs WHERE id = ?", (db_id,))
    return row


def _retention_timestamp_for_row(row: sqlite3.Row) -> datetime | None:
    """Best-effort timestamp for retention comparisons.

    `created_at` is preferred; if it is malformed (legacy/bad data),
    we fall back to `date_dir`. Rows with neither parseable value are
    skipped by prune selection to avoid accidental deletion.
    """
    created_at = row["created_at"]
    if isinstance(created_at, str):
        try:
            return datetime.strptime(created_at, _RETENTION_DATETIME_FORMAT)
        except ValueError:
            pass
    date_dir = row["date_dir"]
    if isinstance(date_dir, str):
        try:
            return datetime.strptime(date_dir, _RETENTION_DATE_FORMAT)
        except ValueError:
            pass
    return None


def find_prunable_runs(
    conn: sqlite3.Connection,
    *,
    older_than_days: int,
    now: datetime,
    include_sources: tuple[str, ...] = _RETENTION_DEFAULT_SOURCES,
    limit: int = 1000,
) -> list[sqlite3.Row]:
    """Select terminal rows older than the retention cutoff.

    Manual retention defaults to pruning only dashboard/CLI rows; discovered
    rows stay untouched unless the caller opts in via `include_sources`.
    """
    if older_than_days < 1:
        raise ValueError(f"older_than_days must be >= 1, got {older_than_days}")
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    if not include_sources:
        return []

    status_placeholders = ", ".join("?" * len(TERMINAL_STATUSES_TUPLE))
    source_placeholders = ", ".join("?" * len(include_sources))
    rows = conn.execute(
        f"""
        SELECT *
        FROM runs
        WHERE status IN ({status_placeholders})
          AND source IN ({source_placeholders})
        """,
        (*TERMINAL_STATUSES_TUPLE, *include_sources),
    ).fetchall()

    cutoff = now - timedelta(days=older_than_days)
    candidates: list[tuple[datetime, sqlite3.Row]] = []
    for row in rows:
        row_ts = _retention_timestamp_for_row(row)
        if row_ts is None:
            continue
        if row_ts < cutoff:
            candidates.append((row_ts, row))

    candidates.sort(key=lambda item: (item[0], item[1]["id"]))
    return [row for _, row in candidates[:limit]]


def prune_runs_by_id(
    conn: sqlite3.Connection,
    *,
    db_ids: list[int],
) -> int:
    """Delete rows by db id. Returns number of deleted rows.

    Defensive: ignores `pending`/`running` ids even if a buggy caller passes
    them in. Retention must never delete in-flight rows.
    """
    if not db_ids:
        return 0
    placeholders = ", ".join("?" * len(db_ids))
    rows = conn.execute(
        f"SELECT id, status FROM runs WHERE id IN ({placeholders})",
        db_ids,
    ).fetchall()
    deletable_ids = [
        int(row["id"])
        for row in rows
        if row["status"] not in NON_DELETABLE_STATUSES_TUPLE
    ]
    if not deletable_ids:
        return 0
    placeholders = ", ".join("?" * len(deletable_ids))
    cursor = conn.execute(
        f"DELETE FROM runs WHERE id IN ({placeholders})",
        deletable_ids,
    )
    return cursor.rowcount


__all__ = [
    "RUN_KINDS_TUPLE",
    "RUN_STATUSES_TUPLE",
    "RUN_SOURCES_TUPLE",
    "MIGRATIONS",
    "connect",
    "connection_scope",
    "get_user_version",
    "set_user_version",
    "apply_migrations",
    "init_db",
    "insert_discovered_run",
    "insert_pending_run",
    "mark_running",
    "mark_terminal",
    "find_active_runs",
    "find_active_run_for_kind_date",
    "list_runs",
    "get_run",
    "delete_run",
    "find_prunable_runs",
    "prune_runs_by_id",
    "RunNotDeletable",
]
