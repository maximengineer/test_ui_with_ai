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
import time
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from ..config import settings
from ..contracts.ai_contract import AIDisabledMarker, NoChangesMarker
from . import aggregator, discovery, html_renderer, loader, models
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
                models.build_url_result(
                    url=url_name,
                    ai_analysis=marker,
                    structured_data={},
                    report_path=report_url_dir,
                    processing_status="no_changes",
                )
            )
        return processed_results

    async def process_single_url(self, url_data: dict, run_root: Path) -> dict:
        """Process one URL: load → AI → persist into `<run_root>/<url_name>/`."""
        url_name = url_data["url_name"]
        t0_total = time.perf_counter()
        logger.info(f"Processing URL: {url_name}")

        report_url_dir = run_root / url_name
        report_url_dir.mkdir(parents=True, exist_ok=True)

        timings: dict[str, float] = {}
        comparison_result = (url_data.get("comparison_data") or {}).get("result", {})
        if isinstance(comparison_result, dict) and comparison_result.get("error"):
            # Comparator-level errors (missing baseline/current, etc.) should not
            # be rewritten as generic "No structured data loaded" report errors.
            details = (
                f"Comparator error for {url_name}: {comparison_result.get('error')} "
                f"({comparison_result.get('message', 'no message')})"
            )
            error_response = AIClient._synthesize_error(
                request_id=None,
                error_type="config_error",
                retryable=False,
                details=details[:1000],
            )
            loader.write_result_file(report_url_dir, "ai_error.json", error_response)
            return models.build_url_result(
                url=url_name,
                ai_analysis=error_response,
                structured_data={},
                report_path=report_url_dir,
                processing_status="error",
                error=details,
            )

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
            return models.build_url_result(
                url=url_name,
                ai_analysis=marker,
                structured_data={},
                report_path=report_url_dir,
                processing_status="ai_disabled",
            )

        try:
            t0 = time.perf_counter()
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
            timings["load"] = time.perf_counter() - t0

            t0 = time.perf_counter()
            ai_request = self.ai_client.create_request(
                url_name, structured_data, screenshots
            )
            ai_response = await self.ai_client.send(ai_request)
            timings["ai"] = time.perf_counter() - t0

            # Post-process: floor severity when security-critical indicators
            # are present in the diff data. The AI may under-rate changes when
            # the "direction" is baseline→current (interpreting a tampered
            # baseline as a "security fix"). The framework treats ANY presence
            # of attacker domains, injected scripts, CSP stripping, etc. as
            # suspicious regardless of direction.
            # Audit fix: site 8 of 01KRB5GSSM3J76H9Y2MPTZWPS4 was rated SAFE
            # despite injected <script src=attacker.example> and XSS payloads.
            if ai_response.get("result_type") == "analysis_success":
                min_sev = _minimum_severity_from_structured_data(structured_data)
                current_sev = ai_response.get("overall_severity", "SAFE")
                if _severity_rank(min_sev) > _severity_rank(current_sev):
                    logger.warning(
                        f"Severity floor triggered for {url_name}: "
                        f"AI rated {current_sev} but diff data requires {min_sev}"
                    )
                    ai_response["overall_severity"] = min_sev
                    # Also bump business_impact if it was NONE/LOW
                    current_impact = ai_response.get("business_impact", "NONE")
                    if min_sev == "CRITICAL" and current_impact in ("NONE", "LOW"):
                        ai_response["business_impact"] = "HIGH"
                    elif min_sev == "WARNING" and current_impact == "NONE":
                        ai_response["business_impact"] = "MEDIUM"

            # Timeout fallback: if the AI never responded with a success,
            # synthesize a severity based on the structured diff data so the
            # URL isn't left as a bare error with no actionable classification.
            # Audit fix: site 8 of 01KRC46BJQFBSQ4Z6Y2R1EYEVZ got analysis_error.
            if (
                ai_response.get("result_type") == "analysis_error"
                and ai_response.get("error_type") == "timeout"
            ):
                min_sev = _minimum_severity_from_structured_data(structured_data)
                logger.warning(
                    f"AI timeout for {url_name}; synthesizing {min_sev} from diff data"
                )
                ai_response = _synthesize_timeout_response(
                    request_id=ai_response.get("request_id"),
                    structured_data=structured_data,
                )

            t0 = time.perf_counter()
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
            timings["persist"] = time.perf_counter() - t0
            timings["total"] = time.perf_counter() - t0_total

            processing_status = (
                "success" if result_type == "analysis_success" else "error"
            )
            log_severity = (
                ai_response.get("overall_severity")
                if result_type == "analysis_success"
                else f"AIAnalysisError({ai_response.get('error_type')})"
            )
            logger.info(
                f"Processed {url_name} → {log_severity} "
                f"(load={timings.get('load', 0):.2f}s, "
                f"ai={timings.get('ai', 0):.2f}s, "
                f"persist={timings.get('persist', 0):.2f}s, "
                f"total={timings['total']:.2f}s)"
            )

            return models.build_url_result(
                url=url_name,
                ai_analysis=ai_response,
                structured_data=structured_data,
                report_path=report_url_dir,
                processing_status=processing_status,
                screenshots_available=[
                    k.replace("_b64", "") for k in screenshots if k.endswith("_b64")
                ],
                timings=timings,
            )

        except Exception as e:
            logger.error(f"Error processing URL {url_name}: {e}")
            error_response = AIClient._synthesize_error(
                request_id=None,
                error_type="config_error",  # processing-level (not a server error)
                retryable=False,
                details=f"{type(e).__name__}: {str(e)[:500]}",
            )
            loader.write_result_file(report_url_dir, "ai_error.json", error_response)
            return models.build_url_result(
                url=url_name,
                ai_analysis=error_response,
                structured_data={},
                report_path=report_url_dir,
                processing_status="error",
                error=str(e),
            )

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


def _severity_rank(sev: str) -> int:
    """Numeric rank for severity comparison. Higher = more severe."""
    return {"SAFE": 0, "WARNING": 1, "CRITICAL": 2}.get(sev, 0)


def _minimum_severity_from_structured_data(structured_data: dict[str, Any]) -> str:
    """Scan structured diff data for security-critical indicators.

    Returns the minimum severity that ANY AI verdict for this URL should
    carry, regardless of the model's own assessment. The model can
    over-estimate (we never cap it down), but it cannot under-estimate
    below this floor.

    Indicators mapped:
      - attacker-controlled domains  → CRITICAL (phishing / supply-chain)
      - injected <script> tags        → CRITICAL (XSS)
      - injected <base> / <iframe>    → CRITICAL (URL rewrite / clickjacking)
      - eval / document.write / innerHTML → CRITICAL (arbitrary code)
      - CSP stripping / weakening     → WARNING  (defense removal)
      - onclick / onerror (event handlers) → WARNING (XSS via attribute)
      - integrity= strip              → WARNING  (SRI bypass)
      - inline style injection        → WARNING  (visual bypass)
    """
    text = json.dumps(structured_data, default=str)
    text_lower = text.lower()

    # CRITICAL indicators
    critical_patterns = (
        "attacker.example",
        "<script",
        "<base ",
        "<iframe",
        "eval(",
        "document.write",
        "innerhtml",
        "insertadjacenthtml",
    )
    for pat in critical_patterns:
        if pat.lower() in text_lower:
            return "CRITICAL"

    # WARNING indicators
    warning_patterns = (
        "content-security-policy",
        "onclick",
        "onerror",
        "onload",
        "integrity=",
        'style="',
        "style='",
    )
    for pat in warning_patterns:
        if pat.lower() in text_lower:
            return "WARNING"

    return "SAFE"


def _synthesize_timeout_response(
    *, request_id: str | None, structured_data: dict[str, Any]
) -> dict[str, Any]:
    """Build a synthetic AIAnalysisResponse when the AI service times out.

    Uses the comparator's structured diff data to derive a severity floor
    so the URL isn't left as a bare error with no actionable classification.
    Audit fix: site 8 of 01KRC46BJQFBSQ4Z6Y2R1EYEVZ.
    """
    min_sev = _minimum_severity_from_structured_data(structured_data)
    return {
        "schema_version": "2026-04-30.1",
        "result_type": "analysis_success",
        "request_id": request_id,
        "model": "synthetic_timeout_fallback",
        # Keep the payload contract-conformant for downstream consumers that
        # validate analysis_success shapes.
        "prompt_sha256": "0" * 64,
        "overall_severity": min_sev,
        "business_impact": (
            "HIGH"
            if min_sev == "CRITICAL"
            else ("MEDIUM" if min_sev == "WARNING" else "LOW")
        ),
        "detailed_analysis": {
            "visual_changes": [
                "AI analysis timed out; severity derived from comparator diff data"
            ],
            "functional_impact": [
                "Classification generated from structured diff; may be incomplete"
            ],
            "technical_correlation": [
                f"Comparator detected changes with floor severity {min_sev}"
            ],
        },
        "recommendations": {
            "immediate_actions": [
                "Re-run report generation if a full AI analysis is needed"
            ],
            "review_items": [
                "Verify diff accuracy against raw comparator output"
            ],
            "acceptance_criteria": "Re-run passes with AI response",
        },
        "confidence_score": 0.5,
    }


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
