"""Workflow precondition checks for the CLI (Phase B.2.2).

Three guards a user-facing command can apply before doing real work:

  1. `require_complete_run(kind_root, date, kind)` - verifies the latest
     run for the given (kind, date) has `manifest.status == "complete"`.
     Used by `compare` (needs complete baseline + current) and
     `enhanced-report` (needs complete comparator).

  2. `require_no_live_lock(date_dir)` - refuses to start if a sibling
     `.tmp-*/.lock` belongs to a process that's still alive. Used by
     `snapshot` and `current` to prevent two crawls clobbering the same
     kind+date concurrently.

The functions raise `PreconditionFailed` with a human-readable message;
the CLI catches and prints with `console.print(...)` then `click.Abort()`.
Pulling the messages out of the Click commands keeps them testable in
isolation and consistent across commands.
"""

from __future__ import annotations

from pathlib import Path

from .locks import find_live_lock_in_date


class PreconditionFailed(RuntimeError):
    """A user-facing CLI precondition failed. Message is safe to print verbatim."""


def require_complete_run(kind_root: Path, date: str, *, kind_label: str) -> Path:
    """Return the latest complete run dir for `(kind_root, date)`, or raise.

    Resolution order:
      1. Date dir missing → raise with "no runs found".
      2. Newest ULID subdir with `status="complete"` → return it.
      3. Otherwise → raise with the actual status of the newest run, so the
         operator gets a specific hint (re-run, wait, investigate) rather
         than a generic "no complete run".

    `kind_label` is the human-readable name of what we're checking (e.g.
    "baseline", "current", "comparator") - used only for the error message.
    """
    # Imports local to dodge a potential circular (this module is imported
    # by cli.py, which itself pulls in comparator.finder via the Orchestrator).
    from .manifest import read_manifest
    from .run_id import is_valid_run_id

    date_dir = kind_root / date
    if not date_dir.exists():
        raise PreconditionFailed(
            f"No {kind_label} runs found for {date}: {date_dir} does not exist. "
            f"Run the {kind_label} step first."
        )

    # Scan for ULID-named subdirs. Sort descending (ULIDs encode time so
    # lex sort = chronological). Prefer the newest complete run; fall
    # back to reporting the newest *any* run's status if nothing's complete.
    ulid_dirs = sorted(
        (c for c in date_dir.iterdir() if c.is_dir() and is_valid_run_id(c.name)),
        key=lambda p: p.name,
        reverse=True,
    )

    if not ulid_dirs:
        raise PreconditionFailed(
            f"No complete {kind_label} run found for {date} in {date_dir}. "
            "Legacy <date>/<url_dir> artifacts are not supported on read paths. "
            "Run scripts/migrate_run_layout.py first."
        )

    # Walk newest → oldest. Return the first complete one. Track the newest
    # non-complete status for the error message if we find no complete run.
    newest_status: str | None = None
    newest_run_id: str | None = None
    for run_dir in ulid_dirs:
        try:
            manifest = read_manifest(run_dir)
        except FileNotFoundError:
            if newest_status is None:
                newest_status = "missing-manifest"
                newest_run_id = run_dir.name
            continue
        except Exception as e:
            if newest_status is None:
                newest_status = f"corrupt-manifest ({type(e).__name__})"
                newest_run_id = run_dir.name
            continue

        if manifest.status == "complete":
            return run_dir

        if newest_status is None:
            newest_status = manifest.status
            newest_run_id = run_dir.name

    raise PreconditionFailed(
        f"No complete {kind_label} run found for {date} in {date_dir}. "
        f"Newest run {newest_run_id} is status={newest_status!r}. "
        f"{_status_hint(newest_status or '')}"
    )


def _status_hint(status: str) -> str:
    """Friendly nudge for each non-complete status."""
    return {
        "running": "Another process may still be working - check the .lock file inside the run dir.",
        "failed": "The run errored out; inspect the manifest's `details` field if present, then re-run.",
        "interrupted": "The run was Ctrl-C'd; just re-run it.",
        # Synthetic statuses produced by require_complete_run when the
        # manifest itself is unreadable (rare but possible after a crash).
        "missing-manifest": "The run dir exists but has no manifest.json - likely a crashed run. Delete the dir and re-run.",
    }.get(
        status,
        # Catches "corrupt-manifest (...)" and any unexpected literal status.
        "Inspect the run dir manually, then delete it and re-run."
        if status.startswith("corrupt-manifest")
        else f"Unexpected status: {status!r}.",
    )


def require_no_live_lock(date_dir: Path, *, kind_label: str) -> None:
    """Raise if a same-kind+date `.tmp-*/.lock` is held by a live process.

    Stale locks (dead PGID, mismatched /proc starttime) are silently
    ignored - `find_live_lock_in_date` logs a WARNING for each so they
    aren't completely invisible.
    """
    holder = find_live_lock_in_date(date_dir)
    if holder is None:
        return
    lock_path, lock = holder
    raise PreconditionFailed(
        f"Another {kind_label} run is already in progress for this date.\n"
        f"  Lock: {lock_path}\n"
        f"  PID: {lock.pid} (PGID {lock.pgid}) on {lock.hostname}\n"
        f"  Started: {lock.started_at}\n"
        f"  Command: {lock.command}\n"
        f"Wait for it to finish, or kill the process and remove the lock file."
    )


__all__ = [
    "PreconditionFailed",
    "require_complete_run",
    "require_no_live_lock",
]
