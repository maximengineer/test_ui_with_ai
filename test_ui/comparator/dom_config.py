"""DOM comparator constants and impact heuristics."""

from __future__ import annotations

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
    # Added post-audit-01KRB5GSSM3J76H9Y2MPTZWPS4: <noscript> can contain
    # arbitrary HTML that renders when JS is disabled; injection is a
    # real vector for cloaked content or tracking pixels.
    "noscript",
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
    # New (post-audit-01KRB5GSSM3J76H9Y2MPTZWPS4):
    "noscript",
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

__all__ = [
    "HIGH_IMPACT_TAGS",
    "MEDIUM_IMPACT_TAGS",
    "LOW_IMPACT_TAGS",
    "KEY_ATTRIBUTES",
    "WILDCARD_ATTRIBUTE_TAGS",
    "HEADING_TAGS",
    "TAG_TYPES",
    "assess_element_impact",
]
