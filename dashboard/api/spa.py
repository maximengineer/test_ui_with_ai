"""SPA static-asset serving (Phase C.3).

In production the dashboard serves both the React SPA and the JSON API
from the same origin, eliminating CORS as a concern. This module owns
that mounting:

  - `/assets/*`  → hashed JS/CSS files emitted by `vite build`
  - `/<anything-else>` → `dist/index.html` (so React Router's
    client-side routes like `/sites` and `/runs` survive a hard refresh)

API routes live under `/api/*` and are registered BEFORE the SPA
catch-all, so they take priority. The Vite dev server (used by
`make dashboard-dev`) bypasses this module entirely — the developer
hits :5173 and Vite proxies `/api` to the backend.

Mounting is INTENTIONALLY OPTIONAL: if no built `dist/` is on disk,
we skip the mount with a single INFO log instead of failing import.
That keeps the Python test suite (which never builds the SPA) happy.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger


def _spa_dist_dir() -> Path:
    """Resolve the React build output dir.

    `dashboard/web/dist` is where Vite emits production builds. Resolved
    relative to this module's __file__ so the path is correct under
    both source-tree dev (Python project root + dashboard/web/dist)
    and the Docker image (/app/dashboard/web/dist after stage-1 copy).
    """
    return Path(__file__).resolve().parent.parent / "web" / "dist"


def mount_spa(app: FastAPI) -> bool:
    """Mount the SPA assets + catch-all on `app`. Returns True iff a
    built `dist/` was present and the mount was installed.

    Skipped quietly if `dist/` doesn't exist — that's the normal state
    in pytest (no `npm run build` in the test pipeline) and during
    `make dashboard-dev` (Vite owns the SPA serving, not FastAPI).

    Order of operations is load-bearing:
      1. `/assets` mounted FIRST so hashed bundle URLs hit the static
         file handler before the catch-all.
      2. The catch-all is registered LAST so /api/* routes (already
         attached) take priority — `app.routes` is checked in order.
    """
    dist = _spa_dist_dir()
    if not dist.is_dir():
        logger.info(
            f"dashboard: no SPA build at {dist}; serving API only "
            "(run `npm --prefix dashboard/web run build` to bundle)."
        )
        return False

    index_html = dist / "index.html"
    if not index_html.is_file():
        # Half-built dist — partial copy, interrupted build. Don't
        # half-mount; fail loud at import so the operator notices.
        raise RuntimeError(
            f"dashboard: {dist} exists but {index_html} is missing — "
            "rebuild the SPA before starting the dashboard."
        )

    # Static files: vite emits hashed bundle names so we can cache
    # aggressively (1 day). The catch-all returning index.html uses
    # no-cache so the SPA can update its asset references.
    app.mount(
        "/assets",
        StaticFiles(directory=dist / "assets"),
        name="spa-assets",
    )

    # Also serve a couple of common root-level files vite may produce
    # (favicon, vite.svg, etc.) directly when present. We DON'T use
    # StaticFiles at "/" because that would shadow the API routes.
    @app.get("/favicon.svg", include_in_schema=False)
    async def _favicon():
        candidate = dist / "favicon.svg"
        if not candidate.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(candidate)

    # Catch-all. Registered LAST so it doesn't shadow /api/*. We can't
    # use a path-converter here that excludes /api/* because FastAPI
    # routes by registration order, not specificity — and /api/* is
    # already in the app by the time mount_spa runs (lifespan
    # constructs the app via create_app, which calls include_router
    # for sites/runs/reports/health BEFORE mount_spa).
    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_catchall(full_path: str, request: Request):
        # Defense-in-depth: anything starting with `/api` should already
        # have been routed by the API handlers. If it lands here, it's
        # an unmatched API path — return 404, not the SPA shell, so the
        # client gets the right error.
        if full_path.startswith("api/") or full_path == "api":
            raise HTTPException(status_code=404)
        # The browser hit /sites or /runs/123 directly — React Router
        # picks up the path on the client side once index.html loads.
        # `request` consumed to silence unused-arg lint while keeping
        # the FastAPI signature shape (signals "this is a route").
        del request
        return FileResponse(
            index_html,
            headers={"cache-control": "no-cache"},
        )

    logger.info(f"dashboard: SPA mounted from {dist}")
    return True


__all__ = ["mount_spa"]
