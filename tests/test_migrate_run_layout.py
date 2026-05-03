"""Migration script tests (Phase B.1.6).

Pins the conversion from pre-B.1 (`<kind>/<date>/<url_dir>/`) to B.1
(`<kind>/<date>/<run_id>/<url_dir>/`).

Idempotency is the most important property — running the migration twice
must be a no-op so dashboard / cron-style "always run on startup" use is
safe.
"""

from __future__ import annotations

from pathlib import Path

from test_ui.common.manifest import read_manifest
from test_ui.common.run_id import is_valid_run_id


def _seed_legacy_url_dir(date_dir: Path, url_name: str) -> Path:
    """Lay down a pre-B.1-shaped url_dir with a stub file inside."""
    url_dir = date_dir / url_name
    url_dir.mkdir(parents=True, exist_ok=True)
    (url_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    return url_dir


def _run_migration(data_root: Path) -> int:
    """Invoke the migration script's main() with --data-root pointing at tmp."""
    import sys
    from importlib import reload

    # Reload to pick up monkeypatched sys.path / settings if a previous test
    # imported it with different state.
    sys.argv = ["migrate_run_layout.py", "--data-root", str(data_root)]
    from scripts import migrate_run_layout

    reload(migrate_run_layout)
    return migrate_run_layout.main()


def test_migrates_single_date_dir(tmp_path):
    """One date with one url_dir → one run_id with manifest + url_dir inside it."""
    legacy = tmp_path / "baseline" / "01-01-2099"
    _seed_legacy_url_dir(legacy, "example.com")

    rc = _run_migration(tmp_path)
    assert rc == 0

    children = list(legacy.iterdir())
    run_dirs = [c for c in children if c.is_dir() and is_valid_run_id(c.name)]
    assert len(run_dirs) == 1, (
        f"expected 1 run_id dir, got {[c.name for c in children]}"
    )
    run_dir = run_dirs[0]

    # url_dir moved INTO run_dir.
    assert (run_dir / "example.com" / "index.html").exists()
    # Original url_dir at the date level is gone.
    assert not (legacy / "example.com").exists()

    # Manifest written, status complete, kind correct, url_count correct.
    manifest = read_manifest(run_dir)
    assert manifest.status == "complete"
    assert manifest.kind == "baseline"
    assert manifest.url_count == 1
    assert manifest.files_sha256 is not None and len(manifest.files_sha256) == 64


def test_migration_is_idempotent(tmp_path):
    """Second invocation is a no-op — no extra run_id dirs created."""
    legacy = tmp_path / "current" / "02-01-2099"
    _seed_legacy_url_dir(legacy, "site_a")
    _seed_legacy_url_dir(legacy, "site_b")

    assert _run_migration(tmp_path) == 0
    after_first = sorted(p.name for p in legacy.iterdir())

    assert _run_migration(tmp_path) == 0
    after_second = sorted(p.name for p in legacy.iterdir())

    assert after_first == after_second, "second migration changed the tree"
    run_dirs = [c for c in legacy.iterdir() if c.is_dir() and is_valid_run_id(c.name)]
    assert len(run_dirs) == 1


def test_migration_handles_multiple_kinds_and_dates(tmp_path):
    """Cross product of kinds × dates each get their own migrated run."""
    for kind in ("baseline", "current", "comparator"):
        for date in ("01-01-2099", "02-01-2099"):
            _seed_legacy_url_dir(tmp_path / kind / date, "example.com")

    assert _run_migration(tmp_path) == 0

    for kind in ("baseline", "current", "comparator"):
        for date in ("01-01-2099", "02-01-2099"):
            date_dir = tmp_path / kind / date
            run_dirs = [
                c for c in date_dir.iterdir() if c.is_dir() and is_valid_run_id(c.name)
            ]
            assert len(run_dirs) == 1, f"{kind}/{date}: missing run_id dir"
            assert (run_dirs[0] / "example.com" / "index.html").exists()


def test_migration_skips_already_b1_layout(tmp_path):
    """Date dir with a manifest-bearing ULID subdir is treated as migrated.

    The "already migrated" check requires both a ULID-named dir AND a
    manifest.json inside it, so a stray empty run_dir from a failed prior
    attempt can't poison subsequent runs (covered by the rollback tests).
    """
    from test_ui.common.manifest import Manifest, write_manifest

    date_dir = tmp_path / "report" / "03-01-2099"
    existing_run_id = "01HXX0000000000000000000A0"
    existing_run = date_dir / existing_run_id
    existing_run.mkdir(parents=True)
    (existing_run / "site" / "ai_analysis.json").parent.mkdir()
    (existing_run / "site" / "ai_analysis.json").write_text("{}", encoding="utf-8")
    write_manifest(
        existing_run,
        Manifest(
            run_id=existing_run_id,
            kind="report",
            started_at="01-01-2099 00:00:00",
            finished_at="01-01-2099 00:00:01",
            status="complete",
        ),
    )

    # Stray legacy-looking url_dir at date level — must NOT get folded in
    # because a real B.1 run already exists for this date.
    (date_dir / "stray_legacy_url").mkdir()

    assert _run_migration(tmp_path) == 0

    # Existing run untouched.
    assert (existing_run / "site" / "ai_analysis.json").exists()
    # Stray dir still present (caller can clean up manually if intentional).
    assert (date_dir / "stray_legacy_url").exists()
    # No second run_id appeared.
    run_dirs = [c for c in date_dir.iterdir() if c.is_dir() and is_valid_run_id(c.name)]
    assert len(run_dirs) == 1
    assert run_dirs[0].name == existing_run_id


def test_migration_recovers_from_orphan_empty_run_dir(tmp_path):
    """An orphan empty ULID dir (no manifest) must NOT block a fresh migration.

    Simulates the post-failure state where a previous migration's rollback
    couldn't `rmdir` the new run_dir (e.g. a stray .tmp- file inside it).
    The next migration must be able to recover by treating the date as
    not-yet-migrated.
    """
    legacy = tmp_path / "baseline" / "04-01-2099"
    _seed_legacy_url_dir(legacy, "example.com")

    # Pre-create an orphan ULID dir without a manifest — looks like B.1
    # layout to the eye but is incomplete.
    orphan_id = "01HZZ0000000000000000000B0"
    (legacy / orphan_id).mkdir()

    assert _run_migration(tmp_path) == 0

    # The legacy url_dir should have been migrated INTO a *new* ULID dir
    # (with a manifest), and the orphan should still be there but ignored.
    run_dirs_with_manifest = [
        c
        for c in legacy.iterdir()
        if c.is_dir() and is_valid_run_id(c.name) and (c / "manifest.json").exists()
    ]
    assert len(run_dirs_with_manifest) == 1, "expected exactly one real run dir"
    assert (run_dirs_with_manifest[0] / "example.com" / "index.html").exists()
    # Orphan still present (operator's call to clean up).
    assert (legacy / orphan_id).exists()


def test_migration_dry_run_changes_nothing(tmp_path):
    """--dry-run prints the plan but doesn't touch disk."""
    import sys

    legacy = tmp_path / "baseline" / "01-01-2099"
    _seed_legacy_url_dir(legacy, "example.com")

    sys.argv = ["migrate_run_layout.py", "--data-root", str(tmp_path), "--dry-run"]
    from importlib import reload

    from scripts import migrate_run_layout

    reload(migrate_run_layout)
    rc = migrate_run_layout.main()
    assert rc == 0

    # url_dir should still be at the date level — nothing moved.
    assert (legacy / "example.com" / "index.html").exists()
    # No run_id dir created.
    run_dirs = [c for c in legacy.iterdir() if c.is_dir() and is_valid_run_id(c.name)]
    assert run_dirs == []


def test_migration_skips_empty_date_dirs(tmp_path):
    """Empty `<kind>/<date>/` and dirs containing only `latest` symlink are
    silently skipped — no spurious run_id dir is created."""
    empty_date = tmp_path / "baseline" / "01-01-2099"
    empty_date.mkdir(parents=True)

    assert _run_migration(tmp_path) == 0
    children = list(empty_date.iterdir())
    assert children == [], f"empty date dir should remain empty, got {children}"


# ---------------------------------------------------------------------------
# Failure / rollback paths
# ---------------------------------------------------------------------------


def test_migration_rollback_restores_state_on_partial_rename_failure(
    tmp_path, monkeypatch
):
    """If `rename` raises mid-batch, every successful move is reversed and
    the empty run_dir is removed, so the next migration sees the date as
    un-migrated and re-attempts cleanly.

    Without rollback, the date silently loses the un-renamed url_dirs forever
    (the orphan run_id dir would block the next migration via the idempotency
    check). This test guards that the safety net works.
    """
    legacy = tmp_path / "baseline" / "05-01-2099"
    for name in ("alpha", "beta", "gamma", "delta"):
        _seed_legacy_url_dir(legacy, name)

    # Snapshot original layout for post-rollback comparison.
    original_url_names = sorted(p.name for p in legacy.iterdir())

    # Patch Path.rename to raise on the third invocation. The first two
    # successful moves should be reversed by the rollback logic.
    real_rename = Path.rename
    call_count = {"n": 0}

    def _flaky_rename(self, target):
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise OSError("simulated mid-batch rename failure")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", _flaky_rename)

    # Migration should raise (the failed rename propagates out of main()).
    import pytest

    with pytest.raises(OSError, match="simulated mid-batch rename failure"):
        _run_migration(tmp_path)

    # Restore real rename so the subsequent re-migration works normally.
    monkeypatch.setattr(Path, "rename", real_rename)

    # State after rollback: the date dir looks the same as before.
    after_rollback = sorted(p.name for p in legacy.iterdir())
    assert after_rollback == original_url_names, (
        f"rollback failed to restore state: {after_rollback} != {original_url_names}"
    )

    # Re-running the migration must succeed cleanly (the orphan run_dir was
    # cleaned up so the idempotency check doesn't block).
    assert _run_migration(tmp_path) == 0

    # All four url_dirs should now live under a single run_id dir with a manifest.
    run_dirs = [
        c
        for c in legacy.iterdir()
        if c.is_dir() and is_valid_run_id(c.name) and (c / "manifest.json").exists()
    ]
    assert len(run_dirs) == 1, "expected exactly one migrated run dir"
    moved_names = sorted(p.name for p in run_dirs[0].iterdir() if p.is_dir())
    assert moved_names == ["alpha", "beta", "delta", "gamma"]
