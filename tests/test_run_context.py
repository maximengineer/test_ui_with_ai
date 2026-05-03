"""Lifecycle context manager tests (post-B.3 cleanup).

`run_context` is the new single-seam wrapper for atomic publication +
manifest + lock + failure dispatch. Pin every state transition: clean
success, KeyboardInterrupt → interrupted, generic exception → failed,
forgot-to-call-complete → failed-with-RuntimeError.
"""

from __future__ import annotations

import pytest

from test_ui.common.locks import LOCK_FILENAME
from test_ui.common.manifest import read_manifest
from test_ui.common.run_context import run_context


def test_clean_success_publishes_with_complete_status(tmp_path):
    """Body succeeds + calls complete → atomic publish + manifest=complete."""
    date_dir = tmp_path / "01-01-2099"
    date_dir.mkdir()
    run_id = "01HXX0000000000000000000A0"

    with run_context(date_dir, run_id, kind="baseline", command="afr snapshot") as ctx:
        (ctx.run_root / "payload.txt").write_text("hi", encoding="utf-8")
        ctx.complete(url_count=1)

    # Promoted to final dir.
    final = date_dir / run_id
    assert final.exists() and final.is_dir()
    assert (final / "payload.txt").read_text() == "hi"
    # No tmp dir leftover.
    assert not (date_dir / f".tmp-{run_id}").exists()
    # Manifest reports complete.
    manifest = read_manifest(final)
    assert manifest.status == "complete"
    assert manifest.url_count == 1
    assert manifest.kind == "baseline"
    # Lock was removed before publish.
    assert not (final / LOCK_FILENAME).exists()


def test_keyboard_interrupt_marks_interrupted_and_preserves_tmp(tmp_path):
    """Ctrl-C inside the body → status='interrupted', tmp dir preserved."""
    date_dir = tmp_path / "01-01-2099"
    date_dir.mkdir()
    run_id = "01HYY0000000000000000000A0"

    with pytest.raises(KeyboardInterrupt):
        with run_context(date_dir, run_id, kind="comparator", command="x") as ctx:
            (ctx.run_root / "partial.txt").write_text("oops", encoding="utf-8")
            raise KeyboardInterrupt("simulated ^C")

    # NOT promoted — tmp dir preserved for inspection.
    assert not (date_dir / run_id).exists()
    tmp_dir = date_dir / f".tmp-{run_id}"
    assert tmp_dir.exists()
    assert (tmp_dir / "partial.txt").read_text() == "oops"
    # Manifest reflects the interruption.
    manifest = read_manifest(tmp_dir)
    assert manifest.status == "interrupted"
    assert manifest.kind == "comparator"


def test_generic_exception_marks_failed(tmp_path):
    """Any non-(KeyboardInterrupt|SystemExit) exception → status='failed'."""
    date_dir = tmp_path / "01-01-2099"
    date_dir.mkdir()
    run_id = "01HZZ0000000000000000000A0"

    with pytest.raises(RuntimeError, match="boom"):
        with run_context(date_dir, run_id, kind="report", command="x"):
            raise RuntimeError("boom")

    tmp_dir = date_dir / f".tmp-{run_id}"
    assert tmp_dir.exists()
    manifest = read_manifest(tmp_dir)
    assert manifest.status == "failed"


def test_systemexit_maps_to_interrupted(tmp_path):
    """SystemExit (e.g. sys.exit() inside the body) is treated as an
    operator-initiated interruption, not a failure."""
    date_dir = tmp_path / "01-01-2099"
    date_dir.mkdir()
    run_id = "01HAA0000000000000000000A0"

    with pytest.raises(SystemExit):
        with run_context(date_dir, run_id, kind="baseline", command="x"):
            raise SystemExit(2)

    tmp_dir = date_dir / f".tmp-{run_id}"
    manifest = read_manifest(tmp_dir)
    assert manifest.status == "interrupted"


def test_forgetting_complete_is_a_loud_failure(tmp_path):
    """Body returns cleanly but forgot to call ctx.complete() → RuntimeError
    AND the manifest is marked failed (not silently complete or running).

    The original boilerplate had the symmetry built in (complete_manifest
    was the LAST call inside the try). Wrapping it in a CM made it possible
    for a caller to forget — surface this as a hard error so the bug is
    found in dev, not by an operator looking at a status="running" manifest
    days later.
    """
    date_dir = tmp_path / "01-01-2099"
    date_dir.mkdir()
    run_id = "01HBB0000000000000000000A0"

    with pytest.raises(RuntimeError, match="without calling ctx.complete"):
        with run_context(date_dir, run_id, kind="baseline", command="x") as ctx:
            (ctx.run_root / "x.txt").write_text("ok", encoding="utf-8")
            # Forgot ctx.complete()!

    tmp_dir = date_dir / f".tmp-{run_id}"
    manifest = read_manifest(tmp_dir)
    assert manifest.status == "failed"


def test_source_run_ids_recorded_in_manifest(tmp_path):
    """source_run_ids passed to run_context lands in the manifest unchanged."""
    date_dir = tmp_path / "01-01-2099"
    date_dir.mkdir()
    run_id = "01HCC0000000000000000000A0"
    sources = {"baseline": "BB", "current": "CC"}

    with run_context(
        date_dir, run_id, kind="comparator", command="x", source_run_ids=sources
    ) as ctx:
        ctx.complete(url_count=0)

    manifest = read_manifest(date_dir / run_id)
    assert manifest.source_run_ids == sources


def test_post_complete_raise_preserves_complete_status(tmp_path):
    """If the body calls ctx.complete() and THEN a later line raises (e.g.
    a post-success cleanup step fails), the manifest must stay at
    status='complete' — NOT silently get downgraded to 'failed'.

    Pre-fix bug: the bare `except BaseException` rewrote status='complete'
    → 'failed' for a successful run that crashed in cleanup. Operator
    inspecting the tmp dir would think the work itself failed when in fact
    only the post-step did. The exception still propagates to the caller
    so they know something went wrong; they just see an honest manifest.
    """
    date_dir = tmp_path / "01-01-2099"
    date_dir.mkdir()
    run_id = "01HEE0000000000000000000A0"

    with pytest.raises(RuntimeError, match="post-cleanup"):
        with run_context(date_dir, run_id, kind="baseline", command="x") as ctx:
            (ctx.run_root / "real_output.txt").write_text("good", encoding="utf-8")
            ctx.complete(url_count=1)
            # Simulate something that runs after work is logically done
            # (e.g. a future caller adding a metrics-publish step inside
            # the with-block). It raises — but the work IS done.
            raise RuntimeError("post-cleanup failure")

    # The tmp dir is preserved (atomic_run_dir doesn't promote on exception).
    tmp_dir = date_dir / f".tmp-{run_id}"
    assert tmp_dir.exists()
    # And the manifest reflects the truthful state: the work completed.
    manifest = read_manifest(tmp_dir)
    assert manifest.status == "complete", (
        f"manifest must NOT be downgraded after complete(); got {manifest.status}"
    )
    assert manifest.url_count == 1


def test_complete_is_idempotent(tmp_path):
    """A second `.complete()` call short-circuits — does NOT rewrite
    finished_at or re-hash the dir.

    Pinning this catches a regression where someone removes the
    `if self._completed: return` guard. Without it, a second call would
    silently shift `finished_at` to the later timestamp and re-compute
    the file checksum (which can differ if the body wrote more files
    between the two calls) — producing a different audit trail than the
    operator expects from a "completed" run.
    """
    date_dir = tmp_path / "01-01-2099"
    date_dir.mkdir()
    run_id = "01HGG0000000000000000000A0"

    with run_context(date_dir, run_id, kind="baseline", command="x") as ctx:
        (ctx.run_root / "first.txt").write_text("a", encoding="utf-8")
        first_manifest = ctx.complete(url_count=1)

        # Add another file AFTER complete + call complete again with a
        # different count. The second call must NOT re-hash or update
        # url_count / finished_at.
        (ctx.run_root / "second.txt").write_text("b", encoding="utf-8")
        second_manifest = ctx.complete(url_count=999)

    assert second_manifest.url_count == 1, "second complete() must not change url_count"
    assert second_manifest.files_sha256 == first_manifest.files_sha256, (
        "second complete() must not re-hash"
    )
    assert second_manifest.finished_at == first_manifest.finished_at, (
        "second complete() must not bump finished_at"
    )


def test_post_complete_keyboard_interrupt_also_preserves_complete(tmp_path):
    """Same protection for KeyboardInterrupt: a Ctrl-C during post-success
    cleanup must not rewrite a `complete` manifest to `interrupted`."""
    date_dir = tmp_path / "01-01-2099"
    date_dir.mkdir()
    run_id = "01HFF0000000000000000000A0"

    with pytest.raises(KeyboardInterrupt):
        with run_context(date_dir, run_id, kind="report", command="x") as ctx:
            ctx.complete(url_count=2)
            raise KeyboardInterrupt("ctrl-c during cleanup")

    manifest = read_manifest(date_dir / f".tmp-{run_id}")
    assert manifest.status == "complete"


def test_lock_file_present_during_body_absent_after_complete(tmp_path):
    """The lock lives only during the body, never in the published dir."""
    date_dir = tmp_path / "01-01-2099"
    date_dir.mkdir()
    run_id = "01HDD0000000000000000000A0"

    with run_context(date_dir, run_id, kind="baseline", command="x") as ctx:
        # Lock exists during the body.
        assert (ctx.run_root / LOCK_FILENAME).exists()
        ctx.complete(url_count=0)

    # Lock removed before publish.
    assert not (date_dir / run_id / LOCK_FILENAME).exists()
