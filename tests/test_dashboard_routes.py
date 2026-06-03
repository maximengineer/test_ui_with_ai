"""Read-route HTTP-level tests using FastAPI's TestClient (Phase C.1).

The TestClient drives the app through the full ASGI stack (lifespan,
middleware, dependency injection) so these tests catch wiring breakage
that unit tests of the underlying functions would miss.

DB isolation: each test points `settings.runs_db_path` at a tmp file +
overrides `get_db` so requests use that connection. AI-analyzer URL is
pointed at a non-routable address so `/api/health` exercises the
"analyzer down" branch deterministically.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard.api import db as dbmod
from dashboard.api.main import app
from dashboard.api.routes import get_db
from test_ui.common.manifest import Manifest, write_manifest
from test_ui.common.run_id import new_run_id
from test_ui.config import settings


def _wire_settings_to_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point `settings` paths + analyzer URL at tmp/non-routable values.

    Returns the DB path. Extracted from the original `client` fixture so
    tests that need to seed data BEFORE TestClient construction (and thus
    before lifespan's startup-sync runs) can reuse the same wiring.
    """
    data_root = tmp_path / "data"
    monkeypatch.setattr(settings, "data_root", data_root)
    monkeypatch.setattr(settings, "baseline_dir", data_root / "baseline")
    monkeypatch.setattr(settings, "current_dir", data_root / "current")
    monkeypatch.setattr(settings, "comparator_dir", data_root / "comparator")
    monkeypatch.setattr(settings, "report_dir", data_root / "report")
    db_path = tmp_path / "dashboard.db"
    monkeypatch.setattr(settings, "runs_db_path", db_path)
    # Non-routable analyzer so /api/health is deterministic.
    monkeypatch.setattr(settings, "ai_analyzer_service_url", "http://127.0.0.1:1")
    return db_path


@contextlib.contextmanager
def _client_with(db_path: Path):
    """Open a TestClient with `get_db` swapped to use `db_path`.

    Always cleans up `app.dependency_overrides` on exit, even if the body
    raises - without that, a failing test would leak its DB connection
    factory into the next test that imports `app`. (Module-level `app`
    is shared across tests; the contextmanager pattern is the discipline
    that makes that safe.)
    """

    def _override_get_db():
        with dbmod.connection_scope(db_path) as conn:
            yield conn

    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app, backend_options={"use_uvloop": True}) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Build a TestClient with a tmp DB + tmp data root.

    Composes `_wire_settings_to_tmp` (env wiring) + `_client_with` (the
    dependency override + TestClient). Tests that need to interleave
    seed-data and TestClient construction differently use those helpers
    directly.
    """
    db_path = _wire_settings_to_tmp(tmp_path, monkeypatch)
    with _client_with(db_path) as c:
        yield c


def _seed_run(date_dir_root: Path, date: str, run_id: str, kind: str = "baseline"):
    """Write a `manifest.json` under the right kind root."""
    run_dir = date_dir_root / date / run_id
    run_dir.mkdir(parents=True)
    write_manifest(
        run_dir,
        Manifest(
            run_id=run_id,
            kind=kind,
            started_at="01-01-2099 00:00:00",
            finished_at="01-01-2099 00:00:01",
            status="complete",
            url_count=1,
        ),
    )


def test_health_reports_db_ok_and_analyzer_down(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["db_ok"] is True
    assert body["ai_analyzer_ok"] is False  # we pointed it at :1
    assert body["ok"] is True  # ok mirrors db_ok, not analyzer


def test_sites_returns_loaded_sites(client):
    """The route reads from the real `test_ui/sites.yml`. Smoke-test that
    we get a list back with the expected shape - the YAML content varies
    so we don't pin specific entries."""
    r = client.get("/api/sites")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    if body:
        for site in body:
            assert set(site.keys()) == {"id", "name", "url"}


# --------------------------------------------------------------------------- #
# Sites CRUD routes (Phase C.2 slice). Tests use a tmp sites.yml via the     #
# `_sites_path` indirection - monkeypatching the route's path resolver lets  #
# us exercise the real on-disk write path without touching the bundled file. #
# --------------------------------------------------------------------------- #


@pytest.fixture
def sites_client(tmp_path, monkeypatch):
    """A TestClient pointed at a tmp sites.yml seeded with two entries."""
    db_path = _wire_settings_to_tmp(tmp_path, monkeypatch)
    sites_yml = tmp_path / "sites.yml"
    sites_yml.write_text(
        "sites:\n"
        "  - id: a\n    name: A\n    url: https://a.example\n"
        "  - id: b\n    name: B\n    url: https://b.example\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "dashboard.api.routes._sites_path",
        lambda: sites_yml,
    )
    with _client_with(db_path) as c:
        yield c


def test_post_sites_creates_with_next_numeric_id(sites_client):
    """The fixture seeds `a` and `b` (slug-style ids retained in the
    fixture). Adding a new site assigns the next available numeric id;
    `next_numeric_id` returns "1" because the fixture's slug ids don't
    collide with the integer namespace."""
    r = sites_client.post(
        "/api/sites", json={"name": "My New Site", "url": "https://new.example"}
    )
    assert r.status_code == 201
    body = r.json()
    assert body == {
        "id": "1",
        "name": "My New Site",
        "url": "https://new.example",
    }
    # GET sees three sites now.
    listing = sites_client.get("/api/sites").json()
    assert len(listing) == 3


def test_post_sites_assigns_smallest_unused_numeric_id(sites_client):
    """Two posts in a row → ids 1 and 2 (sequential, no gap)."""
    r1 = sites_client.post(
        "/api/sites", json={"name": "First", "url": "https://1.example"}
    )
    r2 = sites_client.post(
        "/api/sites", json={"name": "Second", "url": "https://2.example"}
    )
    assert r1.json()["id"] == "1"
    assert r2.json()["id"] == "2"


def test_post_sites_422_for_empty_url(sites_client):
    """Pydantic rejects min_length=0 url at the input boundary."""
    r = sites_client.post("/api/sites", json={"name": "X", "url": ""})
    assert r.status_code == 422


def test_post_sites_422_for_private_network_url(sites_client):
    """SSRF protection failures from the persistence layer surface as 422."""
    r = sites_client.post(
        "/api/sites", json={"name": "Router", "url": "http://192.168.1.1"}
    )
    assert r.status_code == 422
    assert "not allowed" in str(r.json()["detail"])


def test_post_sites_422_for_missing_name(sites_client):
    r = sites_client.post("/api/sites", json={"url": "https://x.example"})
    assert r.status_code == 422


def test_post_sites_bulk_creates_with_sequential_ids(sites_client):
    """POST /api/sites/bulk with 3 URLs → 3 sites with sequential ids."""
    r = sites_client.post(
        "/api/sites/bulk",
        json={"urls": ["https://a.example", "https://b.example", "https://c.example"]},
    )
    assert r.status_code == 201
    body = r.json()
    assert len(body) == 3
    assert [s["id"] for s in body] == ["1", "2", "3"]
    assert all(s["name"] == s["url"] for s in body), (
        "name should default to the URL itself"
    )


def test_post_sites_bulk_422_for_empty_urls(sites_client):
    """min_length=1 on the urls list → 422 when client sends []."""
    r = sites_client.post("/api/sites/bulk", json={"urls": []})
    assert r.status_code == 422


def test_post_sites_bulk_422_for_invalid_url_aborts_batch(sites_client):
    """One bad URL aborts the whole batch (all-or-nothing).

    Pre-batch: 2 sites in the fixture. Post: still 2 (no partial write).
    """
    before = sites_client.get("/api/sites").json()
    r = sites_client.post(
        "/api/sites/bulk",
        json={"urls": ["https://good.example", "", "https://also-good.example"]},
    )
    assert r.status_code == 422
    after = sites_client.get("/api/sites").json()
    assert len(after) == len(before)


def test_post_sites_bulk_422_for_private_network_url_aborts_batch(sites_client):
    before = sites_client.get("/api/sites").json()
    r = sites_client.post(
        "/api/sites/bulk",
        json={"urls": ["https://good.example", "http://169.254.169.254/latest"]},
    )
    assert r.status_code == 422
    after = sites_client.get("/api/sites").json()
    assert len(after) == len(before)


def test_post_sites_422_for_extra_field(sites_client):
    """extra='forbid' on SiteCreateIn surfaces typos as 422."""
    r = sites_client.post(
        "/api/sites",
        json={"name": "X", "url": "https://x.example", "color": "red"},
    )
    assert r.status_code == 422


def test_patch_site_renames(sites_client):
    r = sites_client.patch("/api/sites/a", json={"name": "Renamed"})
    assert r.status_code == 200
    assert r.json() == {"id": "a", "name": "Renamed", "url": "https://a.example"}


def test_patch_site_404_for_unknown(sites_client):
    r = sites_client.patch("/api/sites/no-such", json={"name": "x"})
    assert r.status_code == 404


def test_patch_site_no_op_when_body_empty(sites_client):
    """Empty PATCH returns the current row, no error."""
    r = sites_client.patch("/api/sites/a", json={})
    assert r.status_code == 200
    assert r.json()["name"] == "A"


def test_patch_site_422_for_empty_url(sites_client):
    r = sites_client.patch("/api/sites/a", json={"url": ""})
    assert r.status_code == 422


def test_patch_site_422_for_private_network_url(sites_client):
    r = sites_client.patch("/api/sites/a", json={"url": "http://127.0.0.1/admin"})
    assert r.status_code == 422


def test_delete_site_removes_entry(sites_client):
    r = sites_client.delete("/api/sites/a")
    assert r.status_code == 204
    listing = sites_client.get("/api/sites").json()
    assert {s["id"] for s in listing} == {"b"}


def test_delete_site_404_for_unknown(sites_client):
    r = sites_client.delete("/api/sites/no-such")
    assert r.status_code == 404


def test_post_sites_bulk_delete_returns_per_id_outcomes(sites_client):
    """Mixed batch of present + absent ids → both get reported, present
    ids actually leave sites.yml. Mirrors runs/bulk-delete semantics."""
    r = sites_client.post(
        "/api/sites/bulk-delete",
        json={"ids": ["a", "b", "ghost"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(body["deleted"]) == ["a", "b"]
    assert body["skipped_not_found"] == ["ghost"]
    listing = sites_client.get("/api/sites").json()
    assert listing == []


def test_post_sites_bulk_delete_422_for_empty_list(sites_client):
    """min_length=1 forbids `{"ids": []}` so a no-op call surfaces as
    422 not 200 - matches SiteBulkCreateIn / RunBulkDeleteIn behavior."""
    r = sites_client.post("/api/sites/bulk-delete", json={"ids": []})
    assert r.status_code == 422


def test_post_sites_bulk_delete_idempotent_on_resubmit(sites_client):
    """Re-deleting the same ids returns deleted=[], skipped=both - the
    first call removes them, the second sees them absent. The yaml file
    isn't rewritten on the second call (deleted=[] short-circuits the
    write) but the API contract still reports cleanly."""
    first = sites_client.post("/api/sites/bulk-delete", json={"ids": ["a", "b"]})
    assert first.status_code == 200
    assert sorted(first.json()["deleted"]) == ["a", "b"]

    second = sites_client.post("/api/sites/bulk-delete", json={"ids": ["a", "b"]})
    assert second.status_code == 200
    assert second.json()["deleted"] == []
    assert sorted(second.json()["skipped_not_found"]) == ["a", "b"]


def test_get_sites_returns_empty_when_file_missing(tmp_path, monkeypatch):
    """Round-3 #H1 fix: a missing sites.yml at request time MUST yield
    `[]`, not a 500. Pre-fix the import-time check would have killed the
    whole dashboard if the file was deleted between deployments."""
    db_path = _wire_settings_to_tmp(tmp_path, monkeypatch)
    # Point at a path that doesn't exist.
    monkeypatch.setattr(
        "dashboard.api.routes._sites_path",
        lambda: tmp_path / "does-not-exist.yml",
    )
    with _client_with(db_path) as c:
        r = c.get("/api/sites")
    assert r.status_code == 200
    assert r.json() == []


def test_dates_returns_empty_when_data_root_empty(client):
    """No data/ contents → all four lists empty (not 500)."""
    r = client.get("/api/dates")
    assert r.status_code == 200
    body = r.json()
    assert body == {"baseline": [], "current": [], "comparator": [], "report": []}


def test_dates_lists_present_dirs_newest_first(client):
    _seed_run(settings.baseline_dir, "01-01-2099", new_run_id())
    _seed_run(settings.baseline_dir, "15-03-2099", new_run_id())
    _seed_run(settings.current_dir, "10-02-2099", new_run_id())

    r = client.get("/api/dates")
    body = r.json()
    assert body["baseline"] == ["15-03-2099", "01-01-2099"]
    assert body["current"] == ["10-02-2099"]
    assert body["comparator"] == []


def test_dates_skips_date_dir_with_only_dangling_latest_symlink(client):
    """After deleting the last run for a date, the date dir is left
    behind containing only a (now-dangling) `latest` symlink. The
    operator's screenshot bug: the Reports page kept showing the date
    plaque because `_list_date_dirs` enumerated any subdirectory.

    A date dir with no real published-run subdir must NOT show up.
    """
    # Real published date with a run - should appear.
    _seed_run(settings.baseline_dir, "15-03-2099", new_run_id())
    # "Ghost" date with only a dangling `latest` symlink, mimicking the
    # post-delete on-disk state. The symlink target doesn't have to
    # exist (this models the "after rmtree" condition exactly).
    ghost = settings.baseline_dir / "01-01-2099"
    ghost.mkdir(parents=True)
    (ghost / "latest").symlink_to("01KQTAQSK6BSW7KDADMV4NFFWJ")

    r = client.get("/api/dates")
    body = r.json()
    assert body["baseline"] == ["15-03-2099"], (
        "Ghost date with only a `latest` symlink should NOT be listed."
    )


def test_dates_skips_completely_empty_date_dir(client):
    """A date dir with no children at all (e.g. operator manually
    rmtree'd the contents but left the parent) shouldn't show up either."""
    _seed_run(settings.baseline_dir, "15-03-2099", new_run_id())
    (settings.baseline_dir / "01-01-2099").mkdir(parents=True)
    r = client.get("/api/dates")
    assert r.json()["baseline"] == ["15-03-2099"]


def test_delete_run_prunes_empty_date_dir_and_dangling_latest(client):
    """End-to-end: deleting the LAST run for a date should leave nothing
    behind under `data/<kind>/<date>/`. Pre-fix the date dir + `latest`
    symlink survived and the date kept appearing in /api/dates.
    """
    rid = new_run_id()
    _seed_run(settings.baseline_dir, "15-03-2099", rid)
    # Mirror the publish layout: `latest` symlink alongside the real run.
    (settings.baseline_dir / "15-03-2099" / "latest").symlink_to(rid)
    # Sync so the row exists in the DB.
    client.post("/api/sync")
    # Find the db_id by name.
    listing = client.get("/api/runs").json()["items"]
    db_id = next(r["id"] for r in listing if r["run_id"] == rid)

    r = client.delete(f"/api/runs/{db_id}")
    assert r.status_code == 204
    # Date dir gone; /api/dates therefore lists nothing.
    assert not (settings.baseline_dir / "15-03-2099").exists()
    body = client.get("/api/dates").json()
    assert body["baseline"] == []


def test_delete_run_keeps_date_dir_when_other_runs_remain(client):
    """Deleting one of two runs in the same date dir must NOT prune the
    parent - the surviving sibling needs to stay reachable."""
    rid_keep = new_run_id()
    rid_delete = new_run_id()
    _seed_run(settings.baseline_dir, "15-03-2099", rid_keep)
    _seed_run(settings.baseline_dir, "15-03-2099", rid_delete)
    client.post("/api/sync")
    listing = client.get("/api/runs").json()["items"]
    db_id = next(r["id"] for r in listing if r["run_id"] == rid_delete)

    r = client.delete(f"/api/runs/{db_id}")
    assert r.status_code == 204
    assert (settings.baseline_dir / "15-03-2099").exists()
    assert (settings.baseline_dir / "15-03-2099" / rid_keep).exists()
    body = client.get("/api/dates").json()
    assert body["baseline"] == ["15-03-2099"]


def test_runs_list_paginates(client):
    """Sync at startup populates rows; /api/runs returns them paginated."""
    for i in range(5):
        _seed_run(
            settings.baseline_dir,
            f"0{i + 1}-01-2099",
            new_run_id(),
        )
    # Re-trigger sync (the lifespan ran before we seeded).
    r = client.post("/api/sync")
    assert r.status_code == 200
    body = r.json()
    assert body["scanned"] == 5
    assert body["synced"] == 5

    r = client.get("/api/runs?limit=3")
    body = r.json()
    assert body["total"] == 5
    assert len(body["items"]) == 3


def test_runs_list_filters_by_kind(client):
    _seed_run(settings.baseline_dir, "01-01-2099", new_run_id())
    _seed_run(settings.current_dir, "01-01-2099", new_run_id(), kind="current")
    client.post("/api/sync")

    r = client.get("/api/runs?kind=baseline")
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["kind"] == "baseline"


def test_runs_list_filters_by_date_dir(client):
    """Round-2 review CRITICAL #1 fix: backend supports date_dir query
    so the Reports page can fetch runs for one date without client-side
    filtering of an unbounded result set."""
    _seed_run(settings.baseline_dir, "01-01-2099", new_run_id())
    _seed_run(settings.baseline_dir, "02-01-2099", new_run_id())
    _seed_run(settings.baseline_dir, "03-01-2099", new_run_id())
    client.post("/api/sync")

    r = client.get("/api/runs?date_dir=02-01-2099")
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["date_dir"] == "02-01-2099"


def test_runs_list_combines_kind_and_date_dir_filters(client):
    """Filters must AND: kind=baseline AND date_dir=X."""
    _seed_run(settings.baseline_dir, "01-01-2099", new_run_id())
    _seed_run(settings.baseline_dir, "02-01-2099", new_run_id())
    _seed_run(settings.current_dir, "01-01-2099", new_run_id(), kind="current")
    client.post("/api/sync")

    r = client.get("/api/runs?kind=baseline&date_dir=01-01-2099")
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["kind"] == "baseline"
    assert body["items"][0]["date_dir"] == "01-01-2099"


def test_run_detail_404_for_unknown_id(client):
    r = client.get("/api/runs/99999")
    assert r.status_code == 404


def test_run_detail_returns_full_row(client):
    rid = new_run_id()
    _seed_run(settings.baseline_dir, "01-01-2099", rid)
    client.post("/api/sync")

    listing = client.get("/api/runs").json()
    db_id = listing["items"][0]["id"]

    r = client.get(f"/api/runs/{db_id}")
    body = r.json()
    assert body["run_id"] == rid
    assert body["kind"] == "baseline"
    assert body["status"] == "done"  # complete → done mapping
    assert body["source"] == "discovered"
    assert body["args"] == {}  # discovered rows have empty args
    assert body["command"] == []


def test_runs_query_validates_pagination_bounds(client):
    """limit must be 1..500; offset must be >= 0. Out-of-range → 422."""
    assert client.get("/api/runs?limit=0").status_code == 422
    assert client.get("/api/runs?limit=501").status_code == 422
    assert client.get("/api/runs?offset=-1").status_code == 422


def test_openapi_includes_all_routes(client):
    """Sanity: openapi.json lists every route we registered. The frontend's
    type generator depends on this."""
    r = client.get("/openapi.json")
    paths = set(r.json()["paths"].keys())
    expected = {
        "/api/sites",
        "/api/dates",
        "/api/runs",
        "/api/runs/{db_id}",
        "/api/sync",
        "/api/health",
    }
    assert expected.issubset(paths), f"missing in OpenAPI: {expected - paths}"


def test_sync_at_startup_picks_up_pre_existing_manifest(tmp_path, monkeypatch):
    """Spawn a fresh app with a pre-seeded data dir → lifespan's startup
    sync must have inserted the row by the time the first request lands.

    Uses the helpers directly because the order matters: env wiring →
    seed data → THEN open the TestClient (which runs lifespan). The
    `client` fixture would seed too late.
    """
    db_path = _wire_settings_to_tmp(tmp_path, monkeypatch)
    _seed_run(settings.baseline_dir, "01-01-2099", new_run_id())
    with _client_with(db_path) as c:
        r = c.get("/api/runs")
        assert r.json()["total"] == 1


def test_runs_route_handles_corrupt_args_json_gracefully(client):
    """A row with garbage in `args_json` must NOT take down the route.

    `_row_to_runrow` substitutes `{}` (and `[]` for command_json) when the
    JSON parse fails. Pre-fix this had no test - only the happy path was
    exercised, and the defensive logging branch could rot silently.
    """
    db_path = settings.runs_db_path
    assert db_path is not None
    with dbmod.connection_scope(db_path) as conn:
        # Bypass `insert_discovered_run` so we can write deliberately-bad JSON.
        conn.execute(
            """
            INSERT INTO runs (
                run_id, kind, status, created_at, args_json, command_json, source
            ) VALUES (?, ?, ?, ?, ?, ?, 'discovered')
            """,
            (
                "01HCORRUPT00000000000000A0",
                "baseline",
                "done",
                "01-01-2099 00:00:00",
                "{not json",
                "[not json",
            ),
        )

    listing = client.get("/api/runs").json()
    assert listing["total"] == 1
    item = listing["items"][0]
    assert item["args"] == {}, "corrupt args_json must surface as {}, not 500"
    assert item["command"] == [], "corrupt command_json must surface as []"


def test_dates_filters_out_garbage_dir_names(client):
    """Names that don't match strict DD-MM-YYYY are dropped from the listing.

    Pre-fix the route would include a name like `not-a-date` in the
    response - `str.split("-")` happens to yield exactly 3 tokens for it
    so the sort key was `'date-a-not'`, which sorted ABOVE real dates.
    The frontend's date picker would have shown garbage as a selectable
    option. Now: strict regex filter, garbage drops out, sort is total."""
    settings.baseline_dir.mkdir(parents=True, exist_ok=True)
    (settings.baseline_dir / "not-a-date").mkdir()
    (settings.baseline_dir / "latest").mkdir()  # stray symlink-target
    # Real DD-MM-YYYY dirs need an actual run subdir inside them, otherwise
    # the post-fix `_date_dir_has_published_run` filter (added to suppress
    # ghost dates after run-deletion) would drop them too. We're testing
    # the NAME filter here, so put a real run in each to isolate the
    # behavior under test.
    _seed_run(settings.baseline_dir, "01-01-2099", new_run_id())
    _seed_run(settings.baseline_dir, "15-03-2099", new_run_id())

    r = client.get("/api/dates")
    assert r.status_code == 200
    body = r.json()
    assert body["baseline"] == ["15-03-2099", "01-01-2099"]


def test_dates_rejects_impossible_calendar_dates(client):
    """Round 1 used a regex-only check that accepted shapes like
    "32-13-2099" and "00-00-0000" - clickable dates in the picker that
    don't correspond to a real day. Round 2 switched to `datetime.strptime`
    so impossible dates are filtered out alongside garbage names."""
    settings.baseline_dir.mkdir(parents=True, exist_ok=True)
    # All shape-valid (2 digits, 2 digits, 4 digits) but not real calendar
    # dates. Must NOT appear in the response. Empty-dir mkdir() is fine
    # for the impossible names because they get filtered on the name-rule
    # before the published-run check runs.
    (settings.baseline_dir / "32-01-2099").mkdir()  # day 32
    (settings.baseline_dir / "01-13-2099").mkdir()  # month 13
    (settings.baseline_dir / "00-00-0000").mkdir()  # day 0, month 0
    (settings.baseline_dir / "29-02-2099").mkdir()  # not a leap year
    # The one real date needs a real run inside or the published-run
    # filter (added to suppress ghost dates after run-deletion) would
    # drop it too.
    _seed_run(settings.baseline_dir, "01-01-2099", new_run_id())

    r = client.get("/api/dates")
    assert r.status_code == 200
    assert r.json()["baseline"] == ["01-01-2099"]


def test_run_detail_with_non_int_id_returns_422(client):
    """FastAPI's path-int validation must catch this before our handler runs.
    Pre-fix this branch had no explicit test."""
    r = client.get("/api/runs/not-an-int")
    assert r.status_code == 422


def test_health_reports_db_ok_false_when_db_path_unset(tmp_path, monkeypatch):
    """If `settings.runs_db_path` is None, /api/health must NOT 500 - it
    must return `db_ok=False, ok=False`. This is the entire point of N3:
    surface degraded state instead of crashing."""
    db_path = _wire_settings_to_tmp(tmp_path, monkeypatch)
    with _client_with(db_path) as c:
        # AFTER lifespan ran (it needed the path), unset it for the request.
        monkeypatch.setattr(settings, "runs_db_path", None)
        r = c.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["db_ok"] is False
        assert body["ok"] is False
