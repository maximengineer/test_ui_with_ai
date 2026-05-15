"""End-to-end API happy path (Phase C.3 deliverable).

Drives the FastAPI TestClient through the FULL operator workflow,
simulating what the React UI would do click-by-click:

  1. POST /api/sites               - operator adds a site
  2. POST /api/runs (baseline)     - kicks off a baseline crawl
  3. GET  /api/runs/{db_id}        - polls status (mocked subprocess
                                     completes immediately with done)
  4. POST /api/runs (current)      - kicks off a current crawl
  5. POST /api/runs (comparator)   - diff baseline vs current
  6. POST /api/runs (report)       - produce the AI-narrated report
  7. GET  /api/dates               - confirm new dates are visible
  8. GET  /api/runs?kind=report    - confirm the report run is listed
  9. GET  /api/reports/{date}/{run_id}/urls   - list URLs in the report
 10. GET  /api/reports/{date}/{run_id}/url?id=...  - drill into one URL
 11. GET  /api/reports/{date}/{run_id}/screenshot  - fetch a screenshot

The actual subprocess work (crawling, comparing, AI analysis) is
mocked: `runner.spawn_run` is replaced with a fake that immediately
marks the row `done` AND materializes plausible on-disk artifacts
under `data/<kind>/<today>/<run_id>/` (manifest.json + per-URL files
for the report kind). That gives the downstream routes real data to
operate on without spending minutes per test on real Playwright
crawls.

Catches API-level wiring breakage between routes - the kind of bug
where a backend rename makes the frontend's chained calls break
silently because no single-route test exercises the chain.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard.api import db as dbmod
from dashboard.api import runner
from dashboard.api.main import app
from dashboard.api.routes import get_db
from test_ui.common.manifest import Manifest, write_manifest
from test_ui.config import settings


# 1×1 transparent PNG - smallest valid PNG bytes. Used for fake
# screenshots in the report run dir.
_PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6300010000000500010d0a2db40000000049454e44ae426082"
)


def _wire_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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


def _seed_complete_run(kind_root: Path, date: str, kind: str, run_id: str) -> None:
    """Materialize a complete-status manifest under <kind_root>/<date>/<run_id>/.

    Mirrors what the real CLI subprocess would have written if it ran.
    The lifecycle test uses these to satisfy the precondition checks
    on comparator (needs baseline + current) and report (needs comparator).
    """
    run_dir = kind_root / date / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
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


def _seed_report_artifacts(date: str, run_id: str, url_id: str) -> None:
    """Write per-URL report artifacts under <report_dir>/<date>/<run_id>/<url_id>/
    so the report drill-in routes have something to return."""
    url_dir = settings.report_dir / date / run_id / url_id
    url_dir.mkdir(parents=True, exist_ok=True)
    (url_dir / "ai_analysis.json").write_text(
        json.dumps(
            {
                "overall_severity": "WARNING",
                "url": f"https://{url_id}.example",
                "summary": "Minor visual diff in header.",
            }
        ),
        encoding="utf-8",
    )
    (url_dir / "structured_data.json").write_text(
        json.dumps({"diff": "synthetic"}), encoding="utf-8"
    )
    screens = url_dir / "screenshots"
    screens.mkdir()
    for kind in ("baseline", "current", "visual_diff"):
        (screens / f"{kind}.png").write_bytes(_PNG_1X1)


@pytest.fixture
def e2e_env(tmp_path, monkeypatch):
    """Wire settings + an isolated sites.yml + the spawn fake.

    The spawn fake is the single most important piece of this fixture:
    it replaces `runner.spawn_run` with a function that
      (a) marks the DB row `running` immediately, then
      (b) materializes the kind-specific on-disk artifacts the next
          stage of the workflow expects to find, then
      (c) marks the row `done`.
    All synchronously inside the route - no race for the test to wait on.
    """
    db_path = _wire_settings(tmp_path, monkeypatch)

    # Isolated sites.yml the route helpers point at.
    sites_yml = tmp_path / "sites.yml"
    sites_yml.write_text("sites: []\n", encoding="utf-8")
    monkeypatch.setattr("dashboard.api.routes._sites_path", lambda: sites_yml)

    today = settings.get_current_date()

    async def _fake_spawn(*, db_id, run_id, kind, log_path, db_path):
        """Mock subprocess: synthesize plausible on-disk output, then
        mark the row done. The real runner would fork `python -m test_ui`
        and let _watch handle the terminal status; here we shortcut both
        because what we're testing is the CHAIN of API calls, not the
        runner machinery (which has its own dedicated tests)."""
        kind_to_root = {
            "baseline": settings.baseline_dir,
            "current": settings.current_dir,
            "comparator": settings.comparator_dir,
            "report": settings.report_dir,
        }
        _seed_complete_run(kind_to_root[kind], today, kind, run_id)
        if kind == "report":
            # The report drill-in routes need per-URL artifacts.
            _seed_report_artifacts(today, run_id, url_id="1")
        with dbmod.connection_scope(db_path) as conn:
            dbmod.mark_running(
                conn,
                db_id=db_id,
                pid=99999,
                pgid=99999,
                pid_start_time="0",
                started_at=settings.get_current_datetime(),
            )
            dbmod.mark_terminal(
                conn,
                db_id=db_id,
                status="done",
                finished_at=settings.get_current_datetime(),
                exit_code=0,
            )
        # Sentinel - the route doesn't await it.
        from unittest.mock import MagicMock

        return MagicMock(pid=99999)

    monkeypatch.setattr(runner, "spawn_run", _fake_spawn)
    runner._active_watchers.clear()  # invariant for jobruns fixture

    @contextlib.contextmanager
    def _client():
        def _override():
            with dbmod.connection_scope(db_path) as conn:
                yield conn

        app.dependency_overrides[get_db] = _override
        try:
            with TestClient(app, backend_options={"use_uvloop": True}) as c:
                yield c
        finally:
            app.dependency_overrides.clear()

    return _client


# --------------------------------------------------------------------------- #
# THE happy-path test                                                         #
# --------------------------------------------------------------------------- #


def test_full_operator_workflow_end_to_end(e2e_env):
    """Drive every step the React UI would: add a site, run the four
    stages, drill into the resulting report."""
    today = settings.get_current_date()

    with e2e_env() as c:
        # === 1. Add a site ============================================
        r = c.post(
            "/api/sites",
            json={"name": "Example Site", "url": "https://example.example"},
        )
        assert r.status_code == 201, r.text
        site = r.json()
        assert site["id"] == "1"

        # GET /api/sites should now contain it.
        listing = c.get("/api/sites").json()
        assert any(s["id"] == "1" for s in listing)

        # === 2. Spawn baseline ========================================
        r = c.post("/api/runs", json={"kind": "baseline"})
        assert r.status_code == 202, r.text
        baseline = r.json()
        assert baseline["status"] == "running"
        baseline_db_id = baseline["db_id"]

        # === 3. Poll status - fake_spawn already marked it done ======
        r = c.get(f"/api/runs/{baseline_db_id}")
        assert r.status_code == 200
        row = r.json()
        assert row["status"] == "done"
        assert row["kind"] == "baseline"
        assert row["date_dir"] == today

        # === 4. Spawn current =========================================
        r = c.post("/api/runs", json={"kind": "current"})
        assert r.status_code == 202

        # === 5. Spawn comparator (precondition: complete baseline + current) =
        r = c.post("/api/runs", json={"kind": "comparator"})
        assert r.status_code == 202, (
            f"comparator should pass precondition with complete baseline+current "
            f"on disk; got {r.status_code}: {r.text}"
        )
        comparator = r.json()
        assert comparator["status"] == "running"

        # === 6. Spawn report (precondition: complete comparator) ======
        r = c.post("/api/runs", json={"kind": "report"})
        assert r.status_code == 202, (
            f"report should pass precondition with complete comparator on disk; "
            f"got {r.status_code}: {r.text}"
        )
        report = r.json()
        report_db_id = report["db_id"]
        report_run_id = report["run_id"]

        # === 7. /api/dates surfaces the new dates =====================
        dates = c.get("/api/dates").json()
        for k in ("baseline", "current", "comparator", "report"):
            assert today in dates[k], f"{k} dates should include {today}"

        # === 8. /api/runs?kind=report&date_dir=today shows the report =
        r = c.get(f"/api/runs?kind=report&date_dir={today}")
        body = r.json()
        assert body["total"] == 1
        assert body["items"][0]["id"] == report_db_id

        # === 9. /api/reports/{date}/{run_id}/urls drilling in =========
        r = c.get(f"/api/reports/{today}/{report_run_id}/urls")
        assert r.status_code == 200, r.text
        urls = r.json()["items"]
        assert len(urls) == 1
        assert urls[0]["url_id"] == "1"
        assert urls[0]["result_type"] == "analysis_success"
        assert urls[0]["severity"] == "WARNING"

        # === 10. Per-URL detail =======================================
        r = c.get(
            f"/api/reports/{today}/{report_run_id}/url",
            params={"id": "1"},
        )
        assert r.status_code == 200
        detail = r.json()
        assert detail["url_id"] == "1"
        assert detail["analysis"]["overall_severity"] == "WARNING"
        assert sorted(detail["screenshots"]) == ["baseline", "current", "visual_diff"]

        # === 11. Screenshot bytes =====================================
        r = c.get(
            f"/api/reports/{today}/{report_run_id}/screenshot",
            params={"url_id": "1", "which": "diff"},
        )
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
        # ETag is set so the browser can revalidate (Round-3 H2 fix).
        first_etag = r.headers["etag"]
        # Conditional GET → 304.
        r304 = c.get(
            f"/api/reports/{today}/{report_run_id}/screenshot",
            params={"url_id": "1", "which": "diff"},
            headers={"If-None-Match": first_etag},
        )
        assert r304.status_code == 304


def test_idempotency_409_during_workflow(e2e_env):
    """Spawning the same kind+date twice while the first is still running
    yields 409. The fake_spawn here marks `done` immediately, so we
    can't easily catch a `running` state - instead we monkeypatch to
    leave the row pending so the second POST has something to conflict
    with.

    Pin: the lifecycle's idempotency rule survives the chained-call
    workflow, not just the single-route unit tests.
    """
    with e2e_env() as c:
        # Replace the spawn fake with one that leaves the row pending.
        async def _stuck_spawn(*, db_id, run_id, kind, log_path, db_path):
            with dbmod.connection_scope(db_path) as conn:
                dbmod.mark_running(
                    conn,
                    db_id=db_id,
                    pid=12345,
                    pgid=12345,
                    pid_start_time="0",
                    started_at=settings.get_current_datetime(),
                )
            from unittest.mock import MagicMock

            return MagicMock(pid=12345)

        import dashboard.api.runner as runner_mod

        runner_mod.spawn_run = _stuck_spawn

        r1 = c.post("/api/runs", json={"kind": "baseline"})
        assert r1.status_code == 202

        # Same kind+date → 409.
        r2 = c.post("/api/runs", json={"kind": "baseline"})
        assert r2.status_code == 409
        body = r2.json()
        assert body["detail"]["existing_db_id"] == r1.json()["db_id"]


def test_workflow_precondition_412_for_report_without_comparator(e2e_env):
    """Skipping straight to `report` without the comparator step in place
    must yield 412. Pin to make sure the precondition layer doesn't get
    bypassed by some future "convenience" shortcut."""
    with e2e_env() as c:
        r = c.post("/api/runs", json={"kind": "report"})
        assert r.status_code == 412
        assert "comparator" in r.json()["detail"].lower()
