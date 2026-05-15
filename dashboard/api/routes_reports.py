"""Reports drill-in route domain module."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Response
from loguru import logger

from test_ui.common.manifest import read_manifest
from test_ui.common.run_id import is_valid_run_id
from test_ui.config import settings

from .models import ReportSummaryOut, ReportUrlDetail, ReportUrlSummary, ReportUrlsOut
from .routes_common import is_valid_date_dir


reports_router = APIRouter(prefix="/api/reports", tags=["reports"])

_RESULT_FILES: tuple[tuple[str, str], ...] = (
    ("ai_analysis.json", "analysis_success"),
    ("ai_error.json", "analysis_error"),
    ("no_changes.json", "no_changes"),
    ("ai_disabled.json", "ai_disabled"),
)


def _resolve_report_run_dir(date: str, run_id: str) -> Path:
    """Validate `date`+`run_id` and return the absolute report run dir."""
    if not is_valid_date_dir(date):
        raise HTTPException(status_code=400, detail=f"invalid date {date!r}")
    if not is_valid_run_id(run_id):
        raise HTTPException(status_code=400, detail=f"invalid run_id {run_id!r}")
    if settings.report_dir is None:
        raise RuntimeError("settings.report_dir is None")
    root = settings.report_dir.resolve()
    run_dir = (root / date / run_id).resolve()
    if not run_dir.is_relative_to(root):
        raise HTTPException(status_code=400, detail="path escapes report root")
    if not run_dir.is_dir():
        raise HTTPException(
            status_code=404, detail=f"no report run for {date}/{run_id}"
        )
    return run_dir


def _resolve_url_dir(run_dir: Path, url_id: str) -> Path:
    """Validate `url_id` against existing children + return path."""
    valid_ids = {p.name for p in run_dir.iterdir() if p.is_dir()}
    if url_id not in valid_ids:
        raise HTTPException(
            status_code=404, detail=f"no url_id={url_id!r} in this report"
        )
    return run_dir / url_id


def _classify_url_dir(url_dir: Path) -> tuple[str, dict | None]:
    """Pick the highest-priority result file in `url_dir`."""
    matches: list[tuple[str, str]] = [
        (filename, result_type)
        for filename, result_type in _RESULT_FILES
        if (url_dir / filename).exists()
    ]
    if len(matches) > 1:
        logger.warning(
            f"reports: {url_dir.name} has multiple mutually-exclusive "
            f"result files {[m[0] for m in matches]}; picking "
            f"highest-priority ({matches[0][0]}). The writer contract "
            "guarantees these are mutually exclusive - investigate."
        )
    if not matches:
        return "unknown", None
    filename, result_type = matches[0]
    try:
        return result_type, json.loads((url_dir / filename).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(
            f"reports: corrupt {filename} at {url_dir / filename}: "
            f"{type(e).__name__}: {e}"
        )
        return result_type, None


def _list_screenshots(url_dir: Path) -> list[str]:
    """Which of baseline/current/visual_diff PNGs exist for this URL."""
    screens_dir = url_dir / "screenshots"
    if not screens_dir.is_dir():
        return []
    out: list[str] = []
    for kind in ("baseline", "current", "visual_diff"):
        if (screens_dir / f"{kind}.png").is_file():
            out.append(kind)
    return out


@reports_router.get(
    "/{date}/{run_id}",
    response_model=ReportSummaryOut,
    responses={
        400: {"description": "Malformed date or run_id"},
        404: {"description": "No report for this date+run_id"},
    },
)
def get_report_summary(date: str, run_id: str) -> ReportSummaryOut:
    """Top-level metadata: manifest fields + per-result-type counts."""
    run_dir = _resolve_report_run_dir(date, run_id)
    try:
        manifest = read_manifest(run_dir)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"manifest unreadable for {date}/{run_id}: {e}",
        ) from e

    counts: dict[str, int] = {}
    for url_dir in run_dir.iterdir():
        if not url_dir.is_dir():
            continue
        result_type, payload = _classify_url_dir(url_dir)
        counts[result_type] = counts.get(result_type, 0) + 1
        if result_type == "analysis_success" and isinstance(payload, dict):
            sev = payload.get("overall_severity")
            if isinstance(sev, str):
                counts[sev] = counts.get(sev, 0) + 1

    return ReportSummaryOut(
        date=date,
        run_id=run_id,
        started_at=manifest.started_at,
        finished_at=manifest.finished_at,
        url_count=manifest.url_count,
        severity_counts=counts,
    )


def _url_dir_sort_key(p: Path) -> tuple[int, int, str]:
    """Sort URL ids by numeric desc first, then lexicographic fallback."""
    name = p.name
    if name.isdigit():
        return (0, -int(name), name)
    return (1, 0, name)


@reports_router.get(
    "/{date}/{run_id}/urls",
    response_model=ReportUrlsOut,
    responses={
        400: {"description": "Malformed date or run_id"},
        404: {"description": "No report for this date+run_id"},
    },
)
def get_report_urls(date: str, run_id: str) -> ReportUrlsOut:
    """List of URLs in the report with their result_type + severity."""
    run_dir = _resolve_report_run_dir(date, run_id)
    items: list[ReportUrlSummary] = []
    for url_dir in sorted(run_dir.iterdir(), key=_url_dir_sort_key):
        if not url_dir.is_dir():
            continue
        result_type, payload = _classify_url_dir(url_dir)
        severity: str | None = None
        url: str | None = None
        if isinstance(payload, dict):
            sev = payload.get("overall_severity")
            if isinstance(sev, str):
                severity = sev
            u = payload.get("url")
            if isinstance(u, str):
                url = u
        items.append(
            ReportUrlSummary(
                url_id=url_dir.name,
                result_type=result_type,  # type: ignore[arg-type]
                severity=severity,
                url=url,
            )
        )
    return ReportUrlsOut(items=items)


@reports_router.get(
    "/{date}/{run_id}/url",
    response_model=ReportUrlDetail,
    responses={
        400: {"description": "Malformed date / run_id / url_id"},
        404: {"description": "No such url_id in this report"},
    },
)
def get_report_url_detail(
    date: str,
    run_id: str,
    id: Annotated[str, Query(description="The url_id (per-site directory name)")],
) -> ReportUrlDetail:
    """Per-URL detail: analysis payload + structured_data + screenshots list."""
    run_dir = _resolve_report_run_dir(date, run_id)
    url_dir = _resolve_url_dir(run_dir, id)
    result_type, analysis = _classify_url_dir(url_dir)

    structured: dict | None = None
    structured_path = url_dir / "structured_data.json"
    if structured_path.exists():
        try:
            structured = json.loads(structured_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            structured = None

    return ReportUrlDetail(
        url_id=id,
        result_type=result_type,  # type: ignore[arg-type]
        analysis=analysis or {},
        structured_data=structured,
        screenshots=_list_screenshots(url_dir),  # type: ignore[arg-type]
    )


_SCREENSHOT_KINDS: dict[str, str] = {
    "baseline": "baseline.png",
    "current": "current.png",
    "diff": "visual_diff.png",
}


@reports_router.get(
    "/{date}/{run_id}/screenshot",
    responses={
        200: {"content": {"image/png": {}}},
        304: {"description": "Not modified - client's If-None-Match matched"},
        400: {"description": "Malformed parameters"},
        404: {"description": "No such url_id, or no screenshot of that kind"},
    },
)
def get_report_screenshot(
    date: str,
    run_id: str,
    url_id: Annotated[str, Query()],
    which: Annotated[Literal["baseline", "current", "diff"], Query()],
    if_none_match: Annotated[str | None, Header(alias="if-none-match")] = None,
) -> Response:
    """Return one PNG (baseline / current / diff) for one URL."""
    run_dir = _resolve_report_run_dir(date, run_id)
    url_dir = _resolve_url_dir(run_dir, url_id)
    filename = _SCREENSHOT_KINDS[which]
    path = (url_dir / "screenshots" / filename).resolve()

    if settings.report_dir is None:
        raise RuntimeError("settings.report_dir is None")
    if not path.is_relative_to(settings.report_dir.resolve()):
        raise HTTPException(status_code=400, detail="path escapes report root")
    if not path.is_file():
        raise HTTPException(
            status_code=404, detail=f"no {which} screenshot for {url_id}"
        )

    stat = path.stat()
    etag = f'W/"{stat.st_mtime_ns}-{stat.st_size}"'
    if if_none_match is not None and if_none_match == etag:
        return Response(
            status_code=304,
            headers={"etag": etag, "cache-control": "no-cache"},
        )
    return Response(
        content=path.read_bytes(),
        media_type="image/png",
        headers={"etag": etag, "cache-control": "no-cache"},
    )


__all__ = [
    "reports_router",
    "get_report_summary",
    "get_report_urls",
    "get_report_url_detail",
    "get_report_screenshot",
]
