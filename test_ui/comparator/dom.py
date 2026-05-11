"""HTML/DOM diffing for the comparator (Phase A.3 split).

Pure functions extracted from comparator/engine.py - no class state.
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

from test_ui.comparator.assets import normalize_volatile_urls


# Element categories used for impact assessment.
HIGH_IMPACT_TAGS = (
    "form",
    "button",
    "input",
    "nav",
    "header",
    "footer",
    # Added post-audit-01KR1BZE73...: <base> rewrites every relative URL on
    # the page (devastating attack); <iframe> can host arbitrary content
    # including malicious payloads. Both deserve HIGH on count change.
    "base",
    "iframe",
)
MEDIUM_IMPACT_TAGS = (
    "a",
    "img",
    "h1",
    "h2",
    "h3",
    "section",
    "article",
    # Added post-audit-01KR1BZE73...: structural add/remove of these
    # belongs in the medium bucket - <meta>/<style>/<title> are SEO/
    # security-sensitive but not as catastrophic as a <form> change;
    # media tags affect visible content but their count rarely matters
    # without other signals.
    "meta",
    "style",
    "title",
    "video",
    "audio",
    "svg",
    "canvas",
    "picture",
    "source",
)
LOW_IMPACT_TAGS = ("div", "span", "p")

# Security-/SEO-critical attributes tracked POSITIONALLY: per-tag, the Nth
# instance's listed attributes are emitted as `<tag>[N].<attr>` keys for
# structured diffing. Pre-fix the DOM differ only counted elements per
# tag - missing href hijacks (`<a href>` value swap, no count change),
# script src injection (count change yes, but value change went unflagged
# until a count check fired), language flips, etc. These are exactly the
# mutations a phishing/supply-chain attacker would make.
KEY_ATTRIBUTES: dict[str, tuple[str, ...]] = {
    "a": ("href",),
    "form": ("action", "method"),
    "script": ("src",),
    "link": ("href", "rel"),
    "img": ("src",),
    "iframe": ("src",),
    "html": ("lang",),
    # Added post-audit-01KR1BZE73...: <base href="..."> rewrites every
    # relative URL on the page. Catastrophic if hijacked. Was previously
    # only detectable via the structural element-count check (also
    # broken until base joined TAG_TYPES). Now caught at the value
    # level so we see href CHANGES on an existing base, not just
    # add/remove of the tag.
    "base": ("href",),
}

# Elements where we track ALL present attributes (not just specific
# ones from KEY_ATTRIBUTES). The motivation: `<html>` and `<body>`
# carry global app state via attributes - a `<body data-theme>` flip,
# a `<body class>` change for dark/light mode, a phishing-injected
# `<body data-experiment>`, etc. Their attribute namespace is too
# open-ended (any data-*) to enumerate, so wildcard tracking is the
# pragmatic choice. Both elements are singletons in valid HTML, so
# the noise risk is small (one element each per page).
WILDCARD_ATTRIBUTE_TAGS: tuple[str, ...] = ("html", "body")

# Heading tags walked positionally for text-content diffing. The whole-
# document text-length threshold (50/100 chars in compare_dom) misses
# small-but-visible mutations like prepending "[CRITICAL]" to a heading
# - that's only ~10 chars, well below the threshold. Per-heading text
# comparison catches it without lowering the threshold (which would
# create noise from natural-drift content on dynamic pages).
HEADING_TAGS = ("h1", "h2", "h3")


# Tags we count for structural diffs. Order matters for golden-test
# stability - new tags appended at the end so existing rule files
# (per-tag impact assessments, change-summary projections) keep their
# original positions.
#
# Post-audit-01KR1BZE73... additions: meta/style/base/iframe/title/video/
# audio/svg/canvas/picture/source. Without these, structural addition or
# removal of the corresponding elements (e.g., a phishing meta refresh
# injection, a <base href> hijack, a hidden <iframe>) was silently
# invisible to the framework.
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
    # New (post-audit-01KR1BZE73):
    "meta",
    "style",
    "base",
    "iframe",
    "title",
    "video",
    "audio",
    "svg",
    "canvas",
    "picture",
    "source",
)


def assess_element_impact(tag: str, count_diff: int) -> str:
    """Heuristic impact rating for an element-count change.

    Tag-class + magnitude - no asymmetry between added/removed (the
    previous `change_type` parameter was unused since this function
    was extracted; dropped per plan-implementation-flag cleanup).

    `count_diff` is always positive at the call sites (always
    `abs(current - baseline)`, only invoked when `current != baseline`),
    so the HIGH_IMPACT branch is unconditional `"high"` - the previous
    `else "medium"` arm was dead code (would only fire for count_diff=0,
    which never reaches this function).
    """
    if tag in HIGH_IMPACT_TAGS:
        return "high"
    if tag in MEDIUM_IMPACT_TAGS:
        return "medium" if count_diff > 2 else "low"
    if tag in LOW_IMPACT_TAGS:
        return "low" if count_diff < 10 else "medium"
    return "low"


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
    # pre-A.3 behavior - A.3 doesn't change behavior, only structure.
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


# Impact rating per (tag, attr) - drives the AI's severity rollup. The
# defaults reflect the "an attacker would set this to attack" lens:
# href/src/action mutations are HIGH (phishing/supply-chain), passive
# attributes (lang, rel) are MEDIUM.
_ATTR_IMPACT: dict[tuple[str, str], str] = {
    ("a", "href"): "high",
    ("form", "action"): "high",
    ("form", "method"): "medium",
    ("script", "src"): "high",
    ("link", "href"): "medium",
    ("link", "rel"): "medium",
    ("img", "src"): "medium",
    ("iframe", "src"): "high",
    ("base", "href"): "high",  # post-audit-01KR1BZE73: base hijack
    ("html", "lang"): "medium",
    ("html", "class"): "medium",
    ("html", "dir"): "low",
    ("body", "class"): "medium",
    # body data-* attributes are catch-all "low" via the default.
}


def _attr_impact(key: str) -> str:
    """Look up the impact rating for a `<tag>[N].<attr>` key.

    Three layers of resolution:
      1. Specific (tag, attr) entries in _ATTR_IMPACT (e.g. a.href = high).
      2. Pattern fallback for dynamic-attribute classes that can't be
         enumerated in advance:
           - `on*` event handlers → high (XSS-class attribute injection)
           - `style` attribute    → medium (visual / inline-CSS injection)
           - `aria-*` attributes  → medium (a11y regression)
      3. Default → low.
    """
    # Key format: "<tag>[<idx>].<attr>"; split on the bracket then on the dot.
    try:
        tag = key.split("[", 1)[0]
        attr = key.rsplit(".", 1)[1]
    except (IndexError, ValueError):
        return "low"
    if (tag, attr) in _ATTR_IMPACT:
        return _ATTR_IMPACT[(tag, attr)]
    # Pattern-based fallbacks for dynamic attribute classes.
    if attr.startswith("on") and len(attr) > 2:
        return "high"  # onclick, onerror, onload, ... → XSS-class
    if attr == "style":
        return "medium"  # inline-style mutation → visual change
    if attr.startswith("aria-"):
        return "medium"  # accessibility regression
    return "low"


# Tags can contain digits (h1, h2, h3) and HTML5 has hyphenated custom
# elements too. Pre-fix the pattern was `[a-z]+` which silently dropped
# h1/h2/h3 keys - latent bug that surfaced when the dynamic-attribute
# walker started emitting them (e.g., `h1[0].style`).
_KEY_PATTERN = re.compile(r"^([a-z][a-z0-9-]*)\[(\d+)\]\.(.+)$")


def _parse_attr_key(key: str) -> tuple[str, str, int] | None:
    """Pull (tag, attr, idx) out of a `<tag>[<idx>].<attr>` key. None for
    keys that don't match the pattern (defensive - shouldn't happen)."""
    m = _KEY_PATTERN.match(key)
    if not m:
        return None
    return (m.group(1), m.group(3), int(m.group(2)))


def compare_key_attributes(
    baseline: dict[str, str], current: dict[str, str]
) -> list[dict]:
    """Diff two flat key-attribute dicts; emit added/removed/changed records.

    Per (tag, attribute) pair, the baseline and current values form
    POSITION-INDEXED lists (e.g. `a[0].href`, `a[1].href`, ...). Pre-fix
    we compared by raw key, so inserting a `<script>` at position 1
    caused script[1].src, script[2].src, etc. to all appear "changed"
    (each value shifted right by one). N-1 spurious entries per
    insertion - exactly what showed up as `attrs=8` on site 8 of the
    audit when only 2 scripts were actually injected.

    Fix: align by VALUE using difflib's SequenceMatcher. The matcher
    finds the longest common subsequence of values, so inserts /
    deletes / replacements emit one record each, not N. Equal-value
    runs collapse to nothing. Keys for added items are reported at
    their NEW positional indices in current; removed at their OLD
    indices in baseline. Replacements (paired) report the baseline
    index since that's where the change happened.

    Wildcard-tracked attrs (`<body class>`, `<body data-*>`, etc.)
    use the same aligner because their key shape is identical
    (`body[0].class`, `body[0].data-foo`, ...).
    """
    import difflib
    from collections import defaultdict

    # Group both dicts by (tag, attr) → list of (idx, value) pairs.
    # Sort by index so the lists are in document order; difflib cares
    # about sequence position.
    def _grouped(d: dict[str, str]) -> dict[tuple[str, str], list[tuple[int, str]]]:
        out: dict[tuple[str, str], list[tuple[int, str]]] = defaultdict(list)
        for k, v in d.items():
            parsed = _parse_attr_key(k)
            if parsed is None:
                continue
            tag, attr, idx = parsed
            out[(tag, attr)].append((idx, v))
        for key in out:
            out[key].sort(key=lambda pair: pair[0])
        return out

    baseline_groups = _grouped(baseline)
    current_groups = _grouped(current)

    changes: list[dict] = []
    for tag_attr in sorted(set(baseline_groups) | set(current_groups)):
        tag, attr = tag_attr
        baseline_pairs = baseline_groups.get(tag_attr, [])
        current_pairs = current_groups.get(tag_attr, [])
        baseline_values = [v for _, v in baseline_pairs]
        current_values = [v for _, v in current_pairs]
        baseline_indices = [i for i, _ in baseline_pairs]
        current_indices = [i for i, _ in current_pairs]

        # Normalize URL-bearing values (script.src, a.href, form.action,
        # iframe.src, link.href, base.href, ...) before alignment so that
        # third-party URLs which only differ by a per-pageview rotating
        # query param (Matomo `trackerid`, CDN `/vNN/` path, `cb=...`
        # cache-buster) are seen as equal by the SequenceMatcher and
        # don't surface as `attribute_changed` records. The emit path
        # still carries the ORIGINAL value so the audit trail is intact.
        # 01KR1QKTTJQZJ1FJYECQ1M2W6Q audit fix.
        baseline_normalized = [normalize_volatile_urls(v) for v in baseline_values]
        current_normalized = [normalize_volatile_urls(v) for v in current_values]
        matcher = difflib.SequenceMatcher(
            a=baseline_normalized, b=current_normalized, autojunk=False
        )
        impact = _attr_impact(f"{tag}[0].{attr}")

        for op, b_lo, b_hi, c_lo, c_hi in matcher.get_opcodes():
            if op == "equal":
                continue
            if op == "insert":
                # Pure insertions in current: emit one `added` per item.
                for k in range(c_lo, c_hi):
                    changes.append(
                        {
                            "type": "attribute_added",
                            "key": f"{tag}[{current_indices[k]}].{attr}",
                            "new_value": current_values[k],
                            "impact": impact,
                        }
                    )
            elif op == "delete":
                for k in range(b_lo, b_hi):
                    changes.append(
                        {
                            "type": "attribute_removed",
                            "key": f"{tag}[{baseline_indices[k]}].{attr}",
                            "old_value": baseline_values[k],
                            "impact": impact,
                        }
                    )
            elif op == "replace":
                # Pair the overlap as `changed` records; the leftover
                # tail on either side is `added` or `removed`. Reporting
                # changed at the baseline index because "this index
                # changed value" is the most useful framing.
                paired = min(b_hi - b_lo, c_hi - c_lo)
                for k in range(paired):
                    changes.append(
                        {
                            "type": "attribute_changed",
                            "key": f"{tag}[{baseline_indices[b_lo + k]}].{attr}",
                            "old_value": baseline_values[b_lo + k],
                            "new_value": current_values[c_lo + k],
                            "impact": impact,
                        }
                    )
                if (b_hi - b_lo) > paired:
                    for k in range(paired, b_hi - b_lo):
                        changes.append(
                            {
                                "type": "attribute_removed",
                                "key": f"{tag}[{baseline_indices[b_lo + k]}].{attr}",
                                "old_value": baseline_values[b_lo + k],
                                "impact": impact,
                            }
                        )
                else:
                    for k in range(paired, c_hi - c_lo):
                        changes.append(
                            {
                                "type": "attribute_added",
                                "key": f"{tag}[{current_indices[c_lo + k]}].{attr}",
                                "new_value": current_values[c_lo + k],
                                "impact": impact,
                            }
                        )
    return changes


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


def compare_heading_texts(
    baseline: dict[str, str], current: dict[str, str]
) -> list[dict]:
    """Emit one record per heading whose text differs between baseline/current.

    Only flags changes on headings present in BOTH (additions/removals
    are already caught by structural element-count diffing in
    compare_dom). Impact is `medium` because heading mutation is
    visible-and-meaningful but not as critical as a link hijack.
    """
    changes: list[dict] = []
    for key in current:
        if key in baseline and baseline[key] != current[key]:
            changes.append(
                {
                    "type": "heading_text_changed",
                    "key": key,
                    "old_text": baseline[key],
                    "new_text": current[key],
                    "impact": "medium",
                }
            )
    return changes


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
