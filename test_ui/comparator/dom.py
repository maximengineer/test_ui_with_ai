"""HTML/DOM diffing façade.

Phase 3 decomposition keeps this module as the stable import surface while
implementation lives in focused `dom_*` modules.
"""

from __future__ import annotations

from .dom_config import (
    HEADING_TAGS,
    HIGH_IMPACT_TAGS,
    KEY_ATTRIBUTES,
    LOW_IMPACT_TAGS,
    MEDIUM_IMPACT_TAGS,
    TAG_TYPES,
    WILDCARD_ATTRIBUTE_TAGS,
    assess_element_impact,
)
from .dom_core import compare_dom
from .dom_diff import compare_heading_texts, compare_key_attributes, compare_meta_info
from .dom_extract import (
    analyze_navigation_changes,
    clean_html_snippet,
    extract_dynamic_attributes,
    extract_element_code_snippets,
    extract_heading_texts,
    extract_key_attributes,
    extract_meta_info,
)
from .dom_projection import create_html_changes_json


__all__ = [
    "TAG_TYPES",
    "HIGH_IMPACT_TAGS",
    "MEDIUM_IMPACT_TAGS",
    "LOW_IMPACT_TAGS",
    "KEY_ATTRIBUTES",
    "WILDCARD_ATTRIBUTE_TAGS",
    "HEADING_TAGS",
    "assess_element_impact",
    "extract_meta_info",
    "compare_meta_info",
    "analyze_navigation_changes",
    "clean_html_snippet",
    "extract_element_code_snippets",
    "extract_key_attributes",
    "compare_key_attributes",
    "extract_dynamic_attributes",
    "extract_heading_texts",
    "compare_heading_texts",
    "compare_dom",
    "create_html_changes_json",
]
