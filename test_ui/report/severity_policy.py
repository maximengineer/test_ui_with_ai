"""Deterministic report severity policy.

The model can over-rate a change, but it must not under-rate structured
evidence that the comparator already found. This module owns those floors and
the response augmentation that keeps report prose consistent with them.
"""

from __future__ import annotations

import json
from typing import Any


def _severity_rank(sev: str) -> int:
    """Numeric rank for severity comparison. Higher = more severe."""
    return {"SAFE": 0, "WARNING": 1, "CRITICAL": 2}.get(sev, 0)


def _impact_rank(impact: str) -> int:
    """Numeric rank for business-impact comparison. Higher = more impact."""
    return {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}.get(impact, 0)


def _minimum_severity_from_structured_data(structured_data: dict[str, Any]) -> str:
    """Scan structured diff data for review-required indicators."""
    return _severity_floor_details(structured_data)[0]


def _maximum_severity_from_structured_data(structured_data: dict[str, Any]) -> str:
    """Return the highest severity the model may assign from deterministic evidence."""
    return _severity_cap_details(structured_data)[0]


def _maximum_business_impact_for_capped_severity(
    structured_data: dict[str, Any], max_sev: str
) -> str | None:
    """Return a business-impact cap when severity has been capped."""
    if max_sev != "WARNING":
        return None

    assessment = structured_data.get("change_summary", {}).get("overall_assessment", {})
    user_impact = ""
    if isinstance(assessment, dict):
        user_impact = str(assessment.get("user_impact", "")).upper()

    return "HIGH" if user_impact == "HIGH" else "MEDIUM"


def _severity_floor_details(structured_data: dict[str, Any]) -> tuple[str, list[str]]:
    """Return severity floor plus short reasons for synthetic explanations."""
    text = json.dumps(structured_data, default=str)
    text_lower = text.lower()
    floor = "SAFE"
    reasons: list[str] = []

    def consider(severity: str, new_reasons: list[str]) -> None:
        nonlocal floor, reasons
        if severity == "SAFE":
            return
        if _severity_rank(severity) > _severity_rank(floor):
            floor = severity
            reasons = new_reasons
        elif severity == floor:
            reasons.extend(reason for reason in new_reasons if reason not in reasons)

    critical_patterns = (
        "attacker.example",
        "<script",
        "<base ",
        "<iframe",
        "eval(",
        "document.write",
        "innerhtml",
        "insertadjacenthtml",
        "onclick",
        "onerror",
        "onload",
    )
    for pat in critical_patterns:
        if pat.lower() in text_lower:
            consider("CRITICAL", [f"security-sensitive diff marker: {pat}"])
            break

    warning_patterns = (
        "content-security-policy",
        "integrity=",
        'style="',
        "style='",
    )
    for pat in warning_patterns:
        if pat.lower() in text_lower:
            consider("WARNING", [f"review-required diff marker: {pat}"])
            break

    js_severity, js_reasons = _js_severity_floor_details(structured_data)
    consider(js_severity, js_reasons)

    html_severity, html_reasons = _html_severity_floor_details(structured_data)
    consider(html_severity, html_reasons)

    css_severity, css_reasons = _css_severity_floor_details(structured_data)
    consider(css_severity, css_reasons)

    visual = (
        structured_data.get("change_summary", {})
        .get("change_categories", {})
        .get("visual", {})
    )
    if visual.get("visual_changes") is True:
        similarity = visual.get("screenshot_similarity")
        reason = "comparator-confirmed visual diff"
        if similarity is not None:
            reason = f"{reason} (screenshot_similarity={similarity})"
        consider("WARNING", [reason])

    return floor, reasons


def _severity_cap_details(structured_data: dict[str, Any]) -> tuple[str, list[str]]:
    """Cap model-critical output unless structured data has critical markers."""
    floor, reasons = _severity_floor_details(structured_data)
    if floor == "CRITICAL":
        return "CRITICAL", reasons
    return "WARNING", ["no deterministic critical marker in structured diff data"]


def _html_severity_floor_details(
    structured_data: dict[str, Any],
) -> tuple[str, list[str]]:
    """Derive a floor from structured HTML diffs without flagging every markup edit."""
    html_changes = structured_data.get("html_changes", {})
    if not isinstance(html_changes, dict) or not html_changes.get("changes_detected"):
        return "SAFE", []

    summary = html_changes.get("summary", {})
    if not isinstance(summary, dict):
        summary = {}

    severity = str(summary.get("severity", "")).lower()
    if severity in {"medium", "high"}:
        return "WARNING", [f"{severity}-severity HTML diff summary"]

    high_count = _safe_int(summary.get("high_impact_changes"))
    medium_count = _safe_int(summary.get("medium_impact_changes"))
    if high_count:
        return "WARNING", [f"high-impact HTML diff count: {high_count}"]
    if medium_count:
        return "WARNING", [f"medium-impact HTML diff count: {medium_count}"]

    meta_count = _safe_int(summary.get("meta_changes"))
    if meta_count:
        return "WARNING", [f"HTML metadata diff count: {meta_count}"]

    return "SAFE", []


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _js_severity_floor_details(
    structured_data: dict[str, Any],
) -> tuple[str, list[str]]:
    """Derive a floor from structured JS diffs without treating all JS as critical."""
    js_changes = structured_data.get("js_changes", {})
    if not isinstance(js_changes, dict) or not js_changes.get("changes_detected"):
        return "SAFE", []

    changes = js_changes.get("changes", [])
    if not isinstance(changes, list):
        changes = []

    for change in changes:
        if not isinstance(change, dict):
            continue
        reason = _js_critical_reason(change)
        if reason:
            return "CRITICAL", [reason]

    for change in changes:
        if not isinstance(change, dict):
            continue
        reason = _js_warning_reason(change)
        if reason:
            return "WARNING", [reason]

    for change in changes:
        if isinstance(change, dict) and str(change.get("impact", "")).lower() == "high":
            descriptor = _js_change_descriptor(change)
            return "WARNING", [f"high-impact JavaScript diff: {descriptor}"]

    summary = js_changes.get("summary", {})
    if isinstance(summary, dict):
        severity = str(summary.get("severity", "")).lower()
        impact = str(summary.get("functionality_impact", "")).lower()
        if severity == "high" or impact == "high":
            return "WARNING", ["high-severity JavaScript diff summary"]

    return "SAFE", []


def _js_critical_reason(change: dict[str, Any]) -> str | None:
    """Detect JavaScript changes that imply code execution or exfiltration risk."""
    text = _js_change_text(change)
    descriptor = _js_change_descriptor(change)
    critical_markers = (
        ("attacker.example", "attacker-controlled endpoint"),
        ("eval(", "dynamic eval"),
        ("new function", "dynamic Function constructor"),
        ("document.write", "document.write injection sink"),
        ("innerhtml", "innerHTML injection sink"),
        ("insertadjacenthtml", "insertAdjacentHTML injection sink"),
        ('settimeout("', "string-evaluated setTimeout"),
        ("settimeout('", "string-evaluated setTimeout"),
        ('setinterval("', "string-evaluated setInterval"),
        ("setinterval('", "string-evaluated setInterval"),
    )
    for marker, label in critical_markers:
        if marker in text:
            return f"critical JavaScript marker ({label}): {descriptor}"
    return None


def _js_warning_reason(change: dict[str, Any]) -> str | None:
    """Detect JavaScript behavior changes that require review, but not criticality."""
    text = _js_change_text(change)
    descriptor = _js_change_descriptor(change)
    warning_markers = (
        ("document.cookie", "cookie access"),
        ("localstorage", "localStorage access"),
        ("sessionstorage", "sessionStorage access"),
        ("navigator.sendbeacon", "beacon/network telemetry"),
        ("postmessage", "cross-window messaging"),
        ("xmlhttprequest", "network request"),
    )
    for marker, label in warning_markers:
        if marker in text:
            return f"review-required JavaScript marker ({label}): {descriptor}"
    return None


def _js_change_text(change: dict[str, Any]) -> str:
    parts = [
        change.get("change_type"),
        change.get("description"),
        change.get("code_snippet"),
        change.get("old_value"),
        change.get("new_value"),
        change.get("function_name"),
        change.get("file"),
    ]
    return " ".join(str(part) for part in parts if part is not None).lower()


def _js_change_descriptor(change: dict[str, Any]) -> str:
    descriptor = change.get("function_name") or change.get("file") or change.get(
        "description"
    )
    return str(descriptor) if descriptor else "JavaScript change"


def _css_severity_floor_details(
    structured_data: dict[str, Any],
) -> tuple[str, list[str]]:
    """Derive a floor from structured CSS diffs without broad text scanning."""
    css_changes = structured_data.get("css_changes", {})
    if not isinstance(css_changes, dict) or not css_changes.get("changes_detected"):
        return "SAFE", []

    changes = css_changes.get("changes", [])
    if not isinstance(changes, list):
        changes = []

    for change in changes:
        if not isinstance(change, dict):
            continue
        reason = _css_hide_or_interaction_reason(change)
        if reason:
            return "CRITICAL", [reason]

    for change in changes:
        if isinstance(change, dict) and str(change.get("impact", "")).lower() == "high":
            descriptor = change.get("selector") or change.get("description") or "CSS change"
            return "WARNING", [f"high-impact CSS diff: {descriptor}"]

    summary = css_changes.get("summary", {})
    if isinstance(summary, dict) and str(summary.get("severity", "")).lower() == "high":
        return "WARNING", ["high-severity CSS diff summary"]

    return "SAFE", []


def _css_hide_or_interaction_reason(change: dict[str, Any]) -> str | None:
    """Detect CSS rules that hide content or block user interaction."""
    selector = change.get("selector") or change.get("description") or "CSS selector"
    for prop, value in _iter_css_property_values(change):
        prop_l = str(prop).strip().lower()
        value_l = str(value).strip().lower()
        if prop_l == "display" and _css_value_has_token(value_l, "none"):
            return f"CSS hides content: {selector} display={value}"
        if prop_l == "visibility" and _css_value_has_token(value_l, "hidden"):
            return f"CSS hides content: {selector} visibility={value}"
        if prop_l == "opacity" and _css_numeric_zero(value_l):
            return f"CSS hides content: {selector} opacity={value}"
        if prop_l == "pointer-events" and _css_value_has_token(value_l, "none"):
            return f"CSS blocks interaction: {selector} pointer-events={value}"
    return None


def _iter_css_property_values(change: dict[str, Any]):
    properties = change.get("properties")
    if isinstance(properties, dict):
        yield from properties.items()

    property_changes = change.get("property_changes")
    if not isinstance(property_changes, list):
        return
    for item in property_changes:
        if not isinstance(item, dict):
            continue
        prop = item.get("property")
        for key in ("old_value", "new_value"):
            value = item.get(key)
            if prop is not None and value is not None:
                yield prop, value


def _css_value_has_token(value: str, token: str) -> bool:
    return any(part.strip() == token for part in value.replace("!important", "").split())


def _css_numeric_zero(value: str) -> bool:
    normalized = value.replace("!important", "").strip()
    try:
        return float(normalized) == 0
    except ValueError:
        return False


def _apply_severity_floor_to_response(
    ai_response: dict[str, Any], *, min_sev: str, reasons: list[str]
) -> None:
    """Mutate an AI success response so text matches deterministic severity floors."""
    ai_response["overall_severity"] = min_sev
    _normalize_business_impact_for_severity(ai_response)

    reason_text = "; ".join(reasons) if reasons else "structured diff severity floor"
    is_visual_floor = any("visual diff" in reason for reason in reasons)
    is_html_floor = any("HTML" in reason for reason in reasons)
    is_css_floor = any("CSS" in reason for reason in reasons)
    is_js_floor = any("JavaScript" in reason for reason in reasons)
    if is_visual_floor:
        visual_note = (
            "Comparator-confirmed visual difference requires human review; "
            "do not dismiss it as rendering noise without checking the visual_diff."
        )
        action = (
            "Review the baseline/current/visual_diff screenshots and confirm whether "
            "the visual difference is intentional."
        )
        review_item = "Verify the visual diff against the raw comparator output."
    elif is_html_floor:
        visual_note = (
            "Review-required HTML diff marker found; severity was raised by "
            "deterministic report policy."
        )
        action = (
            "Review the HTML diff for metadata, attributes, structure, and "
            "visible content impact before accepting the change."
        )
        review_item = (
            "Confirm whether the HTML change is intentional and whether it "
            "affects metadata, navigation, accessibility, or page semantics."
        )
    elif is_css_floor:
        visual_note = (
            "Review-required CSS diff marker found; severity was raised by "
            "deterministic report policy."
        )
        action = (
            "Review the CSS diff for styling, visibility, and interaction impact "
            "before accepting the change."
        )
        review_item = (
            "Confirm whether the CSS change is intentional and whether it hides "
            "content or blocks user interaction."
        )
    elif is_js_floor:
        visual_note = (
            "Review-required JavaScript diff marker found; severity was raised by "
            "deterministic report policy."
        )
        action = (
            "Review the JavaScript diff for runtime behavior, storage/cookie, "
            "network, and tracking impact before accepting the change."
        )
        review_item = (
            "Confirm whether the JavaScript change is intentional and whether it "
            "changes execution, storage, network, or analytics behavior."
        )
    else:
        visual_note = (
            "Security-sensitive or review-required structured diff marker found; "
            "severity was raised by deterministic report policy."
        )
        action = (
            "Review the structured diff for security-sensitive markers and verify "
            "whether this is authorized remediation or an unexpected regression."
        )
        review_item = (
            "Confirm the source and expected direction of the security-sensitive diff."
        )

    detailed = ai_response.setdefault("detailed_analysis", {})
    _prepend_unique(detailed.setdefault("visual_changes", []), visual_note)
    _prepend_unique(
        detailed.setdefault("technical_correlation", []),
        f"Deterministic severity floor applied: {reason_text}.",
    )
    _prepend_unique(
        detailed.setdefault("functional_impact", []),
        f"Final severity is at least {min_sev} because structured diff data "
        "contains review-required evidence independent of the model narrative.",
    )

    recs = ai_response.setdefault("recommendations", {})
    immediate = recs.setdefault("immediate_actions", [])
    recs["immediate_actions"] = [
        item for item in immediate if not _is_dismissive_immediate_action(item)
    ]
    _prepend_unique(recs["immediate_actions"], action)
    _prepend_unique(recs.setdefault("review_items", []), review_item)
    recs.setdefault(
        "acceptance_criteria",
        "Severity floor reviewed against raw comparator output.",
    )


def _apply_severity_cap_to_response(
    ai_response: dict[str, Any],
    *,
    max_sev: str,
    reasons: list[str],
    max_impact: str | None = None,
) -> None:
    """Mutate an AI success response when the model over-rates severity."""
    ai_response["overall_severity"] = max_sev
    if max_impact and _impact_rank(
        str(ai_response.get("business_impact", "NONE"))
    ) > _impact_rank(max_impact):
        ai_response["business_impact"] = max_impact
    _normalize_business_impact_for_severity(ai_response)

    reason_text = "; ".join(reasons) if reasons else "structured diff severity cap"
    detailed = ai_response.setdefault("detailed_analysis", {})
    _prepend_unique(
        detailed.setdefault("technical_correlation", []),
        f"Deterministic severity cap applied: {reason_text}.",
    )
    _prepend_unique(
        detailed.setdefault("functional_impact", []),
        f"Final severity is capped at {max_sev} because structured diff data "
        "does not contain deterministic critical evidence.",
    )

    recs = ai_response.setdefault("recommendations", {})
    _prepend_unique(
        recs.setdefault("review_items", []),
        "Verify the model narrative against the structured diff before treating "
        "this as critical.",
    )
    recs.setdefault(
        "acceptance_criteria",
        "Severity cap reviewed against raw comparator output.",
    )


def _prepend_unique(items: list[Any], value: str) -> None:
    """Prepend a string once, preserving existing model-generated detail."""
    if value not in items:
        items.insert(0, value)


def _normalize_business_impact_for_severity(ai_response: dict[str, Any]) -> None:
    """Keep report severity and business impact internally consistent."""
    if ai_response.get("overall_severity") == "CRITICAL" and ai_response.get(
        "business_impact"
    ) in ("NONE", "LOW", "MEDIUM"):
        ai_response["business_impact"] = "HIGH"
    elif ai_response.get("overall_severity") == "WARNING" and ai_response.get(
        "business_impact"
    ) in ("NONE", "LOW"):
        ai_response["business_impact"] = "MEDIUM"


def _is_dismissive_immediate_action(item: Any) -> bool:
    """Detect actions that conflict with a deterministic severity floor."""
    if not isinstance(item, str):
        return False
    text = item.lower()
    dismissive_markers = (
        "no immediate action",
        "mark this change as a false positive",
        "acceptable dynamic variance",
        "ignore",
    )
    return any(marker in text for marker in dismissive_markers)


__all__ = [
    "_apply_severity_cap_to_response",
    "_apply_severity_floor_to_response",
    "_maximum_business_impact_for_capped_severity",
    "_maximum_severity_from_structured_data",
    "_minimum_severity_from_structured_data",
    "_normalize_business_impact_for_severity",
    "_severity_cap_details",
    "_severity_floor_details",
    "_severity_rank",
]
