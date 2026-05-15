"""DOM extractor helpers (metadata, navigation, attributes, headings)."""

from __future__ import annotations

import re

from loguru import logger

from .dom_config import HEADING_TAGS, KEY_ATTRIBUTES, WILDCARD_ATTRIBUTE_TAGS, assess_element_impact

def extract_meta_info(soup) -> dict[str, str]:
    """Pull `<meta>` pairs into a flat dict.

    Sources, in priority order:
      - `<meta name="...">` (SEO + arbitrary metadata)
      - `<meta property="...">` (OpenGraph / Twitter cards)
      - `<meta http-equiv="...">` (CSP, X-Frame-Options, refresh, etc.)

    `http-equiv` keys get prefixed `http-equiv:` so they can't collide
    with a `name=` of the same string. Pre-fix the differ ignored
    http-equiv entirely - so a `<meta http-equiv="Content-Security-Policy"
    content="...">` change (CSP weakening) was silently invisible
    (audit 01KR1BZE73...). Now CSP/refresh/X-Frame-Options/etc. value
    changes show up in the meta-changes diff.
    """
    meta_info: dict[str, str] = {}
    for meta in soup.find_all("meta"):
        content = meta.get("content")
        if not content:
            continue
        # Try name → property first (back-compat with prior behavior),
        # then fall through to http-equiv (new). Each source emits a
        # distinct dict key so the differ can attribute changes
        # correctly even when the same string appears in both name=
        # and http-equiv= namespaces.
        name = meta.get("name") or meta.get("property")
        if name:
            meta_info[name] = content
        http_equiv = meta.get("http-equiv")
        if http_equiv:
            meta_info[f"http-equiv:{http_equiv}"] = content
    description = soup.find("meta", attrs={"name": "description"})
    if description:
        meta_info["description"] = description.get("content", "")
    keywords = soup.find("meta", attrs={"name": "keywords"})
    if keywords:
        meta_info["keywords"] = keywords.get("content", "")
    return meta_info


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
    # Deterministic ordering keeps html_changes output stable across runs.
    for item in sorted(current_set - baseline_set):
        changes.append(
            {"type": "navigation_item_added", "item": item, "impact": "medium"}
        )
    for item in sorted(baseline_set - current_set):
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
                        "impact": assess_element_impact(tag, 1),
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
                        "impact": assess_element_impact(tag, 1),
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


def extract_key_attributes(soup) -> dict[str, str]:
    """Map `<tag>[index].<attr>` → value for security-critical attributes.

    Walks each tag in `KEY_ATTRIBUTES` in document order, emitting one
    entry per (instance, tracked-attribute) pair. Only present attributes
    are emitted - absent attributes simply don't appear in either dict,
    so the differ won't synthesize false "added/removed" records when
    e.g. a `<form>` legitimately has no `method` attribute on either side.

    Positional indexing (`a[0]`, `a[1]`, ...) means the differ is order-
    sensitive: if a list of `<a>` elements gets reordered the values get
    "reassigned" to different positions and shows up as N changes. For
    static pages that's a feature (it surfaces the reorder); for dynamic
    pages it's noise. Acceptable trade-off for the regression use case
    where pages are expected to render in stable order.
    """
    out: dict[str, str] = {}
    for tag, attrs in KEY_ATTRIBUTES.items():
        for idx, elem in enumerate(soup.find_all(tag)):
            for attr in attrs:
                value = elem.get(attr)
                if value is not None:
                    out[f"{tag}[{idx}].{attr}"] = str(value)
    # Wildcard tracking on `<html>` and `<body>`: emit one entry per
    # present attribute regardless of name. Catches `<body data-theme>`,
    # `<body class="...">`, injected `data-*` markers, etc. that the
    # specific KEY_ATTRIBUTES list can't enumerate.
    for tag in WILDCARD_ATTRIBUTE_TAGS:
        for idx, elem in enumerate(soup.find_all(tag)):
            for attr_name, value in elem.attrs.items():
                # BeautifulSoup parses multi-value attributes (`class`,
                # `rel`) as lists; flatten back to a stable string so
                # the diff comparison is on canonical text.
                if isinstance(value, list):
                    value = " ".join(value)
                key = f"{tag}[{idx}].{attr_name}"
                # Don't double-emit if KEY_ATTRIBUTES already covered
                # this exact (tag, attr) pair (e.g., `html.lang`).
                if key not in out:
                    out[key] = str(value)
    return out

def extract_dynamic_attributes(soup) -> dict[str, str]:
    """Walk every element, emit `<tag>[idx].<attr>` for `on*`, `style`,
    and `aria-*` attributes.

    These attribute classes can't be listed in advance:
      - `on*` event handlers: onclick, onerror, onload, onmouseover, ...
        any of which is an XSS vector if attacker-controlled.
      - `style` (inline): bypasses the CSS file diff entirely; an
        attacker who can write `style="..."` controls the rendering
        without ever touching css/.
      - `aria-*`: open-ended namespace (aria-label, aria-labelledby,
        aria-describedby, aria-live, ...). a11y regressions.

    Pre-fix the framework was blind to all three classes. Site 9
    (xss_inline_attrs) of the audit run flagged NOTHING because both
    onclick and style attributes slipped through. Site 12's aria-strip
    likewise.

    Per-element-type positional indexing means the resulting key set
    pairs naturally with the existing compare_key_attributes aligner
    (a[5].onclick, h1[0].style, button[2].aria-label, etc.).
    """
    out: dict[str, str] = {}
    counts: dict[str, int] = {}
    for elem in soup.find_all(True):
        tag = elem.name
        if tag is None:
            continue
        # Per-tag positional index. Independent of whether THIS element
        # has a tracked attribute - we want consistent indexing across
        # baseline and current so the same element shows up at the
        # same `tag[idx]` position when it has a tracked attribute on
        # one side and not the other.
        idx = counts.get(tag, 0)
        counts[tag] = idx + 1
        for attr_name, value in elem.attrs.items():
            tracked = (
                (attr_name.startswith("on") and len(attr_name) > 2)
                or attr_name == "style"
                or attr_name.startswith("aria-")
            )
            if not tracked:
                continue
            # BeautifulSoup parses multi-value attrs as lists; flatten
            # to a stable string for diffing.
            if isinstance(value, list):
                value = " ".join(value)
            out[f"{tag}[{idx}].{attr_name}"] = str(value)
    return out


def extract_heading_texts(soup) -> dict[str, str]:
    """Map `<htag>[index]` → stripped text content for h1/h2/h3.

    Empty headings are skipped (often present as decorative spacers in
    SPA frameworks; their absence on either side would otherwise spam
    the diff). Position-indexed for the same reason as attributes -
    fragile to reordering, fine for stable pages.
    """
    out: dict[str, str] = {}
    for tag in HEADING_TAGS:
        for idx, elem in enumerate(soup.find_all(tag)):
            text = elem.get_text(strip=True)
            if text:
                out[f"{tag}[{idx}]"] = text
    return out

__all__ = [
    "extract_meta_info",
    "analyze_navigation_changes",
    "clean_html_snippet",
    "extract_element_code_snippets",
    "extract_key_attributes",
    "extract_dynamic_attributes",
    "extract_heading_texts",
]
