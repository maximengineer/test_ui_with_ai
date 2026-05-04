"""Comparator orchestrator (Phase A.3 split, B.1 run-id layout).

All heavy lifting lives in sibling modules:
- finder.py      → date + run-id directory discovery
- screenshots.py → SSIM + visual diff (hard-imports cv2/skimage)
- dom.py         → BeautifulSoup HTML diff
- assets.py      → CSS/JS/media diffing
- summary.py     → master change-summary aggregation

Phase B.1: each `compare_all` call publishes a fresh run_id at
`<comparator_root>/<date>/<run_id>/` via atomic publication. The manifest
records `source_run_ids = {"baseline": "...", "current": "..."}` so the
report stage can trace its provenance back to specific input runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from ..common.run_context import run_context
from ..common.run_id import is_valid_run_id, new_run_id
from ..common.run_record import write_run_record
from ..common.sites import site_dir_name as _site_dir_name
from ..config import settings
from . import assets, dom, finder, summary
from .screenshots import compare_screenshots


class ComparatorEngine:
    """Walks URLs, calls per-category comparators, persists results."""

    def __init__(self):
        pass

    # Run-dir discovery — was date-only pre-B.1. Now drills through
    # date → latest complete run, falling back to the date dir itself for
    # legacy layouts (see finder.find_latest_run_dir for details).
    @classmethod
    def find_latest_baseline(cls, baseline_root: Path) -> Optional[Path]:
        return finder.find_latest_run_dir(baseline_root)

    @classmethod
    def find_latest_current(cls, current_root: Path) -> Optional[Path]:
        return finder.find_latest_run_dir(current_root)

    def compare_all(
        self,
        baseline_dir: Path,
        current_dir: Path,
        sites: list[dict],
        *,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Compare all snapshots; publish a comparator run; return aggregated list.

        `baseline_dir` and `current_dir` are the resolved RUN directories
        (e.g. `data/baseline/01-01-2099/01HXX.../`). For legacy callers
        passing date dirs directly, the source_run_ids will be empty since
        we can't infer a run_id from a date-named dir.

        `sites` is the list of `{id, url, name}` dicts from sites.yml. Per-
        site directory names are resolved via `_site_dir_name` (Phase B.3
        — prefers `site["id"]`, falls back to `url_to_dirname(url)` for
        legacy/test callers passing dicts without an id).
        """
        date_str = settings.get_current_date()
        # Caller (e.g. dashboard) may pre-allocate a run_id so it can track
        # the subprocess by ID before it starts. Validated as a real ULID
        # so a typo lands here, not in directory naming downstream.
        if run_id is None:
            run_id = new_run_id()
        elif not is_valid_run_id(run_id):
            raise ValueError(f"run_id={run_id!r} is not a valid ULID")
        date_dir = settings.comparator_dir / date_str
        date_dir.mkdir(parents=True, exist_ok=True)

        # Derive source_run_ids when the input dirs are run-id dirs. If they're
        # legacy date dirs (B.1 grace mode), leave empty — the manifest will
        # show the gap rather than lie about the source.
        source_run_ids: dict[str, str] = {}
        if is_valid_run_id(baseline_dir.name):
            source_run_ids["baseline"] = baseline_dir.name
        if is_valid_run_id(current_dir.name):
            source_run_ids["current"] = current_dir.name

        logger.info(
            f"Starting comparator run {run_id} for date {date_str} "
            f"(baseline={source_run_ids.get('baseline', '<legacy>')}, "
            f"current={source_run_ids.get('current', '<legacy>')})"
        )

        # Phase B.3.4: invocation record for retry / dashboard introspection.
        write_run_record(
            run_id,
            kind="comparator",
            args={
                "baseline_dir": str(baseline_dir),
                "current_dir": str(current_dir),
                "site_count": len(sites),
                "source_run_ids": source_run_ids,
            },
        )

        baseline_sites = {d.name for d in baseline_dir.iterdir() if d.is_dir()}
        current_sites = {d.name for d in current_dir.iterdir() if d.is_dir()}

        with run_context(
            date_dir,
            run_id,
            kind="comparator",
            command="comparator.compare_all",
            source_run_ids=source_run_ids,
        ) as ctx:
            all_results = self._compare_all_into(
                run_root=ctx.run_root,
                baseline_dir=baseline_dir,
                current_dir=current_dir,
                baseline_sites=baseline_sites,
                current_sites=current_sites,
                sites=sites,
            )
            ctx.complete(url_count=len(all_results))

        # Publish the latest pointer so subsequent `report` calls find this run.
        try:
            finder.update_latest_symlink(date_dir, run_id)
        except Exception as e:
            logger.warning(f"Could not update comparator 'latest' symlink: {e}")

        logger.info(
            f"Comparator run {run_id} published "
            f"({len(all_results)} URLs) → {date_dir / run_id}"
        )
        return all_results

    def _compare_all_into(
        self,
        *,
        run_root: Path,
        baseline_dir: Path,
        current_dir: Path,
        baseline_sites: set[str],
        current_sites: set[str],
        sites: list[dict],
    ) -> list[dict[str, Any]]:
        """Inner per-site loop. Writes into `run_root/<site_dir>/`.

        Phase B.3: each `site` dict yields its dir name via `_site_dir_name`
        (`site["id"]`, falling back to `url_to_dirname(url)` for legacy
        callers without ids). The per-URL output also records the resolved
        `site_id` so the report stage can group by stable identifier.
        """
        all_results: list[dict[str, Any]] = []
        for site in sites:
            url = site["url"]
            site_name = _site_dir_name(site)
            url_output_dir = run_root / site_name
            url_output_dir.mkdir(parents=True, exist_ok=True)
            diffs_dir = url_output_dir / "diffs"

            logger.info(f"Comparing site {site_name!r} ({url}) -> {url_output_dir}")

            if site_name not in baseline_sites:
                logger.warning(f"Site '{site_name}' not found in baseline")
                result = {
                    "url": url,
                    "error": "missing_baseline",
                    "message": "Site not found in baseline",
                }
            elif site_name not in current_sites:
                logger.warning(f"Site '{site_name}' not found in current crawl")
                result = {
                    "url": url,
                    "error": "missing_current",
                    "message": "Site not found in current crawl",
                }
            else:
                result = _compare_single_site(
                    baseline_path=baseline_dir / site_name,
                    current_path=current_dir / site_name,
                    url=url,
                    diffs_dir=diffs_dir,
                )
                result["url"] = url

            comparison_data = {
                "metadata": {
                    "timestamp": settings.get_current_datetime(),
                    "url": url,
                    "site_id": site_name,
                    "baseline_path": str(baseline_dir.absolute()),
                    "current_path": str(current_dir.absolute()),
                    "output_path": str(url_output_dir.absolute()),
                },
                "result": result,
            }

            output_file = url_output_dir / "comparison_results.json"
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(comparison_data, f, indent=2, ensure_ascii=False)

            logger.info(f"Comparison results for {url} saved to: {output_file}")
            all_results.append(comparison_data)

        return all_results


def _compare_single_site(
    baseline_path: Path, current_path: Path, url: str, diffs_dir: Path
) -> dict[str, Any]:
    """Run all per-category comparators against one site; persist diffs if any."""
    logger.info(f"Comparing site: {url}")

    try:
        screenshot_result = compare_screenshots(
            baseline_path / "screenshot.png",
            current_path / "screenshot.png",
            url,
            diffs_dir,
        )
        css_result = assets.compare_assets(baseline_path, current_path, "css")
        js_result = assets.compare_assets(baseline_path, current_path, "js")
        media_result = assets.compare_assets(baseline_path, current_path, "media")
        dom_result = dom.compare_dom(
            baseline_path / "index.html", current_path / "index.html"
        )

        visual_changes = screenshot_result.get("visual_changes", False)
        html_changes = (
            dom_result.get("has_changes", False) and "error" not in dom_result
        )
        css_changes = css_result.get("has_changes", False)
        js_changes = js_result.get("has_changes", False)
        media_changes = media_result.get("has_changes", False)
        any_changes = any(
            [visual_changes, html_changes, css_changes, js_changes, media_changes]
        )

        if any_changes:
            diffs_dir.mkdir(exist_ok=True)
            logger.info(f"Changes detected for {url}, creating structured diff data")

            _write_json(
                diffs_dir / "html_changes.json",
                dom.create_html_changes_json(dom_result),
            )
            _write_json(
                diffs_dir / "css_changes.json",
                assets.create_css_changes_json(css_result),
            )
            _write_json(
                diffs_dir / "js_changes.json", assets.create_js_changes_json(js_result)
            )
            _write_json(
                diffs_dir / "change_summary.json",
                summary.create_change_summary_json(
                    screenshot_result,
                    dom_result,
                    css_result,
                    js_result,
                    media_result,
                ),
            )
            logger.info(f"Structured diff data saved to {diffs_dir}")
        else:
            logger.info(f"No changes detected for {url}, no diffs directory created")

        return {
            "screenshot": screenshot_result,
            "assets": {"css": css_result, "js": js_result, "media": media_result},
            "dom": dom_result,
            "changes_detected": any_changes,
            "diffs_created": any_changes,
        }
    except Exception as e:
        logger.error(f"Error comparing {url}: {e}")
        return {"error": f"Comparison failed: {e!s}"}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
