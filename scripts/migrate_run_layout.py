"""One-time migration from the pre-B.1 date-only layout to the run_id layout.

Pre-B.1 layout: ``data/<kind>/DD-MM-YYYY/<url_dir>/...``
Post-B.1 layout: ``data/<kind>/DD-MM-YYYY/<run_id>/<url_dir>/...``

The migration walks each `data/<kind>/<date>/` directory whose children are
URL dirs (legacy), generates a single ULID `run_id` per (kind, date) pair
with a timestamp matching the date dir's mtime, moves every url_dir into
`<date>/<run_id>/`, and writes a `complete`-status manifest so the
finder + discovery code treats it as a usable run.

**Idempotent.** Re-running on an already-migrated tree is a no-op:

  - If `<date>/` already contains ULID-named subdirs (B.1 layout already
    in place), the date is skipped entirely — never moves data twice.
  - If a `<date>/` is empty or contains only the `latest` symlink and
    leftover state, also skipped.

Usage::

    python scripts/migrate_run_layout.py [--data-root data/] [--dry-run]

Default `--data-root` is the project's `settings.data_root`. With
`--dry-run`, prints what would happen without touching disk.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ulid import ULID  # noqa: E402

from test_ui.common.manifest import (  # noqa: E402
    Manifest,
    compute_files_sha256,
    write_manifest,
)
from test_ui.common.run_id import is_valid_run_id  # noqa: E402
from test_ui.comparator.finder import is_valid_date_dir  # noqa: E402


KINDS = ("baseline", "current", "comparator", "report")


def _format_dublin(epoch_seconds: float) -> str:
    """Format an epoch timestamp as DD-MM-YYYY HH:MM:SS Dublin-style.

    Imports settings lazily so the script can also be invoked outside an
    initialized project (CI test fixtures etc.). Falls back to UTC if the
    project's settings module isn't loadable for any reason.
    """
    try:
        from test_ui.config import settings  # local import for dodge

        # Use the project's own formatter for consistency with other timestamps.
        from datetime import datetime

        import pytz

        tz = pytz.timezone(settings.timezone)
        return datetime.fromtimestamp(epoch_seconds, tz=tz).strftime(
            "%d-%m-%Y %H:%M:%S"
        )
    except Exception:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime(
            "%d-%m-%Y %H:%M:%S"
        )


def _date_dir_already_migrated(date_dir: Path) -> bool:
    """True if `date_dir` contains a ULID-named subdir with a manifest (real B.1 run).

    Requires a manifest.json to be present, not just any ULID-named directory.
    Without that check, an orphan run_dir left by a failed migration rollback
    (where rmdir couldn't finalize cleanup) would silently mark the date as
    "already migrated" forever, dropping any leftover legacy url_dirs.
    """
    from test_ui.common.manifest import MANIFEST_FILENAME

    for child in date_dir.iterdir():
        if (
            child.is_dir()
            and is_valid_run_id(child.name)
            and (child / MANIFEST_FILENAME).exists()
        ):
            return True
    return False


def _legacy_url_dirs(date_dir: Path) -> list[Path]:
    """Return children of `date_dir` that look like url_dirs (not run_id, not symlink).

    Excludes `.tmp-*` work-in-progress dirs and the `latest` symlink so the
    migration doesn't accidentally fold them into a synthetic run.
    """
    out = []
    for child in date_dir.iterdir():
        if not child.is_dir():
            continue
        if child.is_symlink():
            continue
        if child.name.startswith(".tmp-"):
            continue
        if is_valid_run_id(child.name):
            continue  # already-published B.1 run; should have been caught earlier
        out.append(child)
    return out


def migrate_date_dir(kind: str, date_dir: Path, *, dry_run: bool = False) -> str | None:
    """Migrate one `<kind>/<date>/` to the run-id layout. Returns the new run_id, or None.

    None means "nothing to do" — either already migrated, empty, or no url_dirs.
    """
    if _date_dir_already_migrated(date_dir):
        return None
    legacy_url_dirs = _legacy_url_dirs(date_dir)
    if not legacy_url_dirs:
        return None

    # Synthesize a ULID with the date dir's mtime as timestamp (in ms).
    # This makes the migrated run sort correctly relative to any future
    # B.1-native runs on the same date — older mtime → older ULID.
    mtime_seconds = date_dir.stat().st_mtime
    synthesized = ULID.from_timestamp(mtime_seconds)
    run_id = str(synthesized)
    new_run_dir = date_dir / run_id

    print(
        f"  {date_dir.relative_to(date_dir.parent.parent.parent)}: "
        f"{len(legacy_url_dirs)} url dirs → {run_id}"
    )
    if dry_run:
        return run_id

    new_run_dir.mkdir(parents=False, exist_ok=False)

    # Track successfully-moved dirs so we can roll back on partial failure.
    # Without this, an OSError on rename N of M leaves M-N url_dirs orphaned
    # at the date level AND no manifest in new_run_dir, so the next migration
    # invocation skips this date entirely (idempotency check returns True
    # because the run_id subdir exists) — silently dropping the leftover URLs.
    moved: list[tuple[Path, Path]] = []
    try:
        for url_dir in legacy_url_dirs:
            target = new_run_dir / url_dir.name
            url_dir.rename(target)
            moved.append((url_dir, target))
    except OSError as e:
        print(
            f"  ✗ rename failed at url_dir {len(moved) + 1}/{len(legacy_url_dirs)}: {e}",
            file=sys.stderr,
        )
        # Roll back: move successful renames back to their original location.
        for original, new in reversed(moved):
            try:
                new.rename(original)
            except OSError as rollback_err:
                print(
                    f"    ✗ rollback failed for {new} → {original}: {rollback_err}",
                    file=sys.stderr,
                )
        # Remove the now-empty (or partially-cleaned) run dir so the next
        # invocation doesn't see it and skip via the idempotency check.
        # `_date_dir_already_migrated` also now requires a manifest.json to
        # be present, so an orphan empty run_dir can't poison subsequent runs
        # — but we still clean up to avoid debris.
        try:
            new_run_dir.rmdir()
        except OSError as rmdir_err:
            # Non-empty (stray .tmp- or similar) — operator should investigate.
            print(
                f"    ⚠ could not remove orphan run_dir {new_run_dir}: {rmdir_err}",
                file=sys.stderr,
            )
        raise

    started_at = _format_dublin(mtime_seconds)
    manifest = Manifest(
        run_id=run_id,
        kind=kind,
        started_at=started_at,
        finished_at=started_at,  # we don't know the real finish time; use mtime
        status="complete",
        source_run_ids={},  # unknown for migrated runs
        url_count=len(legacy_url_dirs),
    )
    # Compute checksum after the move so it covers the migrated payload.
    manifest.files_sha256 = compute_files_sha256(new_run_dir)
    write_manifest(new_run_dir, manifest)

    return run_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root",
        default=None,
        help="Root of the data tree. Defaults to settings.data_root.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen; do not touch disk.",
    )
    args = parser.parse_args()

    if args.data_root:
        data_root = Path(args.data_root).resolve()
    else:
        from test_ui.config import settings

        data_root = Path(settings.data_root).resolve()

    if not data_root.exists():
        print(f"Data root not found: {data_root}", file=sys.stderr)
        return 1

    print(f"Migrating data tree at {data_root} (dry_run={args.dry_run})")
    migrated = 0
    skipped = 0
    for kind in KINDS:
        kind_root = data_root / kind
        if not kind_root.exists():
            continue
        print(f"\n{kind}/")
        for date_dir in sorted(kind_root.iterdir()):
            if not date_dir.is_dir():
                continue
            if not is_valid_date_dir(date_dir.name):
                continue
            run_id = migrate_date_dir(kind, date_dir, dry_run=args.dry_run)
            if run_id is None:
                skipped += 1
            else:
                migrated += 1

    print(
        f"\nDone. Migrated {migrated} date dirs; skipped {skipped} (already-converted or empty)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
