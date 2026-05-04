"""Existing-data sync tests (Phase C.1).

Pin: sync walks all four kind dirs, skips invalid run-ids and corrupt
manifests, maps `complete → done`, and is idempotent on re-run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dashboard.api import db as dbmod
from dashboard.api.sync import sync_runs
from test_ui.common.manifest import Manifest, write_manifest
from test_ui.config import settings


def _seed_manifest(
    kind_root: Path, date: str, run_id: str, *, status: str = "complete"
) -> Path:
    """Materialize a `<kind_root>/<date>/<run_id>/manifest.json`."""
    run_dir = kind_root / date / run_id
    run_dir.mkdir(parents=True)
    write_manifest(
        run_dir,
        Manifest(
            run_id=run_id,
            kind="baseline" if "baseline" in str(kind_root) else "current",
            started_at="01-01-2099 00:00:00",
            finished_at="01-01-2099 00:00:01",
            status=status,
            url_count=3,
        ),
    )
    return run_dir


@pytest.fixture
def sync_env(tmp_path, monkeypatch):
    """Wire `settings` to a tmp data root + return an open DB connection.

    Sync reads `settings.{baseline,current,comparator,report}_dir` directly,
    so monkeypatching them isolates the test from the real `data/` tree.
    """
    data_root = tmp_path / "data"
    monkeypatch.setattr(settings, "data_root", data_root)
    monkeypatch.setattr(settings, "baseline_dir", data_root / "baseline")
    monkeypatch.setattr(settings, "current_dir", data_root / "current")
    monkeypatch.setattr(settings, "comparator_dir", data_root / "comparator")
    monkeypatch.setattr(settings, "report_dir", data_root / "report")

    db_path = tmp_path / "dashboard.db"
    monkeypatch.setattr(settings, "runs_db_path", db_path)
    dbmod.init_db(db_path)
    with dbmod.connection_scope(db_path) as conn:
        yield conn


def test_sync_inserts_one_row_per_manifest(sync_env):
    base = settings.baseline_dir
    _seed_manifest(base, "01-01-2099", "01HXX0000000000000000000A0")
    _seed_manifest(base, "02-01-2099", "01HYY0000000000000000000A0")

    scanned, inserted = sync_runs(sync_env)
    assert (scanned, inserted) == (2, 2)


def test_sync_is_idempotent(sync_env):
    """Re-running sync must NOT insert duplicate rows for the same run_id."""
    _seed_manifest(settings.baseline_dir, "01-01-2099", "01HXX0000000000000000000A0")
    sync_runs(sync_env)

    scanned, inserted = sync_runs(sync_env)
    assert scanned == 1
    assert inserted == 0, "second sync of the same manifest must insert nothing"


def test_sync_skips_invalid_run_id_dirs(sync_env):
    """`.tmp-<id>` and any non-ULID dir must NOT be scanned."""
    base = settings.baseline_dir
    # Real run.
    _seed_manifest(base, "01-01-2099", "01HXX0000000000000000000A0")
    # Junk dirs that should be ignored.
    (base / "01-01-2099" / ".tmp-in-progress").mkdir()
    (base / "01-01-2099" / "garbage_dir").mkdir()
    (base / "01-01-2099" / "garbage_dir" / "manifest.json").write_text("{}")

    scanned, inserted = sync_runs(sync_env)
    assert (scanned, inserted) == (1, 1)


def test_sync_maps_complete_to_done(sync_env):
    """The `complete → done` vocabulary translation must apply at insert time."""
    _seed_manifest(
        settings.baseline_dir,
        "01-01-2099",
        "01HXX0000000000000000000A0",
        status="complete",
    )
    sync_runs(sync_env)
    rows, _ = dbmod.list_runs(sync_env, kind="baseline")
    assert len(rows) == 1
    assert rows[0]["status"] == "done"


@pytest.mark.parametrize("manifest_status", ["failed", "interrupted", "running"])
def test_sync_passes_through_non_complete_statuses(sync_env, manifest_status):
    """`failed`, `interrupted`, `running` are already in our vocabulary —
    must be stored verbatim, not coerced."""
    _seed_manifest(
        settings.baseline_dir,
        "01-01-2099",
        "01HXX0000000000000000000A0",
        status=manifest_status,
    )
    sync_runs(sync_env)
    rows, _ = dbmod.list_runs(sync_env, kind="baseline")
    assert rows[0]["status"] == manifest_status


def test_sync_skips_corrupt_manifest_without_aborting(sync_env, tmp_path):
    """A single corrupt manifest must not stop the rest of the sync."""
    base = settings.baseline_dir
    _seed_manifest(base, "01-01-2099", "01HXX0000000000000000000A0")  # good
    bad_dir = base / "01-01-2099" / "01HZZ0000000000000000000A0"
    bad_dir.mkdir()
    (bad_dir / "manifest.json").write_text("{not json")  # bad

    scanned, inserted = sync_runs(sync_env)
    # Both manifests scanned; only the readable one inserted. The corrupt
    # one is logged + skipped (not raised).
    assert (scanned, inserted) == (2, 1)


def test_sync_handles_missing_kind_root(sync_env):
    """If `comparator/` doesn't exist on disk, sync just skips that kind —
    no error. (Common case: operator hasn't run any comparators yet.)"""
    # No setup at all — the `sync_env` fixture creates the DB but no kind dirs.
    scanned, inserted = sync_runs(sync_env)
    assert (scanned, inserted) == (0, 0)


def test_sync_records_date_dir_correctly(sync_env):
    """`runs.date_dir` must hold the DD-MM-YYYY string from the directory
    name, not from the manifest (which is a different field with a different
    purpose). The frontend filters by date_dir."""
    _seed_manifest(settings.baseline_dir, "15-03-2099", "01HXX0000000000000000000A0")
    sync_runs(sync_env)
    rows, _ = dbmod.list_runs(sync_env, kind="baseline")
    assert rows[0]["date_dir"] == "15-03-2099"
