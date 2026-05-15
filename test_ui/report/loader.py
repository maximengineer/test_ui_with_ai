"""Per-URL on-disk I/O for the report stage (Phase A.3 split).

Reads the comparator's structured diff JSON + screenshots into the in-memory
shapes the AI client and HTML renderer consume. Also owns the four
mutually-exclusive per-URL result files (`ai_analysis.json`, `ai_error.json`,
`no_changes.json`, `ai_disabled.json`) - see `RESULT_FILENAMES` and
`write_result_file` for why exclusivity matters.
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Any

from loguru import logger
from PIL import Image
from pydantic import ValidationError

from ..config import settings
from . import models


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

    Returns `{}` if `diffs_dir` is None or missing. If the directory exists,
    all four required JSON files must be present + valid and must satisfy the
    typed comparator->report contract; otherwise raises `ValueError` with a
    deterministic, operator-readable message.
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
    load_errors: list[str] = []

    for key, filename in json_files.items():
        file_path = diffs_dir / filename
        if file_path.exists():
            try:
                with open(file_path, encoding="utf-8") as f:
                    payload = json.load(f)
                if not isinstance(payload, dict):
                    load_errors.append(f"{filename}: expected JSON object at top-level")
                    continue
                structured_data[key] = payload
                logger.debug(f"Loaded {filename} for structured data")
            except (json.JSONDecodeError, OSError) as e:
                logger.error(f"Error loading {filename}: {e}")
                load_errors.append(f"{filename}: {e}")
        else:
            logger.warning(f"Missing structured file: {filename}")
            load_errors.append(f"{filename}: file not found")

    if load_errors:
        raise ValueError(
            "Structured diff files missing/invalid: " + "; ".join(load_errors)
        )

    visual_diff_path = diffs_dir / "visual_diff.png"
    structured_data["visual_diff_image"] = (
        str(visual_diff_path.absolute()) if visual_diff_path.exists() else None
    )

    files_loaded_count = len(json_files)
    structured_data["metadata"] = {
        "diffs_directory": str(diffs_dir.absolute()),
        "files_loaded": files_loaded_count,
        "timestamp": settings.get_current_datetime(),
    }

    try:
        validated = models.validate_structured_data_envelope(structured_data)
    except ValidationError as e:
        raise ValueError(
            "Structured diff payload failed contract validation: "
            f"{models.format_validation_error(e)}"
        ) from e

    logger.info(f"Loaded structured data from {diffs_dir} - {files_loaded_count} files")
    return validated.model_dump(mode="python")


def _resolve_screenshot_path(path_str: str) -> Path | None:
    """Resolve a path from comparator metadata to an existing screenshot.

    Handles Docker-absolute paths (e.g. /data/baseline/...) by falling
    back to a relative path under the project root when the absolute
    path doesn't exist locally.
    """
    if not path_str:
        return None
    p = Path(path_str)
    if p.exists():
        return p
    if p.is_absolute() and len(p.parts) > 1 and p.parts[1] == "data":
        relative = Path(*p.parts[1:])
        if relative.exists():
            return relative
    return None


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

        baseline_path = _resolve_screenshot_path(metadata.get("baseline_path", ""))
        current_path = _resolve_screenshot_path(metadata.get("current_path", ""))

        if baseline_path:
            baseline_screenshot = baseline_path / url_dir.name / "screenshot.png"
            if baseline_screenshot.exists():
                screenshot_files["baseline"] = baseline_screenshot

        if current_path:
            current_screenshot = current_path / url_dir.name / "screenshot.png"
            if current_screenshot.exists():
                screenshot_files["current"] = current_screenshot

        diff_image_path = result.get("screenshot", {}).get("diff_image_path")
        resolved_diff = _resolve_screenshot_path(diff_image_path) if diff_image_path else None
        if resolved_diff and resolved_diff.exists():
            screenshot_files["visual_diff"] = resolved_diff

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
    max_dim = settings.ai_max_screenshot_dimension
    for key, file_path in screenshot_files.items():
        try:
            raw = file_path.read_bytes()
            resized = _resize_image_for_ai(raw, max_dim)
            if len(resized) < len(raw):
                logger.debug(
                    f"Resized {key} screenshot: {len(raw):,} → {len(resized):,} bytes"
                )
            screenshots[f"{key}_b64"] = base64.b64encode(resized).decode("utf-8")
            screenshots[f"{key}_path"] = str(file_path.absolute())
            logger.debug(f"Loaded {key} screenshot: {file_path}")
        except OSError as e:
            logger.error(f"Error loading {key} screenshot {file_path}: {e}")
            screenshots[f"{key}_error"] = str(e)

    loaded_count = len([k for k in screenshots if k.endswith("_b64")])
    logger.info(f"Loaded {loaded_count} screenshots for {url_dir.name}")
    return screenshots


def _resize_image_for_ai(raw_bytes: bytes, max_dim: int) -> bytes:
    """Resize a PNG to fit within `max_dim` on its longest edge.

    Preserves aspect ratio and outputs PNG. If the image is already
    smaller than the limit, returns the original bytes unchanged.
    """
    try:
        img = Image.open(io.BytesIO(raw_bytes))
    except Exception:
        return raw_bytes
    w, h = img.size
    if max(w, h) <= max_dim:
        return raw_bytes
    ratio = min(max_dim / w, max_dim / h)
    new_size = (int(w * ratio), int(h * ratio))
    resized = img.resize(new_size, Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    resized.save(buf, format="PNG")
    return buf.getvalue()


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
    for url_dir in sorted(run_root.iterdir(), key=lambda p: p.name):
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
            ai_analysis_raw = json.loads(analysis_file.read_text(encoding="utf-8"))
            if not isinstance(ai_analysis_raw, dict):
                logger.warning(
                    f"Invalid analysis payload in {analysis_file}: "
                    "expected JSON object at top-level"
                )
                continue
            structured_data_file = url_dir / "structured_data.json"
            structured_data = (
                json.loads(structured_data_file.read_text(encoding="utf-8"))
                if structured_data_file.exists()
                else {}
            )

            # Typed result path (post-A.1.8) with strict envelope validation.
            if "result_type" in ai_analysis_raw:
                rt_to_status = {
                    "analysis_success": "success",
                    "analysis_error": "error",
                    "no_changes": "no_changes",
                    "ai_disabled": "ai_disabled",
                }
                processing_status = rt_to_status.get(ai_analysis_raw.get("result_type"))
                if processing_status is None:
                    logger.warning(
                        f"Unknown result_type in {analysis_file}: "
                        f"{ai_analysis_raw.get('result_type')}"
                    )
                    continue
                row = models.build_url_result(
                    url=url_dir.name,
                    ai_analysis=ai_analysis_raw,
                    structured_data=structured_data,
                    report_path=url_dir,
                    processing_status=processing_status,
                    screenshots_available=get_available_screenshots(
                        url_dir / "screenshots"
                    ),
                )
                all_url_results.append(row)
            else:
                # Legacy fallback for files persisted before A.1.8.
                processing_status = (
                    "error"
                    if (
                        status_label == "success"
                        and ai_analysis_raw.get("overall_severity") == "ERROR"
                    )
                    else status_label
                )
                if processing_status not in ("success", "error"):
                    # Legacy envelopes without result_type only map to these two.
                    processing_status = "success"
                row = models.build_legacy_url_result(
                    url=url_dir.name,
                    ai_analysis=ai_analysis_raw,
                    structured_data=structured_data,
                    report_path=url_dir,
                    processing_status=processing_status,
                    screenshots_available=get_available_screenshots(
                        url_dir / "screenshots"
                    ),
                )
                all_url_results.append(row)
        except ValidationError as e:
            logger.warning(
                f"Invalid analysis payload in {analysis_file}: "
                f"{models.format_validation_error(e)}"
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
