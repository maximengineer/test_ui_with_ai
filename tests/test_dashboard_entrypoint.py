"""Entry-point + dev-mode toggle tests (Phase C.1 review fixes).

`dashboard.api.__main__._resolve_port` and `dashboard.api.main._is_dev_mode`
are tiny but load-bearing - they sit at the seam between the operator's
env-var input and the running server. A bad port or unexpected truthy
value here turns into a confusing crash or a quietly-wide-open CORS
policy. Pin both.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from dashboard.api.__main__ import _resolve_port
from dashboard.api.main import _is_dev_mode, _require_linux, create_app


# --------------------------------------------------------------------------- #
# AFR_DASHBOARD_PORT                                                         #
# --------------------------------------------------------------------------- #


def test_resolve_port_unset_returns_default(monkeypatch):
    monkeypatch.delenv("AFR_DASHBOARD_PORT", raising=False)
    assert _resolve_port() == 8080


def test_resolve_port_empty_string_returns_default(monkeypatch):
    """Empty env var (`export AFR_DASHBOARD_PORT=`) is the same as unset -
    common operator footgun where they `unset` half-heartedly."""
    monkeypatch.setenv("AFR_DASHBOARD_PORT", "")
    assert _resolve_port() == 8080


def test_resolve_port_valid_int_returned(monkeypatch):
    monkeypatch.setenv("AFR_DASHBOARD_PORT", "9090")
    assert _resolve_port() == 9090


@pytest.mark.parametrize("bad", ["abc", "8080.5", "0x1f90", "  ", "8080a"])
def test_resolve_port_non_int_exits(monkeypatch, bad):
    """Non-integer strings must hard-fail with SystemExit(2), not raise an
    unhandled ValueError that prints a traceback as the operator's first
    impression."""
    monkeypatch.setenv("AFR_DASHBOARD_PORT", bad)
    with pytest.raises(SystemExit) as exc:
        _resolve_port()
    assert exc.value.code == 2


@pytest.mark.parametrize("bad", ["0", "-1", "65536", "999999"])
def test_resolve_port_out_of_range_exits(monkeypatch, bad):
    """Ports outside 1..65535 must hard-fail before uvicorn produces a
    confusing 'cannot assign requested address' error."""
    monkeypatch.setenv("AFR_DASHBOARD_PORT", bad)
    with pytest.raises(SystemExit) as exc:
        _resolve_port()
    assert exc.value.code == 2


# --------------------------------------------------------------------------- #
# AFR_DASHBOARD_DEV_MODE - drives whether CORS middleware is installed.      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", ["true", "True", "1", "yes", "on"])
def test_is_dev_mode_truthy_values(monkeypatch, value):
    monkeypatch.setenv("AFR_DASHBOARD_DEV_MODE", value)
    assert _is_dev_mode() is True


@pytest.mark.parametrize("value", ["", "0", "false", "False", "no"])
def test_is_dev_mode_falsy_values(monkeypatch, value):
    """Unset, empty, and the standard boolean-false strings must all be
    treated as off. Critical because dev mode loosens CORS - defaulting
    on would be a security regression."""
    monkeypatch.setenv("AFR_DASHBOARD_DEV_MODE", value)
    assert _is_dev_mode() is False


def test_is_dev_mode_unset_is_false(monkeypatch):
    """Production posture: unset env var must be safe-by-default."""
    monkeypatch.delenv("AFR_DASHBOARD_DEV_MODE", raising=False)
    assert _is_dev_mode() is False


# --------------------------------------------------------------------------- #
# CORS wiring - proves the middleware is ACTUALLY installed (or not) based   #
# on the dev_mode flag, not just that the helper returns the right boolean. #
# --------------------------------------------------------------------------- #
#
# Round 1 added an env-toggle helper but the `if _is_dev_mode():` ran at
# module import - so flipping the env in a test couldn't change CORS state.
# Round 2 introduced `create_app(dev_mode=...)` precisely so these tests
# are possible. Without them, a refactor that broke the CORS branch would
# only surface once the React SPA started seeing CORS errors in dev.


def _cors_origin_for(dev_mode: bool) -> str | None:
    """Build a fresh app + send a CORS preflight; return the value of the
    `access-control-allow-origin` response header, or None if the header
    is absent (i.e. the middleware did not install the rule).

    Deliberately does NOT use TestClient as a context manager - that would
    trigger the lifespan, which calls `init_db(settings.runs_db_path)` and
    would create `data/dashboard.db` as a test side-effect. CORS preflight
    is handled by starlette middleware BEFORE any route handler (and thus
    before any DB access), so skipping the lifespan is safe here.
    """
    app = create_app(dev_mode=dev_mode)
    client = TestClient(app, backend_options={"use_uvloop": True})
    resp = client.options(
        "/api/health",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    return resp.headers.get("access-control-allow-origin")


def test_cors_middleware_installed_when_dev_mode_true():
    """`dev_mode=True` MUST produce a CORS allow-origin header for the
    Vite dev server. Without this, the SPA would fail with opaque CORS
    errors in the browser console."""
    assert _cors_origin_for(dev_mode=True) == "http://localhost:5173"


def test_cors_middleware_absent_when_dev_mode_false():
    """`dev_mode=False` MUST NOT install CORS. Production posture: any
    cross-origin request gets no allow-origin header, so the browser
    blocks it. Tests that the security-by-default toggle actually works."""
    assert _cors_origin_for(dev_mode=False) is None


def test_create_app_default_consults_env(monkeypatch):
    """If `dev_mode` arg is omitted, the factory must consult AFR_DASHBOARD_DEV_MODE.
    Pin both directions so a future refactor can't accidentally hardcode the default."""
    monkeypatch.setenv("AFR_DASHBOARD_DEV_MODE", "true")
    assert _cors_origin_for_default() == "http://localhost:5173"
    monkeypatch.setenv("AFR_DASHBOARD_DEV_MODE", "false")
    assert _cors_origin_for_default() is None


# --------------------------------------------------------------------------- #
# Platform check - dashboard is Linux-only. Mac/Windows operators must use   #
# Docker. _require_linux runs at lifespan start and hard-fails non-Linux.    #
# --------------------------------------------------------------------------- #


def test_require_linux_passes_on_linux():
    """On the supported platform the check returns silently."""
    # The test suite runs on Linux per project policy; just call it.
    _require_linux()  # must NOT raise


def test_require_linux_raises_on_other_platform(monkeypatch):
    """On a non-Linux platform, the check MUST raise with a message that
    points the operator at Docker - silent misbehavior would be worse
    than a hard fail at startup."""
    monkeypatch.setattr("dashboard.api.main.sys.platform", "darwin")
    with pytest.raises(RuntimeError, match="Docker"):
        _require_linux()


def _cors_origin_for_default() -> str | None:
    """Same as `_cors_origin_for` but uses the factory's default arg
    (which reads the env var). Used by `test_create_app_default_consults_env`."""
    app = create_app()
    client = TestClient(app, backend_options={"use_uvloop": True})
    resp = client.options(
        "/api/health",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    return resp.headers.get("access-control-allow-origin")


def test_dashboard_api_package_import_does_not_bootstrap_main():
    """`import dashboard.api` alone must NOT import `dashboard.api.main`.

    Utility scripts import `dashboard.api.db` and should not trigger app
    bootstrap side effects (SPA mount logs, startup wiring) just to reach
    DB helpers.
    """
    script = """
import sys
import dashboard.api
assert "dashboard.api.main" not in sys.modules, sys.modules.keys()
print("ok")
"""
    cp = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert cp.stdout.strip() == "ok"
