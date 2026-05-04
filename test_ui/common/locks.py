"""Per-run lock files + PGID-based liveness check (Phase B.2.1 + B.2.3).

A run-in-progress writes `<run_root>/.lock` containing
`{pid, pgid, hostname, started_at, command}`. The lock is removed on clean
completion (before atomic publication, so the final published run dir
contains no `.lock`). On hard kill (SIGKILL, OOM, host crash) the lock
stays in the orphaned `.tmp-<run_id>/` dir - future runs inspect it,
verify the holding PGID is dead via `os.kill(pgid, 0)` + `/proc/<pid>/stat`
start-time check (Linux), and proceed (logging a warning).

**Concurrency model.** Lock check + take is NOT atomic across processes
(no `flock` here - the file lives inside an atomic publication tmp dir
that's already unique per run_id). This is fine because the kind+date
precondition check at the CLI level is the real arbiter - the lock file
is for after-the-fact diagnostics ("which PID is currently crawling?")
and stale-run detection, not mutual exclusion.

**Why /proc start-time matters.** PIDs recycle. If we crash leaving a
lock with PID=12345 and the OS later reuses 12345 for an unrelated
process, `os.kill(12345, 0)` would succeed and we'd refuse to start.
Comparing the recorded `started_at` against `/proc/<pid>/stat`'s
field-22 (process start time in jiffies since boot) catches this:
mismatch → it's not the original holder → safe to proceed.
"""

from __future__ import annotations

import os
import socket
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from loguru import logger
from pydantic import BaseModel, ConfigDict

from ..config import settings


LOCK_FILENAME = ".lock"


class LockFile(BaseModel):
    """JSON shape persisted to `<run_root>/.lock`."""

    model_config = ConfigDict(extra="forbid")

    pid: int
    pgid: int
    hostname: str
    started_at: str  # DD-MM-YYYY HH:MM:SS, settings.get_current_datetime()
    command: str  # the CLI invocation, e.g. "afr snapshot --output data/baseline"
    proc_starttime: int | None = None  # /proc/<pid>/stat field 22, Linux only


class LockHeldError(RuntimeError):
    """Raised when a live lock for the same kind+date already exists."""

    def __init__(self, lock_path: Path, holder: LockFile):
        self.lock_path = lock_path
        self.holder = holder
        super().__init__(
            f"Run is already in progress (lock at {lock_path}): "
            f"pid={holder.pid} pgid={holder.pgid} on {holder.hostname} "
            f"started {holder.started_at} ({holder.command!r})"
        )


# ---------------------------------------------------------------------------
# /proc-based start-time helper (Linux only)
# ---------------------------------------------------------------------------


def _read_proc_starttime(pid: int) -> int | None:
    """Return field 22 of /proc/<pid>/stat (starttime in jiffies since boot).

    Returns None on non-Linux platforms, missing PID, or any read error -
    callers fall back to the os.kill(pgid, 0) check alone, accepting the
    small PID-recycling risk on those platforms.
    """
    proc_path = Path(f"/proc/{pid}/stat")
    if not proc_path.exists():
        return None
    try:
        # Field 2 is the comm name in parentheses and may itself contain
        # spaces / parens - split on the LAST `)` to skip past it cleanly.
        raw = proc_path.read_text(encoding="utf-8")
        after_comm = raw.rsplit(")", 1)[1]
        # After-comm fields are space-separated. Field 22 in the original
        # is field 20 here (we sliced off pid + comm = 2 fields).
        fields = after_comm.split()
        return int(fields[19])
    except (OSError, IndexError, ValueError):
        return None


def is_pgid_alive(pgid: int, recorded_proc_starttime: int | None = None) -> bool:
    """Check whether the process group `pgid` has any live members.

    Uses `os.killpg(pgid, 0)` (POSIX `kill(-pgid, 0)`) - the GROUP variant -
    so we correctly detect "a child is still working even though the group
    leader exited." Plain `os.kill(pgid, 0)` would only check the leader's
    PID and miss orphan children that inherited the group.

    Returns False (safe to take over) on ProcessLookupError. PermissionError
    means the group exists but is owned elsewhere - conservatively assume
    alive and refuse to take over.

    If `recorded_proc_starttime` is non-None we ALSO read /proc/<pgid>/stat
    (the leader's stat) and compare. Mismatch means the original leader's
    PID was recycled - treat as dead. If the leader is gone but children
    are alive, /proc/<pgid>/stat may not exist; we then trust killpg's
    "alive" result.
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # EINVAL on bad signal shouldn't fire for 0; conservative: alive.
        return True

    # Group is alive. If we have a recorded leader-starttime AND the leader
    # is still around to read, verify it hasn't drifted (PID recycling for
    # the leader specifically).
    if recorded_proc_starttime is not None:
        current = _read_proc_starttime(pgid)
        if current is not None and current != recorded_proc_starttime:
            return False  # leader PID recycled - original group is gone

    return True


# ---------------------------------------------------------------------------
# Lock acquire / release
# ---------------------------------------------------------------------------


def _make_lock(command: str | None = None) -> LockFile:
    """Build a LockFile model populated with this process's identity.

    `command` defaults to None → use `sys.argv` so an ad-hoc caller that
    forgot to pass one still gets useful diagnostics in the lock file.
    """
    pid = os.getpid()
    try:
        pgid = os.getpgid(0)
    except OSError:
        # Windows or odd environments don't have process groups; fall back
        # to the PID itself (single-process group of one).
        pgid = pid
    return LockFile(
        pid=pid,
        pgid=pgid,
        hostname=socket.gethostname(),
        started_at=settings.get_current_datetime(),
        command=command if command is not None else " ".join(sys.argv),
        proc_starttime=_read_proc_starttime(pgid),
    )


def write_lock(run_dir: Path, command: str) -> LockFile:
    """Persist a fresh lock file to `run_dir/.lock`. Overwrites any existing."""
    lock = _make_lock(command)
    (run_dir / LOCK_FILENAME).write_text(
        lock.model_dump_json(indent=2), encoding="utf-8"
    )
    return lock


def read_lock(run_dir_or_path: Path) -> LockFile | None:
    """Read `.lock` from a run dir (or pass the lock-file path directly).

    Returns None if no lock file exists or it fails to parse - corrupt
    locks are treated as not-held so a manual cleanup attempt won't be
    blocked by a parse error.
    """
    path = (
        run_dir_or_path
        if run_dir_or_path.name == LOCK_FILENAME
        else run_dir_or_path / LOCK_FILENAME
    )
    if not path.exists():
        return None
    try:
        return LockFile.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Corrupt lock at {path}: {type(e).__name__}: {e}")
        return None


def remove_lock(run_dir: Path) -> None:
    """Remove `<run_dir>/.lock`. Silent if already absent.

    Raises on ANY other OSError (permission denied, RO mount, NFS hiccup).
    This is deliberate - a leaked lock would be silently published into the
    final run dir if we swallowed the error here, and downstream tools
    looking for live locks only scan `.tmp-*/` siblings (not published
    dirs), so the leak would be invisible. Failing loudly forces the
    operator to notice.
    """
    try:
        (run_dir / LOCK_FILENAME).unlink()
    except FileNotFoundError:
        pass


@contextmanager
def acquire_lock(run_dir: Path, command: str) -> Iterator[LockFile]:
    """Context manager: write the lock on enter, remove on exit.

    Always removes the lock - including on exception - so the post-failure
    state of `<.tmp-run_id>/` doesn't include a stale lock. The atomic-
    publication tmp dir's presence (without a lock) is itself the signal
    that a run failed and someone should look.

    Use this INSIDE the `atomic_run_dir` context: the lock lives alongside
    the manifest in the tmp dir, gets removed on clean exit before the
    rename, so the published `<run_id>/` is lock-free.
    """
    lock = write_lock(run_dir, command=command)
    try:
        yield lock
    finally:
        remove_lock(run_dir)


# ---------------------------------------------------------------------------
# Cross-run: scan a date dir for any live lock holders
# ---------------------------------------------------------------------------


def find_live_lock_in_date(date_dir: Path) -> tuple[Path, LockFile] | None:
    """Scan `<date_dir>/.tmp-*/.lock` for a live holder. Returns (path, lock) or None.

    Used by the workflow precondition check: `baseline`/`current` refuse
    to start if any tmp run dir for the same kind+date holds a live lock.
    Stale locks (dead PGID / mismatched start-time) are silently ignored
    so they don't block a new run; the operator can clean up the leftover
    `.tmp-` dir manually if they care.
    """
    if not date_dir.exists():
        return None
    for tmp_dir in sorted(date_dir.glob(".tmp-*")):
        if not tmp_dir.is_dir():
            continue
        lock = read_lock(tmp_dir)
        if lock is None:
            continue
        if is_pgid_alive(lock.pgid, lock.proc_starttime):
            return tmp_dir / LOCK_FILENAME, lock
        else:
            logger.warning(
                f"Ignoring stale lock at {tmp_dir / LOCK_FILENAME} "
                f"(holder pid={lock.pid} pgid={lock.pgid} no longer alive)"
            )
    return None


__all__ = [
    "LOCK_FILENAME",
    "LockFile",
    "LockHeldError",
    "is_pgid_alive",
    "write_lock",
    "read_lock",
    "remove_lock",
    "acquire_lock",
    "find_live_lock_in_date",
]
