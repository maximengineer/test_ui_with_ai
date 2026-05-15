"""Discover per-URL comparator output and split into changed / unchanged buckets.

Phase A.3 split - extracted from report/generator.py. Pure function over the
filesystem; no AI, no network.

**Phase B.1:** drills through `<comparator_root>/<date>/<run_id>/` to find
the latest complete comparator run, then iterates URL subfolders inside it.
Legacy `<comparator_root>/<date>/<url_dir>/` layout is not supported.

**A.3 simplification (multi-signal-detection):** trusts
`result.changes_detected` as the single source of truth for successful
comparator payloads. Exception: comparator error payloads (`result.error`)
are routed to the "with_changes" bucket so the report stage emits an error
marker, not a misleading `no_changes` marker.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

from ..comparator.finder import find_latest_run_dir_in_date


def discover_comparison_data(
    comparator_root: Path, date: str
) -> dict[str, list[dict[str, Any]]]:
    """Scan the latest comparator run for `date` and bucket URLs by whether they changed.

    Returns `{"with_changes": [...], "without_changes": [...]}`. Unreadable or
    missing comparison_results.json files are logged and skipped, not raised -
    one bad URL shouldn't kill the whole report.

    Resolution: looks under `<comparator_root>/<date>/` and drills to the
    latest complete `<run_id>` subdir. If no complete run is found, returns
    empty buckets.
    """
    run_root = find_latest_run_dir_in_date(comparator_root / date)
    if run_root is None:
        # Date dir missing or contains only running/failed runs.
        return {"with_changes": [], "without_changes": []}

    urls_with_changes: list[dict[str, Any]] = []
    urls_without_changes: list[dict[str, Any]] = []

    for url_dir in run_root.iterdir():
        if not url_dir.is_dir():
            continue

        comparison_file = url_dir / "comparison_results.json"
        if not comparison_file.exists():
            logger.warning(f"No comparison_results.json found for {url_dir.name}")
            continue

        try:
            with open(comparison_file, encoding="utf-8") as f:
                comparison_data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Error reading comparison data for {url_dir.name}: {e}")
            continue

        result_raw = comparison_data.get("result", {})
        result = result_raw if isinstance(result_raw, dict) else {}
        has_comparison_error = "error" in result
        if result.get("changes_detected", False) or has_comparison_error:
            diffs_dir = url_dir / "diffs"
            if has_comparison_error:
                logger.warning(
                    f"Comparator reported error for {url_dir.name}: {result.get('error')}"
                )
            urls_with_changes.append(
                {
                    "url_name": url_dir.name,
                    "url_dir": url_dir,
                    "structured_data_path": diffs_dir if diffs_dir.exists() else None,
                    "has_changes": True,
                    "comparison_data": comparison_data,
                }
            )
        else:
            urls_without_changes.append(
                {
                    "url_name": url_dir.name,
                    "url_dir": url_dir,
                    "has_changes": False,
                    "comparison_data": comparison_data,
                }
            )

    logger.info(
        f"Discovered {len(urls_with_changes)} URLs with changes, "
        f"{len(urls_without_changes)} without changes"
    )
    return {"with_changes": urls_with_changes, "without_changes": urls_without_changes}


__all__ = ["discover_comparison_data"]
