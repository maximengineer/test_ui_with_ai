#!/usr/bin/env python3
"""scripts/prune_runs.py - manual retention prune for dashboard runs.

Deletes old terminal rows from `data/dashboard.db` and removes matching
artifacts under:
  - data/<kind>/<DD-MM-YYYY>/<run_id>/
  - data/runs/<db_id>.log
  - data/runs/<run_id>.run.json

Defaults:
  - `AFR_RETENTION_DAYS=30`
  - prune only `source in ('dashboard','cli')`
  - dry-run off

Example (inside dashboard container):
    cat scripts/prune_runs.py | docker exec -i test_ui_with_ai-dashboard-1 python -
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from dashboard.api import db as dbmod
from test_ui.config import settings


RETENTION_DAYS = int(os.environ.get("AFR_RETENTION_DAYS", "30"))
RETENTION_LIMIT = int(os.environ.get("AFR_RETENTION_LIMIT", "1000"))
INCLUDE_DISCOVERED = os.environ.get("AFR_RETENTION_INCLUDE_DISCOVERED", "").lower() in (
    "1",
    "true",
    "yes",
)
DRY_RUN = os.environ.get("AFR_RETENTION_DRY_RUN", "").lower() in ("1", "true", "yes")


def _kind_root(kind: str) -> Path | None:
    return {
        "baseline": settings.baseline_dir,
        "current": settings.current_dir,
        "comparator": settings.comparator_dir,
        "report": settings.report_dir,
    }.get(kind)


def _date_dir_has_published_run(date_dir: Path) -> bool:
    try:
        for entry in date_dir.iterdir():
            if entry.name.startswith("."):
                continue
            if entry.is_symlink():
                continue
            try:
                if entry.is_dir():
                    return True
            except OSError:
                continue
    except OSError:
        return False
    return False


def _prune_empty_date_dir(date_dir: Path) -> None:
    if not date_dir.exists() or not date_dir.is_dir():
        return
    if _date_dir_has_published_run(date_dir):
        return
    try:
        for entry in date_dir.iterdir():
            if entry.is_symlink() or entry.is_file():
                entry.unlink()
            elif entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
        date_dir.rmdir()
    except OSError as e:
        print(
            f"WARN: failed to prune empty date dir {date_dir}: {type(e).__name__}: {e}",
            file=sys.stderr,
        )


def _delete_artifacts_for_row(row) -> list[str]:
    removed: list[str] = []
    kind = row["kind"]
    date_dir = row["date_dir"]
    run_id = row["run_id"]
    db_id = row["id"]

    kind_root = _kind_root(kind)
    if kind_root is not None and isinstance(date_dir, str) and isinstance(run_id, str):
        run_root = kind_root / date_dir / run_id
        if run_root.exists():
            try:
                shutil.rmtree(run_root)
                removed.append(str(run_root))
            except OSError as e:
                print(
                    f"WARN: failed to remove {run_root}: {type(e).__name__}: {e}",
                    file=sys.stderr,
                )
        _prune_empty_date_dir(kind_root / date_dir)

    if settings.runs_log_dir is not None:
        for path in (
            settings.runs_log_dir / f"{db_id}.log",
            settings.runs_log_dir / f"{run_id}.run.json",
        ):
            try:
                if path.exists():
                    path.unlink()
                    removed.append(str(path))
            except OSError as e:
                print(
                    f"WARN: failed to remove {path}: {type(e).__name__}: {e}",
                    file=sys.stderr,
                )
    return removed


def main() -> None:
    if settings.runs_db_path is None:
        raise RuntimeError("settings.runs_db_path is None")
    if RETENTION_DAYS < 1:
        raise ValueError(f"AFR_RETENTION_DAYS must be >= 1, got {RETENTION_DAYS}")
    if RETENTION_LIMIT < 1:
        raise ValueError(f"AFR_RETENTION_LIMIT must be >= 1, got {RETENTION_LIMIT}")

    include_sources: tuple[str, ...] = (
        ("dashboard", "cli", "discovered")
        if INCLUDE_DISCOVERED
        else ("dashboard", "cli")
    )
    now = datetime.strptime(settings.get_current_datetime(), settings.datetime_format)

    with dbmod.connection_scope(settings.runs_db_path) as conn:
        candidates = dbmod.find_prunable_runs(
            conn,
            older_than_days=RETENTION_DAYS,
            now=now,
            include_sources=include_sources,
            limit=RETENTION_LIMIT,
        )

        deleted_rows = candidates
        deleted_count = 0
        if not DRY_RUN:
            ids = [int(row["id"]) for row in candidates]
            deleted_count = dbmod.prune_runs_by_id(conn, db_ids=ids)
            deleted_rows = [
                row for row in candidates if dbmod.get_run(conn, int(row["id"])) is None
            ]
        else:
            deleted_count = len(candidates)

    removed_paths: list[str] = []
    if not DRY_RUN:
        for row in deleted_rows:
            removed_paths.extend(_delete_artifacts_for_row(row))

    print(
        json.dumps(
            {
                "dry_run": DRY_RUN,
                "retention_days": RETENTION_DAYS,
                "retention_limit": RETENTION_LIMIT,
                "include_sources": list(include_sources),
                "candidates": len(candidates),
                "would_delete_rows": len(candidates),
                "deleted_rows": deleted_count if not DRY_RUN else 0,
                "deleted_db_ids": [int(row["id"]) for row in deleted_rows],
                "removed_paths_count": len(removed_paths),
                "removed_paths_sample": removed_paths[:25],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
