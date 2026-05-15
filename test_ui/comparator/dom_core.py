"""Top-level DOM compare orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from loguru import logger

from .dom_config import TAG_TYPES, assess_element_impact
from .dom_diff import compare_heading_texts, compare_key_attributes, compare_meta_info
from .dom_extract import (
    analyze_navigation_changes,
    extract_dynamic_attributes,
    extract_element_code_snippets,
    extract_heading_texts,
    extract_key_attributes,
    extract_meta_info,
)

def compare_dom(baseline_html: Path, current_html: Path) -> dict[str, Any]:
    """Top-level DOM diff between two HTML files. See module docstring for shape."""
    if not baseline_html.exists() or not current_html.exists():
        return {"error": "HTML files missing"}
    try:
        with open(baseline_html, encoding="utf-8") as f:
            baseline_soup = BeautifulSoup(f.read(), "lxml")
        with open(current_html, encoding="utf-8") as f:
            current_soup = BeautifulSoup(f.read(), "lxml")

        baseline_title = baseline_soup.find("title")
        current_title = current_soup.find("title")
        baseline_title_text = (
            baseline_title.get_text(strip=True) if baseline_title else ""
        )
        current_title_text = current_title.get_text(strip=True) if current_title else ""

        baseline_counts = {tag: len(baseline_soup.find_all(tag)) for tag in TAG_TYPES}
        current_counts = {tag: len(current_soup.find_all(tag)) for tag in TAG_TYPES}

        element_changes: list[dict] = []
        specific_element_changes: list[dict] = []
        for tag in TAG_TYPES:
            baseline_count = baseline_counts[tag]
            current_count = current_counts[tag]
            if baseline_count == current_count:
                continue
            change_type = "added" if current_count > baseline_count else "removed"
            count_diff = abs(current_count - baseline_count)
            code_examples = extract_element_code_snippets(
                baseline_soup.find_all(tag),
                current_soup.find_all(tag),
                tag,
                change_type,
            )
            element_changes.append(
                {
                    "element": tag,
                    "change_type": change_type,
                    "count_change": count_diff,
                    "baseline_count": baseline_count,
                    "current_count": current_count,
                    "impact": assess_element_impact(tag, count_diff),
                    "code_examples": code_examples,
                }
            )
            specific_element_changes.extend(code_examples)

        baseline_text = baseline_soup.get_text(strip=True)
        current_text = current_soup.get_text(strip=True)
        content_length_change = len(current_text) - len(baseline_text)

        baseline_meta = extract_meta_info(baseline_soup)
        current_meta = extract_meta_info(current_soup)
        meta_changes = compare_meta_info(baseline_meta, current_meta)
        nav_changes = analyze_navigation_changes(baseline_soup, current_soup)

        # Security/SEO-critical attributes (href, src, action, lang, ...) +
        # per-heading text. Pre-fix the differ only checked element COUNTS
        # and total text length, so href hijacks (`<a href>` value swap,
        # no count change) and small heading-text mutations (e.g.
        # prepending "[CRITICAL]") were silently missed.
        baseline_attrs = extract_key_attributes(baseline_soup)
        current_attrs = extract_key_attributes(current_soup)
        attribute_changes = compare_key_attributes(baseline_attrs, current_attrs)

        # Dynamic-attribute walker for on* / style / aria-* (post-audit-
        # 01KR1BZE73 fix). Reuses compare_key_attributes for alignment
        # since the key shape is identical.
        baseline_dyn = extract_dynamic_attributes(baseline_soup)
        current_dyn = extract_dynamic_attributes(current_soup)
        dynamic_attribute_changes = compare_key_attributes(
            baseline_dyn, current_dyn
        )

        baseline_headings = extract_heading_texts(baseline_soup)
        current_headings = extract_heading_texts(current_soup)
        heading_changes = compare_heading_texts(baseline_headings, current_headings)

        title_changed = baseline_title_text != current_title_text
        content_changed = abs(content_length_change) > 50
        structure_changed = len(element_changes) > 0
        meta_changed = len(meta_changes) > 0
        nav_changed = len(nav_changes) > 0
        attributes_changed = len(attribute_changes) > 0
        dynamic_attributes_changed = len(dynamic_attribute_changes) > 0
        headings_changed = len(heading_changes) > 0
        has_changes = (
            title_changed
            or content_changed
            or structure_changed
            or meta_changed
            or nav_changed
            or attributes_changed
            or dynamic_attributes_changed
            or headings_changed
        )

        return {
            "title": {
                "changed": title_changed,
                "baseline": baseline_title_text,
                "current": current_title_text,
            },
            "structure": {
                "element_changes": element_changes,
                "specific_changes": specific_element_changes,
                "tag_counts": {"baseline": baseline_counts, "current": current_counts},
            },
            "content": {
                "baseline_length": len(baseline_text),
                "current_length": len(current_text),
                "length_change": content_length_change,
                "significant_change": abs(content_length_change) > 100,
            },
            "meta": {"changes": meta_changes},
            "navigation": {"changes": nav_changes},
            "key_attributes": {"changes": attribute_changes},
            # post-audit-01KR1BZE73: separate bucket for the dynamic
            # attribute classes (on*/style/aria-*) so consumers can
            # distinguish them from the curated KEY_ATTRIBUTES list -
            # impact rules and recommendations differ per class.
            "dynamic_attributes": {"changes": dynamic_attribute_changes},
            "headings": {"changes": heading_changes},
            "has_changes": has_changes,
        }
    except Exception as e:
        logger.error(f"Error comparing DOM: {e}")
        return {"error": f"DOM comparison failed: {e!s}"}

__all__ = ["compare_dom"]
