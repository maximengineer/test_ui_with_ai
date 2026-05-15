"""Projection helpers for CSS/JS diff outputs consumed by AI/reporting."""

from __future__ import annotations

import json
import re
from typing import Any

def _is_security_relevant_css(change: dict[str, Any]) -> bool:
    """True if a CSS content change carries a security-relevant signal."""
    text = json.dumps(change, default=str).lower()
    return any(
        kw in text
        for kw in (
            "attacker.example",
            "display: none",
            "opacity: 0",
            "pointer-events: none",
            "!important",
            "@import",
            "@font-face",
            "content:",
            "phish",
            "xss",
        )
    )


def create_css_changes_json(css_result: dict[str, Any]) -> dict[str, Any]:
    """Project compare_assets(css) output into the json shape the AI consumes.

    Post-audit-01KRC46BJQFBSQ4Z6Y2R1EYEVZ fix: the per-rule content changes
    (css_selector_added/removed/modified) were computed but discarded before
    reaching the AI. Without them the AI can't distinguish a benign color tweak
    from an injected phishing pseudo-element or @import supply-chain attack.

    We now surface high-impact and security-relevant per-rule diffs alongside
    the file-level records. Capped at 10 entries so the prompt doesn't bloat.
    """
    changes: list[dict] = []
    change_types: list[str] = []
    if css_result.get("has_changes", False):
        for f in css_result.get("added", []):
            changes.append(
                {
                    "file": f,
                    "change_type": "added",
                    "description": f"New CSS file added: {f}",
                    "impact": "layout",
                    "severity": "medium",
                }
            )
        for f in css_result.get("removed", []):
            changes.append(
                {
                    "file": f,
                    "change_type": "removed",
                    "description": f"CSS file removed: {f}",
                    "impact": "layout",
                    "severity": "high",
                }
            )
        for f in css_result.get("changed", []):
            changes.append(
                {
                    "file": f,
                    "change_type": "modified",
                    "description": f"CSS file modified: {f}",
                    "impact": "layout",
                    "severity": "medium",
                }
            )
        if changes:
            change_types = ["layout", "styling"]

        # Surface per-rule content changes that are high-impact or security-
        # relevant. Without this the AI is blind to attacker-controlled URLs
        # in @import, pseudo-elements, etc.
        content_changes = css_result.get("content_changes", [])
        surfaced: list[dict] = []
        for cc in content_changes:
            if cc.get("impact") == "high" or _is_security_relevant_css(cc):
                rule_record: dict[str, Any] = {
                    "file": cc.get("file", "unknown"),
                    "change_type": cc.get("type", "unknown").replace("css_", ""),
                    "description": cc.get(
                        "description",
                        f"{cc.get('type')} on {cc.get('selector', 'unknown')}",
                    ),
                    "selector": cc.get("selector", "unknown"),
                    "impact": cc.get("impact", "medium"),
                }
                if "property_changes" in cc:
                    rule_record["property_changes"] = cc["property_changes"]
                if "code_snippet" in cc:
                    rule_record["code_snippet"] = cc["code_snippet"]
                if "properties" in cc:
                    rule_record["properties"] = cc["properties"]
                surfaced.append(rule_record)

        # Cap to avoid prompt bloat; sort by impact (high first).
        _IMPACT_ORDER = {"high": 0, "medium": 1, "low": 2}
        surfaced.sort(key=lambda c: _IMPACT_ORDER.get(c.get("impact", "low"), 3))
        for sr in surfaced[:10]:
            changes.append(sr)
            if "styling" not in change_types:
                change_types.append("styling")

    severity = "none"
    if changes:
        if any(c.get("severity") == "high" or c.get("impact") == "high" for c in changes):
            severity = "high"
        elif any(c.get("severity") == "medium" or c.get("impact") == "medium" for c in changes):
            severity = "medium"
        else:
            severity = "low"

    return {
        "changes_detected": len(changes) > 0,
        "change_types": change_types,
        "files_changed": (
            css_result.get("added", [])
            + css_result.get("changed", [])
            + css_result.get("removed", [])
        ),
        "changes": changes,
        "summary": {
            "total_changes": len(changes),
            "layout_affecting": len(changes),
            "visual_only": 0,
            "severity": severity,
        },
    }


def _is_security_relevant_js(change: dict[str, Any]) -> bool:
    """True if a JS content change carries a security-relevant signal."""
    text = json.dumps(change, default=str).lower()
    return any(
        kw in text
        for kw in (
            "attacker.example",
            "eval(",
            "document.write",
            "innerhtml",
            "insertadjacenthtml",
            "fetch(",
            "sendbeacon",
            "import(",
            "settimeout",
            "cookie",
            "localstorage",
            "xss",
        )
    )


def _truncate_code_snippet(s: str, max_len: int = 300) -> str:
    """Collapse whitespace + cap length for prompt-size control."""
    collapsed = re.sub(r"\s+", " ", s.strip())
    return collapsed if len(collapsed) <= max_len else collapsed[:max_len] + "..."


def create_js_changes_json(js_result: dict[str, Any]) -> dict[str, Any]:
    """Project compare_assets(js) output into the json shape the AI consumes.

    Post-audit-01KRC46BJQFBSQ4Z6Y2R1EYEVZ fix: per-function diffs were computed
    but discarded, so the AI couldn't see eval(), fetch(), document.write,
    innerHTML, or dynamic import() injections. We now surface high-impact and
    security-relevant function-level changes. Capped at 10 entries.

    Minified-code noise is filtered by requiring either:
      - impact == "high", or
      - security-relevant keywords in the snippet, or
      - the function name matches known-security patterns.
    """
    changes: list[dict] = []
    change_types: list[str] = []
    if js_result.get("has_changes", False):
        for f in js_result.get("added", []):
            changes.append(
                {
                    "file": f,
                    "change_type": "added",
                    "description": f"New JavaScript file added: {f}",
                    "functionality_impact": "medium",
                }
            )
        for f in js_result.get("removed", []):
            changes.append(
                {
                    "file": f,
                    "change_type": "removed",
                    "description": f"JavaScript file removed: {f}",
                    "functionality_impact": "high",
                }
            )
        for f in js_result.get("changed", []):
            changes.append(
                {
                    "file": f,
                    "change_type": "modified",
                    "description": f"JavaScript file modified: {f}",
                    "functionality_impact": "medium",
                }
            )
        if changes:
            change_types = ["functionality"]

        # Surface per-function content changes that are high-impact or
        # security-relevant. Filter out minified-code boundary-drift noise.
        content_changes = js_result.get("content_changes", [])
        surfaced: list[dict] = []
        seen_names: set[str] = set()
        for cc in content_changes:
            name = cc.get("function_name", "")
            # Skip duplicate function names (boundary-drift artifacts on
            # minified code often emit the same name multiple times).
            if name and name in seen_names:
                continue
            if name:
                seen_names.add(name)

            if cc.get("impact") == "high" or _is_security_relevant_js(cc):
                fn_record: dict[str, Any] = {
                    "file": cc.get("file", "unknown"),
                    "change_type": cc.get("type", "unknown").replace("js_", ""),
                    "description": cc.get(
                        "description",
                        f"{cc.get('type')} {cc.get('function_name', 'unknown')}",
                    ),
                    "function_name": cc.get("function_name", "unknown"),
                    "impact": cc.get("impact", "medium"),
                }
                if "code_snippet" in cc:
                    fn_record["code_snippet"] = _truncate_code_snippet(
                        cc["code_snippet"]
                    )
                surfaced.append(fn_record)

        # Cap + sort by impact.
        _IMPACT_ORDER = {"high": 0, "medium": 1, "low": 2}
        surfaced.sort(key=lambda c: _IMPACT_ORDER.get(c.get("impact", "low"), 3))
        for sr in surfaced[:10]:
            changes.append(sr)
            if "functionality" not in change_types:
                change_types.append("functionality")

        # Surface raw-content security indicators that the regex parser
        # missed (appended eval, fetch, document.write, etc.).
        # These live in detailed_analysis[file].security_indicators_*.
        for fname, fmeta in js_result.get("detailed_analysis", {}).items():
            if not isinstance(fmeta, dict):
                continue
            added = fmeta.get("security_indicators_added", [])
            removed = fmeta.get("security_indicators_removed", [])
            for snippet in added[:3]:
                changes.append(
                    {
                        "file": fname,
                        "change_type": "security_indicator_added",
                        "description": f"Security-relevant code added to {fname}",
                        "code_snippet": _truncate_code_snippet(snippet),
                        "impact": "high",
                    }
                )
            for snippet in removed[:3]:
                changes.append(
                    {
                        "file": fname,
                        "change_type": "security_indicator_removed",
                        "description": f"Security-relevant code removed from {fname}",
                        "code_snippet": _truncate_code_snippet(snippet),
                        "impact": "high",
                    }
                )
            if added or removed:
                if "functionality" not in change_types:
                    change_types.append("functionality")

    severity = "none"
    if changes:
        if any(
            c.get("functionality_impact") == "high" or c.get("impact") == "high"
            for c in changes
        ):
            severity = "high"
        elif any(
            c.get("functionality_impact") == "medium" or c.get("impact") == "medium"
            for c in changes
        ):
            severity = "medium"
        else:
            severity = "low"

    return {
        "changes_detected": len(changes) > 0,
        "change_types": change_types,
        "files_changed": (
            js_result.get("added", [])
            + js_result.get("changed", [])
            + js_result.get("removed", [])
        ),
        "changes": changes,
        "summary": {
            "total_changes": len(changes),
            "functionality_impact": "high"
            if severity == "high"
            else "medium"
            if severity == "medium"
            else "none",
            "severity": severity,
        },
    }

__all__ = ["create_css_changes_json", "create_js_changes_json"]
