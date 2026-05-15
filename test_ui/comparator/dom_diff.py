"""DOM diff helpers (meta, attributes, heading text)."""

from __future__ import annotations

import re

from test_ui.comparator.assets_url import normalize_volatile_urls

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

__all__ = [
    "compare_meta_info",
    "compare_key_attributes",
    "compare_heading_texts",
]
