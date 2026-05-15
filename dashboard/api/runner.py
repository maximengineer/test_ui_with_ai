"""Subprocess job runner (Phase C.1 second slice).

**Linux-only.** The dashboard reads `/proc/<pid>/stat` for the PID-recycling
defense; it has no Mac/Windows fallback. Operators on those platforms run
the dashboard inside the Linux container (the project's `Dockerfile.dashboard`
+ docker-compose service). The `_startup_sync` in `main.py` raises at
lifespan start if `sys.platform != "linux"` so a host-native invocation on
the wrong OS fails fast with a clear message.

The dashboard never calls `Orchestrator` directly. It spawns
`python -m test_ui <command> --run-id <ULID> ...` as a separate process,
captures the PID/PGID, and watches for exit. This split is deliberate:

  - **Cleanup safety**: if the dashboard dies, the subprocess is in its own
    process group (`start_new_session=True`) so `os.killpg(pgid, SIGTERM)`
    at restart-recovery cleanly tears down the whole work tree, not just
    the leader. SIGTERM-then-SIGKILL with a 5s grace period is the same
    pattern systemd / k8s use.

  - **Crash isolation**: a crash inside the crawler (e.g. a Playwright
    segfault) takes down the subprocess only. The dashboard stays up
    and can mark the row `failed` from the post-mortem exit code.

  - **PID recycling defense**: `pid_start_time` (from /proc/<pid>/stat
    field 22) is recorded at spawn. At restart-recovery we re-read /proc
    and refuse to SIGTERM a PGID whose leader's start time has changed -
    that means the OS recycled the PID for an unrelated process, and
    killing it would be a footgun.

This module is pure machinery - routes live in `routes.py`. The split
keeps the routes file thin and the subprocess code testable in isolation.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from importlib.resources import files
from pathlib import Path
from typing import Callable

from loguru import logger

from test_ui.config import settings

from .db import (
    connection_scope,
    find_active_runs,
    mark_running,
    mark_terminal,
)


# Map dashboard `kind` → the CLI command that implements it. The CLI uses
# `snapshot` / `current` / `compare` / `enhanced-report`; the dashboard
# uses `baseline` / `current` / `comparator` / `report` (matching the
# manifest vocabulary). One translation table beats four if/elif blocks.
_KIND_TO_CLI_COMMAND: dict[str, str] = {
    "baseline": "snapshot",
    "current": "current",
    "comparator": "compare",
    "report": "enhanced-report",
}


# Strong references to in-flight watcher Tasks. asyncio's event loop only
# keeps WEAK references to tasks created via `create_task`, so an
# unreferenced task can be garbage-collected mid-await - silently leaving
# the row stuck in `running` for the next startup recovery to clean up.
# `_track_watcher` adds the task here on creation and removes it via
# `add_done_callback` so the set doesn't grow unboundedly. Tests can
# inspect this to assert no orphan tasks leak between cases.
_active_watchers: set[asyncio.Task] = set()

# PGIDs of subprocesses we currently believe are alive. Populated by
# `spawn_run` after PGID capture, removed by `_watch` when the process
# exits. Used by `shutdown_active_subprocesses` (called from the
# lifespan's shutdown side) to SIGTERM them eagerly instead of leaking
# them to the next startup recovery.
_active_pgids: set[int] = set()


def _track_watcher(task: asyncio.Task) -> None:
    """Strong-ref `task` until it completes; then drop the ref."""
    _active_watchers.add(task)
    task.add_done_callback(_active_watchers.discard)


# Concurrency cap on simultaneously-spawned subprocesses. Default 2 -
# each subprocess drives a Playwright Chromium that's ~500MB resident,
# so without a cap an operator clicking "Run report" across 10 dates
# can OOM the host. (Round-3 milestone-review HIGH #3.)
#
# Configurable via AFR_DASHBOARD_MAX_CONCURRENT_RUNS env var so the
# operator can dial it up on a beefier box. Read at module import; the
# semaphore IS shared across requests for the same dashboard process.
_MAX_CONCURRENT_RUNS = max(
    1, int(os.environ.get("AFR_DASHBOARD_MAX_CONCURRENT_RUNS", "2"))
)
_spawn_semaphore: asyncio.Semaphore | None = None


def _get_spawn_semaphore() -> asyncio.Semaphore:
    """Lazy-init the semaphore so it binds to the running event loop.

    `asyncio.Semaphore()` constructed at module import binds to whatever
    loop is current then - which under uvicorn isn't the same loop as
    the one serving requests. Lazy init defers the bind to first use.
    Tests reset via `_reset_spawn_semaphore_for_tests`.
    """
    global _spawn_semaphore
    if _spawn_semaphore is None:
        _spawn_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_RUNS)
    return _spawn_semaphore


def _reset_spawn_semaphore_for_tests() -> None:
    """Drop the semaphore so the next `_get_spawn_semaphore` rebuilds it
    on the test's loop. Used by fixtures that span multiple TestClient
    lifecycles in one pytest run."""
    global _spawn_semaphore
    _spawn_semaphore = None


# ---------------------------------------------------------------------------
# Process-identity primitives.
# ---------------------------------------------------------------------------


def pid_start_time(pid: int) -> str | None:
    """Read /proc/<pid>/stat field 22 (start_time, in clock ticks since boot).

    Returns the integer as a string for direct round-trip storage in the
    `pid_start_time` TEXT column. Returns None when the PID doesn't exist
    or /proc/<pid>/stat can't be parsed (corrupt format from a kernel
    we don't recognize). Linux-only - the dashboard refuses to start on
    other platforms (see `dashboard/api/main.py:_startup_sync`).

    Field 22 is the right field after handling the parenthesized comm field
    (which can contain spaces): split on the LAST `)` and count tokens
    from there. Doing this naively (`split()[21]`) breaks on processes
    with spaces in their name. Linux man page proc(5) is the reference.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, PermissionError):
        return None
    # comm is field 2, wrapped in parentheses; everything after the LAST `)`
    # is space-delimited fields starting at field 3. start_time is field 22,
    # so index 22 - 3 = 19 in the post-) split.
    rsplit = raw.rsplit(")", 1)
    if len(rsplit) != 2:
        return None
    fields = rsplit[1].split()
    if len(fields) < 20:
        return None
    return fields[19]


def _pgid_alive_and_ours(pgid: int, recorded_start_time: str | None) -> bool:
    """True iff the PGID is still alive AND we can VERIFY it's our process.

    Verification rules:
      - `os.killpg(pgid, 0)` raises ProcessLookupError if the PGID is gone.
      - We compare `pid_start_time(pgid)` against the value recorded at
        spawn. If they differ, the OS has recycled the PID for an
        unrelated process - return False so we DON'T SIGTERM it.
      - If `recorded_start_time` is None (rare: /proc parse failure at
        spawn time), we **refuse to claim ownership** rather than fall
        back to "trust the PGID". The cost is a leaked subprocess on
        dashboard restart (operator nukes manually); the avoided cost is
        SIGTERM'ing an unrelated process group after PID recycling.
        Correctness > resource cleanup.
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but isn't ours - never our process. Don't kill.
        return False

    if recorded_start_time is None:
        # /proc parse failed at spawn time - we don't have enough info to
        # verify identity. Refuse to claim ownership rather than risk
        # killing the wrong process group.
        return False

    current = pid_start_time(pgid)
    if current is None:
        # Had a start time at spawn but don't now (process gone in the
        # brief window between killpg and stat). Treat as not-ours.
        return False
    return current == recorded_start_time


def _kill_pgid(pgid: int, *, grace_seconds: float = 5.0) -> None:
    """SIGTERM the PGID, wait `grace_seconds`, then SIGKILL if still alive.

    Sync function (called from startup recovery, which runs in a thread).
    Intended for orphan-cleanup, not normal shutdown.
    """
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return  # already gone

    deadline = grace_seconds
    step = 0.1
    while deadline > 0:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        # Sync sleep - recovery already runs in a worker thread via
        # `asyncio.to_thread`, so blocking the thread is fine.
        time.sleep(step)
        deadline -= step

    # Still alive after grace; escalate to SIGKILL.
    try:
        os.killpg(pgid, signal.SIGKILL)
        logger.warning(f"runner: SIGKILL'd unresponsive PGID {pgid}")
    except ProcessLookupError:
        pass


# ---------------------------------------------------------------------------
# Subprocess spawn + watch.
# ---------------------------------------------------------------------------


def _resolve_sites_file() -> Path:
    """Locate `test_ui/sites.yml` for the spawned subprocess.

    The CLI's `--sites-file` defaults to `./sites.yml`, but the dashboard
    is typically launched from a different cwd. We persist the bundled
    resource to `settings.data_root/.cache/sites.yml` and return that
    stable on-disk path. This works in source-tree, editable install,
    and zipped-wheel contexts.

    If cache write fails (permissions, read-only FS), we log + fall back
    to the package-adjacent path as a best effort.
    """
    resource = files("test_ui") / "sites.yml"
    sites_bytes = resource.read_bytes()

    cache_dir = settings.data_root / ".cache"
    cache_path = cache_dir / "sites.yml"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        if not cache_path.exists() or cache_path.read_bytes() != sites_bytes:
            cache_path.write_bytes(sites_bytes)
        return cache_path
    except OSError as e:
        logger.warning(
            f"runner: failed to refresh cached sites.yml at {cache_path}: "
            f"{type(e).__name__}: {e}; falling back to package path"
        )
        import test_ui as _test_ui_pkg

        return Path(_test_ui_pkg.__file__).parent / "sites.yml"


def _build_command(
    *,
    kind: str,
    run_id: str,
) -> list[str]:
    """Build the argv for `asyncio.create_subprocess_exec`.

    The `python -m test_ui` invocation needs the data-root paths the CLI
    expects. For baseline/current that's `--output <dir>`; for comparator
    it's `--baseline <dir> --current <dir>`; for report it's
    `--comparator-data <dir>`. Defaulting them from `settings` here means
    the request body doesn't have to repeat what's already configured.

    `--sites-file` is set explicitly to the bundled `test_ui/sites.yml`
    because the CLI's relative-path default won't resolve when the
    dashboard is launched from a different cwd than the project root.
    """
    cli_command = _KIND_TO_CLI_COMMAND[kind]
    argv: list[str] = [
        sys.executable,
        "-m",
        "test_ui",
        "--sites-file",
        str(_resolve_sites_file()),
        cli_command,
        "--run-id",
        run_id,
    ]

    if kind == "baseline":
        argv += ["--output", str(settings.baseline_dir)]
    elif kind == "current":
        argv += ["--output", str(settings.current_dir)]
    elif kind == "comparator":
        # The compare command needs the kind-root directories (it walks
        # them via `find_latest_baseline` / `find_latest_current`).
        argv += [
            "--baseline",
            str(settings.baseline_dir),
            "--current",
            str(settings.current_dir),
        ]
    elif kind == "report":
        argv += ["--comparator-data", str(settings.comparator_dir)]

    return argv


async def spawn_run(
    *,
    db_id: int,
    run_id: str,
    kind: str,
    log_path: Path,
    db_path: Path,
) -> asyncio.subprocess.Process:
    """Fork the CLI subprocess, capture PID/PGID, promote pending → running,
    and schedule `_watch`. Returns the spawned `Process` (mostly for tests).

    The caller MUST already have INSERTed a `pending` row with this
    `db_id`+`run_id`. We don't INSERT here because the route handler needs
    to know the `db_id` BEFORE the spawn (so it can return it in the 202
    response) and INSERTing twice would be racy.

    `log_path` is opened in append mode and held by the subprocess as
    stdout+stderr; the watcher closes the FD when the process exits.

    PID/PGID/start-time capture is wrapped in try/except: a subprocess that
    exits between `create_subprocess_exec` and `os.getpgid` would otherwise
    raise `ProcessLookupError` here, leaving the row stuck `pending`. The
    fallback values (`pgid=process.pid`, `start_time=None`) ensure the row
    can still be promoted; recovery's `_pgid_alive_and_ours` will correctly
    refuse to kill an already-dead leader.
    """
    argv = _build_command(kind=kind, run_id=run_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fd = log_path.open("ab")

    # Concurrency cap: hold the semaphore across the spawn-and-track
    # window. Released in the watcher's finally block so a long crawl
    # doesn't hold the slot indefinitely. The acquire CAN block - by
    # design - so an operator queuing 10 runs at once sees them spawn
    # in batches of `AFR_DASHBOARD_MAX_CONCURRENT_RUNS` instead of
    # OOMing the host.
    sem = _get_spawn_semaphore()
    await sem.acquire()
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=log_fd,
            stderr=asyncio.subprocess.STDOUT,
            # New session = new process group; lets restart-recovery
            # SIGTERM the entire tree via os.killpg(pgid, ...) instead
            # of leaking grandchildren (e.g. Playwright's chromium).
            start_new_session=True,
        )
    except Exception:
        # Spawn failed BEFORE we got a process to track; release the
        # semaphore so we don't leak the slot. Subsequent error
        # handling (FileNotFoundError → mark_terminal in the route)
        # is unchanged.
        sem.release()
        log_fd.close()
        raise

    # Race-safe PID/PGID capture. A `/bin/false`-style subprocess can exit
    # between create_subprocess_exec and getpgid - that raises
    # ProcessLookupError on some kernels. Fall back to process.pid (which
    # asyncio cached at spawn time) so the UPDATE still has SOMETHING in
    # the pgid column, and recovery's identity check fails fast.
    try:
        pgid = os.getpgid(process.pid)
    except ProcessLookupError:
        pgid = process.pid
    start_time = pid_start_time(process.pid)
    started_at = settings.get_current_datetime()

    with connection_scope(db_path) as conn:
        promoted = mark_running(
            conn,
            db_id=db_id,
            pid=process.pid,
            pgid=pgid,
            pid_start_time=start_time,
            started_at=started_at,
        )
    if not promoted:
        # Race: _watch already saw the process exit and marked the row
        # terminal before we got here. Don't roll back - _watch's update
        # is the truthful one. Just log so this rare path is visible.
        logger.warning(
            f"runner: db_id={db_id} pending → running UPDATE found row "
            "already terminal; subprocess exited before mark_running landed."
        )

    # Track this PGID in the eager-shutdown set so the lifespan can
    # SIGTERM it on uvicorn restart (instead of leaking it to next
    # startup recovery). Removed by the watcher when the process exits.
    _active_pgids.add(pgid)

    # Strong-ref the watcher task so asyncio's weak-ref-only tracking
    # can't garbage-collect it mid-execution (PEP / asyncio docs).
    # Pass the semaphore so the watcher releases it on exit.
    task = asyncio.create_task(
        _watch(
            process,
            db_id=db_id,
            db_path=db_path,
            log_fd=log_fd,
            pgid=pgid,
            semaphore=sem,
        )
    )
    _track_watcher(task)
    return process


async def _watch(
    process: asyncio.subprocess.Process,
    *,
    db_id: int,
    db_path: Path,
    log_fd,
    pgid: int | None = None,
    semaphore: asyncio.Semaphore | None = None,
) -> None:
    """Await the subprocess, close the log FD, write the terminal status.

    Status mapping:
      - exit 0       → done
      - exit < 0     → interrupted (signal-killed; signal-driven
                       termination is operator-initiated, not a bug)
      - exit > 0     → failed (with `error` set to "exit code N")
      - cancelled    → interrupted (with `error="watcher cancelled"`); the
                       subprocess is detached from the watcher but
                       `start_new_session=True` means its PGID survives
                       and the next startup recovery will SIGTERM it.

    Both the log FD close AND the terminal-status DB write run in `finally`
    so a `CancelledError` (raised when the lifespan tears down or the
    asyncio task is cancelled) doesn't strand the row in `running` until
    the next dashboard restart. The CancelledError still propagates after
    the cleanup runs.
    """
    exit_code: int | None = None
    cancelled = False
    try:
        exit_code = await process.wait()
    except asyncio.CancelledError:
        cancelled = True
        # Don't re-raise yet - let finally write the row first, then
        # propagate. (asyncio.CancelledError is the one BaseException-
        # derived class we MUST re-raise so the task is correctly marked
        # cancelled in the asyncio bookkeeping.)
    finally:
        try:
            log_fd.close()
        except Exception:
            pass  # log close is best-effort; nothing actionable

        # Drop PGID from the eager-shutdown set + release the semaphore
        # slot. Both safe to call unconditionally (discard is idempotent;
        # release() balances the spawn-side acquire). The watcher OWNS
        # both - exit MUST clean them up so the next spawn slot frees
        # up and the next graceful-shutdown doesn't try to SIGTERM a
        # gone PGID. Wrapped in try/except so a failure here can't
        # prevent the DB write below.
        if pgid is not None:
            _active_pgids.discard(pgid)
        if semaphore is not None:
            try:
                semaphore.release()
            except ValueError:
                # Over-release - would happen if some test or future
                # refactor double-calls _watch on the same task. Log
                # rather than crash the watcher.
                logger.warning(f"runner: db_id={db_id} semaphore over-release ignored")

        if cancelled:
            status, error = "interrupted", "watcher cancelled"
        elif exit_code == 0:
            status, error = "done", None
        elif exit_code is not None and exit_code < 0:
            status, error = "interrupted", f"killed by signal {-exit_code}"
        else:
            status, error = "failed", f"exit code {exit_code}"

        finished_at = settings.get_current_datetime()
        try:
            with connection_scope(db_path) as conn:
                updated = mark_terminal(
                    conn,
                    db_id=db_id,
                    status=status,
                    finished_at=finished_at,
                    exit_code=exit_code,
                    error=error,
                )
            if not updated:
                # Already terminal - restart-recovery beat us (e.g.
                # dashboard restarted between subprocess exit and watcher
                # rescheduling). The recovery's status is the truthful one.
                logger.info(
                    f"runner: db_id={db_id} _watch found row already terminal; "
                    f"would have set {status}/exit={exit_code}."
                )
        except Exception as e:
            # Don't let a transient DB error prevent CancelledError from
            # propagating, and don't crash the event loop with the
            # exception either. Log and let the next startup recovery
            # observe a still-running row.
            logger.error(
                f"runner: db_id={db_id} terminal UPDATE failed: {type(e).__name__}: {e}"
            )

    if cancelled:
        raise asyncio.CancelledError()


# ---------------------------------------------------------------------------
# Startup recovery.
# ---------------------------------------------------------------------------


def recover_orphaned_runs(
    db_path: Path,
    *,
    killer: Callable[[int], None] = _kill_pgid,
) -> int:
    """At dashboard startup, mark every pending/running row as `interrupted`,
    killing the underlying PGID first IF it's still alive AND has the same
    leader start-time we recorded at spawn (defending against PID recycling).

    Returns the number of rows transitioned to `interrupted`.

    `killer` is parameterized so tests can swap in a fake that records
    calls without actually shooting at PIDs. The default sends real
    signals - only ever invoked at startup, so the blast radius is the
    one previous run's tree.

    Killer failures are caught per-row so one rogue PGID can't abort
    recovery for the rest. The `interrupted` status still gets written
    even when the kill fails (the OS-level cleanup leaks but the
    DB-level state is consistent for the operator).

    All UPDATEs run inside one BEGIN IMMEDIATE / COMMIT so recovery is
    atomic - either all rows transition or none do, even if the dashboard
    crashes mid-loop.

    Intentionally synchronous so the lifespan can run it inside the same
    `asyncio.to_thread` block as `init_db` + `sync_runs`.
    """
    finished_at = settings.get_current_datetime()
    interrupted = 0
    with connection_scope(db_path) as conn:
        active = find_active_runs(conn)
        if not active:
            return 0
        # `BEGIN IMMEDIATE` takes the write lock up front so concurrent
        # writers (e.g. a route handler) can't interleave with recovery.
        # autocommit mode (isolation_level=None) means we have to issue
        # this explicitly - see db.py for the rationale.
        conn.execute("BEGIN IMMEDIATE")
        try:
            for row in active:
                db_id = int(row["id"])
                pgid = row["pgid"]
                pid_start = row["pid_start_time"]

                if pgid is not None:
                    try:
                        pgid_int = int(pgid)
                    except (TypeError, ValueError):
                        pgid_int = None
                    if pgid_int is not None and _pgid_alive_and_ours(
                        pgid_int, pid_start
                    ):
                        logger.warning(
                            f"runner: orphan PGID {pgid_int} for db_id={db_id} "
                            "is still alive; SIGTERM (then SIGKILL after 5s)."
                        )
                        try:
                            killer(pgid_int)
                        except Exception as e:
                            # Per-row defense - one rogue process can't
                            # abort recovery for the rest. Log so the
                            # operator can investigate the leaked PGID.
                            logger.error(
                                f"runner: killer(pgid={pgid_int}) raised "
                                f"{type(e).__name__}: {e}; "
                                "marking row interrupted anyway."
                            )
                    elif pgid_int is not None:
                        logger.info(
                            f"runner: orphan PGID {pgid_int} for db_id={db_id} "
                            "is gone or recycled; not killing."
                        )

                mark_terminal(
                    conn,
                    db_id=db_id,
                    status="interrupted",
                    finished_at=finished_at,
                    exit_code=None,
                    error="dashboard restarted",
                )
                interrupted += 1
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return interrupted


def shutdown_active_subprocesses(*, grace_seconds: float = 5.0) -> int:
    """Eagerly SIGTERM every PGID we're tracking. Returns the count.

    Called from the lifespan's shutdown side. Without this, uvicorn's
    SIGTERM cancels the watcher tasks (which propagate CancelledError
    cleanly and mark the row interrupted), but the OS subprocesses
    survive - they're in a separate process group thanks to
    `start_new_session=True`. The next dashboard startup's recovery
    pass would then SIGTERM them after a 5s grace, leaking N Playwright
    processes for that window.

    Eager shutdown closes that gap: signal first, let recovery clean
    up the DB rows separately. Returns the number of PGIDs signaled
    so the lifespan can log a meaningful message.

    Sync, takes the same `_kill_pgid` helper recovery uses, runs in
    `asyncio.to_thread` from the lifespan to avoid blocking the loop
    during shutdown.
    """
    pgids = list(_active_pgids)
    for pgid in pgids:
        try:
            _kill_pgid(pgid, grace_seconds=grace_seconds)
        except Exception as e:
            logger.warning(
                f"runner: shutdown SIGTERM(pgid={pgid}) raised "
                f"{type(e).__name__}: {e} - continuing with the rest"
            )
        _active_pgids.discard(pgid)
    return len(pgids)


__all__ = [
    "pid_start_time",
    "recover_orphaned_runs",
    "shutdown_active_subprocesses",
    "spawn_run",
]
