"""Reports drill-in routes (Phase C.2 second slice).

Pin:
  - Path-traversal defense at every layer (date, run_id, url_id, final
    `is_relative_to`).
  - Result-type classification picks the right file in priority order.
  - Severity counts roll up correctly.
  - Screenshots: `which=` enum mapping is correct; bogus values 422.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard.api import db as dbmod
from dashboard.api.main import app
from dashboard.api.routes import get_db
from test_ui.common.manifest import Manifest, write_manifest
from test_ui.common.run_id import new_run_id
from test_ui.config import settings


# --------------------------------------------------------------------------- #
# Fixtures                                                                   #
# --------------------------------------------------------------------------- #


def _wire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_root = tmp_path / "data"
    monkeypatch.setattr(settings, "data_root", data_root)
    monkeypatch.setattr(settings, "baseline_dir", data_root / "baseline")
    monkeypatch.setattr(settings, "current_dir", data_root / "current")
    monkeypatch.setattr(settings, "comparator_dir", data_root / "comparator")
    monkeypatch.setattr(settings, "report_dir", data_root / "report")
    db_path = tmp_path / "dashboard.db"
    monkeypatch.setattr(settings, "runs_db_path", db_path)
    monkeypatch.setattr(settings, "runs_log_dir", tmp_path / "runs")
    monkeypatch.setattr(settings, "ai_analyzer_service_url", "http://127.0.0.1:1")
    return db_path


@contextlib.contextmanager
def _client_with(db_path: Path):
    def _override():
        with dbmod.connection_scope(db_path) as conn:
            yield conn

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


def _seed_report_run(
    *,
    date: str,
    url_results: dict[str, tuple[str, dict]],
    screenshots_for: set[str] = frozenset(),
) -> str:
    """Materialize a fake report run dir on disk.

    `url_results` is `{url_id: (result_filename, payload)}`. Each entry
    creates `<report_dir>/<date>/<run_id>/<url_id>/<result_filename>`
    with the payload as JSON.

    `screenshots_for` is a set of url_ids that should also get all 3
    screenshot PNGs (1x1 transparent placeholders).

    Returns the generated run_id.
    """
    rid = new_run_id()
    run_dir = settings.report_dir / date / rid
    run_dir.mkdir(parents=True)
    write_manifest(
        run_dir,
        Manifest(
            run_id=rid,
            kind="report",
            started_at="01-01-2099 00:00:00",
            finished_at="01-01-2099 00:00:01",
            status="complete",
            url_count=len(url_results),
        ),
    )
    # 1x1 transparent PNG bytes - smallest valid PNG.
    png_1x1 = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c6300010000000500010d0a2db40000000049454e44ae426082"
    )
    for url_id, (filename, payload) in url_results.items():
        url_dir = run_dir / url_id
        url_dir.mkdir()
        (url_dir / filename).write_text(json.dumps(payload), encoding="utf-8")
        # Also write structured_data so the detail route has something
        # to return (otherwise it stays None - also a tested case).
        (url_dir / "structured_data.json").write_text(
            json.dumps({"diff": "synthetic"}), encoding="utf-8"
        )
        if url_id in screenshots_for:
            screens = url_dir / "screenshots"
            screens.mkdir()
            for kind in ("baseline", "current", "visual_diff"):
                (screens / f"{kind}.png").write_bytes(png_1x1)
    return rid


@pytest.fixture
def reports_client(tmp_path, monkeypatch):
    db_path = _wire(tmp_path, monkeypatch)
    with _client_with(db_path) as c:
        yield c


# --------------------------------------------------------------------------- #
# /api/reports/{date}/{run_id}                                               #
# --------------------------------------------------------------------------- #


def test_summary_returns_manifest_fields_and_counts(reports_client):
    rid = _seed_report_run(
        date="01-01-2099",
        url_results={
            "site-a": ("ai_analysis.json", {"overall_severity": "CRITICAL"}),
            "site-b": ("ai_analysis.json", {"overall_severity": "WARNING"}),
            "site-c": ("ai_analysis.json", {"overall_severity": "SAFE"}),
            "site-d": ("ai_error.json", {"error_type": "provider_error"}),
            "site-e": ("no_changes.json", {}),
        },
    )
    r = reports_client.get(f"/api/reports/01-01-2099/{rid}")
    assert r.status_code == 200
    body = r.json()
    assert body["date"] == "01-01-2099"
    assert body["run_id"] == rid
    assert body["url_count"] == 5
    counts = body["severity_counts"]
    # result_type buckets
    assert counts["analysis_success"] == 3
    assert counts["analysis_error"] == 1
    assert counts["no_changes"] == 1
    # severity sub-bucket (computed from overall_severity in success files)
    assert counts["CRITICAL"] == 1
    assert counts["WARNING"] == 1
    assert counts["SAFE"] == 1


def test_summary_404_for_missing_run(reports_client):
    rid = new_run_id()
    r = reports_client.get(f"/api/reports/01-01-2099/{rid}")
    assert r.status_code == 404


def test_summary_400_for_bogus_date(reports_client):
    """Path-traversal defense: malformed date never reaches the filesystem."""
    rid = new_run_id()
    r = reports_client.get(f"/api/reports/not-a-date/{rid}")
    assert r.status_code == 400


def test_summary_400_for_bogus_run_id(reports_client):
    """Same defense for run_id - must be a valid ULID."""
    r = reports_client.get("/api/reports/01-01-2099/not-a-ulid")
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# /api/reports/{date}/{run_id}/urls                                          #
# --------------------------------------------------------------------------- #


def test_urls_lists_all_with_result_type_and_severity(reports_client):
    rid = _seed_report_run(
        date="01-01-2099",
        url_results={
            "alpha": (
                "ai_analysis.json",
                {"overall_severity": "WARNING", "url": "https://a.example"},
            ),
            "beta": ("ai_error.json", {"error_type": "provider_error"}),
        },
    )
    r = reports_client.get(f"/api/reports/01-01-2099/{rid}/urls")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 2
    by_id = {i["url_id"]: i for i in items}
    assert by_id["alpha"]["result_type"] == "analysis_success"
    assert by_id["alpha"]["severity"] == "WARNING"
    assert by_id["alpha"]["url"] == "https://a.example"
    assert by_id["beta"]["result_type"] == "analysis_error"
    assert by_id["beta"]["severity"] is None


def test_urls_sorts_numeric_ids_descending(reports_client):
    """Numeric URL ids (post slug-to-numeric migration) must come back in
    strict numeric DESCENDING order, not lexicographic. Previously sorted
    by name, which gave "1, 10, 11, ..., 19, 2, 20, ..." for a typical
    site set - operator-confusing."""
    rid = _seed_report_run(
        date="01-01-2099",
        url_results={
            str(i): ("no_changes.json", {"checked_at": "01-01-2099 00:00:00"})
            for i in [1, 2, 5, 10, 11, 19, 20, 27, 39]
        },
    )
    r = reports_client.get(f"/api/reports/01-01-2099/{rid}/urls")
    assert r.status_code == 200, r.text
    ids = [item["url_id"] for item in r.json()["items"]]
    assert ids == ["39", "27", "20", "19", "11", "10", "5", "2", "1"]


def test_urls_mixed_numeric_and_slug_ids_keep_numeric_first(reports_client):
    """Pre-migration slug dirs may still exist alongside numeric ones.
    Numeric ids sort first (descending); slugs trail (ascending)."""
    rid = _seed_report_run(
        date="01-01-2099",
        url_results={
            "2": ("no_changes.json", {"checked_at": "01-01-2099 00:00:00"}),
            "10": ("no_changes.json", {"checked_at": "01-01-2099 00:00:00"}),
            "legacy-site": ("no_changes.json", {"checked_at": "01-01-2099 00:00:00"}),
            "another-legacy": (
                "no_changes.json",
                {"checked_at": "01-01-2099 00:00:00"},
            ),
        },
    )
    r = reports_client.get(f"/api/reports/01-01-2099/{rid}/urls")
    assert r.status_code == 200
    ids = [item["url_id"] for item in r.json()["items"]]
    assert ids == ["10", "2", "another-legacy", "legacy-site"]


def test_urls_returns_empty_items_for_run_with_no_url_dirs(reports_client):
    """A run dir with only a manifest.json (no per-URL subdirs) returns
    an empty items list - not a 404, not a 500."""
    rid = new_run_id()
    run_dir = settings.report_dir / "01-01-2099" / rid
    run_dir.mkdir(parents=True)
    write_manifest(
        run_dir,
        Manifest(
            run_id=rid,
            kind="report",
            started_at="01-01-2099 00:00:00",
            finished_at=None,
            status="complete",
            url_count=0,
        ),
    )
    r = reports_client.get(f"/api/reports/01-01-2099/{rid}/urls")
    assert r.status_code == 200
    assert r.json() == {"items": []}


# --------------------------------------------------------------------------- #
# /api/reports/{date}/{run_id}/url?id=<url_id>                               #
# --------------------------------------------------------------------------- #


def test_url_detail_returns_analysis_and_screenshot_inventory(reports_client):
    rid = _seed_report_run(
        date="01-01-2099",
        url_results={
            "site-a": ("ai_analysis.json", {"overall_severity": "CRITICAL", "x": 1}),
        },
        screenshots_for={"site-a"},
    )
    r = reports_client.get(
        f"/api/reports/01-01-2099/{rid}/url", params={"id": "site-a"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["url_id"] == "site-a"
    assert body["result_type"] == "analysis_success"
    assert body["analysis"]["overall_severity"] == "CRITICAL"
    assert body["structured_data"] == {"diff": "synthetic"}
    assert sorted(body["screenshots"]) == ["baseline", "current", "visual_diff"]


def test_url_detail_404_for_unknown_url_id(reports_client):
    rid = _seed_report_run(
        date="01-01-2099",
        url_results={"real": ("ai_analysis.json", {})},
    )
    r = reports_client.get(
        f"/api/reports/01-01-2099/{rid}/url", params={"id": "missing"}
    )
    assert r.status_code == 404


def test_url_detail_path_traversal_in_url_id_404(reports_client):
    """Even if the client sends `..`, validation against the run dir's real
    children means it never resolves to anything traversable."""
    rid = _seed_report_run(
        date="01-01-2099",
        url_results={"real": ("ai_analysis.json", {})},
    )
    r = reports_client.get(
        f"/api/reports/01-01-2099/{rid}/url", params={"id": "../../etc/passwd"}
    )
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# /api/reports/{date}/{run_id}/screenshot                                    #
# --------------------------------------------------------------------------- #


def test_screenshot_returns_png_bytes(reports_client):
    rid = _seed_report_run(
        date="01-01-2099",
        url_results={"site-a": ("ai_analysis.json", {})},
        screenshots_for={"site-a"},
    )
    r = reports_client.get(
        f"/api/reports/01-01-2099/{rid}/screenshot",
        params={"url_id": "site-a", "which": "baseline"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    # PNG signature bytes.
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.parametrize("which", ["baseline", "current", "diff"])
def test_screenshot_all_three_kinds(reports_client, which):
    rid = _seed_report_run(
        date="01-01-2099",
        url_results={"site-a": ("ai_analysis.json", {})},
        screenshots_for={"site-a"},
    )
    r = reports_client.get(
        f"/api/reports/01-01-2099/{rid}/screenshot",
        params={"url_id": "site-a", "which": which},
    )
    assert r.status_code == 200, f"{which} screenshot must be servable"


def test_screenshot_422_for_invalid_which(reports_client):
    """`which` is a Literal['baseline','current','diff'] so anything else
    is rejected at the FastAPI validation layer, NOT by reaching the
    handler with a None mapping."""
    rid = _seed_report_run(
        date="01-01-2099",
        url_results={"site-a": ("ai_analysis.json", {})},
        screenshots_for={"site-a"},
    )
    r = reports_client.get(
        f"/api/reports/01-01-2099/{rid}/screenshot",
        params={"url_id": "site-a", "which": "evil"},
    )
    assert r.status_code == 422


def test_screenshot_404_when_kind_missing(reports_client):
    """The url has SOME screenshots but not the requested kind."""
    rid = _seed_report_run(
        date="01-01-2099",
        url_results={"site-a": ("ai_analysis.json", {})},
        # No screenshots seeded for site-a.
    )
    r = reports_client.get(
        f"/api/reports/01-01-2099/{rid}/screenshot",
        params={"url_id": "site-a", "which": "baseline"},
    )
    assert r.status_code == 404


def test_screenshot_path_traversal_in_url_id_404(reports_client):
    rid = _seed_report_run(
        date="01-01-2099",
        url_results={"site-a": ("ai_analysis.json", {})},
        screenshots_for={"site-a"},
    )
    r = reports_client.get(
        f"/api/reports/01-01-2099/{rid}/screenshot",
        params={"url_id": "../../etc/passwd", "which": "baseline"},
    )
    assert r.status_code == 404


def test_screenshot_returns_304_on_matching_if_none_match(reports_client):
    """Round-3 #H2 fix: the screenshot route must honor `If-None-Match`
    and return 304 with no body when the client's cached ETag matches
    the current file's. Pre-fix the route emitted an ETag but always
    returned 200 + full bytes (round-2 H6 was incomplete)."""
    rid = _seed_report_run(
        date="01-01-2099",
        url_results={"site-a": ("ai_analysis.json", {})},
        screenshots_for={"site-a"},
    )
    # First request → 200 + ETag.
    r1 = reports_client.get(
        f"/api/reports/01-01-2099/{rid}/screenshot",
        params={"url_id": "site-a", "which": "baseline"},
    )
    assert r1.status_code == 200
    etag = r1.headers["etag"]
    assert len(r1.content) > 0

    # Second request with matching If-None-Match → 304, no body.
    r2 = reports_client.get(
        f"/api/reports/01-01-2099/{rid}/screenshot",
        params={"url_id": "site-a", "which": "baseline"},
        headers={"If-None-Match": etag},
    )
    assert r2.status_code == 304
    assert r2.content == b""
    # ETag echoed back per RFC 7232 §4.1.
    assert r2.headers["etag"] == etag


def test_screenshot_returns_200_on_stale_if_none_match(reports_client):
    """If the client sends a stale ETag (e.g. file was overwritten by
    a re-run), the server must return 200 + new bytes - not 304."""
    rid = _seed_report_run(
        date="01-01-2099",
        url_results={"site-a": ("ai_analysis.json", {})},
        screenshots_for={"site-a"},
    )
    r = reports_client.get(
        f"/api/reports/01-01-2099/{rid}/screenshot",
        params={"url_id": "site-a", "which": "baseline"},
        headers={"If-None-Match": 'W/"different-etag-bytes"'},
    )
    assert r.status_code == 200
    assert len(r.content) > 0


def test_screenshot_emits_etag_for_cache_revalidation(reports_client):
    """Round-2 review HIGH #6 fix: the screenshot route MUST emit an ETag
    based on the file mtime so the browser revalidates after a re-run
    overwrites the bytes (URL is run-id-stable, so without ETag the
    browser would serve cached old bytes forever)."""
    rid = _seed_report_run(
        date="01-01-2099",
        url_results={"site-a": ("ai_analysis.json", {})},
        screenshots_for={"site-a"},
    )
    r = reports_client.get(
        f"/api/reports/01-01-2099/{rid}/screenshot",
        params={"url_id": "site-a", "which": "baseline"},
    )
    assert r.status_code == 200
    etag = r.headers.get("etag")
    assert etag is not None and etag.startswith('W/"'), (
        'weak ETag expected; format is W/"<mtime_ns>-<size>"'
    )
    # cache-control: no-cache forces conditional revalidation rather
    # than blind cache-hit.
    assert r.headers.get("cache-control") == "no-cache"


def test_classify_url_dir_warns_on_multiple_result_files(reports_client):
    """Round-2 review LOW #15: if both ai_analysis.json AND ai_error.json
    exist (writer contract violation), a WARNING fires so the operator
    sees the corruption instead of it being silently masked.

    Captures loguru output via a dedicated sink - `caplog` (stdlib) and
    `capsys` (stderr capture) don't see loguru's writes reliably under
    pytest because pytest's stderr capture races with loguru's
    asynchronous-flushable handler.
    """
    from loguru import logger

    rid = _seed_report_run(
        date="01-01-2099",
        url_results={"site-a": ("ai_analysis.json", {"overall_severity": "SAFE"})},
    )
    # Manually inject a SECOND result file alongside the first.
    extra = settings.report_dir / "01-01-2099" / rid / "site-a" / "ai_error.json"
    extra.write_text(json.dumps({"error_type": "spurious"}), encoding="utf-8")

    captured: list[str] = []
    sink_id = logger.add(captured.append, level="WARNING", format="{message}")
    try:
        r = reports_client.get(f"/api/reports/01-01-2099/{rid}/urls")
    finally:
        logger.remove(sink_id)
    assert r.status_code == 200
    # The first-by-priority result still wins:
    assert r.json()["items"][0]["result_type"] == "analysis_success"
    assert any("multiple mutually-exclusive result files" in msg for msg in captured), (
        f"WARNING not emitted; captured: {captured!r}"
    )


def test_screenshot_includes_cors_header_in_dev_mode(tmp_path, monkeypatch):
    """Round-2 review LOW #12: the dev-mode CORS middleware applies to
    binary responses too. Without this, the React dev server (served
    from :5173) couldn't render <img> from :8080's screenshot route."""
    from dashboard.api.main import create_app
    from dashboard.api import db as dbmod

    _wire(tmp_path, monkeypatch)
    db_path = settings.runs_db_path
    dbmod.init_db(db_path)
    rid = _seed_report_run(
        date="01-01-2099",
        url_results={"site-a": ("ai_analysis.json", {})},
        screenshots_for={"site-a"},
    )

    # Build a fresh app with dev_mode=True (forces CORS install) and a
    # routing override so this test client uses the tmp DB.
    app2 = create_app(dev_mode=True)
    from dashboard.api.routes import get_db

    def _override():
        with dbmod.connection_scope(db_path) as conn:
            yield conn

    app2.dependency_overrides[get_db] = _override
    try:
        with TestClient(app2) as c:
            r = c.get(
                f"/api/reports/01-01-2099/{rid}/screenshot",
                params={"url_id": "site-a", "which": "baseline"},
                headers={"Origin": "http://localhost:5173"},
            )
            assert r.status_code == 200
            assert r.headers.get("content-type") == "image/png"
            assert (
                r.headers.get("access-control-allow-origin") == "http://localhost:5173"
            ), (
                "CORS middleware must add allow-origin to image responses "
                "or the SPA can't render the <img> in dev mode"
            )
    finally:
        app2.dependency_overrides.clear()
