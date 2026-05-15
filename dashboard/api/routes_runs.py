"""Runs/date/sync/logs route domain module."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Query, Response
from loguru import logger

from test_ui.common.preconditions import PreconditionFailed, require_complete_run
from test_ui.common.run_id import new_run_id
from test_ui.config import settings

from . import runner
from .db import (
    RunNotDeletable,
    connection_scope,
    delete_run,
    find_active_run_for_kind_date,
    get_run,
    insert_pending_run,
    list_runs,
)
from .models import (
    BaselineRunRequest,
    ComparatorRunRequest,
    CurrentRunRequest,
    DatesOut,
    ReportRunRequest,
    RunBulkDeleteIn,
    RunBulkDeleteOut,
    RunListOut,
    RunRequest,
    RunRow,
    RunSpawnedOut,
    SyncOut,
)
from .routes_common import DbDep, date_dir_has_published_run, list_date_dirs, row_to_runrow
from .sync import sync_runs


runs_router = APIRouter(prefix="/api", tags=["runs"])


@runs_router.get("/dates", response_model=DatesOut)
def get_dates() -> DatesOut:
    return DatesOut(
        baseline=list_date_dirs(settings.baseline_dir),
        current=list_date_dirs(settings.current_dir),
        comparator=list_date_dirs(settings.comparator_dir),
        report=list_date_dirs(settings.report_dir),
    )


@runs_router.get("/runs", response_model=RunListOut)
def get_runs(
    conn: DbDep,
    kind: Annotated[str | None, Query(description="Filter by kind")] = None,
    status: Annotated[str | None, Query(description="Filter by status")] = None,
    date_dir: Annotated[
        str | None,
        Query(
            description=(
                "Filter to runs whose date_dir matches (DD-MM-YYYY). "
                "Lets the Reports page list runs for a single date without "
                "client-side truncation."
            ),
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RunListOut:
    """List runs, newest first. `total` reflects the filtered set."""
    rows, total = list_runs(
        conn, kind=kind, status=status, date_dir=date_dir, limit=limit, offset=offset
    )
    return RunListOut(items=[row_to_runrow(r) for r in rows], total=total)


@runs_router.get("/runs/{db_id}", response_model=RunRow)
def get_run_by_id(db_id: int, conn: DbDep) -> RunRow:
    row = get_run(conn, db_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"run id={db_id} not found")
    return row_to_runrow(row)


@runs_router.post("/sync", response_model=SyncOut)
def post_sync(conn: DbDep) -> SyncOut:
    scanned, inserted = sync_runs(conn)
    return SyncOut(scanned=scanned, synced=inserted)


def _check_workflow_preconditions(req: RunRequest, *, date: str) -> None:
    """Raise `PreconditionFailed` if the upstream artifacts aren't ready."""
    if isinstance(req, ComparatorRunRequest):
        require_complete_run(settings.baseline_dir, date, kind_label="baseline")
        require_complete_run(settings.current_dir, date, kind_label="current")
    elif isinstance(req, ReportRunRequest):
        require_complete_run(settings.comparator_dir, date, kind_label="comparator")


def _resolve_date_for_request(req: RunRequest) -> str:
    """Pick the date_dir this run will live under."""
    if isinstance(req, ReportRunRequest) and req.date is not None:
        return req.date
    return settings.get_current_date()


async def _spawn_run_for_request(request: RunRequest) -> RunSpawnedOut:
    """Shared run spawn lifecycle used by POST /runs and POST /runs/{id}/retry."""
    if settings.runs_db_path is None:
        raise RuntimeError("settings.runs_db_path is None - Settings init failed")
    db_path = settings.runs_db_path

    date = _resolve_date_for_request(request)

    with connection_scope(db_path) as conn:
        existing = find_active_run_for_kind_date(conn, kind=request.kind, date_dir=date)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"a {request.kind} run for {date} is already "
                    f"{existing['status']} (db_id={existing['id']}, "
                    f"run_id={existing['run_id']})"
                ),
                "existing_db_id": existing["id"],
                "existing_run_id": existing["run_id"],
            },
        )

    try:
        _check_workflow_preconditions(request, date=date)
    except PreconditionFailed as e:
        raise HTTPException(status_code=412, detail=str(e)) from e

    run_id = new_run_id()
    with connection_scope(db_path) as conn:
        db_id = insert_pending_run(
            conn,
            run_id=run_id,
            kind=request.kind,
            args=request.model_dump(),
            command=[],
            date_dir=date,
            created_at=settings.get_current_datetime(),
        )

    log_path = settings.runs_log_dir / f"{db_id}.log"
    try:
        await runner.spawn_run(
            db_id=db_id,
            run_id=run_id,
            kind=request.kind,
            log_path=log_path,
            db_path=db_path,
        )
    except FileNotFoundError as e:
        with connection_scope(db_path) as conn:
            from .db import mark_terminal

            mark_terminal(
                conn,
                db_id=db_id,
                status="failed",
                finished_at=settings.get_current_datetime(),
                exit_code=None,
                error=f"spawn failed: {e}",
            )
        raise HTTPException(
            status_code=500, detail=f"failed to spawn subprocess: {e}"
        ) from e

    return RunSpawnedOut(db_id=db_id, run_id=run_id, status="running")


@runs_router.post(
    "/runs",
    response_model=RunSpawnedOut,
    status_code=202,
    responses={
        409: {"description": "Conflicting in-flight run for the same kind+date"},
        412: {"description": "Workflow precondition not met (upstream not complete)"},
    },
)
async def post_runs(
    request: Annotated[RunRequest, Body(discriminator="kind")],
) -> RunSpawnedOut:
    """Spawn a new run as a subprocess. Returns 202 with assigned ids."""
    return await _spawn_run_for_request(request)


@runs_router.post(
    "/runs/{db_id}/retry",
    response_model=RunSpawnedOut,
    status_code=202,
    responses={
        404: {"description": "No such db_id"},
        409: {"description": "Conflicting in-flight run"},
        412: {"description": "Workflow precondition not met"},
    },
)
async def post_run_retry(db_id: int) -> RunSpawnedOut:
    """Re-spawn a run with the same args as `db_id`."""
    if settings.runs_db_path is None:
        raise RuntimeError("settings.runs_db_path is None - Settings init failed")
    db_path = settings.runs_db_path

    with connection_scope(db_path) as conn:
        row = get_run(conn, db_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"run id={db_id} not found")

    try:
        args = json.loads(row["args_json"]) if row["args_json"] else {}
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=500,
            detail=f"original row id={db_id} has corrupt args_json: {e}",
        ) from e

    args = {**args, "kind": row["kind"]}
    request = _request_from_dict(args)
    return await _spawn_run_for_request(request)


def _cleanup_run_artifacts(row: sqlite3.Row) -> None:
    """Best-effort removal of on-disk artifacts for a deleted run row."""
    import shutil

    kind = row["kind"]
    date_dir = row["date_dir"]
    run_id = row["run_id"]
    db_id = row["id"]

    kind_root: Path | None = {
        "baseline": settings.baseline_dir,
        "current": settings.current_dir,
        "comparator": settings.comparator_dir,
        "report": settings.report_dir,
    }.get(kind)

    if kind_root is not None and date_dir and run_id:
        run_root = kind_root / date_dir / run_id
        if run_root.exists():
            try:
                shutil.rmtree(run_root)
            except OSError as e:
                logger.warning(
                    f"delete: rmtree({run_root}) failed: {type(e).__name__}: {e}"
                )
        _prune_empty_date_dir(kind_root / date_dir)

    if settings.runs_log_dir is not None:
        for path in (
            settings.runs_log_dir / f"{db_id}.log",
            settings.runs_log_dir / f"{run_id}.run.json",
        ):
            try:
                path.unlink(missing_ok=True)
            except OSError as e:
                logger.warning(
                    f"delete: unlink({path}) failed: {type(e).__name__}: {e}"
                )


def _prune_empty_date_dir(date_root: Path) -> None:
    """Remove `<kind>/<date>/latest` and date dir if no runs remain."""
    if not date_root.exists() or not date_root.is_dir():
        return
    if date_dir_has_published_run(date_root):
        return
    import shutil as _shutil

    try:
        for entry in date_root.iterdir():
            try:
                if entry.is_symlink() or entry.is_file():
                    entry.unlink()
                elif entry.is_dir():
                    _shutil.rmtree(entry, ignore_errors=True)
            except OSError as e:
                logger.warning(
                    f"delete: cleanup of {entry} failed: {type(e).__name__}: {e}"
                )
        date_root.rmdir()
    except OSError as e:
        logger.warning(f"delete: prune({date_root}) failed: {type(e).__name__}: {e}")


@runs_router.delete(
    "/runs/{db_id}",
    status_code=204,
    responses={
        404: {"description": "No row with this db_id"},
        409: {"description": "Run is pending/running; refuse to delete in-flight"},
    },
)
def delete_run_route(db_id: int) -> Response:
    """Remove a run: DB row + on-disk artifacts + log file."""
    if settings.runs_db_path is None:
        raise RuntimeError("settings.runs_db_path is None")
    with connection_scope(settings.runs_db_path) as conn:
        try:
            row = delete_run(conn, db_id)
        except RunNotDeletable as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
    if row is None:
        raise HTTPException(status_code=404, detail=f"run id={db_id} not found")
    _cleanup_run_artifacts(row)
    return Response(status_code=204)


@runs_router.post(
    "/runs/bulk-delete",
    response_model=RunBulkDeleteOut,
    responses={
        422: {"description": "Empty db_ids list"},
    },
)
def post_runs_bulk_delete(payload: RunBulkDeleteIn) -> RunBulkDeleteOut:
    """Delete many runs at once; per-id outcomes are returned."""
    if settings.runs_db_path is None:
        raise RuntimeError("settings.runs_db_path is None")
    out = RunBulkDeleteOut()
    with connection_scope(settings.runs_db_path) as conn:
        for db_id in payload.db_ids:
            try:
                row = delete_run(conn, db_id)
            except RunNotDeletable:
                out.skipped_in_flight.append(db_id)
                continue
            if row is None:
                out.skipped_not_found.append(db_id)
                continue
            _cleanup_run_artifacts(row)
            out.deleted.append(db_id)
    return out


def _request_from_dict(payload: dict) -> RunRequest:
    """Dispatch a raw dict to the right RunRequest subclass."""
    kind = payload.get("kind")
    if kind == "baseline":
        return BaselineRunRequest.model_validate(payload)
    if kind == "current":
        return CurrentRunRequest.model_validate(payload)
    if kind == "comparator":
        return ComparatorRunRequest.model_validate(payload)
    if kind == "report":
        return ReportRunRequest.model_validate(payload)
    raise HTTPException(status_code=500, detail=f"unknown kind {kind!r}")


_MAX_LOG_BYTES = 1024 * 1024


@runs_router.get(
    "/runs/{db_id}/logs",
    responses={
        404: {"description": "No row, or no log file yet"},
        200: {"content": {"text/plain": {}}},
    },
)
def get_run_logs(
    db_id: int,
    tail: Annotated[
        int | None,
        Query(
            ge=1,
            le=_MAX_LOG_BYTES,
            description=(
                f"Return only the last N bytes of the log (max {_MAX_LOG_BYTES})."
            ),
        ),
    ] = None,
) -> Response:
    """Stream subprocess log file (capped at 1 MB)."""
    if settings.runs_db_path is None:
        raise RuntimeError("settings.runs_db_path is None - Settings init failed")
    with connection_scope(settings.runs_db_path) as conn:
        row = get_run(conn, db_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"run id={db_id} not found")

    log_path = settings.runs_log_dir / f"{db_id}.log"
    resolved = log_path.resolve()
    log_root = settings.runs_log_dir.resolve()
    if not resolved.is_relative_to(log_root):
        raise HTTPException(status_code=400, detail="invalid log path")

    if not resolved.exists():
        raise HTTPException(status_code=404, detail=f"no log for run id={db_id}")

    cap = min(tail, _MAX_LOG_BYTES) if tail is not None else _MAX_LOG_BYTES
    size = resolved.stat().st_size
    seek_to = max(0, size - cap)
    with resolved.open("rb") as f:
        f.seek(seek_to)
        data = f.read(cap)

    return Response(content=data, media_type="text/plain; charset=utf-8")


__all__ = [
    "runs_router",
    "get_dates",
    "get_runs",
    "get_run_by_id",
    "post_sync",
    "post_runs",
    "post_run_retry",
    "delete_run_route",
    "post_runs_bulk_delete",
    "get_run_logs",
]
