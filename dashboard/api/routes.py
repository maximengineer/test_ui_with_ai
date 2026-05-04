"""HTTP route handlers (Phase C.1).

Read routes:    GET    /api/sites, /api/dates, /api/runs, /api/runs/{db_id},
                       /api/runs/{db_id}/logs, /api/health
Mutation:       POST   /api/sync, /api/runs, /api/runs/{db_id}/retry

Conventions:
  - Resource sub-routers (`sites_router`, `runs_router`) so a future split
    into per-resource files is mechanical (no import-cycle work).
  - DB connections come from a FastAPI dependency for the read paths, but
    the job-runner routes (which need to interleave async subprocess
    spawning with sync DB writes) open their own short-lived scopes -
    safer than holding a Connection across `await` boundaries.
  - Response models on every route - documents what we return AND lets
    FastAPI strip extra fields from the row dict before serialization.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from importlib.resources import as_file, files
from pathlib import Path
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Response
from loguru import logger

from test_ui.common.manifest import read_manifest
from test_ui.common.preconditions import PreconditionFailed, require_complete_run
from test_ui.common.run_id import is_valid_run_id, new_run_id
from test_ui.common.sites import (
    SiteNotFound,
    add_site,
    delete_site,
    load_sites,
    update_site,
)
from test_ui.config import settings

from . import runner
from .db import (
    connection_scope,
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
    HealthOut,
    ReportRunRequest,
    ReportSummaryOut,
    ReportUrlDetail,
    ReportUrlSummary,
    ReportUrlsOut,
    RunListOut,
    RunRequest,
    RunRow,
    RunSpawnedOut,
    SiteCreateIn,
    SiteOut,
    SiteUpdateIn,
    SyncOut,
)
from .sync import sync_runs


# --------------------------------------------------------------------------- #
# DB dependency.                                                             #
# --------------------------------------------------------------------------- #


def get_db() -> sqlite3.Connection:
    """FastAPI dependency: yield a connection per request.

    Implemented as a generator so FastAPI runs the `finally` close after
    the response is sent. Tests override this via `app.dependency_overrides`
    to swap in a tmp-path DB without touching `settings.runs_db_path`.
    """
    if settings.runs_db_path is None:
        # `Settings._fill_path_defaults` guarantees this is set, so reaching
        # here means someone unset it explicitly. Hard-fail with context
        # rather than letting `connection_scope(None)` raise a TypeError.
        # (Plain `assert` would be stripped under `python -O`.)
        raise RuntimeError(
            "settings.runs_db_path is None - Settings._fill_path_defaults "
            "did not run, or was overridden after construction."
        )
    with connection_scope(settings.runs_db_path) as conn:
        yield conn


DbDep = Annotated[sqlite3.Connection, Depends(get_db)]


# --------------------------------------------------------------------------- #
# Helpers - row → wire model conversion.                                     #
# --------------------------------------------------------------------------- #


def _row_to_runrow(row: sqlite3.Row) -> RunRow:
    """Project a `runs` SQLite row into the wire model.

    Parses `args_json` / `command_json` here so route handlers don't repeat
    it. Defensive: if a row's JSON is unparseable, log and substitute
    empty values rather than 500ing - a corrupt args field shouldn't
    take down the runs list.
    """
    try:
        args = json.loads(row["args_json"]) if row["args_json"] else {}
    except json.JSONDecodeError:
        logger.warning(f"runs.id={row['id']}: corrupt args_json, substituting {{}}")
        args = {}
    try:
        command = json.loads(row["command_json"]) if row["command_json"] else []
    except json.JSONDecodeError:
        logger.warning(f"runs.id={row['id']}: corrupt command_json, substituting []")
        command = []

    return RunRow(
        id=row["id"],
        run_id=row["run_id"],
        kind=row["kind"],
        status=row["status"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        date_dir=row["date_dir"],
        args=args,
        command=command,
        exit_code=row["exit_code"],
        error=row["error"],
        pid=row["pid"],
        pgid=row["pgid"],
        pid_start_time=row["pid_start_time"],
        source=row["source"],
    )


# --------------------------------------------------------------------------- #
# /api/sites                                                                 #
# --------------------------------------------------------------------------- #


sites_router = APIRouter(prefix="/api/sites", tags=["sites"])


# sites.yml is mounted into the container at the same path as the bundled
# test_ui/sites.yml - see docker-compose.yml's `dashboard` service.
#
# Round-2 added an import-time `.exists()` check intended to fail loud on
# zipped-wheel deployments (where `as_file` extracts to a tempfile that
# vanishes on context exit). Round-3 caught a contradiction: the GET
# route tolerates a missing file (`return []`) but the import check
# refused to even start the dashboard. The two policies disagreed.
#
# Resolution: drop the import-time check entirely. The per-route
# behavior is correct on its own:
#   - GET: empty list when the file is absent (operator hasn't
#     configured any sites yet - legitimate state on a fresh install)
#   - POST: `_atomic_write_yaml` creates the file
#   - PATCH/DELETE: 404 when the id can't be found
#
# Zipped-wheel detection is left as a deployment concern - see TODO in
# `_sites_path` for the cache-extraction approach to add when packaging
# matters. Today we only ship as an editable install, so the issue is
# theoretical.
def _sites_path() -> Path:
    """Resolve the live `test_ui/sites.yml` Path on every call.

    Re-resolves each request because `as_file` semantics are
    deployment-mode-dependent and we want the path to reflect the
    current environment (e.g. a test that mounts a different YAML).
    Tests monkeypatch this function to redirect writes to a tmp file.

    TODO(packaging): for a zipped-wheel deployment, `as_file` returns
    a tempfile that's unlinked on context exit; CRUD writes would 500.
    Switch to extracting the bundled YAML to `settings.data_root /
    ".cache" / "sites.yml"` once at startup when that ships.
    """
    resource = files("test_ui") / "sites.yml"
    with as_file(resource) as p:
        return Path(str(p))


@sites_router.get("", response_model=list[SiteOut])
def get_sites() -> list[SiteOut]:
    """Read sites from `test_ui/sites.yml`."""
    sites_path = _sites_path()
    if not sites_path.exists():
        # Empty config is a valid state (no sites configured yet).
        return []
    sites = load_sites(sites_path)
    return [SiteOut(id=s.id, name=s.name, url=s.url) for s in sites]


@sites_router.post(
    "",
    response_model=SiteOut,
    status_code=201,
    responses={
        422: {"description": "Body validation failed"},
    },
)
def post_sites(payload: SiteCreateIn) -> SiteOut:
    """Append a new site. The id is auto-generated server-side from the
    slugified name with a `-N` suffix on collision (so id-conflict 409s
    are structurally impossible - see `add_site`). Atomic write
    preserves operator-authored YAML comments.
    """
    sites_path = _sites_path()
    site = add_site(sites_path, name=payload.name, url=payload.url)
    return SiteOut(id=site.id, name=site.name, url=site.url)


@sites_router.patch(
    "/{site_id}",
    response_model=SiteOut,
    responses={
        404: {"description": "No site with this id"},
        422: {"description": "Body validation failed"},
    },
)
def patch_site(site_id: str, payload: SiteUpdateIn) -> SiteOut:
    """Mutate name and/or url. id is immutable - use DELETE + POST to rename."""
    sites_path = _sites_path()
    try:
        site = update_site(sites_path, site_id, name=payload.name, url=payload.url)
    except SiteNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return SiteOut(id=site.id, name=site.name, url=site.url)


@sites_router.delete(
    "/{site_id}",
    status_code=204,
    responses={404: {"description": "No site with this id"}},
)
def delete_site_route(site_id: str) -> Response:
    """Remove a site from sites.yml. On-disk per-site data dirs are NOT
    touched - historical artifacts remain readable; only future runs stop
    including this site."""
    sites_path = _sites_path()
    try:
        delete_site(sites_path, site_id)
    except SiteNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return Response(status_code=204)


# --------------------------------------------------------------------------- #
# /api/dates + /api/runs                                                     #
# --------------------------------------------------------------------------- #


runs_router = APIRouter(prefix="/api", tags=["runs"])


def _is_valid_date_dir(name: str) -> bool:
    """True iff `name` is a parseable DD-MM-YYYY date.

    Uses `datetime.strptime` so we reject not just non-shapes ("not-a-date",
    "latest") but also impossible calendar dates ("32-13-2099", "00-00-0000").
    Without this, the round-1 regex-only check would pass impossible dates
    through to the frontend's date picker, where clicking them does nothing.
    """
    try:
        datetime.strptime(name, settings.date_format)
    except ValueError:
        return False
    return True


def _list_date_dirs(root: Path | None) -> list[str]:
    """Return DD-MM-YYYY dir names under `root`, newest first.

    Filters:
      - non-dirs
      - dot-prefixed entries (`.tmp-*` workspaces shouldn't end up here
        but cheap to defend against)
      - anything not parseable as a real DD-MM-YYYY date (`_is_valid_date_dir`)

    FileNotFoundError / PermissionError on `iterdir()` are caught (TOCTOU
    between `exists` and iteration is real on network mounts and during
    operator cleanup).
    """
    if root is None or not root.exists():
        return []
    try:
        entries = list(root.iterdir())
    except (FileNotFoundError, PermissionError) as e:
        logger.warning(f"dates: cannot list {root}: {type(e).__name__}: {e}")
        return []
    dates = [
        p.name
        for p in entries
        if p.is_dir() and not p.name.startswith(".") and _is_valid_date_dir(p.name)
    ]

    # DD-MM-YYYY isn't lexically sortable - convert to YYYY-MM-DD for sort.
    # `_is_valid_date_dir` guarantees split() yields exactly 3 elements
    # corresponding to a real date, so no try/except needed here.
    def _key(s: str) -> str:
        d, m, y = s.split("-")
        return f"{y}-{m}-{d}"

    return sorted(dates, key=_key, reverse=True)


@runs_router.get("/dates", response_model=DatesOut)
def get_dates() -> DatesOut:
    return DatesOut(
        baseline=_list_date_dirs(settings.baseline_dir),
        current=_list_date_dirs(settings.current_dir),
        comparator=_list_date_dirs(settings.comparator_dir),
        report=_list_date_dirs(settings.report_dir),
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
    return RunListOut(items=[_row_to_runrow(r) for r in rows], total=total)


@runs_router.get("/runs/{db_id}", response_model=RunRow)
def get_run_by_id(db_id: int, conn: DbDep) -> RunRow:
    row = get_run(conn, db_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"run id={db_id} not found")
    return _row_to_runrow(row)


# --------------------------------------------------------------------------- #
# /api/sync - manual re-sync (also runs at startup).                         #
# --------------------------------------------------------------------------- #


@runs_router.post("/sync", response_model=SyncOut)
def post_sync(conn: DbDep) -> SyncOut:
    scanned, inserted = sync_runs(conn)
    return SyncOut(scanned=scanned, synced=inserted)


# --------------------------------------------------------------------------- #
# Job runner: POST /api/runs, retry, logs                                    #
# --------------------------------------------------------------------------- #


def _kind_root_for_precondition(kind: str) -> Path | None:
    """Map a request kind to the kind root that `require_complete_run` walks.

    Comparator needs BOTH baseline and current to be complete; report needs
    a complete comparator. Baseline / current have no precondition (they
    produce the seed data). Returns None if the precondition doesn't apply
    so the caller can skip the check cleanly.
    """
    return {
        "comparator": None,  # special-cased below; needs TWO checks
        "report": settings.comparator_dir,
    }.get(kind)


def _check_workflow_preconditions(req: RunRequest, *, date: str) -> None:
    """Raise `PreconditionFailed` if the upstream artifacts aren't ready.

    Thin wrapper around `test_ui.common.preconditions.require_complete_run`
    that knows the dashboard's per-kind dependency graph:
      - baseline / current: no precondition.
      - comparator: needs complete baseline AND complete current for `date`.
      - report:     needs complete comparator for `date`.
    """
    if isinstance(req, ComparatorRunRequest):
        require_complete_run(settings.baseline_dir, date, kind_label="baseline")
        require_complete_run(settings.current_dir, date, kind_label="current")
    elif isinstance(req, ReportRunRequest):
        require_complete_run(settings.comparator_dir, date, kind_label="comparator")
    # baseline / current - no precondition.


def _resolve_date_for_request(req: RunRequest) -> str:
    """Pick the date_dir this run will live under.

    For now the rule is dead-simple: today's date in the configured timezone.
    The plan reserves request-body `date` overrides for retry/replay flows
    that aren't in the MVP - when those land, this function is the single
    place to thread the override through.
    """
    # ReportRunRequest has an optional `date`; honor it so an operator can
    # generate a report for an older comparator run without time-travel.
    if isinstance(req, ReportRunRequest) and req.date is not None:
        return req.date
    return settings.get_current_date()


async def _spawn_run_for_request(request: RunRequest) -> RunSpawnedOut:
    """The shared body of `POST /api/runs` and `POST /api/runs/{id}/retry`.

    Both routes funnel here so a future middleware / dependency added to
    `post_runs` doesn't get silently bypassed by `post_run_retry` calling
    `post_runs` directly. The route handlers stay thin HTTP shells; the
    lifecycle (idempotency → precondition → INSERT → spawn → response)
    lives here.

    Lifecycle:
      1. Resolve target date (today, unless ReportRunRequest.date is set).
      2. Idempotency: 409 if a pending/running row exists for kind+date.
      3. Workflow precondition: 412 if the upstream isn't complete.
      4. Pre-allocate a ULID (so we can return it in the 202 response).
      5. INSERT pending row.
      6. `runner.spawn_run`: fork subprocess, mark_running, schedule _watch.
      7. Return 202.
    """
    if settings.runs_db_path is None:
        raise RuntimeError("settings.runs_db_path is None - Settings init failed")
    db_path = settings.runs_db_path

    date = _resolve_date_for_request(request)

    # 2. Idempotency check.
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

    # 3. Workflow precondition.
    try:
        _check_workflow_preconditions(request, date=date)
    except PreconditionFailed as e:
        raise HTTPException(status_code=412, detail=str(e)) from e

    # 4-6. Pre-allocate, INSERT, spawn.
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
    # We INSERT with an empty command_json and let the spawn build the
    # actual argv. Pinning the argv into the row at insert time would
    # require computing it in two places - accepting the small wire-vs-DB
    # lag instead (the row's `command` is empty until the watcher fires).

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
        # `python` interpreter or test_ui module not on PATH. Mark the row
        # failed so the operator sees the error rather than a stuck pending
        # row. mark_terminal's WHERE NOT IN (terminal) clause matches
        # 'pending' so the UPDATE fires; pid/pgid stay NULL (which is fine,
        # we never had them).
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
    """Spawn a new run as a subprocess. Returns 202 with the assigned ids."""
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
    """Re-spawn a run with the same args as `db_id`.

    Reads `args_json` off the original row + reconstructs the matching
    `RunRequest` subclass; from there it's the same path as `POST /api/runs`,
    so 409 / 412 still apply (a retry of an already-running comparison
    correctly conflicts).
    """
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

    # Reconstruct the typed request from `args` + the row's kind. The kind
    # in the row is authoritative - args may be missing it (older rows
    # discovered from manifests have empty args).
    args = {**args, "kind": row["kind"]}
    request = _request_from_dict(args)
    # Funnel into the SHARED helper, NOT into `post_runs` directly. Calling
    # the route handler directly would bypass any future middleware /
    # `Depends(...)` added to `post_runs`. The helper has the same body
    # so idempotency / precondition still apply.
    return await _spawn_run_for_request(request)


def _request_from_dict(payload: dict) -> RunRequest:
    """Dispatch a raw dict to the right RunRequest subclass.

    Pydantic's discriminated union does this automatically when it's the
    parameter type on a route handler, but for retry we have a dict in
    hand and need to do it explicitly.
    """
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


# Cap log responses at 1 MB so a stuck route can't OOM the dashboard
# rendering a 10 GB Playwright trace. 1 MB matches the plan.
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
    """Stream the subprocess log file. Always capped at 1 MB even without
    `tail` to prevent runaway responses.

    Returns 404 if the row doesn't exist OR if the log file hasn't been
    created yet (the spawn opens it lazily - a row in `pending` for less
    than a few ms may have no log).
    """
    if settings.runs_db_path is None:
        raise RuntimeError("settings.runs_db_path is None - Settings init failed")
    with connection_scope(settings.runs_db_path) as conn:
        row = get_run(conn, db_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"run id={db_id} not found")

    log_path = settings.runs_log_dir / f"{db_id}.log"
    # Path-traversal defense: db_id is typed `int` by FastAPI, so it can't
    # carry a `..`; re-resolve and assert containment as a defense-in-depth
    # check anyway, in case future callers expand the parameter type.
    resolved = log_path.resolve()
    log_root = settings.runs_log_dir.resolve()
    if not resolved.is_relative_to(log_root):
        raise HTTPException(status_code=400, detail="invalid log path")

    if not resolved.exists():
        raise HTTPException(status_code=404, detail=f"no log for run id={db_id}")

    # Read up to the requested byte count from the END of the file. Default
    # is the full file capped at _MAX_LOG_BYTES.
    cap = min(tail, _MAX_LOG_BYTES) if tail is not None else _MAX_LOG_BYTES
    size = resolved.stat().st_size
    seek_to = max(0, size - cap)
    with resolved.open("rb") as f:
        f.seek(seek_to)
        data = f.read(cap)

    return Response(content=data, media_type="text/plain; charset=utf-8")


# --------------------------------------------------------------------------- #
# /api/health                                                                #
# --------------------------------------------------------------------------- #


health_router = APIRouter(prefix="/api", tags=["health"])


@health_router.get("/health", response_model=HealthOut)
def get_health() -> HealthOut:
    """Liveness check. Never raises - degraded states surface as `False`s.

    Does NOT use `DbDep`: if the DB itself can't be opened (disk full,
    permission denied on the WAL file), the dependency would raise BEFORE
    the handler runs and FastAPI would 500. We want the failure to
    surface as `db_ok=False` instead - that's the entire point of having
    a health route.

    The AI analyzer probe is bounded to 2s so a hung analyzer container
    can't make `/api/health` itself appear hung. The overall `ok` mirrors
    `db_ok` only - the analyzer being down doesn't invalidate the dashboard
    (it just disables AI-dependent UI affordances client-side).
    """
    db_ok = False
    if settings.runs_db_path is not None:
        try:
            with connection_scope(settings.runs_db_path) as probe:
                probe.execute("SELECT 1").fetchone()
                db_ok = True
        except Exception as e:
            logger.warning(f"health: DB probe failed: {type(e).__name__}: {e}")

    ai_ok = False
    try:
        with httpx.Client(timeout=2.0) as client:
            resp = client.get(f"{settings.ai_analyzer_service_url}/health")
            ai_ok = resp.status_code == 200
    except Exception:
        # Network error, DNS failure, timeout - all map to "analyzer not
        # reachable from the dashboard right now". Not logged at WARN
        # because /api/health gets polled.
        ai_ok = False

    return HealthOut(ok=db_ok, db_ok=db_ok, ai_analyzer_ok=ai_ok)


# --------------------------------------------------------------------------- #
# Reports drill-in (Phase C.2 second slice).                                  #
# --------------------------------------------------------------------------- #
#
# All four routes resolve to subpaths of `settings.report_dir`. Path-
# traversal defense is layered:
#   1. `date` parameter validated as DD-MM-YYYY via `_is_valid_date_dir`.
#   2. `run_id` parameter validated as a real ULID via `is_valid_run_id`.
#   3. `url_id` (where applicable) validated against the actual on-disk
#      directory listing - we don't trust the client to pick a real id.
#   4. Resolved paths are confined to `settings.report_dir.resolve()` via
#      `is_relative_to` as a final defense (catches symlink escapes too).
# Any layer failing yields 400 (malformed) or 404 (missing); never a 500.


reports_router = APIRouter(prefix="/api/reports", tags=["reports"])


# Mutually-exclusive per-URL result files. Mirrors
# `test_ui.report.loader.RESULT_FILENAMES` in priority order - the first
# present file wins. Centralized here so the routes don't import from
# the loader (which would pull in the AI client + jinja deps).
_RESULT_FILES: tuple[tuple[str, str], ...] = (
    ("ai_analysis.json", "analysis_success"),
    ("ai_error.json", "analysis_error"),
    ("no_changes.json", "no_changes"),
    ("ai_disabled.json", "ai_disabled"),
)


def _resolve_report_run_dir(date: str, run_id: str) -> Path:
    """Validate `date`+`run_id` and return the absolute report run dir.

    Raises HTTPException(400) on malformed inputs and HTTPException(404)
    if the dir doesn't exist. Confines the resolved path to
    `settings.report_dir` as final defense against any traversal that
    survives the per-component validations.
    """
    if not _is_valid_date_dir(date):
        raise HTTPException(status_code=400, detail=f"invalid date {date!r}")
    if not is_valid_run_id(run_id):
        raise HTTPException(status_code=400, detail=f"invalid run_id {run_id!r}")
    if settings.report_dir is None:
        raise RuntimeError("settings.report_dir is None")
    root = settings.report_dir.resolve()
    run_dir = (root / date / run_id).resolve()
    if not run_dir.is_relative_to(root):
        raise HTTPException(status_code=400, detail="path escapes report root")
    if not run_dir.is_dir():
        raise HTTPException(
            status_code=404, detail=f"no report run for {date}/{run_id}"
        )
    return run_dir


def _resolve_url_dir(run_dir: Path, url_id: str) -> Path:
    """Validate `url_id` against the actual run dir's children + return path.

    Refuses to trust the client's url_id at all - instead we list the run
    dir's subdirectories (each one is a real per-URL artifact dir) and
    only allow url_ids that match a real dir name. This makes traversal
    attacks impossible AND surfaces a typo as 404 with a clear message.
    """
    # is_dir() check on each child filters out the per-run files like
    # `aggregated_analysis.json` and `enhanced_report.html`.
    valid_ids = {p.name for p in run_dir.iterdir() if p.is_dir()}
    if url_id not in valid_ids:
        raise HTTPException(
            status_code=404, detail=f"no url_id={url_id!r} in this report"
        )
    return run_dir / url_id


def _classify_url_dir(url_dir: Path) -> tuple[str, dict | None]:
    """Pick the highest-priority result file in `url_dir`. Returns
    `(result_type, parsed_dict_or_None)`. Returns ('unknown', None) if
    no result file is present (defensive - shouldn't happen for a
    cleanly-published run).

    The four result files are mutually exclusive by writer contract
    (see `test_ui.report.loader.write_result_file`). If we observe more
    than one present, log a WARNING - silently picking the first-by-
    priority would mask the data corruption.
    """
    matches: list[tuple[str, str]] = [
        (filename, result_type)
        for filename, result_type in _RESULT_FILES
        if (url_dir / filename).exists()
    ]
    if len(matches) > 1:
        logger.warning(
            f"reports: {url_dir.name} has multiple mutually-exclusive "
            f"result files {[m[0] for m in matches]}; picking "
            f"highest-priority ({matches[0][0]}). The writer contract "
            "guarantees these are mutually exclusive - investigate."
        )
    if not matches:
        return "unknown", None
    filename, result_type = matches[0]
    try:
        return result_type, json.loads((url_dir / filename).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(
            f"reports: corrupt {filename} at {url_dir / filename}: "
            f"{type(e).__name__}: {e}"
        )
        return result_type, None


def _list_screenshots(url_dir: Path) -> list[str]:
    """Which of baseline/current/visual_diff PNGs exist for this URL."""
    screens_dir = url_dir / "screenshots"
    if not screens_dir.is_dir():
        return []
    out: list[str] = []
    for kind in ("baseline", "current", "visual_diff"):
        if (screens_dir / f"{kind}.png").is_file():
            out.append(kind)
    return out


@reports_router.get(
    "/{date}/{run_id}",
    response_model=ReportSummaryOut,
    responses={
        400: {"description": "Malformed date or run_id"},
        404: {"description": "No report for this date+run_id"},
    },
)
def get_report_summary(date: str, run_id: str) -> ReportSummaryOut:
    """Top-level metadata: manifest fields + per-result-type counts."""
    run_dir = _resolve_report_run_dir(date, run_id)
    try:
        manifest = read_manifest(run_dir)
    except Exception as e:
        # The directory exists but the manifest is missing/corrupt.
        # That's a published-run integrity problem, not a 404 - surface
        # as 500 with a useful message rather than synthesizing data.
        raise HTTPException(
            status_code=500,
            detail=f"manifest unreadable for {date}/{run_id}: {e}",
        ) from e

    counts: dict[str, int] = {}
    for url_dir in run_dir.iterdir():
        if not url_dir.is_dir():
            continue
        result_type, payload = _classify_url_dir(url_dir)
        counts[result_type] = counts.get(result_type, 0) + 1
        # For analysis_success, also bucket by overall_severity so the
        # frontend can show a CRITICAL/WARNING/SAFE breakdown without
        # fetching every per-URL detail.
        if result_type == "analysis_success" and isinstance(payload, dict):
            sev = payload.get("overall_severity")
            if isinstance(sev, str):
                counts[sev] = counts.get(sev, 0) + 1

    return ReportSummaryOut(
        date=date,
        run_id=run_id,
        started_at=manifest.started_at,
        finished_at=manifest.finished_at,
        url_count=manifest.url_count,
        severity_counts=counts,
    )


@reports_router.get(
    "/{date}/{run_id}/urls",
    response_model=ReportUrlsOut,
    responses={
        400: {"description": "Malformed date or run_id"},
        404: {"description": "No report for this date+run_id"},
    },
)
def get_report_urls(date: str, run_id: str) -> ReportUrlsOut:
    """List of URLs in the report with their result_type + severity."""
    run_dir = _resolve_report_run_dir(date, run_id)
    items: list[ReportUrlSummary] = []
    for url_dir in sorted(run_dir.iterdir(), key=lambda p: p.name):
        if not url_dir.is_dir():
            continue
        result_type, payload = _classify_url_dir(url_dir)
        severity: str | None = None
        url: str | None = None
        if isinstance(payload, dict):
            sev = payload.get("overall_severity")
            if isinstance(sev, str):
                severity = sev
            # `url` lives at the top of ai_analysis.json / structured_data.json
            # depending on which result file we have. Best-effort grab.
            u = payload.get("url")
            if isinstance(u, str):
                url = u
        items.append(
            ReportUrlSummary(
                url_id=url_dir.name,
                # Cast: result_type is one of the literal values from
                # _RESULT_FILES + 'unknown', all of which are in
                # ReportResultType. Pydantic re-validates anyway.
                result_type=result_type,  # type: ignore[arg-type]
                severity=severity,
                url=url,
            )
        )
    return ReportUrlsOut(items=items)


@reports_router.get(
    "/{date}/{run_id}/url",
    response_model=ReportUrlDetail,
    responses={
        400: {"description": "Malformed date / run_id / url_id"},
        404: {"description": "No such url_id in this report"},
    },
)
def get_report_url_detail(
    date: str,
    run_id: str,
    id: Annotated[str, Query(description="The url_id (per-site directory name)")],
) -> ReportUrlDetail:
    """Per-URL detail: AI analysis verbatim + structured_data + screenshot
    inventory (the actual bytes come from the SCREENSHOT route)."""
    run_dir = _resolve_report_run_dir(date, run_id)
    url_dir = _resolve_url_dir(run_dir, id)
    result_type, analysis = _classify_url_dir(url_dir)

    structured: dict | None = None
    structured_path = url_dir / "structured_data.json"
    if structured_path.exists():
        try:
            structured = json.loads(structured_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            structured = None

    return ReportUrlDetail(
        url_id=id,
        result_type=result_type,  # type: ignore[arg-type]
        analysis=analysis or {},
        structured_data=structured,
        screenshots=_list_screenshots(url_dir),  # type: ignore[arg-type]
    )


# Map the wire `which=` enum to the on-disk filename. Centralized so a
# typo in the wire ↔ disk mapping shows up in one place.
_SCREENSHOT_KINDS: dict[str, str] = {
    "baseline": "baseline.png",
    "current": "current.png",
    "diff": "visual_diff.png",
}


@reports_router.get(
    "/{date}/{run_id}/screenshot",
    responses={
        200: {"content": {"image/png": {}}},
        304: {"description": "Not modified - client's If-None-Match matched"},
        400: {"description": "Malformed parameters"},
        404: {"description": "No such url_id, or no screenshot of that kind"},
    },
)
def get_report_screenshot(
    date: str,
    run_id: str,
    url_id: Annotated[str, Query()],
    which: Annotated[Literal["baseline", "current", "diff"], Query()],
    if_none_match: Annotated[str | None, Header(alias="if-none-match")] = None,
) -> Response:
    """Return one PNG (baseline / current / diff) for one URL.

    Query-param typing on `which` is FastAPI-enforced (a different value
    yields 422). `url_id` is validated against the actual on-disk
    children, so a bogus value can't traverse OR enumerate.
    """
    run_dir = _resolve_report_run_dir(date, run_id)
    url_dir = _resolve_url_dir(run_dir, url_id)
    filename = _SCREENSHOT_KINDS[which]  # safe - Literal narrows the input
    path = (url_dir / "screenshots" / filename).resolve()

    # Defense-in-depth: confine to the report root even after url_id
    # validation. Symlinks inside the run dir are unlikely but not
    # impossible if an operator hand-crafted the structure.
    if settings.report_dir is None:
        raise RuntimeError("settings.report_dir is None")
    if not path.is_relative_to(settings.report_dir.resolve()):
        raise HTTPException(status_code=400, detail="path escapes report root")
    if not path.is_file():
        raise HTTPException(
            status_code=404, detail=f"no {which} screenshot for {url_id}"
        )

    # ETag based on file mtime (ns precision) so the browser revalidates
    # on overwrite. Round-2 added the header; round-3 caught that emit-
    # only wasn't enough - without the conditional check below, every
    # revalidation request would still send the full PNG bytes back.
    # `cache-control: no-cache` forces revalidation; if `If-None-Match`
    # matches our current ETag, we 304 with no body and the browser
    # serves from its cache without re-downloading.
    stat = path.stat()
    etag = f'W/"{stat.st_mtime_ns}-{stat.st_size}"'
    if if_none_match is not None and if_none_match == etag:
        # Standard conditional-GET response: 304, no body, but we still
        # echo the ETag + Cache-Control so the cached entry's metadata
        # stays current (per RFC 7232 §4.1).
        return Response(
            status_code=304,
            headers={"etag": etag, "cache-control": "no-cache"},
        )
    return Response(
        content=path.read_bytes(),
        media_type="image/png",
        headers={"etag": etag, "cache-control": "no-cache"},
    )


__all__ = [
    "sites_router",
    "runs_router",
    "health_router",
    "reports_router",
    "get_db",
]
