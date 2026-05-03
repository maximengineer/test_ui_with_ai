"""Single-seam run lifecycle (post-B.3 cleanup).

Collapses the 4-place duplication of:

    with atomic_run_dir(date_dir, run_id) as run_root:
        start_manifest(run_root, kind=kind, run_id=run_id, source_run_ids=...)
        with acquire_lock(run_root, command=...):
            try:
                <body>
                complete_manifest(run_root, url_count=...)
            except (KeyboardInterrupt, SystemExit):
                fail_manifest(run_root, status="interrupted", run_id=..., kind=...)
                raise
            except BaseException:
                fail_manifest(run_root, status="failed", run_id=..., kind=...)
                raise

…into one context manager:

    with run_context(date_dir, run_id, kind=kind, command=cmd,
                     source_run_ids=src) as ctx:
        <body>
        ctx.complete(url_count=...)

Pre-cleanup, this boilerplate was duplicated across `crawler/engine.py`,
`comparator/engine.py`, `cli.py:generate_enhanced_report`, and
`cli.py:retry_url`. Each copy was ~25 lines. A bug fix in one (e.g. the
B.2 review's interrupted/failed split) had to be applied in all four.
This module is the new single seam.

**Why a class wrapping the CM rather than just yielding the run_root path:**
the caller needs to call `complete_manifest(run_root, url_count=N)` at the
END of its work — that count isn't known until the body finishes, so it
can't move into the CM. The yielded `RunContext` exposes `.complete(count)`
so the caller's intent ("this run finished cleanly with N items") is
explicit.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .locks import acquire_lock
from .manifest import (
    Kind,
    Manifest,
    complete_manifest,
    fail_manifest,
    start_manifest,
)
from .publish import atomic_run_dir


class RunContext:
    """Yielded by `run_context()`. Tracks the in-flight run's state."""

    def __init__(self, run_root: Path, run_id: str, kind: Kind):
        self.run_root = run_root
        self.run_id = run_id
        self.kind = kind
        self._completed = False

    def complete(self, *, url_count: int) -> Manifest:
        """Mark the run complete with the given url_count + checksum.

        Must be called from inside the `with run_context(...)` block. If the
        block exits without calling this, the manifest stays at status="running"
        and atomic_run_dir does NOT publish (because we raise here).

        Idempotent: a second call short-circuits and returns the previously
        written manifest from disk. Without this guard, calling twice would
        rewrite `finished_at` to the second timestamp and re-hash the
        run dir's files (the hash may shift if the body wrote more files
        between calls), silently producing a different audit trail than
        the operator would expect.
        """
        if self._completed:
            from .manifest import read_manifest

            return read_manifest(self.run_root)
        manifest = complete_manifest(self.run_root, url_count=url_count)
        self._completed = True
        return manifest


@contextmanager
def run_context(
    date_dir: Path,
    run_id: str,
    *,
    kind: Kind,
    command: str,
    source_run_ids: dict[str, str] | None = None,
) -> Iterator[RunContext]:
    """One-stop lifecycle: atomic publish + manifest + lock + failure dispatch.

    Yields a `RunContext` whose `.run_root` is the working `.tmp-<run_id>/`
    directory. The caller does its work into that path, then calls
    `ctx.complete(url_count=N)` to mark success.

    Failure handling:
      - `KeyboardInterrupt` / `SystemExit` → manifest status="interrupted"
      - any other `BaseException`           → manifest status="failed"
      - body forgot to call `.complete(...)` → manifest status="failed"
        (we treat "exited cleanly without explicitly completing" as a bug,
        not as success — atomic_run_dir's publish only happens after a
        successful complete_manifest, so a forgetful caller wouldn't get
        their dir promoted anyway)
      - body raises AFTER calling `.complete()` → the prior "complete" status
        is preserved AND the exception still propagates. This protects the
        run from being silently downgraded when post-success cleanup fails.
        (`atomic_run_dir` will still refuse to promote the tmp dir because
        an exception escaped the body — that's intentional, and the operator
        gets a tmp-<run_id>/ dir with a `complete` manifest to inspect.)
    """
    with atomic_run_dir(date_dir, run_id) as run_root:
        start_manifest(
            run_root, kind=kind, run_id=run_id, source_run_ids=source_run_ids or {}
        )
        with acquire_lock(run_root, command=command):
            ctx = RunContext(run_root, run_id, kind)
            try:
                yield ctx
            except (KeyboardInterrupt, SystemExit):
                # Don't downgrade from "complete" if the body finished its
                # work and only crashed in post-success cleanup.
                if not ctx._completed:
                    fail_manifest(
                        run_root, status="interrupted", run_id=run_id, kind=kind
                    )
                raise
            except BaseException:
                if not ctx._completed:
                    fail_manifest(run_root, status="failed", run_id=run_id, kind=kind)
                raise

            # Body returned without raising. If they forgot to call .complete(),
            # promote that to a failed status (atomic_run_dir won't rename
            # the tmp dir to final unless we exit cleanly here AND complete
            # was called — see _completed check).
            if not ctx._completed:
                fail_manifest(run_root, status="failed", run_id=run_id, kind=kind)
                raise RuntimeError(
                    f"run_context body for {kind} {run_id} exited without "
                    f"calling ctx.complete(url_count=N)"
                )


__all__ = ["RunContext", "run_context"]
