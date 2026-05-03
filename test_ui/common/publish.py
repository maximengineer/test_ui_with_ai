"""Atomic run-directory publication (Phase B.1.3).

A run writes everything into `<date>/.tmp-<run_id>/` while in progress.
Only on clean completion does it rename to `<date>/<run_id>/`. Half-written
runs are therefore never visible at their final path — `find_latest_*`
walking `<date>/` sees only complete runs (modulo filtering out `.tmp-*` /
`.lock` / `latest` symlink entries).

The rename is atomic on the same filesystem, which is guaranteed here
because both `<date>/.tmp-<run_id>` and `<date>/<run_id>` are children of
the same parent. POSIX rename(2) won't tear.

If the body raises, the tmp directory is left in place. Callers should
write a `status="failed"` manifest into it first (so post-mortem tools can
see what happened); the directory's `.tmp-` prefix keeps it out of the
"latest" candidate set.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def tmp_dir_for(date_dir: Path, run_id: str) -> Path:
    """The conventional path for a run-in-progress: `<date_dir>/.tmp-<run_id>`."""
    return date_dir / f".tmp-{run_id}"


def final_dir_for(date_dir: Path, run_id: str) -> Path:
    """The conventional path for a completed run: `<date_dir>/<run_id>`."""
    return date_dir / run_id


@contextmanager
def atomic_run_dir(date_dir: Path, run_id: str) -> Iterator[Path]:
    """Context manager: create `.tmp-<run_id>`, yield it, rename on clean exit.

    Usage:
        with atomic_run_dir(date_dir, run_id) as run_root:
            start_manifest(run_root, kind, run_id)
            ... write outputs into run_root / url_dir / ...
            complete_manifest(run_root, url_count=N)

    On exception, the tmp dir is left in place (not promoted, not deleted)
    so the caller can write a failed-state manifest and humans can inspect.
    Future cleanup tools can sweep `.tmp-*` dirs older than N days.
    """
    tmp = tmp_dir_for(date_dir, run_id)
    final = final_dir_for(date_dir, run_id)

    if final.exists():
        # ULID collisions are statistically impossible (80-bit randomness),
        # so this means a programming error — explicit reuse of a run_id.
        raise FileExistsError(f"run already published at {final}")
    if tmp.exists():
        # Same logic — a previous run with this id either crashed or we're
        # being asked to use a duplicate id. Refuse rather than overwrite.
        raise FileExistsError(f"in-progress run dir already exists at {tmp}")

    tmp.mkdir(parents=True, exist_ok=False)
    try:
        yield tmp
    except Exception:
        # Leave tmp/ for debugging. Caller is responsible for the failed
        # manifest write (we don't want to assume the manifest module here
        # because that would create a circular import once locks land).
        raise
    else:
        # Belt-and-braces: scrub any leftover `.lock` so it can never appear
        # inside a published run dir. Normally `acquire_lock`'s context exit
        # already removed it; this guards against future callers that forget
        # the lock context entirely. Hardcoded to avoid importing locks.py
        # (would invert the dependency direction).
        leftover_lock = tmp / ".lock"
        if leftover_lock.exists():
            leftover_lock.unlink()
        tmp.rename(final)


__all__ = ["tmp_dir_for", "final_dir_for", "atomic_run_dir"]
