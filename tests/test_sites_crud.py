"""Sites CRUD helpers (Phase C.2 — dashboard slice).

Pin the round-trip behavior: comments preserved, atomic writes, dedup
on slugified ids, validation via Pydantic before the file is touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from test_ui.common.sites import (
    SiteNotFound,
    add_site,
    delete_site,
    load_sites,
    update_site,
)


def _seed(tmp_path: Path, content: str) -> Path:
    """Create a sites.yml at tmp_path with the given content."""
    p = tmp_path / "sites.yml"
    p.write_text(content, encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# add_site                                                                   #
# --------------------------------------------------------------------------- #


def test_add_site_appends_with_slugified_id(tmp_path):
    p = _seed(
        tmp_path,
        "sites:\n  - id: existing\n    name: Existing\n    url: https://e.example\n",
    )
    new_site = add_site(p, name="My New Site", url="https://new.example")
    assert new_site.id == "my-new-site"
    sites = load_sites(p)
    assert len(sites) == 2
    assert sites[1].id == "my-new-site"
    assert sites[1].name == "My New Site"
    assert sites[1].url == "https://new.example"


def test_add_site_dedupes_id_against_existing(tmp_path):
    """Adding a site whose slugified name collides MUST get -2 suffix."""
    p = _seed(
        tmp_path,
        "sites:\n  - id: my-site\n    name: My Site\n    url: https://a.example\n",
    )
    new = add_site(p, name="My Site", url="https://b.example")
    assert new.id == "my-site-2"


def test_add_site_preserves_comments(tmp_path):
    """The whole point of using ruamel: operator comments must survive a
    round-trip through add_site."""
    p = _seed(
        tmp_path,
        "# Top-of-file comment.\nsites:\n  # An inline comment for the first site.\n  - id: a\n    name: A\n    url: https://a.example\n",
    )
    add_site(p, name="B", url="https://b.example")
    raw = p.read_text(encoding="utf-8")
    assert "# Top-of-file comment." in raw
    assert "# An inline comment for the first site." in raw


def test_add_site_handles_empty_file(tmp_path):
    """A file with no `sites:` key (or `sites:` empty) must still accept
    adds — the loader synthesizes the list."""
    p = _seed(tmp_path, "")
    new = add_site(p, name="First", url="https://first.example")
    assert new.id == "first"
    sites = load_sites(p)
    assert len(sites) == 1


def test_add_site_rejects_empty_url(tmp_path):
    """Pydantic catches this BEFORE the file is touched — a `min_length=1`
    on Site.url is the line of defense."""
    p = _seed(tmp_path, "sites: []\n")
    with pytest.raises(ValidationError):
        add_site(p, name="X", url="")
    # File MUST be unchanged.
    assert p.read_text(encoding="utf-8") == "sites: []\n"


def test_add_site_rolls_back_when_existing_corruption_blocks_full_load(tmp_path):
    """Round-2 review HIGH #4 fix: a successful add MUST be rolled back
    if it produces a file that fails the strict loader (e.g. because a
    pre-existing entry has an invalid id pattern that round-tripping
    through ruamel exposes). Otherwise the next dashboard read fails."""
    # Pre-existing entry with an id the strict loader rejects (uppercase).
    p = _seed(
        tmp_path, "sites:\n  - id: BadCaseID\n    name: X\n    url: https://x.example\n"
    )
    original = p.read_text(encoding="utf-8")

    with pytest.raises(ValidationError):
        add_site(p, name="New Site", url="https://new.example")

    # File MUST be rolled back to the pre-write content.
    assert p.read_text(encoding="utf-8") == original


def test_add_site_max_length_caps(tmp_path):
    """Round-2 review MEDIUM #10 fix: name capped at 200, url at 2048.
    Stops a 10MB-name DoS from bloating sites.yml."""
    p = _seed(tmp_path, "sites: []\n")
    # 201-char name → reject.
    with pytest.raises(ValidationError):
        add_site(p, name="x" * 201, url="https://x.example")
    # 2049-char url → reject.
    with pytest.raises(ValidationError):
        add_site(p, name="X", url="https://" + "x" * 2042)
    # Boundary values pass.
    site = add_site(p, name="x" * 200, url="https://" + "x" * 2032)
    assert len(site.name) == 200


def test_add_site_atomic_no_partial_write_on_failure(tmp_path, monkeypatch):
    """If the rename step fails (simulated), the original file must remain
    intact and no `.tmp` file should leak."""
    p = _seed(
        tmp_path,
        "sites:\n  - id: a\n    name: A\n    url: https://a.example\n",
    )
    original = p.read_text(encoding="utf-8")

    # Force the atomic-write helper to fail at the rename step.
    real_replace = Path.replace

    def _flaky_replace(self, target):
        if str(self).endswith(".yml.tmp"):
            raise OSError("simulated rename failure")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", _flaky_replace)
    with pytest.raises(OSError, match="simulated"):
        add_site(p, name="B", url="https://b.example")
    assert p.read_text(encoding="utf-8") == original
    assert not (tmp_path / "sites.yml.tmp").exists(), "tmp must be cleaned up"


# --------------------------------------------------------------------------- #
# update_site                                                                #
# --------------------------------------------------------------------------- #


def test_update_site_renames(tmp_path):
    p = _seed(
        tmp_path,
        "sites:\n  - id: a\n    name: Old Name\n    url: https://a.example\n",
    )
    updated = update_site(p, "a", name="New Name")
    assert updated.name == "New Name"
    assert updated.url == "https://a.example"
    assert load_sites(p)[0].name == "New Name"


def test_update_site_changes_url(tmp_path):
    p = _seed(
        tmp_path,
        "sites:\n  - id: a\n    name: A\n    url: https://old.example\n",
    )
    updated = update_site(p, "a", url="https://new.example")
    assert updated.url == "https://new.example"


def test_update_site_both_fields(tmp_path):
    p = _seed(
        tmp_path,
        "sites:\n  - id: a\n    name: A\n    url: https://a.example\n",
    )
    updated = update_site(p, "a", name="A2", url="https://a2.example")
    assert (updated.name, updated.url) == ("A2", "https://a2.example")


def test_update_site_no_op_when_both_none_does_not_rewrite(tmp_path):
    """Both fields None → return current row, no file rewrite. Round-2
    review HIGH #5 dropped the misleading "fast path" — the regular
    branch now correctly skips the write when nothing changed."""
    p = _seed(
        tmp_path,
        "sites:\n  - id: a\n    name: A\n    url: https://a.example\n",
    )
    mtime_before = p.stat().st_mtime_ns
    updated = update_site(p, "a")
    mtime_after = p.stat().st_mtime_ns
    assert updated.name == "A"
    assert mtime_before == mtime_after, "file MUST NOT be rewritten on no-op update"


def test_update_site_no_op_when_values_unchanged(tmp_path):
    """Passing the SAME name+url that already exists must also skip
    the rewrite — pin the value-comparison short-circuit."""
    p = _seed(
        tmp_path,
        "sites:\n  - id: a\n    name: A\n    url: https://a.example\n",
    )
    mtime_before = p.stat().st_mtime_ns
    updated = update_site(p, "a", name="A", url="https://a.example")
    mtime_after = p.stat().st_mtime_ns
    assert updated.name == "A"
    assert mtime_before == mtime_after


def test_update_site_404_when_id_unknown(tmp_path):
    p = _seed(tmp_path, "sites:\n  - id: a\n    name: A\n    url: https://a.example\n")
    with pytest.raises(SiteNotFound):
        update_site(p, "does-not-exist", name="X")


def test_update_site_validates_new_url(tmp_path):
    """Empty string url MUST raise before the file is touched."""
    p = _seed(tmp_path, "sites:\n  - id: a\n    name: A\n    url: https://a.example\n")
    original = p.read_text(encoding="utf-8")
    with pytest.raises(ValidationError):
        update_site(p, "a", url="")
    assert p.read_text(encoding="utf-8") == original


# --------------------------------------------------------------------------- #
# delete_site                                                                #
# --------------------------------------------------------------------------- #


def test_delete_site_removes_entry(tmp_path):
    p = _seed(
        tmp_path,
        "sites:\n"
        "  - id: a\n    name: A\n    url: https://a.example\n"
        "  - id: b\n    name: B\n    url: https://b.example\n",
    )
    delete_site(p, "a")
    sites = load_sites(p)
    assert len(sites) == 1
    assert sites[0].id == "b"


def test_delete_site_404_when_id_unknown(tmp_path):
    p = _seed(tmp_path, "sites:\n  - id: a\n    name: A\n    url: https://a.example\n")
    with pytest.raises(SiteNotFound):
        delete_site(p, "missing")


def test_delete_site_preserves_comments(tmp_path):
    """Like add — operator comments must survive."""
    p = _seed(
        tmp_path,
        "# important note\nsites:\n  - id: a\n    name: A\n    url: https://a.example\n  - id: b\n    name: B\n    url: https://b.example\n",
    )
    delete_site(p, "a")
    raw = p.read_text(encoding="utf-8")
    assert "# important note" in raw


def test_delete_site_rolls_back_when_resulting_file_unloadable(tmp_path):
    """Round-3 review #M5 fix: delete_site has the same rollback discipline
    as add_site. Pre-existing corruption (uppercase id) makes the post-write
    re-validation fail; rollback restores the original bytes so the file
    is at least back to its pre-delete state."""
    p = _seed(
        tmp_path,
        "sites:\n"
        "  - id: BadCaseID\n    name: Bad\n    url: https://bad.example\n"
        "  - id: a\n    name: A\n    url: https://a.example\n",
    )
    original = p.read_text(encoding="utf-8")
    with pytest.raises(ValidationError):
        delete_site(p, "a")
    # Original bytes restored — the corrupt entry is still there but at
    # least the operator hasn't lost the entry they tried to delete.
    assert p.read_text(encoding="utf-8") == original


def test_add_site_rollback_is_atomic(tmp_path, monkeypatch):
    """Round-3 review #M4 fix: rollback uses tmp+rename so a crash mid-
    rollback can't leave the file half-overwritten. We force the tmp+
    rename rollback path to fail at `tmp.replace`; the file should
    either be the original or the post-write state — never empty."""
    p = _seed(
        tmp_path,
        "sites:\n  - id: BadCaseID\n    name: X\n    url: https://x.example\n",
    )
    original = p.read_text(encoding="utf-8")

    # Make the rollback's `replace` step fail. The .yml.rollback tmp is
    # different from the .yml.tmp the regular write uses, so we narrow.
    real_replace = Path.replace

    def _flaky_replace(self, target):
        if str(self).endswith(".yml.rollback"):
            raise OSError("simulated rollback rename failure")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", _flaky_replace)
    # The rollback raises, masking the underlying ValidationError.
    with pytest.raises(OSError, match="simulated rollback"):
        add_site(p, name="New", url="https://new.example")

    # The file is in the post-write (corrupt) state — NOT empty / truncated.
    # That's the atomicity guarantee: rollback either fully succeeds or
    # leaves the post-write state intact, never an interrupted-write blob.
    final = p.read_text(encoding="utf-8")
    assert final != "", "rollback failure must NOT leave file empty"
    # And no .rollback tmp leaked.
    assert not (tmp_path / "sites.yml.rollback").exists()
    # Original is recoverable from the operator's git history; we don't
    # need to assert the exact content, just that the file isn't empty.
    del original  # acknowledged unused — kept as documentation
