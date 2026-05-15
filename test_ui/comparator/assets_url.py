"""URL normalization helpers for volatile third-party asset URLs."""

from __future__ import annotations

import re

# URL fragments that bump frequently on third-party CDNs (Google Fonts
# being the canonical offender) without representing a real change to
# the served content. Normalize these to a stable placeholder before
# content-comparing so a bot-driven version bump (e.g.
# `materialsymbolsoutlined/v332/...` → `.../v333/...` between two
# adjacent crawls) doesn't poison every site's `css.has_changes` flag.
#
# Order matters: more specific patterns first so the broader ones don't
# absorb their captures. Idempotent - applying twice is a no-op because
# the placeholders themselves don't match the patterns.
#
# Audited against report 01KQX43MYA5VSZ7HMP3AN5HJHF where this caused
# the entire site corpus (including the untouched control) to flag.
# Extended in report 01KR1QKTTJQZJ1FJYECQ1M2W6Q audit with `trackerid`
# (Matomo HeatmapSessionRecording rotates a per-pageview random token
# in the script src query string) which produced the same control-site
# false positive on citizensinformation.ie.
_VOLATILE_URL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Google-style version path segment: /v\d+/  e.g. /v332/, /v333/
    (re.compile(r"/v\d+/"), "/vN/"),
    # Version query params: ?v=v123, &v=v123, ?v=4
    (re.compile(r"([?&])v=v?\d+"), r"\1v=vN"),
    # Generic "ver" param: ?ver=4, &ver=12
    (re.compile(r"([?&])ver=\d+"), r"\1ver=N"),
    # Common cache-busters: ?_=1234567890, ?t=..., ?cb=...
    (re.compile(r"([?&])_=\d+"), r"\1_=N"),
    (re.compile(r"([?&])t=\d+"), r"\1t=N"),
    (re.compile(r"([?&])cb=\d+"), r"\1cb=N"),
    # Per-pageview rotating tokens. Matomo HeatmapSessionRecording
    # generates a fresh `trackerid=<6-char-alnum>` on every page load;
    # the URL otherwise matches byte-for-byte across captures. Pattern
    # is tightly scoped to a parameter literally named `trackerid` so
    # it can't accidentally normalize a real URL difference.
    (re.compile(r"([?&])trackerid=[A-Za-z0-9_-]+"), r"\1trackerid=N"),
)


def normalize_volatile_urls(content: str) -> str:
    """Replace CDN URL-version artifacts with stable placeholders.

    Some assets (notably Google Fonts CSS, which bumps a `/v\\d+/` path
    segment and an `&v=v\\d+` query param every few weeks) are byte-
    different across crawls but functionally identical. Without this
    normalization the file-content equality check trips on every run,
    forwarding noise to the AI and burning quota - and it broke the
    "control site MUST stay no_changes" invariant in the framework
    audit. Apply this BEFORE any equality check or per-rule diff so
    the CDN noise never enters the change pipeline.

    Public so dom.compare_key_attributes can also normalize URL-bearing
    attribute values (script.src / a.href / form.action / iframe.src).
    Without that the same `trackerid` rotation would flag every site's
    `script[N].src` as a `high`-impact change - exactly the false-
    positive seen on the control site in 01KR1QKTTJQZJ1FJYECQ1M2W6Q.
    """
    for pattern, replacement in _VOLATILE_URL_PATTERNS:
        content = pattern.sub(replacement, content)
    return content


__all__ = ["normalize_volatile_urls"]
