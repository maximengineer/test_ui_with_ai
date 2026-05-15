"""Dashboard FastAPI backend (Phase C.1).

Public surface:
  - `app` - the FastAPI ASGI app; importable as `dashboard.api:app` for
    uvicorn (e.g. `uvicorn dashboard.api:app --reload`).

Submodules:
  - `db`     - SQLite connection helpers, schema, migrations
  - `models` - Pydantic request/response shapes (the wire contract)
  - `sync`   - backfill the `runs` table from on-disk `manifest.json`
  - `routes` - HTTP route handlers, grouped by resource
  - `main`   - app factory; assembles the router + lifespan

**Import discipline for new submodules:** import sibling modules by their
relative names (`from .db import ...`), NEVER from `dashboard.api`
itself. The package re-exports `app` from `.main`, so a sibling that
imports from the package would pull `main.py` (and its full router
graph) into the dependency cycle - a circular-import waiting to happen.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI


def __getattr__(name: str):
    """Lazy-export `app` so package import stays side-effect-light.

    `from dashboard.api import db` should not bootstrap FastAPI (which mounts
    SPA assets and emits logs). Uvicorn's `dashboard.api:app` access still
    works because attribute lookup triggers this hook.
    """
    if name == "app":
        from .main import app

        return app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if TYPE_CHECKING:
    app: FastAPI

__all__ = ["app"]
