"""Projection of DOM compare output into AI-facing json shape."""

from __future__ import annotations

from typing import Any

def create_html_changes_json(dom_result: dict[str, Any]) -> dict[str, Any]:
    """Project a DOM-result dict into the `html_changes.json` shape AI consumes."""
    if "error" in dom_result:
        return {
            "changes_detected": False,
            "change_types": [],
            "changes": [],
            "summary": {
                "total_changes": 0,
                "structural_changes": 0,
                "content_changes": 0,
                "meta_changes": 0,
                "navigation_changes": 0,
                "severity": "none",
            },
        }

    changes: list[dict] = []
    change_types: list[str] = []

    title_info = dom_result.get("title", {})
    if title_info.get("changed", False):
        changes.append(
            {
                "type": "content",
                "element": "title",
                "change": "text_modified",
                "description": "Page title changed",
                "old_value": title_info.get("baseline", ""),
                "new_value": title_info.get("current", ""),
                "impact": "medium",
            }
        )
        change_types.append("content")

    structure_info = dom_result.get("structure", {})
    element_changes = structure_info.get("element_changes", [])
    specific_changes = structure_info.get("specific_changes", [])

    for elem_change in element_changes:
        changes.append(
            {
                "type": "structure",
                "element": elem_change["element"],
                "change": f"{elem_change['change_type']}_element",
                "description": (
                    f"{elem_change['change_type'].title()} {elem_change['count_change']} "
                    f"{elem_change['element']} element(s)"
                ),
                "old_value": elem_change["baseline_count"],
                "new_value": elem_change["current_count"],
                "impact": elem_change["impact"],
                "code_examples_count": len(elem_change.get("code_examples", [])),
            }
        )
        if "structure" not in change_types:
            change_types.append("structure")

    for specific_change in specific_changes:
        changes.append(
            {
                "type": "structure_detail",
                "element": specific_change["element"],
                "change": specific_change["change_type"],
                "description": specific_change["description"],
                "code_snippet": specific_change["code_snippet"],
                "position": specific_change.get("position", ""),
                "impact": specific_change["impact"],
            }
        )

    meta_info = dom_result.get("meta", {})
    meta_changes = meta_info.get("changes", [])
    for meta_change in meta_changes:
        changes.append(
            {
                "type": "attributes",
                "element": f"meta[{meta_change['key']}]",
                "change": meta_change["type"],
                "description": (
                    f"Meta tag '{meta_change['key']}' "
                    f"{meta_change['type'].replace('meta_', '')}"
                ),
                "old_value": meta_change.get("old_value", ""),
                "new_value": meta_change.get("new_value", ""),
                "impact": meta_change["impact"],
            }
        )
        if "attributes" not in change_types:
            change_types.append("attributes")

    nav_info = dom_result.get("navigation", {})
    nav_changes = nav_info.get("changes", [])
    for nav_change in nav_changes:
        changes.append(
            {
                "type": "structure",
                "element": "nav",
                "change": nav_change["type"],
                "description": (
                    "Navigation "
                    + nav_change["type"].replace("navigation_", "").replace("_", " ")
                ),
                "old_value": nav_change.get(
                    "baseline_count", nav_change.get("item", "")
                ),
                "new_value": nav_change.get("current_count", ""),
                "impact": nav_change["impact"],
            }
        )
        if "structure" not in change_types:
            change_types.append("structure")

    # Key-attribute changes (href, src, action, lang, ...). These were
    # previously invisible to the projection - now surfaced as
    # `attributes` so the AI severity rollup picks them up alongside
    # the existing meta-tag changes.
    key_attrs_info = dom_result.get("key_attributes", {})
    for attr_change in key_attrs_info.get("changes", []):
        changes.append(
            {
                "type": "attributes",
                "element": attr_change["key"],
                "change": attr_change["type"],
                "description": (
                    f"Attribute {attr_change['key']} "
                    f"{attr_change['type'].replace('attribute_', '')}"
                ),
                "old_value": attr_change.get("old_value", ""),
                "new_value": attr_change.get("new_value", ""),
                "impact": attr_change["impact"],
            }
        )
        if "attributes" not in change_types:
            change_types.append("attributes")

    # post-audit-01KR1BZE73: project dynamic-attribute changes (on*,
    # style, aria-*) into the AI-consumed shape. Same `type: attributes`
    # bucket as KEY_ATTRIBUTES because the AI reads them the same way;
    # the per-attribute IMPACT rating (high for on*, medium for style/
    # aria) propagates to the change_summary severity rollup.
    dynamic_attrs_info = dom_result.get("dynamic_attributes", {})
    for attr_change in dynamic_attrs_info.get("changes", []):
        changes.append(
            {
                "type": "attributes",
                "element": attr_change["key"],
                "change": attr_change["type"],
                "description": (
                    f"Dynamic attribute {attr_change['key']} "
                    f"{attr_change['type'].replace('attribute_', '')}"
                ),
                "old_value": attr_change.get("old_value", ""),
                "new_value": attr_change.get("new_value", ""),
                "impact": attr_change["impact"],
            }
        )
        if "attributes" not in change_types:
            change_types.append("attributes")

    headings_info = dom_result.get("headings", {})
    for heading_change in headings_info.get("changes", []):
        changes.append(
            {
                "type": "content",
                "element": heading_change["key"],
                "change": "heading_text_modified",
                "description": (f"Heading {heading_change['key']} text changed"),
                "old_value": heading_change["old_text"],
                "new_value": heading_change["new_text"],
                "impact": heading_change["impact"],
            }
        )
        if "content" not in change_types:
            change_types.append("content")

    content_info = dom_result.get("content", {})
    if content_info.get("significant_change", False):
        changes.append(
            {
                "type": "content",
                "element": "body",
                "change": "content_modified",
                "description": (
                    f"Text content changed significantly "
                    f"({content_info.get('baseline_length', 0)} → "
                    f"{content_info.get('current_length', 0)} chars)"
                ),
                "old_value": f"{content_info.get('baseline_length', 0)} characters",
                "new_value": f"{content_info.get('current_length', 0)} characters",
                "impact": "medium"
                if abs(content_info.get("length_change", 0)) > 1000
                else "low",
            }
        )
        if "content" not in change_types:
            change_types.append("content")

    high_impact_count = sum(1 for c in changes if c.get("impact") == "high")
    medium_impact_count = sum(1 for c in changes if c.get("impact") == "medium")
    if high_impact_count > 0:
        severity = "high"
    elif medium_impact_count > 0:
        severity = "medium"
    elif changes:
        severity = "low"
    else:
        severity = "none"

    return {
        "changes_detected": len(changes) > 0,
        "change_types": change_types,
        "changes": changes,
        "summary": {
            "total_changes": len(changes),
            "structural_changes": sum(1 for c in changes if c["type"] == "structure"),
            "content_changes": sum(1 for c in changes if c["type"] == "content"),
            "meta_changes": sum(1 for c in changes if c["type"] == "attributes"),
            "navigation_changes": len(nav_changes),
            "high_impact_changes": high_impact_count,
            "medium_impact_changes": medium_impact_count,
            "severity": severity,
        },
    }

__all__ = ["create_html_changes_json"]
