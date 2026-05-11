"""FastAPI app factory + lifespan (Phase C.1).

Exposes:
  - `create_app(dev_mode: bool | None = None) -> FastAPI` - the factory.
    Pass `dev_mode=True` to force-install the CORS middleware (used by
    tests); leave it None to read `AFR_DASHBOARD_DEV_MODE` from the env.
  - `app` - a module-level instance built via `create_app()`, so
    `uvicorn dashboard.api:app` keeps working without an indirection.

Why the factory: round-1 read `AFR_DASHBOARD_DEV_MODE` at module-import
time. That made the CORS middleware install permanent for the life of
the Python process, no matter what later monkeypatches did to the env
var. The factory lets tests construct fresh `FastAPI` instances per
parametrized case - and verify the middleware actually serves CORS
headers, not just that the helper returns the right boolean.

**Lifespan** runs on startup:
  0. **Linux check** - the dashboard reads `/proc/<pid>/stat` for the
     PID-recycling defense in `runner.py` and has no Mac/Windows
     fallback. Operators on those platforms must run the dashboard
     inside the Linux Docker container (see `Dockerfile.dashboard` /
     `docker-compose.yml`). Hard-fail at startup with a clear pointer
     so a host-native Mac invocation doesn't silently misbehave later.
  1. `init_db` - apply pending migrations (no-op on a fresh schema).
  2. `sync_runs` - backfill `discovered` rows for any on-disk manifest
     not already in the table. Idempotent; safe to run on every start.

**CORS:** in DEV mode (`AFR_DASHBOARD_DEV_MODE=true`) we allow the Vite
dev server at `http://localhost:5173` so the React SPA can hit the API
across origins during development. In production the SPA is served by
FastAPI itself (Phase C.2 deliverable) - same origin, no CORS - so the
middleware is omitted. Defaulting CORS off in production keeps a stray
browser on the operator's network from being able to call the API even
if they discover its address.

**Bind:** uvicorn's --host flag controls binding. The plan calls for
127.0.0.1 by default with an explicit `AFR_DASHBOARD_BIND` opt-in for
LAN - enforced by `dashboard/api/__main__.py` (the `python -m
dashboard.api` entry point), not here. This module just constructs the
ASGI app; whoever serves it picks the bind address.
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from test_ui.config import settings

from .db import connection_scope, init_db
from .routes import health_router, reports_router, runs_router, sites_router
from .runner import recover_orphaned_runs, shutdown_active_subprocesses
from .spa import mount_spa
from .sync import sync_runs


def _is_dev_mode() -> bool:
    """Read AFR_DASHBOARD_DEV_MODE at call time (not import time).

    Pulled out so a test can flip the env var with monkeypatch and the
    next app construction picks it up. Treats empty / unset / "false" /
    "0" as off; anything else as on.
    """
    val = os.environ.get("AFR_DASHBOARD_DEV_MODE", "").strip().lower()
    return val not in ("", "0", "false", "no")


def _require_linux() -> None:
    """Hard-fail on non-Linux. The runner reads /proc and has no fallback.

    Mac/Windows operators run the dashboard via Docker; the message points
    them there. Pulled out of `_startup_sync` so a test that wants to
    exercise the rest of the lifespan can monkeypatch this to a no-op
    without simulating /proc on the host.
    """
    if sys.platform != "linux":
        raise RuntimeError(
            f"dashboard requires Linux (got sys.platform={sys.platform!r}). "
            "Run inside the Docker container - see docker-compose.yml's "
            "`dashboard` service or `make dashboard-dev` (which uses the "
            "container in non-Linux environments)."
        )


def _startup_sync(db_path: Path) -> tuple[int, int, int]:
    """Synchronous wrapper around platform check + init_db + recover + sync.

    Runs in `asyncio.to_thread` so a slow filesystem (NFS, large historical
    tree) can't block the asyncio event loop and delay uvicorn's "ready".

    Order matters:
      0. `_require_linux` - fail fast if /proc isn't available; everything
         downstream assumes it.
      1. `init_db` - must run first so the runs table exists for recovery.
      2. `recover_orphaned_runs` - marks any pending/running rows from the
         previous dashboard instance as `interrupted` (and SIGTERMs their
         PGIDs if still alive). MUST run before sync so a restart-during-
         crawl doesn't leave a row in `running` AND have sync mark the
         on-disk manifest as a separate `discovered` row for the same
         run_id (UNIQUE conflict skip would drop sync's row, but the
         operator would still see the stale `running` status).
      3. `sync_runs` - backfills discovered rows for any on-disk manifest
         not already in the table.

    Returns (scanned, inserted, recovered).
    """
    _require_linux()
    init_db(db_path)
    recovered = recover_orphaned_runs(db_path)
    with connection_scope(db_path) as conn:
        scanned, inserted = sync_runs(conn)
    return scanned, inserted, recovered


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: migrate DB + backfill discovered runs. Shutdown: nothing yet.

    The DB init + sync runs in a worker thread via `asyncio.to_thread` so
    a slow filesystem (NFS, large historical tree) can't block the asyncio
    event loop and delay uvicorn's "ready" signal past health-check
    deadlines. The work itself is unchanged; only the scheduling is.

    Failures here are intentionally fatal - uvicorn will refuse to serve
    if the DB can't be opened. A dashboard with no DB is more confusing
    than a dashboard that doesn't start.
    """
    if settings.runs_db_path is None:
        # Plain `assert` would be stripped under `python -O`. Hard-fail.
        raise RuntimeError(
            "settings.runs_db_path is None - Settings._fill_path_defaults "
            "did not run, or was overridden after construction."
        )
    db_path = settings.runs_db_path
    logger.info(f"dashboard: initializing DB at {db_path}")

    scanned, inserted, recovered = await asyncio.to_thread(_startup_sync, db_path)
    if recovered:
        logger.warning(
            f"dashboard: recovered {recovered} orphaned run(s) from prior "
            "instance (marked interrupted)."
        )
    logger.info(
        f"dashboard: startup sync - {scanned} manifests scanned, "
        f"{inserted} new rows inserted"
    )

    yield

    # Shutdown side. Round-3 milestone-review HIGH #2 caught that we
    # were leaking N Playwright Chromiums for the 5s window between
    # uvicorn SIGTERM and the next startup-recovery's SIGTERM cascade.
    # Eager SIGTERM here - the watcher tasks then observe the
    # subprocess exit and cleanly mark the row `interrupted` via the
    # existing CancelledError handling in `_watch`.
    #
    # to_thread because `_kill_pgid` blocks for up to 5s waiting for
    # SIGTERM to take effect before escalating to SIGKILL; doing that
    # on the event loop would prevent the watcher tasks from observing
    # the subprocess exit and updating the DB.
    killed = await asyncio.to_thread(shutdown_active_subprocesses)
    if killed:
        logger.info(f"dashboard: shutdown - SIGTERM'd {killed} active subprocess(es)")


def create_app(*, dev_mode: bool | None = None) -> FastAPI:
    """Construct and return a fresh FastAPI app.

    `dev_mode=None` (the default) consults `AFR_DASHBOARD_DEV_MODE`. Tests
    pass an explicit boolean to bypass env-var leakage between cases.

    DEV-only CORS rationale: in production the SPA is served by FastAPI
    itself (same-origin, no CORS needed); the absence of the middleware
    there is a deliberate security posture, not an oversight. We DON'T
    use `["*"]` even in dev because allow_credentials=True with wildcard
    origin is forbidden by the spec - being explicit avoids a cryptic
    browser error if auth is added later.
    """
    if dev_mode is None:
        dev_mode = _is_dev_mode()

    app = FastAPI(
        title="AI Frontend Regression Dashboard",
        description=(
            "Local-first dashboard for managing UI regression runs. "
            "See dashboard/README.md for layout, ARCHITECTURE.md for the "
            "system tour."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    if dev_mode:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        logger.info("dashboard: dev mode - Vite CORS rules active")

    app.include_router(sites_router)
    app.include_router(runs_router)
    app.include_router(reports_router)
    app.include_router(health_router)

    # SPA catch-all goes LAST. `mount_spa` is a no-op when no built
    # `dashboard/web/dist` is on disk (the test pipeline never builds
    # the SPA - pytest just exercises the API routes). Production
    # Dockerfile.dashboard's stage 1 emits the bundle into the right
    # path; uvicorn picks it up via this mount.
    mount_spa(app)
    return app


# Module-level instance for uvicorn (`uvicorn dashboard.api:app`). Built
# once at import; tests that need a different config call `create_app()`
# directly instead of mutating this one.
app = create_app()


__all__ = ["app", "create_app", "lifespan"]
