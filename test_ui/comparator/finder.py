"""Date + run-id directory discovery (Phase B.1.4 — extends Phase A.3 split).

Pure functions for resolving "the latest baseline / current / comparator /
report directory I should read from."

**Two layouts coexist during the migration grace period:**

  - **NEW** (Phase B.1):  ``<root>/<DD-MM-YYYY>/<run_id>/<url_dir>/...``
  - **LEGACY** (pre-B.1):  ``<root>/<DD-MM-YYYY>/<url_dir>/...``

`find_latest_run_dir` handles both transparently. It picks the latest valid
date directory, then:

  1. If a `latest` symlink under that date dir resolves to a real subdir,
     return its target. (Fast path; the crawler updates the symlink at
     publish time.)
  2. Else, scan for ULID-named children — the new run-id subdirs. Sort
     descending (ULIDs are time-sortable), then walk newest→oldest until we
     find one whose `manifest.json` says ``status="complete"``.
  3. Else, treat the date dir itself as the data root (legacy layout).

Callers don't need to know which path was taken — they always treat the
returned `Path` as "the directory containing per-URL subdirectories."
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..common.run_id import is_valid_run_id


LATEST_SYMLINK_NAME = "latest"


# ---------------------------------------------------------------------------
# Date-dir helpers (unchanged from Phase A.3)
# ---------------------------------------------------------------------------


def is_valid_date_dir(dirname: str) -> bool:
    """True iff `dirname` matches the `DD-MM-YYYY` shape we use on disk."""
    try:
        parts = dirname.split("-")
        if len(parts) != 3:
            return False
        day, month, year = parts
        return (
            len(day) == 2
            and day.isdigit()
            and len(month) == 2
            and month.isdigit()
            and len(year) == 4
            and year.isdigit()
            and 1 <= int(day) <= 31
            and 1 <= int(month) <= 12
        )
    except (ValueError, AttributeError):
        return False


def parse_date_dir(dirname: str) -> datetime:
    """Parse a `DD-MM-YYYY` directory name to a datetime (day precision)."""
    day, month, year = dirname.split("-")
    return datetime(int(year), int(month), int(day))


def find_latest_date_dir(root_path: Path) -> Path | None:
    """Return the most-recent valid date subdirectory of `root_path`.

    Returns None if `root_path` doesn't exist OR contains no valid date dirs.
    "Most recent" = the highest calendar date, not filesystem mtime.
    """
    if not root_path.exists():
        return None

    date_dirs = [
        item
        for item in root_path.iterdir()
        if item.is_dir() and is_valid_date_dir(item.name)
    ]
    if not date_dirs:
        return None

    date_dirs.sort(key=lambda x: parse_date_dir(x.name), reverse=True)
    return date_dirs[0]


# ---------------------------------------------------------------------------
# Run-id resolution (Phase B.1)
# ---------------------------------------------------------------------------


def _is_complete_run(run_dir: Path) -> bool:
    """Check the run's manifest reports `status="complete"`.

    Imports manifest module locally to dodge a circular import (manifest →
    config → settings → potentially anything). Returns False for any error
    so a corrupt or missing manifest doesn't make a stale run "win".

    Missing manifest is the common case for in-progress / interrupted runs
    and is silently False. A manifest that exists but fails to parse is
    logged at WARNING — that's a corruption signal worth surfacing.
    """
    try:
        from ..common.manifest import read_manifest

        return read_manifest(run_dir).status == "complete"
    except FileNotFoundError:
        return False
    except Exception as e:
        from loguru import logger

        logger.warning(
            f"Corrupt manifest at {run_dir / 'manifest.json'}: {type(e).__name__}: {e}"
        )
        return False


def find_latest_run_dir_in_date(date_dir: Path) -> Path | None:
    """Return the latest *complete* run within a single date dir, or None.

    Resolution order:
      1. `<date_dir>/latest` symlink → its target if it resolves to a real dir.
      2. Sort ULID-named subdirs descending, return first with complete manifest.
      3. Legacy layout: no ULID subdirs → return `date_dir` itself (treat as
         the run root). Caller has no way to verify completeness in this mode.

    Returns None only if the date dir is gone or contains nothing usable.
    """
    if not date_dir.exists():
        return None

    # Fast path: the symlink (when present) is the truth.
    symlink = date_dir / LATEST_SYMLINK_NAME
    if symlink.is_symlink() or symlink.exists():
        try:
            target = symlink.resolve(strict=True)
            if target.is_dir():
                return target
        except (OSError, RuntimeError):
            # Dangling symlink or symlink loop — fall through to the scan.
            pass

    # Slow path: scan for ULID subdirs, sort by id (ULIDs encode time so
    # lexicographic sort = chronological), prefer complete runs.
    run_dirs = [
        item
        for item in date_dir.iterdir()
        if item.is_dir() and is_valid_run_id(item.name)
    ]
    if run_dirs:
        run_dirs.sort(key=lambda p: p.name, reverse=True)
        for run_dir in run_dirs:
            if _is_complete_run(run_dir):
                return run_dir
        # No complete run found — caller almost certainly doesn't want a
        # half-finished one. Return None and let them complain.
        return None

    # Legacy layout fallback: date dir holds url_dir subfolders directly.
    # We can't verify completeness in this mode; trust on faith for the
    # migration grace period, then drop this branch.
    return date_dir


def find_latest_run_dir(root_path: Path) -> Path | None:
    """Top-level lookup: latest date dir → latest complete run within it.

    The single function every caller (comparator, report) should use to
    answer "where is the most recent baseline / current / comparator output?"
    """
    date_dir = find_latest_date_dir(root_path)
    if date_dir is None:
        return None
    return find_latest_run_dir_in_date(date_dir)


# ---------------------------------------------------------------------------
# `latest` symlink maintenance (called by writers, not readers)
# ---------------------------------------------------------------------------


def update_latest_symlink(date_dir: Path, run_id: str) -> None:
    """Atomically point `<date_dir>/latest` at `<run_id>/`.

    Atomic via the os.symlink → os.replace pattern: write a temp symlink
    next to it, then rename onto the existing one. POSIX rename(2) is
    atomic for symlinks too. If the platform lacks symlink support
    (Windows without dev-mode), silently no-op — readers fall back to
    scanning, which is slower but correct.
    """
    target = date_dir / run_id
    if not target.exists():
        raise FileNotFoundError(f"Cannot symlink to nonexistent target: {target}")

    link = date_dir / LATEST_SYMLINK_NAME
    tmp_link = date_dir / f".{LATEST_SYMLINK_NAME}.tmp-{run_id}"

    try:
        # Relative target so the symlink survives moving the date dir.
        tmp_link.symlink_to(run_id)
        tmp_link.replace(link)
    except (OSError, NotImplementedError):
        # Windows without developer mode raises OSError. Caller's job
        # continues fine — readers will scan for the latest ULID instead.
        if tmp_link.exists() or tmp_link.is_symlink():
            try:
                tmp_link.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Convenience aliases mirroring the old ComparatorEngine API.
# ---------------------------------------------------------------------------

# These now resolve the run subdir, not the date dir, so the comparator gets
# the run root directly. Legacy callers (the engine's classmethods) keep
# their old names to avoid a flag-day rename.
find_latest_baseline = find_latest_run_dir
find_latest_current = find_latest_run_dir


__all__ = [
    "LATEST_SYMLINK_NAME",
    "is_valid_date_dir",
    "parse_date_dir",
    "find_latest_date_dir",
    "find_latest_run_dir_in_date",
    "find_latest_run_dir",
    "update_latest_symlink",
    "find_latest_baseline",
    "find_latest_current",
]
