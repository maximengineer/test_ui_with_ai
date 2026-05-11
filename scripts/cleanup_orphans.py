#!/usr/bin/env python3
"""scripts/cleanup_orphans.py - reap leftover artifacts that no longer
correspond to a row in the dashboard's `runs` table.

Two classes of orphan accumulate over time:

  1. `runs_log_dir/<RUN_ID>.run.json` files for run_ids the DB no longer
     knows about. These leak when a row is deleted via a path that
     pre-dates `_cleanup_run_artifacts`, or when the DB is rebuilt
     from a stale on-disk state.

  2. Empty date dirs with dangling `latest` symlinks
     (`data/<kind>/<DATE>/latest -> <run_id_that_was_deleted>`). These
     leak when run-deletes happened before the `_prune_empty_date_dir`
     cleanup landed, or when the `latest` target is rmtree'd by an
     out-of-band cleanup.

Both are user-invisible (the dashboard's read paths already filter
them out: `/api/dates` skips empty date dirs via
`_date_dir_has_published_run`; the runs list reads the DB, not the
log files). But they consume disk + slow down ls and would eventually
matter at scale. Run periodically as `make clean-orphans`.

# Usage

    cat scripts/cleanup_orphans.py \\
      | docker exec -i test_ui_with_ai-dashboard-1 python -

Outputs a JSON manifest of what was reaped. Pass `AFR_CLEANUP_DRY_RUN=1`
to scan without deleting (useful before the first run on a hot system).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

# Defaults match the in-container layout. Override via env if needed.
DATA_ROOT = Path(os.environ.get("AFR_DATA_ROOT", "/data"))
DB_PATH = Path(os.environ.get("AFR_DB_PATH", "/data/dashboard.db"))
DRY_RUN = os.environ.get("AFR_CLEANUP_DRY_RUN", "").lower() in ("1", "true", "yes")

KIND_DIRS = ("baseline", "current", "comparator", "report")


def _alive_run_ids(db_path: Path) -> set[str]:
    """Set of run_ids currently in the DB. Empty set if the DB is
    missing (then we don't reap anything - safer than nuking everything
    when the DB just isn't accessible)."""
    if not db_path.exists():
        print(f"WARN: db missing at {db_path}; skipping log-file cleanup")
        return set()
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT run_id FROM runs").fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def _alive_db_ids(db_path: Path) -> set[int]:
    if not db_path.exists():
        return set()
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT id FROM runs").fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def reap_orphan_log_files(
    runs_dir: Path, alive_run_ids: set[str], alive_db_ids: set[int]
) -> list[str]:
    """Remove `.run.json` and `<db_id>.log` files whose ids aren't in DB.

    Preserves files for the live runs (alive_run_ids / alive_db_ids).
    Returns the list of reaped paths (relative to runs_dir).
    """
    if not runs_dir.exists():
        return []
    reaped: list[str] = []
    for entry in sorted(runs_dir.iterdir()):
        name = entry.name
        if name.endswith(".run.json"):
            run_id = name.removesuffix(".run.json")
            if run_id in alive_run_ids:
                continue
        elif name.endswith(".log"):
            try:
                file_id = int(name.removesuffix(".log"))
            except ValueError:
                continue
            if file_id in alive_db_ids:
                continue
        else:
            continue  # leave unknown files alone
        if not DRY_RUN:
            try:
                entry.unlink()
            except OSError as e:
                print(f"WARN: failed to unlink {entry}: {e}", file=sys.stderr)
                continue
        reaped.append(name)
    return reaped


def reap_empty_date_dirs(data_root: Path) -> list[str]:
    """Remove `<kind>/<DATE>/` dirs that have no real run subdir
    (only stray `latest` symlink, only `.tmp-*` dirs, etc.).

    Mirrors `_date_dir_has_published_run` from the dashboard's routes:
    a date dir is "empty" if it contains no non-symlink, non-dot
    directory. Removes any `latest` symlink + the date dir itself.
    """
    reaped: list[str] = []
    for kind in KIND_DIRS:
        kind_root = data_root / kind
        if not kind_root.exists():
            continue
        for date_dir in sorted(kind_root.iterdir()):
            if not date_dir.is_dir():
                continue
            # Has any real published run subdir?
            has_run = False
            try:
                for entry in date_dir.iterdir():
                    if entry.name.startswith("."):
                        continue
                    if entry.is_symlink():
                        continue
                    if entry.is_dir():
                        has_run = True
                        break
            except OSError:
                continue
            if has_run:
                continue
            # Empty - remove `latest` symlink + the dir.
            if not DRY_RUN:
                try:
                    for entry in date_dir.iterdir():
                        if entry.is_symlink() or entry.is_file():
                            entry.unlink()
                        elif entry.is_dir():
                            shutil.rmtree(entry, ignore_errors=True)
                    date_dir.rmdir()
                except OSError as e:
                    print(
                        f"WARN: failed to prune {date_dir}: {e}",
                        file=sys.stderr,
                    )
                    continue
            reaped.append(str(date_dir.relative_to(data_root)))
    return reaped


def main() -> None:
    alive_run_ids = _alive_run_ids(DB_PATH)
    alive_db_ids = _alive_db_ids(DB_PATH)
    runs_dir = DATA_ROOT / "runs"

    reaped_logs = reap_orphan_log_files(runs_dir, alive_run_ids, alive_db_ids)
    reaped_dirs = reap_empty_date_dirs(DATA_ROOT)

    print(
        json.dumps(
            {
                "dry_run": DRY_RUN,
                "data_root": str(DATA_ROOT),
                "db_path": str(DB_PATH),
                "alive_run_ids": len(alive_run_ids),
                "alive_db_ids": len(alive_db_ids),
                "reaped_log_files": {
                    "count": len(reaped_logs),
                    "samples": reaped_logs[:10],
                    "more": max(0, len(reaped_logs) - 10),
                },
                "reaped_empty_date_dirs": {
                    "count": len(reaped_dirs),
                    "paths": reaped_dirs,
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
