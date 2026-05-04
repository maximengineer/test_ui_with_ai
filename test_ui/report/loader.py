"""Per-URL on-disk I/O for the report stage (Phase A.3 split).

Reads the comparator's structured diff JSON + screenshots into the in-memory
shapes the AI client and HTML renderer consume. Also owns the four
mutually-exclusive per-URL result files (`ai_analysis.json`, `ai_error.json`,
`no_changes.json`, `ai_disabled.json`) - see `RESULT_FILENAMES` and
`write_result_file` for why exclusivity matters.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from loguru import logger

from ..config import settings


# The four mutually-exclusive per-URL result files. Writing any one of these
# clears the other three so a URL's "current verdict" is always exactly one
# file. Otherwise stale results from previous runs leak into the HTML report
# (load_all_url_results picks the first match in priority order, which doesn't
# always agree with reality).
RESULT_FILENAMES: tuple[str, ...] = (
    "ai_analysis.json",
    "ai_error.json",
    "no_changes.json",
    "ai_disabled.json",
)

# Priority order for picking a URL's "current verdict" when the report is
# rendered. Success is most informative; markers are tiebreakers.
_RESULT_PRIORITY: tuple[tuple[str, str], ...] = (
    ("ai_analysis.json", "success"),
    ("ai_error.json", "error"),
    ("no_changes.json", "no_changes"),
    ("ai_disabled.json", "ai_disabled"),
)


def write_result_file(
    report_url_dir: Path, filename: str, payload: dict[str, Any]
) -> None:
    """Write one result file, atomically clearing the other three.

    Raises on unknown filename - caller bug, not a runtime concern.
    """
    if filename not in RESULT_FILENAMES:
        raise ValueError(f"unknown result filename {filename}")
    for other in RESULT_FILENAMES:
        if other != filename:
            (report_url_dir / other).unlink(missing_ok=True)
    (report_url_dir / filename).write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )


def load_structured_data(diffs_dir: Path | None) -> dict[str, Any]:
    """Load all structured diff JSON files for a URL.

    Returns `{}` if `diffs_dir` is None or missing. Per-file failures are
    represented as `{"error": "..."}` entries rather than aborting the load -
    the AI service can still partially analyze with degraded data.
    """
    if not diffs_dir or not diffs_dir.exists():
        logger.warning(f"Diffs directory not found: {diffs_dir}")
        return {}

    structured_data: dict[str, Any] = {}
    json_files = {
        "change_summary": "change_summary.json",
        "html_changes": "html_changes.json",
        "css_changes": "css_changes.json",
        "js_changes": "js_changes.json",
    }

    for key, filename in json_files.items():
        file_path = diffs_dir / filename
        if file_path.exists():
            try:
                with open(file_path, encoding="utf-8") as f:
                    structured_data[key] = json.load(f)
                logger.debug(f"Loaded {filename} for structured data")
            except (json.JSONDecodeError, OSError) as e:
                logger.error(f"Error loading {filename}: {e}")
                structured_data[key] = {"error": f"Failed to load {filename}: {e!s}"}
        else:
            logger.warning(f"Missing structured file: {filename}")
            structured_data[key] = {"error": f"File not found: {filename}"}

    visual_diff_path = diffs_dir / "visual_diff.png"
    structured_data["visual_diff_image"] = (
        visual_diff_path if visual_diff_path.exists() else None
    )

    files_loaded_count = sum(
        1
        for k, v in structured_data.items()
        if k not in ("metadata", "visual_diff_image")
        and (not isinstance(v, dict) or "error" not in v)
    )
    structured_data["metadata"] = {
        "diffs_directory": str(diffs_dir.absolute()),
        "files_loaded": files_loaded_count,
        "timestamp": settings.get_current_datetime(),
    }
    logger.info(f"Loaded structured data from {diffs_dir} - {files_loaded_count} files")
    return structured_data


def load_screenshots(
    url_dir: Path, comparison_data: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Load baseline / current / visual_diff screenshots into base64 + path keys.

    Output keys: `<kind>_b64`, `<kind>_path`, plus `<kind>_error` on read
    failure. Missing screenshots don't appear in the output dict at all
    (they're filtered out upstream by the `.exists()` checks at lines
    134-154); the AI service can still operate with a partial set.
    """
    screenshots: dict[str, Any] = {}
    screenshot_files: dict[str, Path] = {}

    if comparison_data:
        result = comparison_data.get("result", {})
        metadata = comparison_data.get("metadata", {})

        baseline_path = Path(metadata.get("baseline_path", ""))
        current_path = Path(metadata.get("current_path", ""))

        if baseline_path.exists():
            baseline_screenshot = baseline_path / url_dir.name / "screenshot.png"
            if baseline_screenshot.exists():
                screenshot_files["baseline"] = baseline_screenshot

        if current_path.exists():
            current_screenshot = current_path / url_dir.name / "screenshot.png"
            if current_screenshot.exists():
                screenshot_files["current"] = current_screenshot

        diff_image_path = result.get("screenshot", {}).get("diff_image_path")
        if diff_image_path and Path(diff_image_path).exists():
            screenshot_files["visual_diff"] = Path(diff_image_path)

    # Fallback: visual diff in url_dir/diffs/ when comparison_data didn't carry it.
    if "visual_diff" not in screenshot_files:
        diffs_dir = url_dir / "diffs"
        if diffs_dir.exists():
            visual_diff_path = diffs_dir / "visual_diff.png"
            if visual_diff_path.exists():
                screenshot_files["visual_diff"] = visual_diff_path

    # Every entry in screenshot_files was added only after `.exists()` was
    # already True upstream - the existence check + `_missing` branch were
    # dead code (verified in deep-dive audit). Read directly; OSError covers
    # the unlikely race where the file disappears between the upstream
    # exists() check and read here.
    for key, file_path in screenshot_files.items():
        try:
            screenshots[f"{key}_b64"] = base64.b64encode(file_path.read_bytes()).decode(
                "utf-8"
            )
            screenshots[f"{key}_path"] = str(file_path.absolute())
            logger.debug(f"Loaded {key} screenshot: {file_path}")
        except OSError as e:
            logger.error(f"Error loading {key} screenshot {file_path}: {e}")
            screenshots[f"{key}_error"] = str(e)

    loaded_count = len([k for k in screenshots if k.endswith("_b64")])
    logger.info(f"Loaded {loaded_count} screenshots for {url_dir.name}")
    return screenshots


def get_available_screenshots(screenshots_dir: Path) -> list[str]:
    """List which of baseline/current/visual_diff PNGs exist in the dir."""
    if not screenshots_dir.exists():
        return []
    return [
        kind
        for kind in ("baseline", "current", "visual_diff")
        if (screenshots_dir / f"{kind}.png").exists()
    ]


def load_all_url_results(run_root: Path) -> list[dict[str, Any]]:
    """Walk `run_root/` and load every URL's persisted verdict.

    For each URL subdir picks the highest-priority result file present
    (success → error → no_changes → ai_disabled), reads its JSON, and
    bundles it with structured_data and the screenshots inventory into the
    shape the aggregator + HTML renderer expect.

    Phase B.1: takes the report run_root path directly. Pre-B.1 took the
    date string and joined with `settings.report_dir`; the orchestrator now
    owns run-id resolution and passes the run_root in.
    """
    if not run_root.exists():
        raise ValueError(f"No report data found at {run_root}")

    all_url_results: list[dict[str, Any]] = []
    for url_dir in run_root.iterdir():
        if not url_dir.is_dir():
            continue

        picked: tuple[Path, str] | None = None
        for filename, status_label in _RESULT_PRIORITY:
            candidate = url_dir / filename
            if candidate.exists():
                picked = (candidate, status_label)
                break

        if picked is None:
            logger.warning(f"No analysis file found in {url_dir} (skipping)")
            continue

        analysis_file, status_label = picked
        try:
            ai_analysis = json.loads(analysis_file.read_text(encoding="utf-8"))
            structured_data_file = url_dir / "structured_data.json"
            structured_data = (
                json.loads(structured_data_file.read_text(encoding="utf-8"))
                if structured_data_file.exists()
                else {}
            )

            # Map file→processing_status. Aggregator's result_type-based
            # routing also handles this; processing_status remains for legacy
            # consumers (and the synthetic-ERROR back-compat case below).
            if (
                status_label == "success"
                and ai_analysis.get("overall_severity") == "ERROR"
            ):
                processing_status = "error"
            else:
                processing_status = status_label

            all_url_results.append(
                {
                    "url": url_dir.name,
                    "ai_analysis": ai_analysis,
                    "structured_data": structured_data,
                    "report_path": url_dir,
                    "processing_status": processing_status,
                    "screenshots_available": get_available_screenshots(
                        url_dir / "screenshots"
                    ),
                }
            )
        except Exception as e:
            logger.warning(f"Failed to load analysis for {url_dir.name}: {e}")

    return all_url_results


__all__ = [
    "RESULT_FILENAMES",
    "write_result_file",
    "load_structured_data",
    "load_screenshots",
    "get_available_screenshots",
    "load_all_url_results",
]
