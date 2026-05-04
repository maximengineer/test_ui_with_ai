"""Existing-data sync (Phase C.1).

Walks `data/<kind>/<DD-MM-YYYY>/<run_id>/manifest.json` and inserts a
`source='discovered'` row into `runs` for every manifest that isn't
already represented by `run_id`.

Why this exists:
  - The dashboard is being added on top of historical CLI runs. Without
    a backfill the operator opens the dashboard for the first time and
    sees an empty list, which contradicts what `ls data/baseline/` shows.
  - Sync is idempotent (UNIQUE constraint on `run_id` swallows duplicates),
    so it's safe to call on every dashboard startup.
  - It scans, not subscribes — a manifest written *after* sync ran needs
    a re-sync (`POST /api/sync`) or a dashboard restart to appear.

Manifest status `complete` maps to dashboard status `done`. Any other
manifest status (`running`, `failed`, `interrupted`) is preserved as-is —
they're already in our vocabulary. A `running` discovered run means the
manifest was written by a CLI invocation that's still in flight; we don't
adopt it (no PID/PGID means we can't manage its lifecycle), but we surface
it so the operator sees what's happening.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from loguru import logger

from test_ui.common.manifest import MANIFEST_FILENAME, read_manifest
from test_ui.common.run_id import is_valid_run_id
from test_ui.config import settings

from .db import insert_discovered_run


def _kind_root(kind: str) -> Path:
    """Return the on-disk root for a kind. KeyError on typo (closed set)."""
    # `[kind]` not `.get(kind)` — every caller iterates the closed tuple
    # of valid kinds, so a typo should explode loudly here rather than
    # silently return None and skip a whole kind.
    return {
        "baseline": settings.baseline_dir,
        "current": settings.current_dir,
        "comparator": settings.comparator_dir,
        "report": settings.report_dir,
    }[kind]


def _safe_iterdir(p: Path) -> list[Path]:
    """`list(p.iterdir())` that swallows races + permission errors.

    A `data/` tree on a network mount, or one being concurrently
    `rm -rf`'d by an operator, can vanish between our `.exists()` check
    and the actual iteration. Treat that as "no children" rather than
    bubbling a 500 from the API. PermissionError on a sub-tree owned
    by root is the same idea — log it, return empty, keep going.
    """
    try:
        return list(p.iterdir())
    except FileNotFoundError:
        return []
    except PermissionError as e:
        logger.warning(f"sync: permission denied iterating {p}: {e}")
        return []


def _scan_kind(kind: str) -> list[tuple[str, Path]]:
    """Find all `<run_id>/manifest.json` paths for one kind.

    Returns `[(date_dir_name, run_dir_path), ...]`. Invalid ULIDs and
    `.tmp-*` workspace dirs are filtered here so the caller doesn't have
    to know the layout convention. A missing kind root is not an error
    (operator may have only ever run baselines, for example).
    """
    root = _kind_root(kind)
    if root is None or not root.exists():
        return []

    found: list[tuple[str, Path]] = []
    # `is_dir()` follows symlinks deliberately. Operators commonly maintain
    # convenience symlinks (e.g. `data/baseline/latest -> 15-03-2026`, or
    # date dirs pointing to a network archive). Skipping symlinks would
    # break those legitimate setups, and the threat the skip would defend
    # against (a malicious symlink into /etc) is moot for a local-first
    # dev tool whose data dir is operator-owned — anyone who can write to
    # data/ can also write a manifest.json directly.
    for date_dir in _safe_iterdir(root):
        if not date_dir.is_dir():
            continue
        for run_dir in _safe_iterdir(date_dir):
            if not run_dir.is_dir():
                continue
            if not is_valid_run_id(run_dir.name):
                # Skips both `.tmp-<id>` workspaces and any pre-B.1 legacy
                # dirs that don't follow the ULID naming. Those need a
                # separate migration; sync only handles the ULID layout.
                continue
            if not (run_dir / MANIFEST_FILENAME).exists():
                continue
            found.append((date_dir.name, run_dir))
    return found


def _manifest_status_to_run_status(manifest_status: str) -> str:
    """Translate manifest vocabulary → dashboard vocabulary.

    Only `complete → done` actually changes; the others are passthrough.
    Centralizing the map (vs. inlining a string compare) makes future
    additions to either vocabulary obvious to find.
    """
    return "done" if manifest_status == "complete" else manifest_status


def sync_runs(conn: sqlite3.Connection) -> tuple[int, int]:
    """Backfill discovered rows for any on-disk manifest not already in `runs`.

    Returns `(scanned, inserted)`. `scanned` is the total number of manifests
    seen across all kinds; `inserted` is how many new rows landed (the
    rest were UNIQUE-conflict skips, which is the success path on a re-run).

    Doesn't open its own connection — the caller passes one in so this
    can run inside a request, in startup, or in a test fixture without
    each call knowing about `settings.runs_db_path`.
    """
    scanned = 0
    inserted = 0
    for kind in ("baseline", "current", "comparator", "report"):
        for date_dir_name, run_dir in _scan_kind(kind):
            scanned += 1
            try:
                manifest = read_manifest(run_dir)
            except Exception as e:
                # Two cases land here:
                #   1. Genuinely-corrupt manifest (operator should clean up).
                #   2. Mid-write race — a CLI run is currently writing
                #      manifest.json and we caught it half-flushed. Benign:
                #      the next sync (manual or restart) will see the
                #      complete file. This is the deliberate trade for not
                #      taking a filesystem lock during sync.
                # Either way: skip + log, don't insert a half-known row.
                logger.warning(
                    f"sync: skipping unreadable manifest at {run_dir} "
                    "(corruption or mid-write race): "
                    f"{type(e).__name__}: {e}"
                )
                continue
            # `created_at` semantics: when this row was inserted into the DB.
            # For dashboard-spawned runs (Phase C.2) this will also be set to
            # `now()` at INSERT time, so the column is comparable across
            # discovered and dashboard rows. The manifest's actual start
            # timestamp lives in `started_at` (the next field).
            new_id = insert_discovered_run(
                conn,
                run_id=manifest.run_id,
                kind=manifest.kind,
                status=_manifest_status_to_run_status(manifest.status),
                created_at=settings.get_current_datetime(),
                started_at=manifest.started_at,
                finished_at=manifest.finished_at,
                date_dir=date_dir_name,
            )
            if new_id is not None:
                inserted += 1
    return scanned, inserted


__all__ = ["sync_runs"]
