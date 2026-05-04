"""Lock module tests (Phase B.2.1 + B.2.3).

Covers:
  - acquire_lock context manager: writes on enter, removes on exit
    (including on exception - the lock must NEVER persist into a published
    run dir)
  - LockFile JSON shape round-trips through Pydantic
  - is_pgid_alive: live PID returns True, made-up PID returns False
  - PID-recycling guard: same PID, different /proc starttime → dead
  - find_live_lock_in_date: walks .tmp-* siblings, ignores stale locks
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from test_ui.common.locks import (
    LOCK_FILENAME,
    LockFile,
    acquire_lock,
    find_live_lock_in_date,
    is_pgid_alive,
    read_lock,
    remove_lock,
    write_lock,
)


# ---------------------------------------------------------------------------
# LockFile model
# ---------------------------------------------------------------------------


def test_lockfile_serializes_and_round_trips():
    lock = LockFile(
        pid=12345,
        pgid=12345,
        hostname="test-host",
        started_at="01-01-2099 00:00:00",
        command="afr snapshot --output data/baseline",
    )
    json_str = lock.model_dump_json()
    parsed = LockFile.model_validate_json(json_str)
    assert parsed == lock


def test_lockfile_rejects_unknown_fields():
    """extra='forbid' is essential - a typo'd field name would silently
    drop on read otherwise, masking a real bug in the writer."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        LockFile.model_validate_json(
            '{"pid": 1, "pgid": 1, "hostname": "h", '
            '"started_at": "x", "command": "y", "rogue_field": 42}'
        )


# ---------------------------------------------------------------------------
# write_lock / read_lock / remove_lock
# ---------------------------------------------------------------------------


def test_write_lock_records_current_process_identity(tmp_path):
    lock = write_lock(tmp_path, command="test-command")

    # Self-attestation: the lock describes us.
    assert lock.pid == os.getpid()
    assert lock.command == "test-command"
    assert lock.hostname  # non-empty
    assert lock.started_at  # populated by settings.get_current_datetime()

    # The file landed where we expected.
    assert (tmp_path / LOCK_FILENAME).exists()
    on_disk = read_lock(tmp_path)
    assert on_disk == lock


def test_read_lock_accepts_run_dir_or_lock_path(tmp_path):
    """Convenience: read_lock works with either the run dir or the .lock path."""
    write_lock(tmp_path, command="x")
    via_dir = read_lock(tmp_path)
    via_path = read_lock(tmp_path / LOCK_FILENAME)
    assert via_dir == via_path


def test_read_lock_returns_none_when_missing(tmp_path):
    assert read_lock(tmp_path) is None


def test_read_lock_returns_none_for_corrupt_file(tmp_path):
    """A garbage .lock file should be treated as not-held (warn, then proceed),
    not raise. Otherwise a one-byte typo blocks all future runs."""
    (tmp_path / LOCK_FILENAME).write_text("not json", encoding="utf-8")
    assert read_lock(tmp_path) is None


def test_remove_lock_silent_on_missing(tmp_path):
    """Idempotent: removing a non-existent lock is a no-op."""
    remove_lock(tmp_path)  # should not raise


def test_remove_lock_raises_on_unexpected_oserror(tmp_path, monkeypatch):
    """Anything other than FileNotFoundError must propagate.

    Pin the post-B.2-review behavior: remove_lock used to swallow ALL
    OSErrors with a log warning, which let a leaked .lock get carried
    through atomic_run_dir's rename into a published run dir (where
    `find_live_lock_in_date` can't see it). Now we fail loudly so the
    operator notices.
    """
    write_lock(tmp_path, command="x")

    real_unlink = Path.unlink

    def _failing_unlink(self, missing_ok=False):
        if self.name == LOCK_FILENAME:
            raise PermissionError("simulated read-only filesystem")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _failing_unlink)

    with pytest.raises(PermissionError, match="simulated"):
        remove_lock(tmp_path)


# ---------------------------------------------------------------------------
# acquire_lock context manager - must always remove
# ---------------------------------------------------------------------------


def test_acquire_lock_removes_on_clean_exit(tmp_path):
    with acquire_lock(tmp_path, command="x"):
        assert (tmp_path / LOCK_FILENAME).exists()
    assert not (tmp_path / LOCK_FILENAME).exists()


def test_acquire_lock_removes_on_exception(tmp_path):
    """The lock MUST NOT persist into a published run dir if the work raised.

    If we left the .lock behind, the next workflow precondition check
    would see it and (after PGID-liveness verification) decide it's stale -
    correct outcome but unnecessary noise. Cleaner to remove on exit.
    """
    with pytest.raises(RuntimeError, match="boom"):
        with acquire_lock(tmp_path, command="x"):
            assert (tmp_path / LOCK_FILENAME).exists()
            raise RuntimeError("boom")
    assert not (tmp_path / LOCK_FILENAME).exists()


# ---------------------------------------------------------------------------
# is_pgid_alive
# ---------------------------------------------------------------------------


def test_is_pgid_alive_for_self():
    """Our own PID is, by definition, alive."""
    assert is_pgid_alive(os.getpid()) is True


def test_is_pgid_alive_for_pid_one():
    """PID 1 is always alive on POSIX (init / systemd / docker pid 1)."""
    assert is_pgid_alive(1) is True


def test_is_pgid_alive_returns_false_for_made_up_pid():
    """A wildly-out-of-range PID definitely has no process."""
    # 2**31 - 1 = INT_MAX; OS PID limits are typically ~4M, so this is safe.
    assert is_pgid_alive(2**31 - 1) is False


@pytest.mark.skipif(
    sys.platform != "linux", reason="/proc start-time check is Linux-only"
)
def test_is_pgid_alive_pid_recycle_detection():
    """Same PID + mismatched /proc starttime → treat as dead.

    This is the PID-recycling guard. A real-world recycle is hard to
    induce in a test; we simulate by recording our OWN PID's starttime
    then passing a deliberately-wrong value.
    """
    from test_ui.common.locks import _read_proc_starttime

    real_starttime = _read_proc_starttime(os.getpid())
    assert real_starttime is not None, "couldn't read /proc/self/stat starttime"

    # Real value matches → alive.
    assert is_pgid_alive(os.getpid(), real_starttime) is True
    # Wrong value (off by one) → treat as recycled / dead.
    assert is_pgid_alive(os.getpid(), real_starttime + 1) is False


# ---------------------------------------------------------------------------
# find_live_lock_in_date
# ---------------------------------------------------------------------------


def test_find_live_lock_returns_none_for_missing_date_dir(tmp_path):
    assert find_live_lock_in_date(tmp_path / "no-such-date") is None


def test_find_live_lock_returns_none_for_empty_date_dir(tmp_path):
    """Date dir exists but has no .tmp-* entries → nothing to scan."""
    (tmp_path / "01-01-2099").mkdir()
    assert find_live_lock_in_date(tmp_path / "01-01-2099") is None


def test_find_live_lock_finds_self_lock(tmp_path):
    """Real lock from this process should be detected as live."""
    date_dir = tmp_path / "01-01-2099"
    tmp_run = date_dir / ".tmp-01HXX0000000000000000000A0"
    tmp_run.mkdir(parents=True)
    write_lock(tmp_run, command="self-test")

    found = find_live_lock_in_date(date_dir)
    assert found is not None
    lock_path, holder = found
    assert lock_path == tmp_run / LOCK_FILENAME
    assert holder.pid == os.getpid()


def test_find_live_lock_ignores_stale_lock(tmp_path):
    """Lock with a dead PID should NOT block - return None (and log a warning)."""
    date_dir = tmp_path / "01-01-2099"
    tmp_run = date_dir / ".tmp-01HZZ0000000000000000000B0"
    tmp_run.mkdir(parents=True)

    # Hand-craft a lock pointing at a PID that definitely doesn't exist.
    stale = LockFile(
        pid=2**31 - 1,
        pgid=2**31 - 1,
        hostname="ghost",
        started_at="01-01-1999 00:00:00",
        command="dead-process",
    )
    (tmp_run / LOCK_FILENAME).write_text(stale.model_dump_json(), encoding="utf-8")

    assert find_live_lock_in_date(date_dir) is None


def test_find_live_lock_skips_files_and_non_tmp_dirs(tmp_path):
    """Only `.tmp-*` directories are considered. Stray .lock files at the
    date level (or inside published run_id dirs) must not be scanned -
    a published run that left a .lock by mistake shouldn't lock the date."""
    date_dir = tmp_path / "01-01-2099"
    date_dir.mkdir()
    # A regular file and a non-tmp dir at the date level - both must be ignored.
    (date_dir / "stray.txt").write_text("not a dir", encoding="utf-8")
    weird_dir = date_dir / "01HXX0000000000000000000A0"  # ULID, not .tmp-
    weird_dir.mkdir()
    write_lock(weird_dir, command="should be ignored")

    assert find_live_lock_in_date(date_dir) is None


def test_find_live_lock_skips_tmp_dir_without_lock(tmp_path):
    """A tmp dir without a .lock file means the lock context already cleaned
    up (or never wrote one). Don't trip on its presence."""
    date_dir = tmp_path / "01-01-2099"
    (date_dir / ".tmp-01HXX0000000000000000000A0").mkdir(parents=True)
    assert find_live_lock_in_date(date_dir) is None
