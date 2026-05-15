"""Asset content analyzers and top-level directory diff orchestration."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from loguru import logger

from .assets_parsers import (
    _format_rule,
    _scan_js_security_indicators,
    assess_css_impact,
    extract_js_functions,
    extract_js_variables,
    parse_css_rules,
    parse_css_rules_indexed,
)
from .assets_url import normalize_volatile_urls

def analyze_css_content_changes(
    baseline_file: Path, current_file: Path, filename: str
) -> dict[str, Any]:
    """Per-selector CSS diff. Currently informational - the json files the AI
    sees only carry file-level CSS changes (see create_css_changes_json)."""
    try:
        baseline_raw = baseline_file.read_text(encoding="utf-8")
        current_raw = current_file.read_text(encoding="utf-8")

        # Normalize CDN URL noise (Google Fonts `/v333/` → `/vN/`, etc.)
        # BEFORE any equality check or rule parsing - otherwise the
        # /vNN/ path bump shows up as a `src` property change on every
        # @font-face and contaminates the framework's signal-to-noise.
        baseline_content = normalize_volatile_urls(baseline_raw)
        current_content = normalize_volatile_urls(current_raw)

        if baseline_content == current_content:
            return {"has_changes": False, "changes": [], "analysis": {}}

        baseline_rules = parse_css_rules_indexed(baseline_content)
        current_rules = parse_css_rules_indexed(current_content)
        changes: list[dict] = []

        for selector in current_rules:
            if selector not in baseline_rules:
                # New selector (all occurrences are new)
                for occ_idx, props in enumerate(current_rules[selector]):
                    changes.append(
                        {
                            "type": "css_selector_added",
                            "file": filename,
                            "selector": selector,
                            "occurrence": occ_idx + 1,
                            "properties": props,
                            "impact": assess_css_impact(selector, props),
                            "code_snippet": _format_rule(selector, props),
                        }
                    )
        for selector in baseline_rules:
            if selector not in current_rules:
                for occ_idx, props in enumerate(baseline_rules[selector]):
                    changes.append(
                        {
                            "type": "css_selector_removed",
                            "file": filename,
                            "selector": selector,
                            "occurrence": occ_idx + 1,
                            "properties": props,
                            "impact": assess_css_impact(selector, props),
                            "code_snippet": _format_rule(selector, props),
                        }
                    )
        for selector in baseline_rules:
            if selector in current_rules:
                b_list = baseline_rules[selector]
                c_list = current_rules[selector]
                if len(b_list) != len(c_list):
                    # Occurrence count changed (e.g. a rule was duplicated or removed)
                    changes.append(
                        {
                            "type": "css_selector_count_changed",
                            "file": filename,
                            "selector": selector,
                            "baseline_count": len(b_list),
                            "current_count": len(c_list),
                            "impact": assess_css_impact(
                                selector, c_list[-1] if c_list else {}
                            ),
                        }
                    )
                paired = min(len(b_list), len(c_list))
                for occ_idx in range(paired):
                    b_props = b_list[occ_idx]
                    c_props = c_list[occ_idx]
                    if b_props != c_props:
                        prop_changes = []
                        for prop in sorted(set(b_props) | set(c_props)):
                            old_val = b_props.get(prop)
                            new_val = c_props.get(prop)
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
                                "occurrence": occ_idx + 1,
                                "property_changes": prop_changes,
                                "impact": assess_css_impact(selector, c_props),
                                "code_snippet": _format_rule(selector, c_props),
                            }
                        )

        # Fallback: the naive regex can't parse nested at-rules (@media,
        # @keyframes) and may miss changes that live inside them. If the
        # raw content still differs after normalization but the rule-level
        # diff found nothing, emit a generic record so the change isn't
        # silently invisible. (Site 14 audit fix.)
        if not changes and baseline_content != current_content:
            changes.append(
                {
                    "type": "css_content_changed",
                    "file": filename,
                    "description": (
                        "CSS file content changed (rule-level details unavailable "
                        "due to at-rules or parsing limitations)"
                    ),
                    "impact": "medium",
                }
            )

        return {
            "has_changes": True,
            "changes": changes,
            "analysis": {
                "total_selectors_baseline": sum(len(v) for v in baseline_rules.values()),
                "total_selectors_current": sum(len(v) for v in current_rules.values()),
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


def analyze_js_content_changes(
    baseline_file: Path, current_file: Path, filename: str
) -> dict[str, Any]:
    """Per-function JS diff. Same caveat as CSS - file-level only at AI-output."""
    try:
        baseline_raw = baseline_file.read_text(encoding="utf-8")
        current_raw = current_file.read_text(encoding="utf-8")

        # Same CDN-noise normalization as CSS - JS files occasionally
        # reference versioned CDN URLs in string literals (analytics
        # scripts, font loaders) that bump independently of the JS
        # logic itself. Cheap to apply universally; harmless if the
        # JS contains no URLs.
        baseline_content = normalize_volatile_urls(baseline_raw)
        current_content = normalize_volatile_urls(current_raw)

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

        # Raw-content security scan: catches appended attack vectors
        # that the regex parser misses (eval, fetch, document.write, ...).
        baseline_indicators = _scan_js_security_indicators(baseline_raw)
        current_indicators = _scan_js_security_indicators(current_raw)
        indicators_removed = [s for s in baseline_indicators if s not in current_indicators]
        indicators_added = [s for s in current_indicators if s not in baseline_indicators]

        return {
            "has_changes": True,
            "changes": changes,
            "analysis": {
                "functions_baseline": len(baseline_functions),
                "functions_current": len(current_functions),
                "variables_baseline": len(baseline_vars),
                "variables_current": len(current_vars),
                "content_length_change": len(current_content) - len(baseline_content),
                "security_indicators_baseline": baseline_indicators,
                "security_indicators_current": current_indicators,
                "security_indicators_added": indicators_added,
                "security_indicators_removed": indicators_removed,
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
        if asset_type in {"css", "js"}:
            content = file_path.read_text(encoding="utf-8")
            analysis: dict[str, Any] = {
                "change_type": change_type,
                "file_size": len(content),
                "line_count": len(content.splitlines()),
            }
        else:
            # Media assets are often binary; reading as text raises decode errors.
            raw = file_path.read_bytes()
            analysis = {
                "change_type": change_type,
                "file_size": len(raw),
                "line_count": len(raw.splitlines()),
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

    for filename in sorted(common):
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

__all__ = [
    "analyze_css_content_changes",
    "analyze_js_content_changes",
    "analyze_new_file",
    "compare_assets",
]
