"""Canonical URL → directory name mapping (Phase A.3).

Single source of truth for converting a URL into a safe filesystem directory
name. Pre-A.3 this logic was duplicated across the crawler engine and the
comparator engine; the duplicates have been collapsed into this one
function so they can't drift.

Phase B.3 made this the LEGACY-FALLBACK path: per-site directory naming
now derives from `site["id"]` via `common/sites.site_dir_name`, which
falls back to `url_to_dirname(site["url"])` only for callers that pass
dicts without an id (mostly tests).

The current implementation matches the pre-A.3 behavior exactly so the
A.2 goldens continue to pass without regeneration.
"""

from __future__ import annotations

from urllib.parse import urlparse


def url_to_dirname(url: str) -> str:
    """Convert a URL to a filesystem-safe directory name.

    Examples:
        https://gov.ie/                  → 'gov.ie'
        https://www.gov.ie/about/        → 'gov.ie_about'
        https://www.gov.ie/en/news/2025/ → 'gov.ie_en_news_2025'

    Strips `www.` prefix from the host. Replaces path slashes with underscores.
    Drops the trailing slash. Does not handle ports, query strings, or
    fragments — historical behavior we preserve to keep golden snapshots stable.
    """
    parsed = urlparse(url)
    domain = parsed.netloc.replace("www.", "")
    path = parsed.path.strip("/").replace("/", "_")
    if path:
        return f"{domain}_{path}"
    return domain


# Historical alias for the crawler's old `sanitize_filename` name. Kept so
# external imports (if any exist) don't break, and so the crawler module's
# legacy name continues to work without a flag-day rename.
sanitize_filename = url_to_dirname


__all__ = ["url_to_dirname", "sanitize_filename"]
