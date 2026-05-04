"""url_id edge-case tests (Phase A.4).

Pins the behavior of `test_ui/common/url_id.url_to_dirname` - the canonical
URL→directory function that A.3 collapsed from four duplicates. The function
is intentionally NOT a full URL canonicalizer; it preserves pre-A.3 behavior
exactly so the A.2 goldens stay valid. These tests pin that behavior so any
future change is loud.

Several cases are documented as **latent bugs** - surprising or
filesystem-hostile output that the pre-A.3 code already produced. Tests pin
the current value so we don't accidentally "fix" them and break goldens
without realizing. They're tracked as flags in REFACTOR_AND_DASHBOARD_PLAN.md
for a future cleanup pass.
"""

from __future__ import annotations

import pytest

from test_ui.common.url_id import sanitize_filename, url_to_dirname


# ---------------------------------------------------------------------------
# Documented happy-path behavior (matches the docstring examples)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        # The three examples from the docstring - these are the contract.
        ("https://gov.ie/", "gov.ie"),
        ("https://www.gov.ie/about/", "gov.ie_about"),
        ("https://www.gov.ie/en/news/2025/", "gov.ie_en_news_2025"),
    ],
    ids=["docstring_root", "docstring_one_level", "docstring_three_levels"],
)
def test_docstring_examples(url, expected):
    assert url_to_dirname(url) == expected


# ---------------------------------------------------------------------------
# Trailing-slash invariance and absent-trailing-slash handling
# ---------------------------------------------------------------------------


def test_trailing_slash_does_not_change_result():
    """`/about/` and `/about` map to the same dirname - strip("/") collapses both."""
    assert url_to_dirname("https://gov.ie/about/") == url_to_dirname(
        "https://gov.ie/about"
    )


def test_root_url_with_and_without_trailing_slash_match():
    assert url_to_dirname("https://gov.ie") == url_to_dirname("https://gov.ie/")


# ---------------------------------------------------------------------------
# www stripping
# ---------------------------------------------------------------------------


def test_strips_www_prefix():
    assert url_to_dirname("https://www.example.com/") == "example.com"


def test_does_not_strip_other_subdomains():
    """api.example.com and other non-www subdomains must be preserved."""
    assert url_to_dirname("https://api.example.com/x") == "api.example.com_x"
    assert url_to_dirname("https://docs.example.com/") == "docs.example.com"


def test_www_stripping_is_too_aggressive_latent_bug():
    """LATENT BUG: `www.` is stripped everywhere, not just leading.

    `str.replace("www.", "")` removes EVERY occurrence. So a (synthetic)
    domain like `www.foo.www.bar.com` collapses to `foo.bar.com`. Real-world
    impact is near-zero because `www.` rarely appears mid-domain, but the
    behavior is surprising. Pinned so a future cleanup is intentional.

    Tracked as: "url_id `www.` stripping is global, not anchored" flag.
    """
    assert url_to_dirname("https://www.foo.www.bar.com/") == "foo.bar.com"


# ---------------------------------------------------------------------------
# Query strings + fragments - both dropped (urlparse separates them out)
# ---------------------------------------------------------------------------


def test_query_string_is_dropped():
    """`?q=1` is not part of urlparse().path; dirname doesn't see it."""
    assert url_to_dirname("https://example.com/page?q=1") == "example.com_page"
    assert url_to_dirname("https://example.com/page?a=1&b=2") == "example.com_page"


def test_fragment_is_dropped():
    """`#anchor` is in urlparse().fragment; dirname doesn't see it."""
    assert url_to_dirname("https://example.com/page#section") == "example.com_page"


def test_query_and_fragment_collide_on_dirname():
    """Two URLs that differ only by query/fragment produce the SAME dirname.

    This is by design (we treat them as the same page) but worth pinning so
    nobody accidentally changes it. If you want per-query reports, the
    canonicalizer would need to grow a query-aware mode.
    """
    a = url_to_dirname("https://example.com/page")
    b = url_to_dirname("https://example.com/page?utm_source=email")
    c = url_to_dirname("https://example.com/page#top")
    assert a == b == c == "example.com_page"


# ---------------------------------------------------------------------------
# Ports - preserved verbatim (incl. the colon)
# ---------------------------------------------------------------------------


def test_port_is_preserved_with_colon():
    """Port appears in netloc → ends up in dirname as `host:port_path`.

    LATENT BUG: a colon in a directory name is invalid on Windows / NTFS and
    can confuse some shells. Real-world impact: zero today (we only run on
    Linux / Docker). Worth flagging for future Windows support.
    """
    assert url_to_dirname("https://example.com:8080/path") == "example.com:8080_path"


# ---------------------------------------------------------------------------
# Scheme is ignored
# ---------------------------------------------------------------------------


def test_http_and_https_produce_same_dirname():
    """Scheme is in urlparse().scheme; dirname doesn't see it."""
    assert url_to_dirname("http://example.com/") == url_to_dirname(
        "https://example.com/"
    )


# ---------------------------------------------------------------------------
# Path normalization edges
# ---------------------------------------------------------------------------


def test_consecutive_slashes_in_path_yield_double_underscore():
    """LATENT BUG: `/a//b/` → `example.com_a__b` (double underscore).

    `strip("/")` removes leading/trailing only; `replace("/", "_")` then
    converts every internal `/` 1:1, so `//` becomes `__`. Pinned because
    crawl4ai sometimes emits URLs with collapsed-slash paths and we don't
    want their dirnames to drift if this is ever "fixed".
    """
    assert url_to_dirname("https://example.com/a//b/") == "example.com_a__b"


def test_case_is_preserved():
    """LATENT BUG: case is not normalized.

    `https://EXAMPLE.com/` and `https://example.com/` produce DIFFERENT
    dirnames. On case-insensitive filesystems (macOS HFS+, NTFS) this would
    collide; on case-sensitive (Linux ext4) it's two separate URLs from the
    pipeline's perspective - which is wrong because they're the same site.

    Pinned for now; future canonicalizer should `.lower()` the netloc.
    """
    assert url_to_dirname("https://EXAMPLE.com/") == "EXAMPLE.com"
    assert (
        url_to_dirname("https://example.com/PATH/With/Caps/")
        == "example.com_PATH_With_Caps"
    )


# ---------------------------------------------------------------------------
# Unicode + percent-encoding
# ---------------------------------------------------------------------------


def test_unicode_path_is_preserved_verbatim():
    """Non-ASCII characters in the path are kept as-is.

    urlparse doesn't decode them; replace("/", "_") doesn't touch them. The
    crawler writes data into directories named with these characters and
    Python+Linux+ext4 handles UTF-8 filenames fine, so this is OK in practice.
    """
    assert url_to_dirname("https://example.com/café") == "example.com_café"
    assert url_to_dirname("https://example.com/日本語/") == "example.com_日本語"


def test_percent_encoded_path_is_preserved_verbatim():
    """Percent-encoded paths are NOT decoded - `caf%C3%A9` stays as-is.

    This means `caf%C3%A9` and `café` produce DIFFERENT dirnames even though
    they refer to the same URL. Pinning the current behavior; a future
    canonicalizer should unquote() before sanitizing.
    """
    assert url_to_dirname("https://example.com/caf%C3%A9") == "example.com_caf%C3%A9"


# ---------------------------------------------------------------------------
# Degenerate inputs - pin the (broken) outputs so they don't silently change
# ---------------------------------------------------------------------------


def test_empty_string_produces_empty_string():
    """Empty input → empty dirname. Caller should validate URL before passing."""
    assert url_to_dirname("") == ""


def test_path_only_produces_underscore_prefix_latent_bug():
    """LATENT BUG: a path-only string ('/foo') gets a leading underscore.

    `urlparse('/foo')` returns netloc='' and path='/foo', so domain=''
    and path='foo' → result is '_foo'. The caller is expected to pass a
    fully-qualified URL; we pin this so the failure mode is at least
    consistent and visible if someone passes a bare path by mistake.
    """
    assert url_to_dirname("/foo") == "_foo"


# ---------------------------------------------------------------------------
# Determinism + idempotence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/",
        "https://www.gov.ie/en/news/2025/",
        "https://api.example.com:8080/v1/users?limit=10#top",
    ],
)
def test_dirname_is_deterministic(url):
    """Same input → same output every call. No global state or randomness."""
    assert url_to_dirname(url) == url_to_dirname(url) == url_to_dirname(url)


# ---------------------------------------------------------------------------
# Backward-compat alias
# ---------------------------------------------------------------------------


def test_sanitize_filename_alias_is_identical_function():
    """Crawler imports the legacy `sanitize_filename` name; must be the same callable."""
    assert sanitize_filename is url_to_dirname


@pytest.mark.parametrize(
    "url",
    [
        "https://gov.ie/",
        "https://www.example.com/path/to/page",
        "https://api.example.com:8080/v1/users",
    ],
)
def test_sanitize_filename_returns_same_as_url_to_dirname(url):
    """Defensive: even if `is` aliasing breaks, the outputs must agree."""
    assert sanitize_filename(url) == url_to_dirname(url)
