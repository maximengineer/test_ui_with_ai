"""CSS/JS/media diffing façade.

Phase 3 decomposition keeps this module as the stable import surface while
implementation lives in focused `assets_*` modules.
"""

from __future__ import annotations

from .assets_analysis import (
    analyze_css_content_changes,
    analyze_js_content_changes,
    analyze_new_file,
    compare_assets,
)
from .assets_parsers import (
    _scan_js_security_indicators,
    assess_css_impact,
    extract_js_functions,
    extract_js_variables,
    parse_css_rules,
    parse_css_rules_indexed,
)
from .assets_projection import create_css_changes_json, create_js_changes_json
from .assets_url import normalize_volatile_urls


__all__ = [
    "compare_assets",
    "create_css_changes_json",
    "create_js_changes_json",
    "analyze_css_content_changes",
    "analyze_js_content_changes",
    "analyze_new_file",
    "parse_css_rules",
    "parse_css_rules_indexed",
    "extract_js_functions",
    "extract_js_variables",
    "assess_css_impact",
    "normalize_volatile_urls",
    "_scan_js_security_indicators",
]
