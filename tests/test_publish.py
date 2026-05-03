"""atomic_run_dir tests (Phase B.1.3 + B.2 belt-and-braces).

Pins the rename-on-clean-exit semantics + the defensive `.lock` scrub
that prevents a stale lock from being published into the final run dir.
"""

from __future__ import annotations


import pytest

from test_ui.common.publish import atomic_run_dir, final_dir_for, tmp_dir_for


def test_renames_tmp_to_final_on_clean_exit(tmp_path):
    """The happy path: yield the .tmp- dir, rename to <run_id>/ at exit."""
    run_id = "01HXX0000000000000000000A0"

    with atomic_run_dir(tmp_path, run_id) as run_root:
        assert run_root == tmp_dir_for(tmp_path, run_id)
        assert run_root.name == f".tmp-{run_id}"
        (run_root / "payload.txt").write_text("hello", encoding="utf-8")

    final = final_dir_for(tmp_path, run_id)
    assert final.exists() and final.is_dir()
    assert (final / "payload.txt").read_text() == "hello"
    assert not (tmp_path / f".tmp-{run_id}").exists(), (
        "tmp dir should be gone after rename"
    )


def test_leaves_tmp_in_place_on_exception(tmp_path):
    """No promotion if the body raises — the .tmp- dir stays for inspection."""
    run_id = "01HYY0000000000000000000A0"

    with pytest.raises(RuntimeError, match="boom"):
        with atomic_run_dir(tmp_path, run_id) as run_root:
            (run_root / "partial.txt").write_text("oops", encoding="utf-8")
            raise RuntimeError("boom")

    assert (tmp_path / f".tmp-{run_id}").exists(), "tmp dir must persist for debugging"
    assert not (tmp_path / run_id).exists(), "must NOT publish on exception"


def test_refuses_when_final_already_exists(tmp_path):
    """ULID collisions are statistically impossible; treat existence as a programming bug."""
    run_id = "01HZZ0000000000000000000A0"
    (tmp_path / run_id).mkdir()

    with pytest.raises(FileExistsError, match="already published"):
        with atomic_run_dir(tmp_path, run_id):
            pass


def test_refuses_when_tmp_already_exists(tmp_path):
    """Same logic — if the tmp dir exists, refuse rather than overwrite."""
    run_id = "01HAA0000000000000000000A0"
    (tmp_path / f".tmp-{run_id}").mkdir()

    with pytest.raises(FileExistsError, match="in-progress"):
        with atomic_run_dir(tmp_path, run_id):
            pass


def test_scrubs_leftover_lock_before_rename(tmp_path):
    """Belt-and-braces (B.2 review): a `.lock` left in the tmp dir at exit
    must NEVER be carried into the published run dir.

    Normally `acquire_lock`'s context exit removes the lock before
    atomic_run_dir's rename. This test simulates a buggy caller that
    forgot to use the lock context (or where remove_lock somehow failed
    silently in an older code path) and verifies atomic_run_dir cleans up.
    """
    run_id = "01HBB0000000000000000000A0"

    with atomic_run_dir(tmp_path, run_id) as run_root:
        # Simulate a lock that wasn't removed before exit.
        (run_root / ".lock").write_text("orphan lock", encoding="utf-8")
        (run_root / "real_output.txt").write_text("hi", encoding="utf-8")

    final = final_dir_for(tmp_path, run_id)
    assert final.exists()
    assert (final / "real_output.txt").exists(), "real payload must survive"
    assert not (final / ".lock").exists(), "leftover .lock must be scrubbed"
