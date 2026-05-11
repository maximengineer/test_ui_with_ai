"""Report orchestrator (Phase A.3 - slimmed from 1735 LOC god-object).

All heavy lifting now lives in sibling modules:
- discovery.py    → scan comparator output, bucket URLs by changed/unchanged
- loader.py       → per-URL file I/O (RESULT_FILENAMES, structured data, screenshots)
- ai_client.py    → POST to AI analyzer, retry, semaphore, contract validation
- aggregator.py   → cross-URL summaries, patterns, recommendations, confidence
- html_renderer.py → Jinja template + render

This module just walks the URL list, dispatches to those modules, persists
the four mutually-exclusive per-URL result files, and writes the final HTML
report. Don't add new domain logic here - push it into the appropriate sibling.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from ..config import settings
from ..contracts.ai_contract import AIDisabledMarker, NoChangesMarker
from . import aggregator, discovery, html_renderer, loader
from .ai_client import AIClient


class ReportGenerator:
    """Orchestrates the report stage: discovery → AI analysis → aggregate → render."""

    def __init__(self, config, ai_analyzer_url: str, client: httpx.AsyncClient):
        """The caller (Orchestrator) owns the httpx.AsyncClient lifecycle.

        Phase A.1.7 - client is constructed at the asyncio.run() boundary and
        passed in here so the connection pool is cleanly released. Was previously
        constructed (and never closed) by this class.

        Phase A.1.10 - AIClient owns the per-instance semaphore that caps
        in-flight AI requests (default 3, AFR_AI_CONCURRENCY env override).
        """
        self.config = config
        self.ai_analyzer_url = ai_analyzer_url
        self.client = client
        self.ai_client = AIClient(client=client, ai_analyzer_url=ai_analyzer_url)

    # ------------------------------------------------------------------
    # Discovery + per-URL processing
    # ------------------------------------------------------------------

    def discover_comparison_data(
        self, comparator_root: Path, date: str
    ) -> dict[str, list[dict]]:
        return discovery.discover_comparison_data(comparator_root, date)

    def process_urls_without_changes(
        self, urls_without_changes: list[dict], run_root: Path
    ) -> list[dict]:
        """Write a typed NoChangesMarker for each URL the comparator flagged unchanged.

        Phase B.1: writes go into `<run_root>/<url_name>/`. The orchestrator
        owns `run_root` (the atomic-publication tmp dir) and passes it in.

        Phase A.1.9 - replaces the old synthetic SAFE-severity blob (which
        falsely implied AI looked at the URL and decided it was safe) with a
        marker that records "AI was not invoked because there was nothing to
        analyze."
        """
        processed_results = []
        for url_data in urls_without_changes:
            url_name = url_data["url_name"]
            report_url_dir = run_root / url_name
            report_url_dir.mkdir(parents=True, exist_ok=True)

            marker = NoChangesMarker(
                checked_at=settings.get_current_datetime()
            ).model_dump(mode="json")
            loader.write_result_file(report_url_dir, "no_changes.json", marker)
            logger.info(f"no_changes.json written for {url_name}")

            processed_results.append(
                {
                    "url": url_name,
                    "has_changes": False,
                    "ai_analysis": marker,
                    "report_path": report_url_dir,
                    "processing_status": "no_changes",
                }
            )
        return processed_results

    async def process_single_url(self, url_data: dict, run_root: Path) -> dict:
        """Process one URL: load → AI → persist into `<run_root>/<url_name>/`."""
        url_name = url_data["url_name"]
        logger.info(f"Processing URL: {url_name}")

        report_url_dir = run_root / url_name
        report_url_dir.mkdir(parents=True, exist_ok=True)

        # AFR_AI_ENABLED=false short-circuits before any AI call. Used for
        # sensitive sites where DOM/screenshots shouldn't leave the local
        # network. Distinct from no_changes (which means the comparator found
        # nothing) - ai_disabled means we deliberately chose not to ask.
        if not settings.ai_enabled:
            marker = AIDisabledMarker(
                checked_at=settings.get_current_datetime()
            ).model_dump(mode="json")
            loader.write_result_file(report_url_dir, "ai_disabled.json", marker)
            logger.info(
                f"ai_disabled.json written for {url_name} (AFR_AI_ENABLED=false)"
            )
            return {
                "url": url_name,
                "structured_data": {},
                "ai_analysis": marker,
                "report_path": report_url_dir,
                "processing_status": "ai_disabled",
            }

        try:
            structured_data = loader.load_structured_data(
                url_data["structured_data_path"]
            )
            if not structured_data:
                raise ValueError("No structured data loaded")

            screenshots = loader.load_screenshots(
                url_data["url_dir"], url_data.get("comparison_data")
            )
            if not any(k.endswith("_b64") for k in screenshots):
                raise ValueError("No screenshots loaded")

            ai_request = self.ai_client.create_request(
                url_name, structured_data, screenshots
            )
            ai_response = await self.ai_client.send(ai_request)

            # Persist structured data for HTML rendering / debugging.
            (report_url_dir / "structured_data.json").write_text(
                json.dumps(structured_data, indent=2, default=str),
                encoding="utf-8",
            )

            # Different filename per result_type so loader can route by name,
            # and so success files stay uncontaminated by error envelopes.
            # write_result_file clears the other three so each URL has exactly
            # one current verdict on disk.
            result_type = ai_response.get("result_type", "analysis_success")
            output_filename = (
                "ai_error.json"
                if result_type == "analysis_error"
                else "ai_analysis.json"
            )
            loader.write_result_file(report_url_dir, output_filename, ai_response)

            _copy_screenshots_to_report(screenshots, report_url_dir / "screenshots")

            processing_status = (
                "success" if result_type == "analysis_success" else "error"
            )
            log_severity = (
                ai_response.get("overall_severity")
                if result_type == "analysis_success"
                else f"AIAnalysisError({ai_response.get('error_type')})"
            )
            logger.info(f"Processed {url_name} → {log_severity}")

            return {
                "url": url_name,
                "structured_data": structured_data,
                "ai_analysis": ai_response,
                "report_path": report_url_dir,
                "processing_status": processing_status,
                "screenshots_available": [
                    k.replace("_b64", "") for k in screenshots if k.endswith("_b64")
                ],
            }

        except Exception as e:
            logger.error(f"Error processing URL {url_name}: {e}")
            error_response = AIClient._synthesize_error(
                request_id=None,
                error_type="config_error",  # processing-level (not a server error)
                retryable=False,
                details=f"{type(e).__name__}: {str(e)[:500]}",
            )
            loader.write_result_file(report_url_dir, "ai_error.json", error_response)
            return {
                "url": url_name,
                "structured_data": {},
                "ai_analysis": error_response,
                "report_path": report_url_dir,
                "processing_status": "error",
                "error": str(e),
            }

    # ------------------------------------------------------------------
    # Final aggregation + HTML rendering
    # ------------------------------------------------------------------

    async def generate_enhanced_report(self, run_root: Path, report_date: str) -> Path:
        """Load per-URL results from `run_root/`, aggregate, write HTML there.

        `run_root` is the atomic-publication tmp dir (the orchestrator owns
        the publish lifecycle). `report_date` is passed through to the
        template for display purposes only.
        """
        logger.info(f"Generating enhanced report into {run_root}")

        all_url_results = loader.load_all_url_results(run_root)
        if not all_url_results:
            raise ValueError(f"No processed URL results found in {run_root}")

        aggregated_file = run_root / "aggregated_analysis.json"
        if aggregated_file.exists():
            logger.info("Loading existing aggregated analysis")
            aggregated_analysis = json.loads(
                aggregated_file.read_text(encoding="utf-8")
            )
        else:
            logger.info("Generating new aggregated analysis")
            aggregated_analysis = aggregator.aggregate_analyses(all_url_results)
            aggregated_file.write_text(
                json.dumps(aggregated_analysis, indent=2, default=str),
                encoding="utf-8",
            )

        template_data = html_renderer.build_template_data(
            aggregated_analysis,
            all_url_results,
            report_date,
        )
        html_content = html_renderer.render(template_data)

        enhanced_report_path = run_root / "enhanced_analysis_report.html"
        enhanced_report_path.write_text(html_content, encoding="utf-8")
        logger.info(f"Enhanced report generated: {enhanced_report_path}")
        return enhanced_report_path


def _copy_screenshots_to_report(screenshots: dict[str, Any], dest_dir: Path) -> None:
    """Copy baseline/current/visual_diff PNGs into the report directory for HTML access."""
    dest_dir.mkdir(exist_ok=True)
    for kind in ("baseline", "current", "visual_diff"):
        src = screenshots.get(f"{kind}_path")
        if not src:
            continue
        source_path = Path(src)
        if source_path.exists():
            dest_path = dest_dir / f"{kind}.png"
            shutil.copy2(source_path, dest_path)
            logger.debug(f"Copied {kind} screenshot to {dest_path}")
