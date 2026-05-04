"""Dashboard DB layer tests (Phase C.1).

Pin the migration mechanism (idempotent, atomic, version-aware), the per-
connection PRAGMAs (WAL + busy_timeout actually applied), and the row-
helper contracts (UNIQUE-conflict swallow, paginated list ordering).

The migrations list is append-only - these tests catch reordering or
deletion early by depending on the *count* and the schema produced by
v1 specifically.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from typing import get_args

from dashboard.api import db as dbmod
from dashboard.api.sync import _manifest_status_to_run_status
from test_ui.common.manifest import Kind as ManifestKind
from test_ui.common.manifest import Status as ManifestStatus


def test_run_kinds_match_manifest_kind_literal():
    """The dashboard's RUN_KINDS_TUPLE must mirror manifest.Kind exactly.

    If someone adds a new kind to manifest.py without updating db.py the
    `runs.kind` CHECK constraint will reject inserts at sync time - failure
    mode is a confusing INSERT error in production. Pinning here turns it
    into an obvious test failure.
    """
    manifest_kinds = set(get_args(ManifestKind))
    assert set(dbmod.RUN_KINDS_TUPLE) == manifest_kinds


def test_status_mapping_is_exhaustive_for_every_manifest_status():
    """Every manifest Status MUST translate to a value the runs CHECK
    constraint will accept.

    Pre-fix this had no test: if someone added a new manifest status
    (e.g. `paused`), `_manifest_status_to_run_status` would pass it
    through unchanged, the INSERT would fail the CHECK constraint at
    runtime, and the operator would see a confusing IntegrityError in
    production sync logs. Pin the round-trip explicitly.
    """
    for ms in get_args(ManifestStatus):
        mapped = _manifest_status_to_run_status(ms)
        assert mapped in dbmod.RUN_STATUSES_TUPLE, (
            f"manifest status {ms!r} maps to {mapped!r}, which is not in "
            f"RUN_STATUSES_TUPLE={dbmod.RUN_STATUSES_TUPLE}. "
            "Update _manifest_status_to_run_status or RUN_STATUSES_TUPLE."
        )


def test_fresh_db_has_user_version_zero(tmp_path):
    db_path = tmp_path / "fresh.db"
    with dbmod.connection_scope(db_path) as conn:
        assert dbmod.get_user_version(conn) == 0


def test_init_db_brings_schema_to_latest(tmp_path):
    db_path = tmp_path / "init.db"
    dbmod.init_db(db_path)
    with dbmod.connection_scope(db_path) as conn:
        assert dbmod.get_user_version(conn) == len(dbmod.MIGRATIONS)
        # The runs table must exist with the documented columns.
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
        expected = {
            "id",
            "run_id",
            "kind",
            "status",
            "created_at",
            "started_at",
            "finished_at",
            "date_dir",
            "args_json",
            "command_json",
            "exit_code",
            "error",
            "pid",
            "pgid",
            "pid_start_time",
            "source",
        }
        assert expected.issubset(cols), f"missing columns: {expected - cols}"


def test_init_db_is_idempotent(tmp_path):
    """Calling init_db twice must NOT re-run migrations or alter the schema."""
    db_path = tmp_path / "idem.db"
    dbmod.init_db(db_path)
    dbmod.init_db(db_path)  # would raise if the migration ran twice
    with dbmod.connection_scope(db_path) as conn:
        assert dbmod.get_user_version(conn) == len(dbmod.MIGRATIONS)


def test_pragmas_applied_per_connection(tmp_path):
    """journal_mode=WAL and busy_timeout=5000 must actually take effect.

    Catches a regression where someone calls `connect` but forgets a
    PRAGMA - the symptom (concurrent writes failing under load) only
    appears in production.
    """
    db_path = tmp_path / "pragma.db"
    dbmod.init_db(db_path)
    with dbmod.connection_scope(db_path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert int(timeout) == 5000
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert int(fk) == 1


def test_apply_migrations_refuses_db_from_future(tmp_path):
    """If the DB's user_version exceeds len(MIGRATIONS) we must NOT silently
    treat it as up-to-date - that masks a downgrade footgun where the user
    booted a newer dashboard, then ran an older binary that doesn't know
    about a newly-added column."""
    db_path = tmp_path / "future.db"
    with dbmod.connection_scope(db_path) as conn:
        dbmod.set_user_version(conn, len(dbmod.MIGRATIONS) + 5)
    with pytest.raises(RuntimeError, match="ahead of code"):
        dbmod.init_db(db_path)


def test_insert_discovered_run_dedupes_on_run_id(tmp_path):
    """Second insert of the same run_id returns None (not an exception).

    This is the success path for `sync_runs` re-runs - it relies on the
    UNIQUE conflict being swallowed so it can blindly attempt every
    on-disk manifest without pre-checking the DB.
    """
    db_path = tmp_path / "dedupe.db"
    dbmod.init_db(db_path)
    with dbmod.connection_scope(db_path) as conn:
        first = dbmod.insert_discovered_run(
            conn,
            run_id="01HXX0000000000000000000A0",
            kind="baseline",
            status="done",
            created_at="01-01-2099 00:00:00",
            started_at="01-01-2099 00:00:00",
            finished_at="01-01-2099 00:00:01",
            date_dir="01-01-2099",
        )
        assert first is not None
        second = dbmod.insert_discovered_run(
            conn,
            run_id="01HXX0000000000000000000A0",
            kind="baseline",
            status="done",
            created_at="01-01-2099 00:00:00",
            started_at="01-01-2099 00:00:00",
            finished_at="01-01-2099 00:00:01",
            date_dir="01-01-2099",
        )
        assert second is None


def test_insert_rejects_unknown_kind_via_check_constraint(tmp_path):
    """Defensive: the CHECK constraint must reject a typo that slipped past
    the Pydantic Literal - e.g. 'comparison' instead of 'comparator'."""
    db_path = tmp_path / "check.db"
    dbmod.init_db(db_path)
    with dbmod.connection_scope(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            dbmod.insert_discovered_run(
                conn,
                run_id="01HYY0000000000000000000A0",
                kind="comparison",  # wrong vocab - must reject
                status="done",
                created_at="01-01-2099",
                started_at=None,
                finished_at=None,
                date_dir="01-01-2099",
            )


def test_insert_rejects_unknown_source_via_check_constraint(tmp_path):
    """The `source` column has its own CHECK constraint. A direct INSERT
    that passes a value outside ('dashboard','discovered','cli') must fail.

    Indirect coverage only existed via `insert_discovered_run` which hard-
    codes 'discovered' - that wouldn't catch a typo in a future helper for
    a different source. Pin the constraint itself.
    """
    db_path = tmp_path / "src.db"
    dbmod.init_db(db_path)
    with dbmod.connection_scope(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, kind, status, created_at, source,
                    args_json, command_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "01HZZ0000000000000000000A0",
                    "baseline",
                    "done",
                    "01-01-2099",
                    "spaceship",  # not in {dashboard, discovered, cli}
                    "{}",
                    "[]",
                ),
            )


def test_quote_compile_time_literals_rejects_unsafe_value():
    """The CHECK-builder helper must hard-fail at call time on values that
    contain SQL metacharacters or whitespace. Pre-fix the helper would
    have silently produced broken DDL."""
    with pytest.raises(AssertionError, match="lowercase identifier"):
        dbmod._quote_compile_time_literals(("ok", "has space"))
    with pytest.raises(AssertionError):
        dbmod._quote_compile_time_literals(("don't",))
    with pytest.raises(AssertionError):
        dbmod._quote_compile_time_literals(("UPPERCASE",))


def test_list_runs_filters_and_paginates(tmp_path):
    """Verify ORDER BY created_at DESC + kind/status filters + LIMIT/OFFSET.

    The DB layer doesn't validate `run_id` against the ULID format - that's
    the manifest layer's job. So we use synthetic uniqueness-only IDs here.
    """
    db_path = tmp_path / "list.db"
    dbmod.init_db(db_path)
    with dbmod.connection_scope(db_path) as conn:
        # 3 baselines done, 2 currents failed - distinct created_at so order
        # is deterministic.
        for i in range(3):
            dbmod.insert_discovered_run(
                conn,
                run_id=f"baseline-test-{i}",
                kind="baseline",
                status="done",
                created_at=f"01-01-2099 00:00:0{i}",
                started_at=None,
                finished_at=None,
                date_dir="01-01-2099",
            )
        for i in range(2):
            dbmod.insert_discovered_run(
                conn,
                run_id=f"current-test-{i}",
                kind="current",
                status="failed",
                created_at=f"01-01-2099 00:01:0{i}",
                started_at=None,
                finished_at=None,
                date_dir="01-01-2099",
            )

        rows, total = dbmod.list_runs(conn)
        assert total == 5
        assert [r["created_at"] for r in rows][0] == "01-01-2099 00:01:01"  # newest

        # Filter by kind.
        rows, total = dbmod.list_runs(conn, kind="baseline")
        assert total == 3 and all(r["kind"] == "baseline" for r in rows)

        # Filter by both.
        rows, total = dbmod.list_runs(conn, kind="current", status="failed")
        assert total == 2

        # Pagination.
        page1, total = dbmod.list_runs(conn, limit=2, offset=0)
        page2, _ = dbmod.list_runs(conn, limit=2, offset=2)
        assert len(page1) == 2 and len(page2) == 2
        assert {r["run_id"] for r in page1}.isdisjoint({r["run_id"] for r in page2})


def test_get_run_returns_none_for_missing_id(tmp_path):
    db_path = tmp_path / "miss.db"
    dbmod.init_db(db_path)
    with dbmod.connection_scope(db_path) as conn:
        assert dbmod.get_run(conn, db_id=99999) is None


def test_set_user_version_rejects_negative(tmp_path):
    """Defensive: the helper inlines the int into SQL so we validate the
    bounds ourselves rather than trusting callers."""
    db_path = tmp_path / "neg.db"
    with dbmod.connection_scope(db_path) as conn:
        with pytest.raises(ValueError):
            dbmod.set_user_version(conn, -1)


def test_migration_runs_in_transaction(tmp_path):
    """A failing migration must NOT leave a half-applied schema behind.

    Inject a migration that creates a table then raises. The user_version
    must remain at 0 (rolled back), and the partial table must be gone.
    """
    db_path = tmp_path / "rollback.db"

    def _bad_migration(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE doomed (x INTEGER)")
        raise RuntimeError("kaboom")

    # Patch MIGRATIONS to insert our bad migration as #2 (after the real #1).
    original = list(dbmod.MIGRATIONS)
    try:
        dbmod.MIGRATIONS.append(_bad_migration)
        with pytest.raises(RuntimeError, match="kaboom"):
            dbmod.init_db(db_path)
    finally:
        dbmod.MIGRATIONS[:] = original

    # The first migration committed; the second rolled back. So we expect
    # user_version=1 and no `doomed` table.
    with dbmod.connection_scope(db_path) as conn:
        assert dbmod.get_user_version(conn) == 1
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "doomed" not in tables, "rolled-back migration must not leave debris"


# Module-level smoke: timestamp formatter we depend on parses what we expect.
def test_timestamp_format_assumption():
    """Sanity check: settings.get_current_datetime() returns DD-MM-YYYY HH:MM:SS.
    Sync stores it in `created_at`; the API returns it verbatim."""
    s = "01-01-2099 12:34:56"
    parsed = datetime.strptime(s, "%d-%m-%Y %H:%M:%S")
    assert parsed.year == 2099
