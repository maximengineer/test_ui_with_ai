"""Cross-URL aggregation + confidence metrics (Phase A.3 split).

Pure functions over the per-URL results loader produces. No I/O. Routes by the
`result_type` discriminator from the contract (Phase A.1.8) — was previously
severity-based, which couldn't distinguish "AI was never called" (no_changes /
ai_disabled) from "AI failed" (analysis_error). Legacy fallback paths retained
for files persisted before A.1.8 (those don't carry result_type).
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from ..config import settings
from .confidence import calculate_confidence_metrics


def aggregate_analyses(all_url_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Bucket per-URL results by severity / status; compute summary + insights."""
    logger.info(f"Aggregating analysis from {len(all_url_results)} URLs")

    critical_issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    safe_changes: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    no_changes: list[dict[str, Any]] = []

    for result in all_url_results:
        ai_analysis = result.get("ai_analysis", {})
        result_type = ai_analysis.get("result_type")

        if result_type == "analysis_success":
            severity = ai_analysis.get("overall_severity")
            if severity == "CRITICAL":
                critical_issues.append(result)
            elif severity == "WARNING":
                warnings.append(result)
            elif severity == "SAFE":
                safe_changes.append(result)
            else:
                # Schema constrains overall_severity to those three values.
                # Treat anomaly as a warning rather than crashing.
                warnings.append(result)
        elif result_type == "analysis_error":
            errors.append(result)
        elif result_type in ("no_changes", "ai_disabled"):
            # Both mean "AI was not invoked for this URL" — bucket together
            # for aggregation. The HTML renderer can still distinguish them.
            no_changes.append(result)
        else:
            # Legacy fallback for files persisted before A.1.8.
            # Synthetic no-change records have analysis_type="no_changes_detected"
            # AND severity="SAFE" — check analysis_type first or they'd be
            # miscategorized as real SAFE analyses.
            if ai_analysis.get("analysis_type") == "no_changes_detected":
                no_changes.append(result)
            else:
                severity = ai_analysis.get("overall_severity", "UNKNOWN")
                if severity == "CRITICAL":
                    critical_issues.append(result)
                elif severity == "WARNING":
                    warnings.append(result)
                elif severity == "SAFE":
                    safe_changes.append(result)
                elif severity == "ERROR":
                    errors.append(result)
                else:
                    warnings.append(result)

    total_urls = len(all_url_results)
    urls_with_changes = len(
        [
            r
            for r in all_url_results
            if r.get("ai_analysis", {}).get("result_type") == "analysis_success"
            or (
                r.get("processing_status") == "success"
                and r.get("ai_analysis", {}).get("analysis_type")
                != "no_changes_detected"
                and not r.get("ai_analysis", {}).get("result_type")  # legacy
            )
        ]
    )

    return {
        "summary": {
            "total_urls_analyzed": total_urls,
            "urls_with_changes": urls_with_changes,
            "urls_without_changes": len(no_changes),
            "critical_issues": len(critical_issues),
            "warnings": len(warnings),
            "safe_changes": len(safe_changes),
            "errors": len(errors),
            "analysis_timestamp": settings.get_current_datetime(),
        },
        "severity_breakdown": {
            "critical": [
                {
                    "url": r["url"],
                    "impact": r["ai_analysis"].get("business_impact", "UNKNOWN"),
                }
                for r in critical_issues
            ],
            "warnings": [
                {
                    "url": r["url"],
                    "impact": r["ai_analysis"].get("business_impact", "UNKNOWN"),
                }
                for r in warnings
            ],
            "safe": [{"url": r["url"]} for r in safe_changes],
            "errors": [
                {"url": r["url"], "error": r.get("error", "Processing error")}
                for r in errors
            ],
        },
        "patterns": identify_common_patterns(all_url_results),
        "recommendations": generate_global_recommendations(all_url_results),
        "confidence_metrics": calculate_confidence_metrics(all_url_results),
    }


def identify_common_patterns(all_url_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Identify common HTML change types and recurring functional-impact keywords."""
    patterns: dict[str, Any] = {
        "common_html_changes": {},
        "recurring_issues": [],
        "affected_components": {},
        "change_types": {},
        "business_impact_distribution": {},
    }

    html_change_types: dict[str, int] = {}
    total_html_changes = 0

    for result in all_url_results:
        structured_data = result.get("structured_data", {})
        html_changes = structured_data.get("html_changes", {})

        if "changes" in html_changes:
            for change in html_changes["changes"][:10]:  # cap to avoid huge data
                change_type = change.get("type", "unknown")
                html_change_types[change_type] = (
                    html_change_types.get(change_type, 0) + 1
                )
                total_html_changes += 1

    patterns["common_html_changes"] = {
        "total_changes": total_html_changes,
        "change_types": dict(
            sorted(html_change_types.items(), key=lambda x: x[1], reverse=True)[:5]
        ),
    }

    impact_distribution: dict[str, int] = {}
    for result in all_url_results:
        ai_analysis = result.get("ai_analysis", {})
        impact = ai_analysis.get("business_impact", "UNKNOWN")
        impact_distribution[impact] = impact_distribution.get(impact, 0) + 1
    patterns["business_impact_distribution"] = impact_distribution

    functional_impacts: list[str] = []
    for result in all_url_results:
        detailed_analysis = result.get("ai_analysis", {}).get("detailed_analysis", {})
        impacts = detailed_analysis.get("functional_impact", [])
        functional_impacts.extend(impacts[:3])

    impact_counts: dict[str, int] = {}
    for impact in functional_impacts:
        for keyword in impact.lower().split():
            if len(keyword) > 3:
                impact_counts[keyword] = impact_counts.get(keyword, 0) + 1

    recurring_keywords = sorted(
        impact_counts.items(), key=lambda x: x[1], reverse=True
    )[:5]
    patterns["recurring_issues"] = [
        {
            "keyword": keyword,
            "frequency": count,
            "urls_affected": min(count, len(all_url_results)),
        }
        for keyword, count in recurring_keywords
        if count > 1
    ]

    return patterns


def generate_global_recommendations(
    all_url_results: list[dict[str, Any]],
) -> dict[str, list[str]]:
    """Translate severity counts into immediate / strategic / process / monitoring suggestions."""
    critical_count = sum(
        1
        for r in all_url_results
        if r.get("ai_analysis", {}).get("overall_severity") == "CRITICAL"
    )
    warning_count = sum(
        1
        for r in all_url_results
        if r.get("ai_analysis", {}).get("overall_severity") == "WARNING"
    )
    # Phase A.1.8 moved errors out of `overall_severity` (which is now
    # constrained to CRITICAL/WARNING/SAFE) and into a separate
    # AIAnalysisError variant identified by `result_type="analysis_error"`.
    # Counting by severity == "ERROR" was a pre-A.1.8 leftover that always
    # returned 0 for current data — the process_improvements recommendation
    # never fired. Match the discriminator-based logic in aggregate_analyses.
    error_count = sum(
        1
        for r in all_url_results
        if r.get("ai_analysis", {}).get("result_type") == "analysis_error"
    )

    recommendations: dict[str, list[str]] = {
        "immediate_actions": [],
        "strategic_actions": [],
        "process_improvements": [],
        "monitoring_suggestions": [],
    }

    if critical_count > 0:
        s = "s" if critical_count != 1 else ""
        recommendations["immediate_actions"].append(
            f"Address {critical_count} critical issue{s} before deployment"
        )
        recommendations["immediate_actions"].append(
            "Conduct thorough manual testing of affected functionality"
        )

    if warning_count > 0:
        s = "s" if warning_count != 1 else ""
        recommendations["strategic_actions"].append(
            f"Review {warning_count} warning{s} for intentional vs. unintentional changes"
        )

    if error_count > 0:
        s = "s" if error_count != 1 else ""
        recommendations["process_improvements"].append(
            f"Investigate {error_count} analysis error{s} to improve system reliability"
        )

    total_changes = sum(
        1 for r in all_url_results if r.get("processing_status") == "success"
    )
    if total_changes > len(all_url_results) * 0.7:
        recommendations["strategic_actions"].append(
            "High change volume detected - consider impact on user experience consistency"
        )

    recommendations["monitoring_suggestions"].extend(
        [
            "Set up automated regression testing for frequently changing components",
            "Monitor user feedback for any unexpected behavior",
            "Consider implementing gradual rollout for significant changes",
        ]
    )

    return recommendations


__all__ = [
    "aggregate_analyses",
    "identify_common_patterns",
    "generate_global_recommendations",
    "calculate_confidence_metrics",
]
