"""Workflow precondition tests (Phase B.2.2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from test_ui.common.locks import LockFile, LOCK_FILENAME, write_lock
from test_ui.common.manifest import Manifest, write_manifest
from test_ui.common.preconditions import (
    PreconditionFailed,
    require_complete_run,
    require_no_live_lock,
)


def _seed_run(
    date_dir: Path, run_id: str, *, status: str, kind: str = "baseline"
) -> Path:
    run_dir = date_dir / run_id
    run_dir.mkdir(parents=True)
    write_manifest(
        run_dir,
        Manifest(
            run_id=run_id,
            kind=kind,
            started_at="01-01-2099 00:00:00",
            status=status,
            finished_at="01-01-2099 00:00:01" if status != "running" else None,
        ),
    )
    return run_dir


# ---------------------------------------------------------------------------
# require_complete_run
# ---------------------------------------------------------------------------


def test_require_complete_run_returns_run_dir_when_complete(tmp_path):
    date = "01-01-2099"
    run_id = "01HXX0000000000000000000A0"
    expected = _seed_run(tmp_path / date, run_id, status="complete")

    actual = require_complete_run(tmp_path, date, kind_label="baseline")
    assert actual == expected


def test_require_complete_run_raises_when_no_date_dir(tmp_path):
    with pytest.raises(PreconditionFailed, match="No baseline runs found"):
        require_complete_run(tmp_path, "01-01-2099", kind_label="baseline")


def test_require_complete_run_raises_when_only_running(tmp_path):
    """Running runs aren't usable; precondition must refuse."""
    date = "01-01-2099"
    _seed_run(tmp_path / date, "01HXX0000000000000000000A0", status="running")

    with pytest.raises(PreconditionFailed, match="No complete .* run found"):
        require_complete_run(tmp_path, date, kind_label="baseline")


def test_require_complete_run_raises_when_failed(tmp_path):
    date = "01-01-2099"
    _seed_run(tmp_path / date, "01HXX0000000000000000000A0", status="failed")

    with pytest.raises(PreconditionFailed):
        require_complete_run(tmp_path, date, kind_label="comparator")


def test_require_complete_run_status_hint_in_error(tmp_path):
    """Each non-complete status gets a friendly hint."""
    date = "01-01-2099"
    _seed_run(tmp_path / date, "01HXX0000000000000000000A0", status="failed")

    with pytest.raises(PreconditionFailed, match="errored out"):
        require_complete_run(tmp_path, date, kind_label="baseline")


def test_require_complete_run_legacy_layout_passes(tmp_path):
    """Legacy date-only layout (no ULID subdir) is trusted on faith - the
    migration grace period works without forcing a manifest into existence."""
    date_dir = tmp_path / "01-01-2099"
    (date_dir / "example.com").mkdir(parents=True)  # url_dir, not run_id

    result = require_complete_run(tmp_path, "01-01-2099", kind_label="baseline")
    assert result == date_dir


# ---------------------------------------------------------------------------
# require_no_live_lock
# ---------------------------------------------------------------------------


def test_require_no_live_lock_passes_for_empty_dir(tmp_path):
    """No locks present → no objection."""
    require_no_live_lock(tmp_path / "01-01-2099", kind_label="baseline")  # no raise


def test_require_no_live_lock_raises_for_self_held_lock(tmp_path):
    """A lock from the running test process is, by definition, alive."""
    date_dir = tmp_path / "01-01-2099"
    tmp_run = date_dir / ".tmp-01HXX0000000000000000000A0"
    tmp_run.mkdir(parents=True)
    write_lock(tmp_run, command="afr snapshot")

    with pytest.raises(
        PreconditionFailed, match="Another baseline run is already in progress"
    ):
        require_no_live_lock(date_dir, kind_label="baseline")


def test_require_no_live_lock_passes_when_only_stale_locks(tmp_path):
    """A dead-PID lock should be treated as not-held (find_live_lock_in_date
    logs a warning + skips it)."""
    date_dir = tmp_path / "01-01-2099"
    tmp_run = date_dir / ".tmp-01HZZ0000000000000000000B0"
    tmp_run.mkdir(parents=True)

    stale = LockFile(
        pid=2**31 - 1,  # definitely-dead PID
        pgid=2**31 - 1,
        hostname="ghost",
        started_at="01-01-1999 00:00:00",
        command="dead",
    )
    (tmp_run / LOCK_FILENAME).write_text(stale.model_dump_json(), encoding="utf-8")

    require_no_live_lock(date_dir, kind_label="baseline")  # no raise
