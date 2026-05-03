"""Per-run invocation record (Phase B.3.4).

Every run (crawl, comparator, report) writes a small JSON file at
`data/runs/<run_id>.run.json` describing **how it was started** —
the full command, parsed args, kind, and start timestamp.

Why separate from `manifest.json`?
  - manifest.json lives **inside** the run dir (`data/<kind>/<date>/<run_id>/`)
    and describes the *output* (status, file checksum, source provenance).
  - run.json lives **outside** in `data/runs/` and describes the *input*
    (what command did the operator type, what args did the dashboard pass).
  - Future retry: the dashboard reads `<run_id>.run.json` to know how to
    re-fire a failed run with the same parameters.
  - The split lets us keep the manifest canonical even if the run dir gets
    renamed/migrated/archived.

The record is best-effort — failure to write it is logged but doesn't
abort the run (the actual work has already started).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from ..config import settings


# Mirrors the AI/manifest schema versioning so consumers can dispatch on it.
RUN_RECORD_SCHEMA_VERSION = "2026-04-30.1"


class RunRecord(BaseModel):
    """The on-disk shape of `<run_id>.run.json`."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = RUN_RECORD_SCHEMA_VERSION
    run_id: str
    kind: str  # "baseline" / "current" / "comparator" / "report"
    started_at: str  # DD-MM-YYYY HH:MM:SS
    command: list[str]  # sys.argv at invocation
    args: dict[str, Any] = Field(default_factory=dict)  # parsed key/value args


def write_run_record(
    run_id: str,
    kind: str,
    *,
    args: dict[str, Any] | None = None,
    command: list[str] | None = None,
) -> Path:
    """Persist `<runs_log_dir>/<run_id>.run.json`. Returns the written path.

    Best-effort — logs WARNING and returns the would-be path on any I/O
    error so the caller's run isn't aborted by an inability to record
    metadata. The dashboard can detect a missing record by scanning the
    runs/ dir.

    Path is `settings.runs_log_dir` (defaults to `<data_root>/runs/` but
    operators can override via `AFR_RUNS_LOG_DIR` for a separate volume).
    """
    runs_dir = settings.runs_log_dir
    out_path = runs_dir / f"{run_id}.run.json"

    record = RunRecord(
        run_id=run_id,
        kind=kind,
        started_at=settings.get_current_datetime(),
        command=list(command) if command is not None else list(sys.argv),
        args=args or {},
    )
    try:
        runs_dir.mkdir(parents=True, exist_ok=True)
        out_path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
    except OSError as e:
        # Don't propagate — the run itself is more important than the record.
        logger.warning(f"Could not write run record at {out_path}: {e}")
    return out_path


def read_run_record(run_id: str) -> RunRecord | None:
    """Read `<runs_log_dir>/<run_id>.run.json`. Returns None if missing or corrupt."""
    path = settings.runs_log_dir / f"{run_id}.run.json"
    if not path.exists():
        return None
    try:
        return RunRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Corrupt run record at {path}: {type(e).__name__}: {e}")
        return None


__all__ = [
    "RUN_RECORD_SCHEMA_VERSION",
    "RunRecord",
    "write_run_record",
    "read_run_record",
]
