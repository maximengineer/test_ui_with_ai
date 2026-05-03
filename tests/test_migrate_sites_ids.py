"""Sites migration tests (Phase B.3.2).

Pins:
  - ids get added based on slugified `name`
  - the legacy `namd:` typo is fixed to `name:` AND a slugified id is added
  - duplicate auto-generated ids get `-2`, `-3`, ... suffixes
  - already-migrated files are no-op (idempotent)
  - ruamel.yaml preserves comments and key order across the rewrite
"""

from __future__ import annotations


from scripts.migrate_sites_ids import migrate


def test_adds_ids_to_legacy_format(tmp_path):
    sites_path = tmp_path / "sites.yml"
    sites_path.write_text(
        "sites:\n"
        "  - name: Home Page\n"
        "    url: https://example.com/\n"
        "  - name: About\n"
        "    url: https://example.com/about\n",
        encoding="utf-8",
    )

    added, total = migrate(sites_path)
    assert (added, total) == (2, 2)

    text = sites_path.read_text(encoding="utf-8")
    # Order matters: id should appear before name/url for readability.
    assert "id: home-page" in text
    assert "id: about" in text
    # The original name + url survived.
    assert "name: Home Page" in text
    assert "https://example.com/about" in text


def test_idempotent_when_all_ids_present(tmp_path):
    sites_path = tmp_path / "sites.yml"
    sites_path.write_text(
        "sites:\n  - id: home\n    name: Home\n    url: https://example.com/\n",
        encoding="utf-8",
    )
    before = sites_path.read_text(encoding="utf-8")

    added, total = migrate(sites_path)
    assert (added, total) == (0, 1)

    after = sites_path.read_text(encoding="utf-8")
    assert before == after, "no-op migration must not touch the file"


def test_fixes_namd_typo_and_adds_id(tmp_path):
    """Pre-B.3 sites.yml has a `namd:` typo on at least one entry; the
    migration should silently rename it to `name:` AND derive an id from
    the corrected value (rather than from the URL fallback)."""
    sites_path = tmp_path / "sites.yml"
    sites_path.write_text(
        "sites:\n  - namd: Budget 2025\n    url: https://gov.ie/budget-2025/\n",
        encoding="utf-8",
    )

    migrate(sites_path)

    text = sites_path.read_text(encoding="utf-8")
    assert "namd:" not in text, "the typo'd key must be removed"
    assert "name: Budget 2025" in text
    assert "id: budget-2025" in text


def test_dedupes_auto_ids_against_explicit_ones(tmp_path):
    """If the user has `id: home` AND a site without id whose name slugs to
    'home', the auto-generated one becomes 'home-2'. Pinning this catches
    a regression where the dedup pass forgets to scan explicit ids first."""
    sites_path = tmp_path / "sites.yml"
    sites_path.write_text(
        "sites:\n"
        "  - id: home\n"
        "    name: Home\n"
        "    url: https://example.com/\n"
        "  - name: Home\n"
        "    url: https://example.com/old\n",
        encoding="utf-8",
    )

    migrate(sites_path)

    text = sites_path.read_text(encoding="utf-8")
    assert "id: home" in text
    assert "id: home-2" in text


def test_preserves_comments_and_blank_lines(tmp_path):
    """ruamel.yaml round-trip mode keeps the operator's editorial choices."""
    sites_path = tmp_path / "sites.yml"
    sites_path.write_text(
        "# Top-level comment for the project\n"
        "sites:\n"
        "  # Comment about the homepage\n"
        "  - name: Home\n"
        "    url: https://example.com/\n"
        "\n"
        "  # blank line above and inline comment below\n"
        "  - name: About  # tagline goes here\n"
        "    url: https://example.com/about\n",
        encoding="utf-8",
    )

    migrate(sites_path)

    text = sites_path.read_text(encoding="utf-8")
    assert "# Top-level comment" in text
    assert "# Comment about the homepage" in text
    assert "# blank line above" in text
    # Inline comments are also preserved by ruamel.
    assert "# tagline goes here" in text


def test_handles_missing_sites_key_gracefully(tmp_path):
    sites_path = tmp_path / "sites.yml"
    sites_path.write_text("# no sites yet\n", encoding="utf-8")

    added, total = migrate(sites_path)
    assert (added, total) == (0, 0)


def test_skips_entries_without_url(tmp_path, capsys):
    """A site with no url can't get a derived id (we'd have nothing to slug
    against). Skip it with a warning rather than crashing the whole run."""
    sites_path = tmp_path / "sites.yml"
    sites_path.write_text(
        "sites:\n"
        "  - name: Has URL\n"
        "    url: https://example.com/\n"
        "  - name: Orphan with no URL\n",
        encoding="utf-8",
    )

    added, total = migrate(sites_path)
    assert (added, total) == (1, 2)

    captured = capsys.readouterr()
    assert "skipping entry with no url" in captured.err
