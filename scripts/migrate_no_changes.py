"""Migrate legacy synthetic no-change analyses to the typed marker file.

Phase A.1.9 deliverable. Pre-A.1.9 the report layer wrote a synthetic
SAFE-severity AI-analysis blob to `ai_analysis.json` for URLs the comparator
flagged as unchanged. That conflated "AI looked and decided it was safe" with
"AI was never invoked because nothing changed."

This script walks `<report_dir>/<date>/<url>/ai_analysis.json` files, and for
each one that looks like a synthetic no-change record (`analysis_type ==
"no_changes_detected"`), rewrites the data to a typed `NoChangesMarker` at
`no_changes.json` and removes the original.

Idempotent - safe to re-run. Files that aren't synthetic no-change records
(real AI analyses, errors, already-migrated entries) are left alone.

Usage:
    python scripts/migrate_no_changes.py [--dry-run] [--report-dir PATH]

Exits 0 on success, 1 on script error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from test_ui.config import settings  # noqa: E402
from test_ui.contracts.ai_contract import NoChangesMarker  # noqa: E402


def _looks_like_synthetic_no_change(data: dict) -> bool:
    """Return True if `data` is a pre-A.1.9 synthetic no-change blob.

    The legacy shape always carried `analysis_type: "no_changes_detected"`.
    Real AI analyses never had that field. This is the cleanest discriminator.
    """
    return data.get("analysis_type") == "no_changes_detected"


def migrate_one(ai_analysis_file: Path, *, dry_run: bool) -> str:
    """Migrate one ai_analysis.json file if eligible.

    Returns one of: "migrated", "skipped-not-no-change", "skipped-bad-json",
    "skipped-already-migrated".
    """
    no_changes_target = ai_analysis_file.parent / "no_changes.json"

    try:
        data = json.loads(ai_analysis_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "skipped-bad-json"

    if not _looks_like_synthetic_no_change(data):
        # Real AI analysis - never delete it, even if no_changes.json sits
        # alongside (someone may have produced a hybrid state by hand).
        return "skipped-not-no-change"

    if no_changes_target.exists():
        # Idempotent: prior run already produced the new file. Safe to clean
        # up the legacy synthetic ai_analysis.json (we just verified above
        # that it IS a synthetic no-change record, not real analysis).
        if not dry_run:
            ai_analysis_file.unlink()
        return "skipped-already-migrated"

    # Build the typed marker. Preserve the original timestamp if present so
    # historical "when did we check?" data isn't lost. Fall back to current
    # time only if the legacy file lacked a timestamp.
    checked_at = data.get("timestamp") or settings.get_current_datetime()
    marker = NoChangesMarker(checked_at=checked_at).model_dump(mode="json")

    if dry_run:
        return "migrated"

    no_changes_target.write_text(
        json.dumps(marker, indent=2),
        encoding="utf-8",
    )
    ai_analysis_file.unlink()
    return "migrated"


def migrate_all(report_dir: Path, *, dry_run: bool) -> dict[str, int]:
    counts: dict[str, int] = {
        "migrated": 0,
        "skipped-not-no-change": 0,
        "skipped-bad-json": 0,
        "skipped-already-migrated": 0,
    }
    for ai_file in report_dir.rglob("ai_analysis.json"):
        outcome = migrate_one(ai_file, dry_run=dry_run)
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=settings.report_dir,
        help=f"Report root to scan (default: {settings.report_dir})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be migrated without modifying any files.",
    )
    args = parser.parse_args()

    if not args.report_dir.exists():
        print(f"migrate_no_changes: report dir does not exist: {args.report_dir}")
        return 0  # nothing to do is not an error

    print(f"migrate_no_changes: scanning {args.report_dir} (dry_run={args.dry_run})")
    counts = migrate_all(args.report_dir, dry_run=args.dry_run)
    for outcome, n in sorted(counts.items()):
        print(f"  {outcome}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
