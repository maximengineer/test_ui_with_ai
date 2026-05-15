"""Master change-summary aggregator (Phase A.3 split from engine.py).

Consumes the per-category results (screenshot/dom/css/js/media) and produces
the `change_summary.json` shape that flows into AI prompts. Severity logic is
heuristic - the multi-signal change detection flag tracks the planned
overhaul - but the per-detector impact ratings (the DOM differ now emits
`impact: high/medium/low` per attribute / per heading / per meta change)
ARE consulted, so a phishing-class href hijack escalates to `change_severity:
high` instead of being flattened to "low" with the rest of the html bucket.
"""

from __future__ import annotations

from typing import Any


# Severity ladder used to roll up multiple per-detector impacts. Highest
# wins. "none" sorts below all real severities.
_SEVERITY_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}


def _max_severity(severities: list[str]) -> str:
    """Return the highest-ranked severity in `severities`, or 'none' if empty.

    Used to roll up per-detector impact ratings (e.g. the attribute walker
    rates `<a href>` mutations `high`; if any one such record appears the
    aggregator's html severity becomes `high`, not the old flat `low`).
    """
    if not severities:
        return "none"
    return max(severities, key=lambda s: _SEVERITY_RANK.get(s, 0))


def _html_severity_from_dom(dom_result: dict[str, Any]) -> str:
    """Roll up the dom_result into a single severity by walking every
    per-detector impact record and taking the max.

    Sources:
      - title.changed → medium (title affects SEO + tab UX)
      - key_attributes.changes[].impact (high for href/src/action, etc.)
      - dynamic_attributes.changes[].impact (high for on*, medium for
        style/aria-*) — post-audit-01KR1BZE73
      - headings.changes[].impact (medium per the heading walker)
      - meta.changes[].impact (varies per key)
      - structure.element_changes[].impact (per assess_element_impact)

    Returns 'low' when html changed but no detector emitted an impact
    rating - matches pre-fix behavior for that path.
    """
    impacts: list[str] = []
    if dom_result.get("title", {}).get("changed"):
        impacts.append("medium")
    for c in dom_result.get("key_attributes", {}).get("changes", []):
        impacts.append(c.get("impact", "low"))
    for c in dom_result.get("dynamic_attributes", {}).get("changes", []):
        impacts.append(c.get("impact", "low"))
    for c in dom_result.get("headings", {}).get("changes", []):
        impacts.append(c.get("impact", "medium"))
    for c in dom_result.get("meta", {}).get("changes", []):
        impacts.append(c.get("impact", "low"))
    for c in dom_result.get("structure", {}).get("element_changes", []):
        impacts.append(c.get("impact", "low"))
    return _max_severity(impacts) if impacts else "low"


def _affected_components_from_dom(dom_result: dict[str, Any]) -> list[str]:
    """Bucket the html_changes into specific component categories so the
    AI prompt + report rollup point at the right thing instead of always
    landing as ['content', 'structure']."""
    out: list[str] = []
    if dom_result.get("title", {}).get("changed"):
        out.append("title_and_metadata")
    if dom_result.get("meta", {}).get("changes"):
        out.append("title_and_metadata")
    if dom_result.get("structure", {}).get("element_changes"):
        out.extend(["content", "structure"])
    if dom_result.get("headings", {}).get("changes"):
        out.append("headings")
    # Attribute walker: bucket by the kind of attribute that changed.
    # Includes both KEY_ATTRIBUTES (curated list) and dynamic_attributes
    # (on*/style/aria-*) post-audit-01KR1BZE73.
    all_attr_changes = list(
        dom_result.get("key_attributes", {}).get("changes", [])
    ) + list(dom_result.get("dynamic_attributes", {}).get("changes", []))
    for c in all_attr_changes:
        key = c.get("key", "")
        # key shape: "tag[idx].attr" - split out the parts safely.
        try:
            tag = key.split("[", 1)[0]
            attr = key.rsplit(".", 1)[1] if "." in key else ""
        except (IndexError, ValueError):
            tag, attr = "", ""
        if tag == "a" and attr == "href":
            out.append("links_and_navigation")
        elif tag == "form":
            out.append("forms")
        elif tag == "script" and attr == "src":
            out.append("scripts")
        elif tag == "base" and attr == "href":
            out.append("page_globals")  # base.href affects every relative URL
        elif tag == "link":
            out.append("title_and_metadata")
        elif tag == "img":
            out.append("media")
        elif tag == "iframe":
            out.append("embedded_frames")
        elif tag in ("html", "body"):
            out.append("page_globals")
        elif attr.startswith("on") and len(attr) > 2:
            out.append("inline_event_handlers")  # XSS class
        elif attr == "style":
            out.append("inline_styles")
        elif attr.startswith("aria-"):
            out.append("accessibility")
        else:
            out.append("structure")
    return out


def _html_recommendations(dom_result: dict[str, Any]) -> list[str]:
    """Generate human-readable recommendations for what's in dom_result.
    Pre-fix this was empty for HTML-only changes, so the rolled-up
    `recommendation` string came back as 'No changes detected' even
    on phishing-class href hijacks - actively misleading."""
    out: list[str] = []
    if dom_result.get("title", {}).get("changed"):
        out.append("Verify page title (SEO + browser-tab impact)")
    href_or_action = any(
        c.get("key", "").endswith((".href", ".action", ".src"))
        for c in dom_result.get("key_attributes", {}).get("changes", [])
    )
    if href_or_action:
        out.append(
            "Audit changed link/form/script targets - possible phishing or "
            "supply-chain injection"
        )
    # post-audit-01KR1BZE73: dynamic-attribute classes have distinct
    # remediation guidance.
    dyn_changes = dom_result.get("dynamic_attributes", {}).get("changes", [])
    if any((c.get("key", "").rsplit(".", 1)[-1].startswith("on")) for c in dyn_changes):
        out.append(
            "Audit added/changed inline event handlers (on*) - XSS-class "
            "attribute injection vector"
        )
    if any(c.get("key", "").endswith(".style") for c in dyn_changes):
        out.append("Review inline style= attributes - bypasses the CSS file diff")
    if any(".aria-" in c.get("key", "") for c in dyn_changes):
        out.append("Verify aria-* attribute changes - accessibility regression")
    if dom_result.get("headings", {}).get("changes"):
        out.append("Review heading text changes (visible content)")
    if dom_result.get("meta", {}).get("changes"):
        out.append("Verify meta-tag changes (SEO + indexing impact)")
    structure_changes = dom_result.get("structure", {}).get("element_changes", [])
    if structure_changes:
        out.append("Review structural element add/remove changes")
    return out


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

    # Per-category default severities for css/js. These are heuristics
    # (multi-signal-detection flag tracks the planned overhaul) but the
    # html category now reads per-detector impacts via _html_severity_from_dom
    # so phishing/XSS/etc. don't get flattened to "low".
    if css_changes:
        severities.append("medium")
    if js_changes:
        severities.append("high")
    if media_changes:
        # Media files changing (images/video/audio) is user-visible and should
        # require review even when CSS/JS/HTML are untouched.
        severities.append("medium")
    if html_changes:
        severities.append(_html_severity_from_dom(dom_result))

    overall_severity = _max_severity(severities)

    if overall_severity == "high":
        user_impact = "high"
    elif overall_severity == "medium" or visual_changes or css_changes:
        user_impact = "medium"
    elif html_changes:
        user_impact = "low"
    else:
        user_impact = "none"

    affected_components: list[str] = []
    if visual_changes:
        affected_components.append("visual_layout")
    if html_changes:
        affected_components.extend(_affected_components_from_dom(dom_result))
    if css_changes:
        affected_components.append("styling")
    if js_changes:
        affected_components.append("functionality")
    if media_changes:
        affected_components.append("media")

    recommendations: list[str] = []
    if visual_changes:
        recommendations.append("Review visual changes in layout")
    if js_changes:
        recommendations.append("Test JavaScript functionality")
    if css_changes:
        recommendations.append("Verify styling consistency")
    if html_changes:
        recommendations.extend(_html_recommendations(dom_result))
    if media_changes:
        recommendations.append("Review media asset changes")
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
                # New post-fix (audit 01KQX5SV...): surface attribute and
                # heading mutations explicitly so the AI's category check
                # (and any downstream consumers) can see they happened.
                "attribute_changes": len(
                    dom_result.get("key_attributes", {}).get("changes", [])
                ),
                # post-audit-01KR1BZE73: surface dynamic-attribute counts
                # (on*/style/aria-*) separately so the AI can see them
                # distinct from the curated KEY_ATTRIBUTES list.
                "dynamic_attribute_changes": len(
                    dom_result.get("dynamic_attributes", {}).get("changes", [])
                ),
                "heading_changes": len(
                    dom_result.get("headings", {}).get("changes", [])
                ),
                "meta_changes": len(dom_result.get("meta", {}).get("changes", [])),
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
