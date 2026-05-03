"""Asset (CSS/JS/media) diffing for the comparator (Phase A.3 split).

Pure functions extracted from comparator/engine.py — no class state.
Behavior preserved verbatim so the A.2 comparator goldens keep passing.

Public API:
  compare_assets(baseline_dir, current_dir, asset_type) -> dict
  create_css_changes_json(css_result) -> dict
  create_js_changes_json(js_result) -> dict

Implementation note: the per-rule CSS diff and per-function JS diff are
intentionally shallow (regex-based, not AST). Per-selector / per-function
detail is computed but not surfaced in the json files the AI consumes —
those only show file-level changes. See docs/data_shapes.md for the
"limitations" notes on this.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from loguru import logger


# Categories used by _assess_css_impact.
LAYOUT_PROPS = (
    "width",
    "height",
    "margin",
    "padding",
    "display",
    "position",
    "float",
    "flex",
    "grid",
)
HIGH_IMPACT_SELECTORS = ("body", "html", ".header", ".footer", ".nav", ".main")


# ---------------------------------------------------------------------------
# CSS / JS heuristic parsers
# ---------------------------------------------------------------------------


def parse_css_rules(css_content: str) -> dict[str, dict[str, str]]:
    """Naive selector → {prop: value} extraction. Misses nested rules and at-rules."""
    rules: dict[str, dict[str, str]] = {}
    pattern = r"([^{}]+)\s*{\s*([^{}]+)\s*}"
    for selector, properties in re.findall(pattern, css_content):
        selector = selector.strip()
        props: dict[str, str] = {}
        for prop in properties.split(";"):
            if ":" in prop:
                key, value = prop.split(":", 1)
                props[key.strip()] = value.strip()
        if props:
            rules[selector] = props
    return rules


def extract_js_functions(js_content: str) -> dict[str, str]:
    """Extract top-level function declarations + assigned anonymous functions.

    Doesn't handle arrow functions, async functions, or nested braces beyond
    one level. Intentional — pre-A.3 behavior preserved.
    """
    functions: dict[str, str] = {}
    patterns = (
        r"function\s+([a-zA-Z_$][a-zA-Z0-9_$]*)\s*\([^)]*\)\s*{([^}]*(?:{[^}]*}[^}]*)*)}",
        r"const\s+([a-zA-Z_$][a-zA-Z0-9_$]*)\s*=\s*function\s*\([^)]*\)\s*{([^}]*(?:{[^}]*}[^}]*)*)}",
        r"([a-zA-Z_$][a-zA-Z0-9_$]*)\s*:\s*function\s*\([^)]*\)\s*{([^}]*(?:{[^}]*}[^}]*)*)}",
    )
    for pattern in patterns:
        for match in re.findall(pattern, js_content, re.DOTALL):
            if len(match) >= 2:
                func_name, func_body = match[0], match[1]
                functions[func_name] = f"function {func_name}() {{{func_body}}}"
    return functions


def extract_js_variables(js_content: str) -> dict[str, str]:
    """Names of var/let/const declarations. Value is always the literal `'declared'`."""
    variables: dict[str, str] = {}
    for pattern in (
        r"var\s+([a-zA-Z_$][a-zA-Z0-9_$]*)",
        r"let\s+([a-zA-Z_$][a-zA-Z0-9_$]*)",
        r"const\s+([a-zA-Z_$][a-zA-Z0-9_$]*)",
    ):
        for var_name in re.findall(pattern, js_content):
            variables[var_name] = "declared"
    return variables


def assess_css_impact(selector: str, properties: dict[str, str]) -> str:
    """Heuristic: layout-affecting props → high, top-level selectors → medium."""
    if any(prop in properties for prop in LAYOUT_PROPS):
        return "high"
    if any(selector.startswith(sel) for sel in HIGH_IMPACT_SELECTORS):
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Content-level analyzers
# ---------------------------------------------------------------------------


def analyze_css_content_changes(
    baseline_file: Path, current_file: Path, filename: str
) -> dict[str, Any]:
    """Per-selector CSS diff. Currently informational — the json files the AI
    sees only carry file-level CSS changes (see create_css_changes_json)."""
    try:
        baseline_content = baseline_file.read_text(encoding="utf-8")
        current_content = current_file.read_text(encoding="utf-8")

        if baseline_content == current_content:
            return {"has_changes": False, "changes": [], "analysis": {}}

        baseline_rules = parse_css_rules(baseline_content)
        current_rules = parse_css_rules(current_content)
        changes: list[dict] = []

        for selector in current_rules:
            if selector not in baseline_rules:
                changes.append(
                    {
                        "type": "css_selector_added",
                        "file": filename,
                        "selector": selector,
                        "properties": current_rules[selector],
                        "impact": assess_css_impact(selector, current_rules[selector]),
                        "code_snippet": _format_rule(selector, current_rules[selector]),
                    }
                )
        for selector in baseline_rules:
            if selector not in current_rules:
                changes.append(
                    {
                        "type": "css_selector_removed",
                        "file": filename,
                        "selector": selector,
                        "properties": baseline_rules[selector],
                        "impact": assess_css_impact(selector, baseline_rules[selector]),
                        "code_snippet": _format_rule(
                            selector, baseline_rules[selector]
                        ),
                    }
                )
        for selector in baseline_rules:
            if selector in current_rules:
                baseline_props = baseline_rules[selector]
                current_props = current_rules[selector]
                if baseline_props != current_props:
                    prop_changes = []
                    for prop in set(baseline_props.keys()) | set(current_props.keys()):
                        old_val = baseline_props.get(prop)
                        new_val = current_props.get(prop)
                        if old_val != new_val:
                            prop_changes.append(
                                {
                                    "property": prop,
                                    "old_value": old_val,
                                    "new_value": new_val,
                                }
                            )
                    changes.append(
                        {
                            "type": "css_selector_modified",
                            "file": filename,
                            "selector": selector,
                            "property_changes": prop_changes,
                            "impact": assess_css_impact(selector, current_props),
                            "code_snippet": _format_rule(selector, current_props),
                        }
                    )

        return {
            "has_changes": True,
            "changes": changes,
            "analysis": {
                "total_selectors_baseline": len(baseline_rules),
                "total_selectors_current": len(current_rules),
                "added_selectors": len(
                    [c for c in changes if c["type"] == "css_selector_added"]
                ),
                "removed_selectors": len(
                    [c for c in changes if c["type"] == "css_selector_removed"]
                ),
                "modified_selectors": len(
                    [c for c in changes if c["type"] == "css_selector_modified"]
                ),
            },
        }
    except Exception as e:
        logger.error(f"Error analyzing CSS content for {filename}: {e}")
        return {"has_changes": False, "changes": [], "analysis": {"error": str(e)}}


def _format_rule(selector: str, props: dict[str, str]) -> str:
    inner = "; ".join(f"{k}: {v}" for k, v in props.items())
    return f"{selector} {{\n  {inner}\n}}"


def analyze_js_content_changes(
    baseline_file: Path, current_file: Path, filename: str
) -> dict[str, Any]:
    """Per-function JS diff. Same caveat as CSS — file-level only at AI-output."""
    try:
        baseline_content = baseline_file.read_text(encoding="utf-8")
        current_content = current_file.read_text(encoding="utf-8")

        if baseline_content == current_content:
            return {"has_changes": False, "changes": [], "analysis": {}}

        baseline_functions = extract_js_functions(baseline_content)
        current_functions = extract_js_functions(current_content)
        baseline_vars = extract_js_variables(baseline_content)
        current_vars = extract_js_variables(current_content)
        changes: list[dict] = []

        def _truncate(s: str) -> str:
            return s[:200] + "..." if len(s) > 200 else s

        for name in current_functions:
            if name not in baseline_functions:
                changes.append(
                    {
                        "type": "js_function_added",
                        "file": filename,
                        "function_name": name,
                        "code_snippet": _truncate(current_functions[name]),
                        "impact": "high",
                    }
                )
        for name in baseline_functions:
            if name not in current_functions:
                changes.append(
                    {
                        "type": "js_function_removed",
                        "file": filename,
                        "function_name": name,
                        "code_snippet": _truncate(baseline_functions[name]),
                        "impact": "high",
                    }
                )
        for name in baseline_functions:
            if (
                name in current_functions
                and baseline_functions[name] != current_functions[name]
            ):
                changes.append(
                    {
                        "type": "js_function_modified",
                        "file": filename,
                        "function_name": name,
                        "code_snippet": _truncate(current_functions[name]),
                        "impact": "high",
                    }
                )

        if len(baseline_vars) != len(current_vars):
            changes.append(
                {
                    "type": "js_variables_count_changed",
                    "baseline_count": len(baseline_vars),
                    "current_count": len(current_vars),
                    "impact": "medium",
                }
            )

        return {
            "has_changes": True,
            "changes": changes,
            "analysis": {
                "functions_baseline": len(baseline_functions),
                "functions_current": len(current_functions),
                "variables_baseline": len(baseline_vars),
                "variables_current": len(current_vars),
                "content_length_change": len(current_content) - len(baseline_content),
            },
        }
    except Exception as e:
        logger.error(f"Error analyzing JS content for {filename}: {e}")
        return {"has_changes": False, "changes": [], "analysis": {"error": str(e)}}


def analyze_new_file(
    file_path: Path, asset_type: str, change_type: str
) -> dict[str, Any]:
    """Lightweight metadata for an added/removed file (no per-content diff)."""
    try:
        if not file_path.exists():
            return {"error": "File not found", "change_type": change_type}
        content = file_path.read_text(encoding="utf-8")
        analysis: dict[str, Any] = {
            "change_type": change_type,
            "file_size": len(content),
            "line_count": len(content.splitlines()),
        }
        if asset_type == "css":
            rules = parse_css_rules(content)
            analysis.update(
                {
                    "selector_count": len(rules),
                    "sample_selectors": list(rules.keys())[:5],
                    "code_snippet": content[:300] + "..."
                    if len(content) > 300
                    else content,
                }
            )
        elif asset_type == "js":
            functions = extract_js_functions(content)
            analysis.update(
                {
                    "function_count": len(functions),
                    "sample_functions": list(functions.keys())[:5],
                    "code_snippet": content[:300] + "..."
                    if len(content) > 300
                    else content,
                }
            )
        return analysis
    except Exception as e:
        return {"error": str(e), "change_type": change_type}


# ---------------------------------------------------------------------------
# Top-level diff + json projections
# ---------------------------------------------------------------------------


def compare_assets(
    baseline_dir: Path, current_dir: Path, asset_type: str
) -> dict[str, Any]:
    """Diff one asset subdirectory (css / js / media) between baseline and current."""
    baseline_asset_dir = baseline_dir / asset_type
    current_asset_dir = current_dir / asset_type

    if not baseline_asset_dir.exists() and not current_asset_dir.exists():
        return {
            "added": [],
            "removed": [],
            "changed": [],
            "has_changes": False,
            "total_changes": 0,
            "content_changes": [],
            "detailed_analysis": {},
        }

    baseline_files: set[str] = set()
    current_files: set[str] = set()
    if baseline_asset_dir.exists():
        baseline_files = {p.name for p in baseline_asset_dir.glob("*") if p.is_file()}
    if current_asset_dir.exists():
        current_files = {p.name for p in current_asset_dir.glob("*") if p.is_file()}

    added = sorted(current_files - baseline_files)
    removed = sorted(baseline_files - current_files)
    common = baseline_files & current_files

    changed: list[str] = []
    content_changes: list[dict] = []
    detailed_analysis: dict[str, Any] = {}

    for filename in common:
        baseline_file = baseline_asset_dir / filename
        current_file = current_asset_dir / filename
        if not (baseline_file.exists() and current_file.exists()):
            continue
        if asset_type == "css":
            content_analysis = analyze_css_content_changes(
                baseline_file, current_file, filename
            )
        elif asset_type == "js":
            content_analysis = analyze_js_content_changes(
                baseline_file, current_file, filename
            )
        else:
            baseline_hash = hashlib.md5(baseline_file.read_bytes()).hexdigest()
            current_hash = hashlib.md5(current_file.read_bytes()).hexdigest()
            content_analysis = {
                "has_changes": baseline_hash != current_hash,
                "changes": [],
                "analysis": {"file_changed": baseline_hash != current_hash},
            }
        if content_analysis["has_changes"]:
            changed.append(filename)
            content_changes.extend(content_analysis["changes"])
            detailed_analysis[filename] = content_analysis["analysis"]

    for filename in added:
        detailed_analysis[filename] = analyze_new_file(
            current_asset_dir / filename, asset_type, "added"
        )
    for filename in removed:
        detailed_analysis[filename] = analyze_new_file(
            baseline_asset_dir / filename, asset_type, "removed"
        )

    has_changes = bool(added or removed or changed)
    return {
        "added": added,
        "removed": removed,
        "changed": sorted(changed),
        "has_changes": has_changes,
        "total_changes": len(added) + len(removed) + len(changed),
        "content_changes": content_changes,
        "detailed_analysis": detailed_analysis,
    }


def create_css_changes_json(css_result: dict[str, Any]) -> dict[str, Any]:
    """Project compare_assets(css) output into the json shape the AI consumes."""
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

    severity = "none"
    if changes:
        if any(c.get("severity") == "high" for c in changes):
            severity = "high"
        elif any(c.get("severity") == "medium" for c in changes):
            severity = "medium"
        else:
            severity = "low"

    return {
        "changes_detected": len(changes) > 0,
        "change_types": change_types,
        "files_changed": css_result.get("added", []) + css_result.get("changed", []),
        "changes": changes,
        "summary": {
            "total_changes": len(changes),
            "layout_affecting": len(changes),
            "visual_only": 0,
            "severity": severity,
        },
    }


def create_js_changes_json(js_result: dict[str, Any]) -> dict[str, Any]:
    """Project compare_assets(js) output into the json shape the AI consumes."""
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

    severity = "none"
    if changes:
        if any(c.get("functionality_impact") == "high" for c in changes):
            severity = "high"
        elif any(c.get("functionality_impact") == "medium" for c in changes):
            severity = "medium"
        else:
            severity = "low"

    return {
        "changes_detected": len(changes) > 0,
        "change_types": change_types,
        "files_changed": js_result.get("added", []) + js_result.get("changed", []),
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


__all__ = [
    "compare_assets",
    "create_css_changes_json",
    "create_js_changes_json",
    "analyze_css_content_changes",
    "analyze_js_content_changes",
    "analyze_new_file",
    "parse_css_rules",
    "extract_js_functions",
    "extract_js_variables",
    "assess_css_impact",
]
