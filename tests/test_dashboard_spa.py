"""SPA static-asset serving (Phase C.3).

The dashboard's Python module mounts `dashboard/web/dist` so the
production deployment serves both the React SPA and the JSON API
from the same origin. Pin the wiring:

  - `/api/*` MUST take priority over the SPA catch-all (route order)
  - `/<spa-route>` MUST return index.html (so React Router routes
    survive a hard refresh)
  - `/assets/*.js` MUST serve the hashed bundle when present
  - Missing dist/ MUST be a no-op, not a startup crash (tests don't
    build the SPA; a bare backend serves API only)
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard.api.main import create_app
from dashboard.api.spa import mount_spa


def _seed_dist(tmp_path: Path) -> Path:
    """Build a minimal fake `dist/` tree: index.html + assets/foo.js."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><html><body>SPA shell</body></html>",
        encoding="utf-8",
    )
    (dist / "assets" / "bundle.js").write_text(
        "console.log('hi from bundle')",
        encoding="utf-8",
    )
    (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    return dist


@pytest.fixture
def spa_client(tmp_path, monkeypatch):
    """A TestClient over a fresh app with `dist/` redirected to a tmp path.

    Bypasses the lifespan (no DB init needed for SPA tests) by using
    TestClient WITHOUT the `with` context manager.
    """
    dist = _seed_dist(tmp_path)
    monkeypatch.setattr("dashboard.api.spa._spa_dist_dir", lambda: dist)
    app = create_app(dev_mode=False)
    return TestClient(app, backend_options={"use_uvloop": True})


# --------------------------------------------------------------------------- #
# Catch-all serves index.html for SPA routes                                  #
# --------------------------------------------------------------------------- #


def test_root_serves_index_html(spa_client):
    r = spa_client.get("/")
    assert r.status_code == 200
    assert "SPA shell" in r.text
    assert r.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize(
    "spa_path",
    ["/sites", "/runs", "/runs/123", "/reports", "/anything/deeply/nested"],
)
def test_spa_routes_serve_index_html(spa_client, spa_path):
    """React Router needs index.html on a hard refresh of any client-side
    route. The catch-all returns the SPA shell so React Router takes over."""
    r = spa_client.get(spa_path)
    assert r.status_code == 200
    assert "SPA shell" in r.text


def test_assets_serve_bundle(spa_client):
    r = spa_client.get("/assets/bundle.js")
    assert r.status_code == 200
    assert "bundle" in r.text


def test_favicon_serves_when_present(spa_client):
    r = spa_client.get("/favicon.svg")
    assert r.status_code == 200
    assert "<svg/>" in r.text


# --------------------------------------------------------------------------- #
# API routes take priority over the catch-all                                 #
# --------------------------------------------------------------------------- #


def test_api_health_NOT_intercepted_by_spa_catchall(spa_client):
    """If route ordering is wrong, /api/health would return index.html.
    This is the load-bearing test that pins the API/SPA priority."""
    r = spa_client.get("/api/health")
    # Health route returns JSON, NOT the SPA shell.
    assert r.headers.get("content-type", "").startswith("application/json")


def test_unknown_api_path_returns_404_not_spa_shell(spa_client):
    """Defense-in-depth: even if a request slips past the API routes
    (unmatched path under /api/), the catch-all explicitly returns 404
    rather than the SPA shell - otherwise the React app would try to
    render a route from what the client thought was an API call."""
    r = spa_client.get("/api/nonexistent-route")
    assert r.status_code == 404
    assert "SPA shell" not in r.text


def test_root_api_path_returns_404_not_spa_shell(spa_client):
    """The literal path `/api` (no trailing slash) is also rejected -
    not strictly an API call but unambiguously not a real SPA route."""
    r = spa_client.get("/api")
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Missing dist is a no-op (the normal test/dev posture)                       #
# --------------------------------------------------------------------------- #


def test_mount_spa_is_noop_when_dist_missing(tmp_path, monkeypatch):
    """No build → SPA serving silently disabled. Operator gets the API
    only, no module-import failure. Pytest depends on this - the test
    pipeline never runs `npm build`."""
    monkeypatch.setattr("dashboard.api.spa._spa_dist_dir", lambda: tmp_path / "no-such")
    app = create_app(dev_mode=False)
    client = TestClient(app, backend_options={"use_uvloop": True})

    # API still works.
    r = client.get("/openapi.json")
    assert r.status_code == 200

    # No SPA shell - `/` gets the FastAPI default (which is a 404 since
    # we don't define a `/` route ourselves).
    r = client.get("/")
    assert r.status_code == 404


def test_mount_spa_raises_on_half_built_dist(tmp_path):
    """`dist/` exists but `index.html` is missing - likely an interrupted
    build. Better to fail loud at startup than half-mount and serve
    confusing 404s for every SPA route."""
    half = tmp_path / "half-dist"
    (half / "assets").mkdir(parents=True)
    # Deliberately do NOT create index.html.
    app = create_app(dev_mode=False)
    # mount_spa called explicitly - bypasses the create_app's silent
    # mount so we can observe the raise.
    import dashboard.api.spa as spa_module

    orig = spa_module._spa_dist_dir
    spa_module._spa_dist_dir = lambda: half
    try:
        with pytest.raises(RuntimeError, match="rebuild the SPA"):
            mount_spa(app)
    finally:
        spa_module._spa_dist_dir = orig
