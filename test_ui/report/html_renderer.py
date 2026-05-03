"""HTML report rendering (Phase A.3 split).

Owns the Jinja template + the helpers that derive system status / build the
template data dict. Pure functions over the loader+aggregator outputs. The
template itself lives at `templates/enhanced_report.html.j2` so designers can
edit it without touching Python.

**Bug fixes shipped in A.3:**

1. `confidence_metrics` template references corrected. Pre-A.3 the template
   read `aggregation.confidence_metrics.average_confidence` /
   `min_confidence` / `max_confidence`, but `calculate_confidence_metrics`
   actually emits `confidence_metrics.ai_confidence.{average,min,max}`. The
   old paths raised UndefinedError on every render — undetected because no
   end-to-end run had ever fully completed before A.1.

2. `result_type` discriminator now drives the per-URL section. Pre-A.3 the
   template branched on legacy `analysis_type == "no_changes_detected"`,
   which couldn't distinguish the typed result variants. Each of
   analysis_success / analysis_error / no_changes / ai_disabled now has its
   own badge + body, including a clear "AI analysis failed" panel for the
   error case (the critique called this out as missing).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Template

from ..config import settings


_TEMPLATE_PATH = Path(__file__).parent / "templates" / "enhanced_report.html.j2"


def render(template_data: dict[str, Any]) -> str:
    """Render the enhanced-report template against `template_data`. No side effects."""
    return Template(_TEMPLATE_PATH.read_text(encoding="utf-8")).render(template_data)


def determine_system_status(aggregated_analysis: dict[str, Any]) -> str:
    """Map aggregated summary counts to a single status string.

    Priority: critical > warning > error > changes > stable. Used in the
    header chip for at-a-glance triage.
    """
    summary = aggregated_analysis.get("summary", {})
    if summary.get("critical_issues", 0) > 0:
        return "critical"
    if summary.get("warnings", 0) > 0:
        return "warning"
    if summary.get("errors", 0) > 0:
        return "error"
    if summary.get("urls_with_changes", 0) > 0:
        return "changes"
    return "stable"


def build_template_data(
    aggregated_analysis: dict[str, Any],
    all_url_results: list[dict[str, Any]],
    report_date: str,
) -> dict[str, Any]:
    """Assemble the dict the Jinja template consumes. Adds derived fields."""
    summary = aggregated_analysis.get("summary", {})
    confidence = aggregated_analysis.get("confidence_metrics", {})
    composite = confidence.get("composite_confidence", 0.0)

    return {
        "timestamp": settings.get_current_datetime(),
        "report_date": report_date,
        "aggregation": aggregated_analysis,
        "url_results": all_url_results,
        "total_urls": len(all_url_results),
        "has_critical": summary.get("critical_issues", 0) > 0,
        "has_warnings": summary.get("warnings", 0) > 0,
        "has_errors": summary.get("errors", 0) > 0,
        # Composite is the right confidence signal for the header chip — it
        # already weights ai_confidence + data_quality + completeness.
        "confidence_level": "high" if composite >= 0.8 else "medium",
        "system_status": determine_system_status(aggregated_analysis),
    }


__all__ = ["render", "build_template_data", "determine_system_status"]
