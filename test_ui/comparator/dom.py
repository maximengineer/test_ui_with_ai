"""HTML/DOM diffing for the comparator (Phase A.3 split).

Pure functions extracted from comparator/engine.py — no class state.
Behavior preserved verbatim so the A.2 comparator goldens keep passing.

Public API:
  compare_dom(baseline_html, current_html) -> dict
  create_html_changes_json(dom_result) -> dict
  TAG_TYPES (the canonical list of tags we track)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from loguru import logger


# Element categories used for impact assessment.
HIGH_IMPACT_TAGS = ("form", "button", "input", "nav", "header", "footer")
MEDIUM_IMPACT_TAGS = ("a", "img", "h1", "h2", "h3", "section", "article")
LOW_IMPACT_TAGS = ("div", "span", "p")

# Tags we count for structural diffs. Order matters for golden-test stability.
TAG_TYPES = (
    "img",
    "a",
    "script",
    "link",
    "form",
    "button",
    "input",
    "div",
    "span",
    "p",
    "h1",
    "h2",
    "h3",
    "nav",
    "header",
    "footer",
    "section",
    "article",
)


def assess_element_impact(tag: str, change_type: str, count_diff: int) -> str:
    """Heuristic impact rating for an element-count change.

    The change_type parameter is unused today (preserved for API
    compatibility) — the heuristic is purely tag-class + magnitude.
    """
    if tag in HIGH_IMPACT_TAGS:
        return "high" if count_diff > 0 else "medium"
    if tag in MEDIUM_IMPACT_TAGS:
        return "medium" if count_diff > 2 else "low"
    if tag in LOW_IMPACT_TAGS:
        return "low" if count_diff < 10 else "medium"
    return "low"


def extract_meta_info(soup) -> dict[str, str]:
    """Pull `<meta name=… content=…>` pairs into a flat dict.

    Falls back to `property=` for OpenGraph-style tags. `description` and
    `keywords` are explicitly re-fetched at the end so they overwrite any
    weird earlier matches.
    """
    meta_info: dict[str, str] = {}
    for meta in soup.find_all("meta"):
        name = meta.get("name") or meta.get("property")
        content = meta.get("content")
        if name and content:
            meta_info[name] = content
    description = soup.find("meta", attrs={"name": "description"})
    if description:
        meta_info["description"] = description.get("content", "")
    keywords = soup.find("meta", attrs={"name": "keywords"})
    if keywords:
        meta_info["keywords"] = keywords.get("content", "")
    return meta_info


def compare_meta_info(
    baseline_meta: dict[str, str], current_meta: dict[str, str]
) -> list:
    """Diff two flat meta dicts; emit added/removed/changed records."""
    changes: list[dict] = []
    for key in current_meta:
        if key not in baseline_meta:
            changes.append(
                {
                    "type": "meta_added",
                    "key": key,
                    "new_value": current_meta[key],
                    "impact": "low"
                    if key not in ("description", "title", "keywords")
                    else "medium",
                }
            )
    for key in baseline_meta:
        if key not in current_meta:
            changes.append(
                {
                    "type": "meta_removed",
                    "key": key,
                    "old_value": baseline_meta[key],
                    "impact": "low"
                    if key not in ("description", "title", "keywords")
                    else "medium",
                }
            )
    for key in baseline_meta:
        if key in current_meta and baseline_meta[key] != current_meta[key]:
            changes.append(
                {
                    "type": "meta_changed",
                    "key": key,
                    "old_value": baseline_meta[key],
                    "new_value": current_meta[key],
                    "impact": "high" if key in ("description", "title") else "medium",
                }
            )
    return changes


def analyze_navigation_changes(baseline_soup, current_soup) -> list:
    """Compare nav-link sets between two parsed documents."""
    baseline_navs = baseline_soup.find_all(["nav", "menu"]) + baseline_soup.find_all(
        class_=lambda x: x and "nav" in x.lower()
    )
    current_navs = current_soup.find_all(["nav", "menu"]) + current_soup.find_all(
        class_=lambda x: x and "nav" in x.lower()
    )

    baseline_nav_links: list[str] = []
    for nav in baseline_navs:
        baseline_nav_links.extend(
            link.get_text(strip=True) for link in nav.find_all("a")
        )
    current_nav_links: list[str] = []
    for nav in current_navs:
        current_nav_links.extend(
            link.get_text(strip=True) for link in nav.find_all("a")
        )

    changes: list[dict] = []
    if len(baseline_nav_links) != len(current_nav_links):
        changes.append(
            {
                "type": "navigation_count_change",
                "baseline_count": len(baseline_nav_links),
                "current_count": len(current_nav_links),
                "impact": "medium",
            }
        )
    baseline_set = set(baseline_nav_links)
    current_set = set(current_nav_links)
    # Set iteration order is non-deterministic. Preserved verbatim from
    # pre-A.3 behavior — A.3 doesn't change behavior, only structure.
    # A future task can sort here if golden stability becomes a concern.
    for item in current_set - baseline_set:
        changes.append(
            {"type": "navigation_item_added", "item": item, "impact": "medium"}
        )
    for item in baseline_set - current_set:
        changes.append(
            {"type": "navigation_item_removed", "item": item, "impact": "high"}
        )
    return changes


def clean_html_snippet(html_string: str) -> str:
    """Collapse whitespace + truncate at ~300 chars (try to break at a `>`).

    The cap is deliberately stricter than Phase A.1.4's per-snippet 2000-char
    cap so the structured diffs stay small even before the AI-side bound kicks
    in. Don't bump without understanding both layers.
    """
    cleaned = re.sub(r"\s+", " ", html_string.strip())
    if len(cleaned) > 300:
        if ">" in cleaned[200:300]:
            break_point = cleaned.find(">", 200) + 1
            cleaned = cleaned[:break_point] + "..."
        else:
            cleaned = cleaned[:300] + "..."
    return cleaned


def extract_element_code_snippets(
    baseline_elements,
    current_elements,
    tag: str,
    change_type: str,
) -> list:
    """Find the up-to-3 added or removed instances of `tag` and return code snippets."""
    code_examples: list[dict] = []
    try:
        baseline_strings = [str(e) for e in baseline_elements]
        current_strings = [str(e) for e in current_elements]

        if change_type == "added":
            new_elements = [s for s in current_strings if s not in baseline_strings]
            for i, elem_str in enumerate(new_elements[:3]):
                code_examples.append(
                    {
                        "change_type": "added",
                        "element": tag,
                        "code_snippet": clean_html_snippet(elem_str),
                        "description": f"New {tag} element added",
                        "position": f"example_{i + 1}",
                        "impact": assess_element_impact(tag, change_type, 1),
                    }
                )
        elif change_type == "removed":
            removed = [s for s in baseline_strings if s not in current_strings]
            for i, elem_str in enumerate(removed[:3]):
                code_examples.append(
                    {
                        "change_type": "removed",
                        "element": tag,
                        "code_snippet": clean_html_snippet(elem_str),
                        "description": f"{tag} element removed",
                        "position": f"example_{i + 1}",
                        "impact": assess_element_impact(tag, change_type, 1),
                    }
                )
        return code_examples
    except Exception as e:
        logger.error(f"Error extracting code snippets for {tag}: {e}")
        return [
            {
                "change_type": change_type,
                "element": tag,
                "code_snippet": f"Error extracting snippet: {e!s}",
                "description": f"{tag} element {change_type}",
                "impact": "low",
            }
        ]


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
                    "impact": assess_element_impact(tag, change_type, count_diff),
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

        title_changed = baseline_title_text != current_title_text
        content_changed = abs(content_length_change) > 50
        structure_changed = len(element_changes) > 0
        meta_changed = len(meta_changes) > 0
        nav_changed = len(nav_changes) > 0
        has_changes = (
            title_changed
            or content_changed
            or structure_changed
            or meta_changed
            or nav_changed
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
            "has_changes": has_changes,
        }
    except Exception as e:
        logger.error(f"Error comparing DOM: {e}")
        return {"error": f"DOM comparison failed: {e!s}"}


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


__all__ = [
    "TAG_TYPES",
    "HIGH_IMPACT_TAGS",
    "MEDIUM_IMPACT_TAGS",
    "LOW_IMPACT_TAGS",
    "assess_element_impact",
    "extract_meta_info",
    "compare_meta_info",
    "analyze_navigation_changes",
    "clean_html_snippet",
    "extract_element_code_snippets",
    "compare_dom",
    "create_html_changes_json",
]
