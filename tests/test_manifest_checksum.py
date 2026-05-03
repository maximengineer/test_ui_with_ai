"""Tests for `compute_files_sha256` exclusion rules.

The checksum is the manifest's tamper-detection field. It must be stable
across "logically identical" runs — meaning ephemeral debug debris
(`manifest.json.corrupt-*` backups, `.lock` files, `.tmp-*` workspace
dirs) MUST be excluded so a failed-and-rerun-into-the-same-dir scenario
doesn't yield two different checksums for the same payload.
"""

from __future__ import annotations

import time

from test_ui.common.manifest import compute_files_sha256


def test_excludes_manifest_itself(tmp_path):
    (tmp_path / "manifest.json").write_text('{"x": 1}', encoding="utf-8")
    (tmp_path / "payload.txt").write_text("hello", encoding="utf-8")
    h = compute_files_sha256(tmp_path)

    # Modifying manifest.json must not change the digest.
    (tmp_path / "manifest.json").write_text('{"y": 2}', encoding="utf-8")
    assert compute_files_sha256(tmp_path) == h


def test_excludes_corrupt_manifest_backups(tmp_path):
    """B.1 review #2 introduced manifest.json.corrupt-<ts> backups when
    fail_manifest can't parse the existing file. Those are debug-only
    debris and MUST NOT change the checksum (otherwise rerunning a failed
    run into the same dir yields a different hash).
    """
    (tmp_path / "payload.txt").write_text("hello", encoding="utf-8")
    h_before = compute_files_sha256(tmp_path)

    # Simulate: a prior failed run left a backup behind.
    backup = tmp_path / f"manifest.json.corrupt-{int(time.time())}"
    backup.write_text('{"corrupt": true}', encoding="utf-8")

    h_after = compute_files_sha256(tmp_path)
    assert h_after == h_before, (
        f"corrupt-backup file leaked into checksum: {h_before} != {h_after}"
    )


def test_excludes_lock_files(tmp_path):
    (tmp_path / "payload.txt").write_text("x", encoding="utf-8")
    h_before = compute_files_sha256(tmp_path)
    (tmp_path / ".lock").write_text("pid=1", encoding="utf-8")
    assert compute_files_sha256(tmp_path) == h_before


def test_excludes_tmp_dirs(tmp_path):
    (tmp_path / "payload.txt").write_text("x", encoding="utf-8")
    h_before = compute_files_sha256(tmp_path)
    tmp_subdir = tmp_path / ".tmp-01HXX0000000000000000000A0"
    tmp_subdir.mkdir()
    (tmp_subdir / "garbage.txt").write_text("noise", encoding="utf-8")
    assert compute_files_sha256(tmp_path) == h_before


def test_real_payload_changes_DO_change_checksum(tmp_path):
    """Sanity: the exclusion list isn't so aggressive that real changes are missed."""
    (tmp_path / "payload.txt").write_text("hello", encoding="utf-8")
    h_before = compute_files_sha256(tmp_path)
    (tmp_path / "payload.txt").write_text("hello-larger-payload", encoding="utf-8")
    assert compute_files_sha256(tmp_path) != h_before
