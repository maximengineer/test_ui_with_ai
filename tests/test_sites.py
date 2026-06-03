"""Site loader + Pydantic model tests (Phase B.3.1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from test_ui.config import settings
from test_ui.common.sites import Site, load_sites, slugify


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Homepage", "homepage"),
        ("Home Page", "home-page"),
        ("home page  with  extra spaces", "home-page-with-extra-spaces"),
        ("UPPER and lower", "upper-and-lower"),
        ("punct! marks?", "punct-marks"),
        ("---leading-and-trailing---", "leading-and-trailing"),
        ("a/b/c", "a-b-c"),  # slashes folded
        ("under_score", "under-score"),  # underscores → dashes
    ],
)
def test_slugify(value, expected):
    assert slugify(value) == expected


def test_slugify_never_returns_empty():
    """Pure-punctuation input falls back to 'site' so callers never get ''."""
    assert slugify("!!!") == "site"
    assert slugify("") == "site"


# ---------------------------------------------------------------------------
# Site model
# ---------------------------------------------------------------------------


def test_site_minimal_valid():
    s = Site(id="homepage", name="Homepage", url="https://example.com")
    assert s.id == "homepage"
    assert s.name == "Homepage"
    assert s.url == "https://example.com"


@pytest.mark.parametrize(
    "bad_id",
    [
        "",  # empty
        "Has-Caps",  # uppercase
        "starts-with-dash" if False else "-leading-dash",  # leading dash
        "has spaces",
        "has/slashes",
    ],
)
def test_site_id_pattern_rejects_bad(bad_id):
    """The id must be filesystem-safe ASCII slug."""
    with pytest.raises(ValidationError):
        Site(id=bad_id, name="x", url="https://x")


def test_site_rejects_unknown_field():
    """extra='forbid' - silent typos in sites.yml become loud Pydantic errors."""
    with pytest.raises(ValidationError):
        Site(id="x", name="x", url="https://x", color="red")


def test_site_rejects_private_network_url_by_default():
    """Default crawler URL posture blocks loopback/private SSRF targets."""
    with pytest.raises(ValidationError, match="not allowed"):
        Site(id="x", name="x", url="http://127.0.0.1/admin")


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@example.com/",
        "http://0177.0.0.1/admin",
        "http://0x7f.0.0.1/admin",
        "http://%31%32%37.0.0.1/admin",
    ],
)
def test_site_rejects_credentialed_or_obfuscated_private_urls(url):
    with pytest.raises(ValidationError):
        Site(id="x", name="x", url=url)


def test_site_allows_public_ip_literal():
    site = Site(id="x", name="x", url="https://8.8.8.8/")

    assert site.url == "https://8.8.8.8/"


def test_site_allows_private_network_url_with_explicit_override(monkeypatch):
    monkeypatch.setattr(settings, "allow_private_site_urls", True)

    site = Site(id="x", name="x", url="http://127.0.0.1/admin")

    assert site.url == "http://127.0.0.1/admin"


# ---------------------------------------------------------------------------
# load_sites - happy path
# ---------------------------------------------------------------------------


def test_load_sites_explicit_ids(tmp_path):
    sites_file = tmp_path / "sites.yml"
    sites_file.write_text(
        "sites:\n"
        "  - id: home\n"
        "    name: Home\n"
        "    url: https://example.com/\n"
        "  - id: about\n"
        "    name: About\n"
        "    url: https://example.com/about\n",
        encoding="utf-8",
    )
    sites = load_sites(sites_file)
    assert [s.id for s in sites] == ["home", "about"]
    assert [s.url for s in sites] == [
        "https://example.com/",
        "https://example.com/about",
    ]


def test_load_sites_handles_empty_file(tmp_path):
    """Empty / missing `sites:` key returns empty list - don't crash on a
    fresh installation with no sites configured yet."""
    sites_file = tmp_path / "sites.yml"
    sites_file.write_text("", encoding="utf-8")
    assert load_sites(sites_file) == []

    sites_file.write_text("sites:\n", encoding="utf-8")
    assert load_sites(sites_file) == []


def test_load_sites_rejects_non_list_sites(tmp_path):
    sites_file = tmp_path / "sites.yml"
    sites_file.write_text("sites:\n  not: a list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a list"):
        load_sites(sites_file)


# ---------------------------------------------------------------------------
# load_sites - auto-generates id from name (legacy compat)
# ---------------------------------------------------------------------------


def test_load_sites_auto_generates_id_from_name(tmp_path, caplog):
    sites_file = tmp_path / "sites.yml"
    sites_file.write_text(
        "sites:\n"
        "  - name: Home Page\n"
        "    url: https://example.com/\n"
        "  - name: About\n"
        "    url: https://example.com/about\n",
        encoding="utf-8",
    )
    sites = load_sites(sites_file)
    assert sites[0].id == "home-page"
    assert sites[1].id == "about"


def test_load_sites_dedupes_auto_generated_ids(tmp_path):
    """Two entries with identical names get suffixed: 'foo', 'foo-2'."""
    sites_file = tmp_path / "sites.yml"
    sites_file.write_text(
        "sites:\n"
        "  - name: Services\n"
        "    url: https://example.com/services\n"
        "  - name: Services\n"
        "    url: https://example.com/en/services\n"
        "  - name: Services\n"
        "    url: https://example.com/ie/services\n",
        encoding="utf-8",
    )
    sites = load_sites(sites_file)
    assert [s.id for s in sites] == ["services", "services-2", "services-3"]


def test_load_sites_normalizes_namd_typo_like_migration(tmp_path):
    """Pre-B.3 typo `namd:` is auto-fixed to `name:` (matches the migration
    script's behavior). The loader then derives the id from the normalized
    name, NOT from a URL fallback - that way the loader's auto-generated id
    matches what `scripts/migrate_sites_ids.py` would commit to disk.
    """
    sites_file = tmp_path / "sites.yml"
    sites_file.write_text(
        "sites:\n  - namd: Budget 2025\n    url: https://www.gov.ie/budget-2025/\n",
        encoding="utf-8",
    )
    sites = load_sites(sites_file)
    assert len(sites) == 1
    assert sites[0].name == "Budget 2025"
    assert sites[0].id == "budget-2025"


def test_load_sites_rejects_unknown_yaml_field(tmp_path):
    """A typo of `id:` → `idd:` (or any other unknown field) MUST raise.

    Pre-fix bug: `_coerce_to_site` only passed id/name/url to Pydantic, so
    extras silently dropped and the loader auto-generated an id from the
    name - leaving the operator confused why their `idd: my-id` was ignored.
    The fix routes the FULL dict through `Site.model_validate` so
    `extra='forbid'` fires.
    """
    sites_file = tmp_path / "sites.yml"
    sites_file.write_text(
        "sites:\n"
        "  - idd: typoed-id\n"  # typo for `id:`
        "    name: Home\n"
        "    url: https://example.com/\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="idd"):
        load_sites(sites_file)


def test_load_sites_rejects_unknown_yaml_field_alongside_explicit_id(tmp_path):
    """Same as above but with a valid `id:` already present - extras still rejected."""
    sites_file = tmp_path / "sites.yml"
    sites_file.write_text(
        "sites:\n"
        "  - id: home\n"
        "    name: Home\n"
        "    url: https://example.com/\n"
        "    color: red\n",  # not a Site field
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="color"):
        load_sites(sites_file)


# ---------------------------------------------------------------------------
# load_sites - errors
# ---------------------------------------------------------------------------


def test_load_sites_explicit_duplicate_id_raises(tmp_path):
    """Explicit ids that collide are an authoring error - fail loud."""
    sites_file = tmp_path / "sites.yml"
    sites_file.write_text(
        "sites:\n"
        "  - id: home\n"
        "    name: Home\n"
        "    url: https://example.com/\n"
        "  - id: home\n"
        "    name: Home v2\n"
        "    url: https://example.com/v2\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Duplicate id"):
        load_sites(sites_file)


def test_load_sites_missing_url_raises(tmp_path):
    sites_file = tmp_path / "sites.yml"
    sites_file.write_text(
        "sites:\n  - name: orphan\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing required `url`"):
        load_sites(sites_file)
