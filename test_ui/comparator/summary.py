"""Master change-summary aggregator (Phase A.3 split from engine.py).

Consumes the per-category results (screenshot/dom/css/js/media) and produces
the `change_summary.json` shape that flows into AI prompts. Severity logic is
heuristic, not normative - see Implementation Flag "multi-signal change
detection" in REFACTOR_AND_DASHBOARD_PLAN.md for the planned overhaul.

Behaviour preserved verbatim from the pre-split engine to keep Phase A.2
goldens passing - except `affected_components`, which is now `sorted()` to
fix non-deterministic set iteration (was a known A.2 flake source).
"""

from __future__ import annotations

from typing import Any


def create_change_summary_json(
    screenshot_result: dict[str, Any],
    dom_result: dict[str, Any],
    css_result: dict[str, Any],
    js_result: dict[str, Any],
    media_result: dict[str, Any],
) -> dict[str, Any]:
    """Aggregate per-category results into the master change-summary dict."""
    visual_changes = screenshot_result.get("visual_changes", False)
    html_changes = dom_result.get("has_changes", False) and "error" not in dom_result
    css_changes = css_result.get("has_changes", False)
    js_changes = js_result.get("has_changes", False)
    media_changes = media_result.get("has_changes", False)

    changes_detected = any(
        [visual_changes, html_changes, css_changes, js_changes, media_changes]
    )

    severities: list[str] = []
    if "error" not in screenshot_result and visual_changes:
        ssim_score = screenshot_result.get("ssim_score", 1.0)
        if ssim_score < 0.8:
            severities.append("high")
        elif ssim_score < 0.95:
            severities.append("medium")
        else:
            severities.append("low")

    # Per-category default severities. These are heuristics, not measurements;
    # the multi-signal-detection flag tracks replacing this with something
    # actually grounded in diff magnitude.
    if css_changes:
        severities.append("medium")
    if js_changes:
        severities.append("high")
    if html_changes:
        severities.append("low")

    if not severities:
        overall_severity = "none"
    elif "high" in severities:
        overall_severity = "high"
    elif "medium" in severities:
        overall_severity = "medium"
    else:
        overall_severity = "low"

    if overall_severity == "high":
        user_impact = "high"
    elif visual_changes or css_changes:
        user_impact = "medium"
    elif html_changes:
        user_impact = "low"
    else:
        user_impact = "none"

    affected_components: list[str] = []
    if visual_changes:
        affected_components.append("visual_layout")
    if html_changes:
        affected_components.extend(["content", "structure"])
    if css_changes:
        affected_components.append("styling")
    if js_changes:
        affected_components.append("functionality")

    recommendations: list[str] = []
    if visual_changes:
        recommendations.append("Review visual changes in layout")
    if js_changes:
        recommendations.append("Test JavaScript functionality")
    if css_changes:
        recommendations.append("Verify styling consistency")
    recommendation = (
        "; ".join(recommendations) if recommendations else "No changes detected"
    )

    return {
        "overall_assessment": {
            "changes_detected": changes_detected,
            "change_severity": overall_severity,
            "user_impact": user_impact,
            "requires_review": overall_severity != "none",
        },
        "change_categories": {
            "visual": {
                "screenshot_similarity": screenshot_result.get("ssim_score", 1.0),
                "visual_changes": visual_changes,
                "layout_shifts": screenshot_result.get("dimensions_changed", False),
            },
            # A.3 fix - was reading flat keys (title_changed / content_changed /
            # structure_changed) that dom.compare_dom never emits; result was
            # always all-False even when those things had clearly changed.
            # dom.compare_dom emits nested objects; read them directly.
            "content": {
                "title_changed": dom_result.get("title", {}).get("changed", False),
                "text_content_changed": dom_result.get("content", {}).get(
                    "significant_change", False
                ),
                "structure_changed": len(
                    dom_result.get("structure", {}).get("element_changes", [])
                )
                > 0,
            },
            "technical": {
                "html_changes": html_changes,
                "css_changes": css_changes,
                "js_changes": js_changes,
                "asset_changes": media_changes,
            },
        },
        # sorted() not just for stability - affected_components flows into
        # change_summary.json (a Phase A.2 golden) and into AI prompts.
        # Set iteration order is implementation-defined → non-reproducible runs.
        "affected_components": sorted(set(affected_components)),
        "recommendation": recommendation,
        "ai_analysis_priority": overall_severity,
    }


__all__ = ["create_change_summary_json"]
