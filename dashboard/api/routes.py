"""Route composition entrypoint.

Phase 5 decomposition keeps `sites` handlers here (tests monkeypatch
`dashboard.api.routes._sites_path`) and moves other domains to focused modules:
- `routes_runs.py`
- `routes_reports.py`
- `routes_health.py`

`get_db` is re-exported from `routes_common.py` for dependency override tests.
"""

from __future__ import annotations

from importlib.resources import as_file, files
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response
from pydantic import ValidationError

from test_ui.common.sites import (
    SiteNotFound,
    add_site,
    bulk_add_sites,
    bulk_delete_sites,
    delete_site,
    load_sites,
    update_site,
)

from .models import (
    SiteBulkCreateIn,
    SiteBulkDeleteIn,
    SiteBulkDeleteOut,
    SiteCreateIn,
    SiteOut,
    SiteUpdateIn,
)
from .routes_common import get_db
from .routes_health import health_router
from .routes_reports import reports_router
from .routes_runs import runs_router


sites_router = APIRouter(prefix="/api/sites", tags=["sites"])


def _sites_path() -> Path:
    """Resolve the live `test_ui/sites.yml` path on every call."""
    resource = files("test_ui") / "sites.yml"
    with as_file(resource) as p:
        return Path(str(p))


@sites_router.get("", response_model=list[SiteOut])
def get_sites() -> list[SiteOut]:
    """Read sites from `test_ui/sites.yml`."""
    sites_path = _sites_path()
    if not sites_path.exists():
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
    """Append a new site."""
    sites_path = _sites_path()
    try:
        site = add_site(sites_path, name=payload.name, url=payload.url)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors(include_context=False)) from e
    return SiteOut(id=site.id, name=site.name, url=site.url)


@sites_router.post(
    "/bulk",
    response_model=list[SiteOut],
    status_code=201,
    responses={
        422: {"description": "At least one URL failed validation; nothing was written"},
    },
)
def post_sites_bulk(payload: SiteBulkCreateIn) -> list[SiteOut]:
    """Append multiple sites at once. ids + names auto-generated."""
    sites_path = _sites_path()
    try:
        sites = bulk_add_sites(sites_path, payload.urls)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors(include_context=False)) from e
    return [SiteOut(id=s.id, name=s.name, url=s.url) for s in sites]


@sites_router.patch(
    "/{site_id}",
    response_model=SiteOut,
    responses={
        404: {"description": "No site with this id"},
        422: {"description": "Body validation failed"},
    },
)
def patch_site(site_id: str, payload: SiteUpdateIn) -> SiteOut:
    """Mutate name and/or url. id is immutable."""
    sites_path = _sites_path()
    try:
        site = update_site(sites_path, site_id, name=payload.name, url=payload.url)
    except SiteNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors(include_context=False)) from e
    return SiteOut(id=site.id, name=site.name, url=site.url)


@sites_router.post(
    "/bulk-delete",
    response_model=SiteBulkDeleteOut,
    responses={
        422: {"description": "Body validation failed (empty list, etc.)"},
    },
)
def post_sites_bulk_delete(payload: SiteBulkDeleteIn) -> SiteBulkDeleteOut:
    """Remove multiple sites from sites.yml in a single atomic write."""
    sites_path = _sites_path()
    deleted, skipped = bulk_delete_sites(sites_path, payload.ids)
    return SiteBulkDeleteOut(deleted=deleted, skipped_not_found=skipped)


@sites_router.delete(
    "/{site_id}",
    status_code=204,
    responses={404: {"description": "No site with this id"}},
)
def delete_site_route(site_id: str) -> Response:
    """Remove a site from sites.yml."""
    sites_path = _sites_path()
    try:
        delete_site(sites_path, site_id)
    except SiteNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return Response(status_code=204)


__all__ = [
    "sites_router",
    "runs_router",
    "health_router",
    "reports_router",
    "get_db",
    "_sites_path",
]
