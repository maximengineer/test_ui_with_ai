"""Finder + run-id resolution tests (Phase B.1).

Covers `comparator/finder.py`'s new run-aware lookup AND the legacy
fallback. Critical because every read-side consumer (comparator, report)
flows through these functions; a bug here silently picks the wrong data
to compare/report on.

Specifically pins:
  - Latest date dir wins (calendar order, not mtime order).
  - Within a date dir, latest *complete* run wins (ULID-sortable, manifest-checked).
  - Runs with status="running"/"failed" are skipped.
  - The `latest` symlink shortcut is preferred over scanning.
  - Legacy layout (no run_id subdirs) is transparently supported.
  - update_latest_symlink is atomic (no torn state if interrupted).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from test_ui.common.manifest import Manifest, write_manifest
from test_ui.common.run_id import new_run_id
from test_ui.comparator import finder


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run(
    date_dir: Path, run_id: str, *, status: str = "complete", kind: str = "baseline"
) -> Path:
    """Lay out a complete-or-failed run under date_dir/<run_id>/ with manifest."""
    run_dir = date_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
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
# is_valid_date_dir / parse_date_dir — unchanged from A.3
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("01-01-2099", True),
        ("31-12-2099", True),
        ("1-1-2099", False),  # missing zero-padding
        ("00-13-2099", False),  # invalid month
        ("32-01-2099", False),  # invalid day
        ("01-01-99", False),  # 2-digit year
        ("not-a-date", False),
        ("", False),
        ("01_01_2099", False),  # wrong separator
    ],
)
def test_is_valid_date_dir(name, expected):
    assert finder.is_valid_date_dir(name) == expected


# ---------------------------------------------------------------------------
# find_latest_date_dir — unchanged from A.3, just sanity-check we didn't break
# ---------------------------------------------------------------------------


def test_find_latest_date_dir_picks_calendar_max(tmp_path):
    (tmp_path / "01-01-2099").mkdir()
    (tmp_path / "31-12-2099").mkdir()
    (tmp_path / "15-06-2099").mkdir()
    assert finder.find_latest_date_dir(tmp_path).name == "31-12-2099"


def test_find_latest_date_dir_returns_none_for_missing_root(tmp_path):
    assert finder.find_latest_date_dir(tmp_path / "nope") is None


def test_find_latest_date_dir_ignores_non_date_dirs(tmp_path):
    (tmp_path / "01-01-2099").mkdir()
    (tmp_path / "scratch").mkdir()
    (tmp_path / ".git").mkdir()
    assert finder.find_latest_date_dir(tmp_path).name == "01-01-2099"


# ---------------------------------------------------------------------------
# find_latest_run_dir_in_date — new B.1 behavior
# ---------------------------------------------------------------------------


def test_picks_latest_complete_run_by_ulid_sort(tmp_path):
    """ULIDs sort lexicographically; the latest one wins.

    Uses hand-picked ULID strings instead of `new_run_id()` because two ULIDs
    generated in the same millisecond have random ordering between them
    (the bottom 80 bits are random). In production, runs are seconds apart
    so this is fine; in a test we want determinism.
    """
    date_dir = tmp_path / "01-01-2099"
    date_dir.mkdir()

    # Two valid ULIDs in known order. Both have the same shape (26 chars,
    # Crockford base32); only the leading timestamp bytes differ.
    older = "01HXX0000000000000000000A0"  # leading "01HXX..." is older
    newer = "01HZZ0000000000000000000A0"  # leading "01HZZ..." is newer

    _make_run(date_dir, older, status="complete")
    _make_run(date_dir, newer, status="complete")

    result = finder.find_latest_run_dir_in_date(date_dir)
    assert result.name == newer


def test_skips_runs_in_progress_and_failed(tmp_path):
    """A `status="running"` or `"failed"` run must NOT be picked, even if it's
    the lexicographically latest. The next complete run wins."""
    date_dir = tmp_path / "01-01-2099"
    date_dir.mkdir()

    # Hand-picked ordering: complete < running < failed (by lex/time).
    complete_id = "01HXX0000000000000000000A0"
    running_id = "01HYY0000000000000000000A0"
    failed_id = "01HZZ0000000000000000000A0"

    _make_run(date_dir, complete_id, status="complete")
    _make_run(date_dir, running_id, status="running")
    _make_run(date_dir, failed_id, status="failed")

    result = finder.find_latest_run_dir_in_date(date_dir)
    assert result.name == complete_id, "must skip running + failed runs"


def test_returns_none_when_no_complete_run_in_new_layout(tmp_path):
    """All runs running/failed in new-layout date dir → None (don't fall through
    to legacy mode, which would give the date dir itself)."""
    date_dir = tmp_path / "01-01-2099"
    date_dir.mkdir()
    _make_run(date_dir, new_run_id(), status="running")
    _make_run(date_dir, new_run_id(), status="failed")

    assert finder.find_latest_run_dir_in_date(date_dir) is None


def test_legacy_fallback_when_no_run_id_subdirs(tmp_path):
    """Pre-B.1 layout: date_dir contains url_dir subfolders directly. Return
    the date_dir itself so legacy callers keep working during migration grace."""
    date_dir = tmp_path / "01-01-2099"
    date_dir.mkdir()
    (date_dir / "example.com").mkdir()  # url_dir, not a run_id
    (date_dir / "example.org").mkdir()

    result = finder.find_latest_run_dir_in_date(date_dir)
    assert result == date_dir


def test_legacy_fallback_skipped_if_any_run_id_present(tmp_path):
    """Mixed layout (legacy url_dir + new run_id) should NOT silently fall back.
    If any run_id is present, treat it as new layout — legacy entries are
    leftover and should be ignored, not promoted."""
    date_dir = tmp_path / "01-01-2099"
    date_dir.mkdir()
    (date_dir / "stale_url_dir").mkdir()
    run_id = new_run_id()
    _make_run(date_dir, run_id, status="complete")

    result = finder.find_latest_run_dir_in_date(date_dir)
    assert result.name == run_id


# ---------------------------------------------------------------------------
# `latest` symlink shortcut
# ---------------------------------------------------------------------------


def test_latest_symlink_takes_precedence_over_scan(tmp_path):
    """If `latest` resolves to a real dir, return it without scanning ULIDs.

    Important because the symlink is the writer's stated truth — even if a
    scan would pick a different (e.g. lexicographically-later) ULID, we
    trust the symlink.
    """
    date_dir = tmp_path / "01-01-2099"
    date_dir.mkdir()

    older_id = new_run_id()
    newer_id = new_run_id()
    _make_run(date_dir, older_id, status="complete")
    _make_run(date_dir, newer_id, status="complete")

    # Symlink to older — finder should respect it over the newer ULID.
    finder.update_latest_symlink(date_dir, older_id)

    result = finder.find_latest_run_dir_in_date(date_dir)
    assert result.name == older_id


def test_dangling_symlink_falls_through_to_scan(tmp_path):
    """A symlink pointing nowhere must NOT crash — fall back to scanning."""
    date_dir = tmp_path / "01-01-2099"
    date_dir.mkdir()
    run_id = new_run_id()
    _make_run(date_dir, run_id, status="complete")

    # Make the symlink point at a nonexistent target manually.
    (date_dir / finder.LATEST_SYMLINK_NAME).symlink_to("nonexistent-run-id")

    result = finder.find_latest_run_dir_in_date(date_dir)
    assert result.name == run_id, "scan should rescue us from a dangling symlink"


def test_update_latest_symlink_overwrites_atomically(tmp_path):
    """Updating an existing symlink replaces the target without leaving
    a torn intermediate state. Pin the contract (no temp files left behind)."""
    date_dir = tmp_path / "01-01-2099"
    date_dir.mkdir()

    a, b = new_run_id(), new_run_id()
    _make_run(date_dir, a, status="complete")
    _make_run(date_dir, b, status="complete")

    finder.update_latest_symlink(date_dir, a)
    assert (date_dir / "latest").resolve().name == a

    finder.update_latest_symlink(date_dir, b)
    assert (date_dir / "latest").resolve().name == b

    # No leftover temp files (the .latest.tmp-<id> intermediary).
    leftovers = [
        p.name for p in date_dir.iterdir() if p.name.startswith(".latest.tmp-")
    ]
    assert leftovers == [], f"temp symlink leaked: {leftovers}"


def test_update_latest_symlink_refuses_nonexistent_target(tmp_path):
    """If the requested run_id dir doesn't exist, refuse — never create a
    dangling symlink during normal operation."""
    date_dir = tmp_path / "01-01-2099"
    date_dir.mkdir()

    with pytest.raises(FileNotFoundError):
        finder.update_latest_symlink(date_dir, "DOES_NOT_EXIST")


# ---------------------------------------------------------------------------
# find_latest_run_dir (top-level: date + run resolution combined)
# ---------------------------------------------------------------------------


def test_find_latest_run_dir_returns_none_for_missing_root(tmp_path):
    assert finder.find_latest_run_dir(tmp_path / "nope") is None


def test_find_latest_run_dir_returns_none_when_date_has_no_complete_run(tmp_path):
    date_dir = tmp_path / "01-01-2099"
    date_dir.mkdir()
    _make_run(date_dir, new_run_id(), status="running")
    assert finder.find_latest_run_dir(tmp_path) is None


def test_find_latest_run_dir_drills_through_latest_date(tmp_path):
    """Latest date wins, then latest complete run within it."""
    older_date = tmp_path / "01-01-2099"
    newer_date = tmp_path / "02-01-2099"
    older_date.mkdir()
    newer_date.mkdir()

    # Older date has a complete run.
    _make_run(older_date, new_run_id(), status="complete")
    # Newer date has a complete run too — this should win.
    expected = new_run_id()
    _make_run(newer_date, expected, status="complete")

    result = finder.find_latest_run_dir(tmp_path)
    assert result.parent.name == "02-01-2099"
    assert result.name == expected


def test_find_latest_run_dir_legacy_only_root(tmp_path):
    """No new-layout runs anywhere — legacy fallback returns the date dir itself."""
    date_dir = tmp_path / "01-01-2099"
    date_dir.mkdir()
    (date_dir / "example.com").mkdir()  # url_dir, not a run_id

    result = finder.find_latest_run_dir(tmp_path)
    assert result == date_dir


# ---------------------------------------------------------------------------
# Backward-compat aliases
# ---------------------------------------------------------------------------


def test_find_latest_baseline_and_current_aliases(tmp_path):
    """Legacy ComparatorEngine API: find_latest_baseline / find_latest_current
    must still work; both alias to find_latest_run_dir."""
    assert finder.find_latest_baseline is finder.find_latest_run_dir
    assert finder.find_latest_current is finder.find_latest_run_dir
