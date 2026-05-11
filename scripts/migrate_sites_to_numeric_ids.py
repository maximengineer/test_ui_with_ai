"""Renumber sites.yml + on-disk per-site dirs to numeric IDs.

Background: pre-migration, IDs were slugified names (`home-page`,
`department-of-finance`, ...). The dashboard's add-site flow now
auto-assigns numeric IDs (1, 2, 3, ...) per operator preference; this
script renames existing entries to match.

Two halves run in lock-step:
  1. Walk sites.yml in source order; assign id 1, 2, 3, ..., N.
     Records the old_id -> new_id map.
  2. Walk every `data/<kind>/<date>/<run_id>/<old_id>/` directory and
     `mv` to `<new_id>/`. Skips when no on-disk dirs match (clean
     install) or when the destination already exists (operator partial
     re-run -> safe to re-execute).

Idempotent: a second run is a no-op (ids are already 1..N, dirs are
already renamed).

Atomic per-yaml-write (tmp + rename), best-effort per-dir rename
(individual rename failure logged + continued; re-run picks up the rest).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Match scripts/export_schemas.py — let `python scripts/foo.py` find the
# top-level `test_ui` package without requiring a `pip install -e .`
# / Poetry-managed venv on the operator's PATH.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from loguru import logger  # noqa: E402

from test_ui.common.sites import (  # noqa: E402
    Site,
    _atomic_write_yaml,
    _load_for_mutation,
    load_sites,
    next_numeric_id,
)
from test_ui.config import settings  # noqa: E402


def _renumber_yaml(path: Path) -> dict[str, str]:
    """Rewrite sites.yml so every entry has a sequential numeric id.

    Preserves file order. Returns the {old_id: new_id} map for the
    caller to use when renaming on-disk dirs. Skips entries that are
    already numeric (idempotency); they keep their existing id and the
    map records old==new.

    Empty file is allowed (no-op, returns {}).
    """
    if not path.exists():
        logger.warning(f"sites.yml not at {path}; nothing to migrate")
        return {}

    data = _load_for_mutation(path)
    entries = data.get("sites") or []
    if not entries:
        return {}

    # Decide each new id by walking in order. Numeric ids that already
    # exist + are unique stay put; non-numeric or duplicate ids get the
    # next available number. Builds the {old: new} map for dir renames.
    taken: set[str] = set()
    rename_map: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        old_id = entry.get("id")
        if old_id and str(old_id).isdigit() and old_id not in taken:
            # Already numeric and unique - keep as-is.
            taken.add(old_id)
            rename_map[old_id] = old_id
            continue
        new_id = next_numeric_id(taken)
        taken.add(new_id)
        if old_id:
            rename_map[old_id] = new_id
        entry["id"] = new_id

    # Validate the result via the strict loader so any latent corruption
    # surfaces here, not at the next dashboard read.
    original_bytes = path.read_bytes()
    _atomic_write_yaml(path, data)
    try:
        load_sites(path)
    except Exception:
        path.write_bytes(original_bytes)
        raise
    return rename_map


def _rename_data_dirs(rename_map: dict[str, str]) -> tuple[int, int]:
    """Walk every data/<kind>/<date>/<run_id>/ tree and rename per-site
    subdirs from old to new id. Returns (renamed, skipped)."""
    if not rename_map:
        return 0, 0
    # Filter to actual renames (old != new).
    actual = {old: new for old, new in rename_map.items() if old != new}
    if not actual:
        return 0, 0

    renamed = 0
    skipped = 0
    kind_roots = [
        settings.baseline_dir,
        settings.current_dir,
        settings.comparator_dir,
        settings.report_dir,
    ]
    for root in kind_roots:
        if root is None or not root.exists():
            continue
        for date_dir in root.iterdir():
            if not date_dir.is_dir():
                continue
            for run_dir in date_dir.iterdir():
                if not run_dir.is_dir():
                    continue
                for old_id, new_id in actual.items():
                    src = run_dir / old_id
                    if not src.is_dir():
                        continue
                    dst = run_dir / new_id
                    if dst.exists():
                        # Already renamed by a previous run, OR a
                        # genuine collision (shouldn't happen with
                        # well-formed numeric ids). Either way, skip
                        # to avoid clobber.
                        logger.warning(
                            f"skip rename {src} -> {dst}: destination exists"
                        )
                        skipped += 1
                        continue
                    try:
                        src.rename(dst)
                        renamed += 1
                    except OSError as e:
                        logger.error(f"rename {src} -> {dst} failed: {e}")
                        skipped += 1
    return renamed, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sites-file",
        type=Path,
        default=Path("test_ui/sites.yml"),
        help="Path to sites.yml (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned changes; don't write anything.",
    )
    args = parser.parse_args()

    if args.dry_run:
        # Dry-run: load via the strict loader, print the plan, exit.
        sites = load_sites(args.sites_file)
        taken: set[str] = set()
        plan: list[tuple[str, str]] = []
        for site in sites:
            if site.id.isdigit() and site.id not in taken:
                taken.add(site.id)
                plan.append((site.id, site.id))
                continue
            new = next_numeric_id(taken)
            taken.add(new)
            plan.append((site.id, new))
        for old, new in plan:
            marker = "  " if old == new else "->"
            print(f"  {old:40s} {marker} {new}")
        changes = sum(1 for o, n in plan if o != n)
        print(
            f"\n{changes} entries would be renumbered ({len(plan) - changes} unchanged)"
        )
        return 0

    rename_map = _renumber_yaml(args.sites_file)
    if not rename_map:
        print("sites.yml empty or missing; nothing to do.")
        return 0

    actual_renames = sum(1 for o, n in rename_map.items() if o != n)
    print(f"sites.yml: {len(rename_map)} entries, {actual_renames} renumbered")

    renamed, skipped = _rename_data_dirs(rename_map)
    print(f"on-disk dirs: {renamed} renamed, {skipped} skipped")
    # Re-validate by loading via the strict loader one more time.
    Site.model_rebuild()  # defensive; no-op normally
    load_sites(args.sites_file)
    print("sites.yml loads cleanly post-migration")
    return 0


if __name__ == "__main__":
    sys.exit(main())
