"""Tests for the job-runner routes (POST /api/runs, /retry, GET /logs).

The routes themselves are thin - most of the logic lives in `runner.py`
(tested in test_dashboard_runner.py). These tests focus on:
  - HTTP shape of requests and responses
  - 409 idempotency (same kind+date already in flight)
  - 412 workflow precondition (upstream not complete)
  - 422 validation of the discriminated union
  - 404 / log-tail behavior

To avoid spawning real subprocesses (slow + flaky), `runner.spawn_run`
is monkeypatched in the route tests to return a fake `Process`-like
object after race-safely promoting the row to `running`. The runner's
own tests cover the spawn machinery end-to-end.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from dashboard.api import db as dbmod
from dashboard.api import runner
from dashboard.api.main import app
from dashboard.api.routes import get_db
from test_ui.common.manifest import Manifest, write_manifest
from test_ui.common.run_id import new_run_id
from test_ui.config import settings


# --------------------------------------------------------------------------- #
# Fixtures                                                                   #
# --------------------------------------------------------------------------- #


def _wire_settings(tmp_path, monkeypatch):
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


@pytest.fixture
def fake_spawn(monkeypatch):
    """Replace `runner.spawn_run` with an awaitable that promotes the row
    to `running` (so the route's 202 response shape matches reality) without
    actually forking a subprocess.

    Returns the MagicMock so tests can assert on the call args.
    """
    spawn_calls = MagicMock()

    async def _fake_spawn(*, db_id, run_id, kind, log_path, db_path):
        spawn_calls(db_id=db_id, run_id=run_id, kind=kind)
        with dbmod.connection_scope(db_path) as conn:
            dbmod.mark_running(
                conn,
                db_id=db_id,
                pid=99999,  # fake but valid int
                pgid=99999,
                pid_start_time="0",
                started_at=settings.get_current_datetime(),
            )
        # Return a sentinel - tests don't await it.
        sentinel = MagicMock()
        sentinel.pid = 99999
        return sentinel

    monkeypatch.setattr(runner, "spawn_run", _fake_spawn)
    return spawn_calls


@pytest.fixture
def client(tmp_path, monkeypatch, fake_spawn):
    """Standard test client + tmp DB + faked spawn. The `fake_spawn`
    fixture is requested implicitly so all client tests get the
    spawn-mocking behavior - no test should accidentally fork."""
    db_path = _wire_settings(tmp_path, monkeypatch)
    with _client_with(db_path) as c:
        yield c


# --------------------------------------------------------------------------- #
# Helpers                                                                    #
# --------------------------------------------------------------------------- #


def _seed_complete_run(kind_root: Path, date: str, kind: str = "baseline") -> str:
    """Materialize a complete-status manifest under <kind_root>/<date>/<run_id>/."""
    rid = new_run_id()
    run_dir = kind_root / date / rid
    run_dir.mkdir(parents=True)
    write_manifest(
        run_dir,
        Manifest(
            run_id=rid,
            kind=kind,
            started_at="01-01-2099 00:00:00",
            finished_at="01-01-2099 00:00:01",
            status="complete",
            url_count=1,
        ),
    )
    return rid


# --------------------------------------------------------------------------- #
# POST /api/runs - happy path                                                #
# --------------------------------------------------------------------------- #


def test_post_runs_baseline_returns_202_with_db_id_and_run_id(client, fake_spawn):
    r = client.post("/api/runs", json={"kind": "baseline"})
    assert r.status_code == 202
    body = r.json()
    assert "db_id" in body
    assert "run_id" in body
    assert body["status"] == "running"
    assert isinstance(body["db_id"], int)
    assert len(body["run_id"]) == 26  # ULIDs are 26 chars
    fake_spawn.assert_called_once()
    assert fake_spawn.call_args.kwargs["kind"] == "baseline"


def test_post_runs_current_works_same_as_baseline(client):
    r = client.post("/api/runs", json={"kind": "current"})
    assert r.status_code == 202
    assert r.json()["status"] == "running"


# --------------------------------------------------------------------------- #
# POST /api/runs - 412 workflow preconditions                                #
# --------------------------------------------------------------------------- #


def test_post_runs_comparator_412_when_no_baseline(client):
    """Comparator without a complete baseline run for today's date → 412.

    Today's date will be `settings.get_current_date()`; no kind dirs exist
    yet so `require_complete_run` raises immediately.
    """
    r = client.post("/api/runs", json={"kind": "comparator"})
    assert r.status_code == 412
    assert "baseline" in r.json()["detail"].lower()


def test_post_runs_comparator_succeeds_when_baseline_and_current_exist(client):
    """With both upstream artifacts present, the precondition passes and the
    spawn is invoked."""
    today = settings.get_current_date()
    _seed_complete_run(settings.baseline_dir, today, kind="baseline")
    _seed_complete_run(settings.current_dir, today, kind="current")

    r = client.post("/api/runs", json={"kind": "comparator"})
    assert r.status_code == 202


def test_post_runs_report_412_when_no_comparator(client):
    """Report without a complete comparator run → 412."""
    r = client.post("/api/runs", json={"kind": "report"})
    assert r.status_code == 412
    assert "comparator" in r.json()["detail"].lower()


def test_post_runs_report_with_explicit_date_uses_that_date_for_check(client):
    """`{"kind": "report", "date": "01-01-2099"}` must check the comparator
    precondition for THAT date, not today's."""
    _seed_complete_run(settings.comparator_dir, "01-01-2099", kind="comparator")

    r = client.post("/api/runs", json={"kind": "report", "date": "01-01-2099"})
    assert r.status_code == 202


# --------------------------------------------------------------------------- #
# POST /api/runs - 409 idempotency                                           #
# --------------------------------------------------------------------------- #


def test_post_runs_409_when_kind_date_already_in_flight(client):
    """Second POST for the same kind+date while the first is still running
    must return 409, not spawn a duplicate subprocess."""
    r1 = client.post("/api/runs", json={"kind": "baseline"})
    assert r1.status_code == 202
    body1 = r1.json()

    r2 = client.post("/api/runs", json={"kind": "baseline"})
    assert r2.status_code == 409
    body2 = r2.json()
    assert body2["detail"]["existing_db_id"] == body1["db_id"]
    assert body2["detail"]["existing_run_id"] == body1["run_id"]


def test_post_runs_does_not_409_when_prior_run_is_terminal(client, fake_spawn):
    """A done/failed/interrupted run for the same kind+date does NOT
    block a new request. Only `pending` and `running` count as in-flight."""
    r1 = client.post("/api/runs", json={"kind": "baseline"})
    db_id_1 = r1.json()["db_id"]
    # Force the first row to terminal as if `_watch` ran.
    db_path = settings.runs_db_path
    with dbmod.connection_scope(db_path) as conn:
        dbmod.mark_terminal(
            conn,
            db_id=db_id_1,
            status="done",
            finished_at=settings.get_current_datetime(),
            exit_code=0,
        )

    r2 = client.post("/api/runs", json={"kind": "baseline"})
    assert r2.status_code == 202


# --------------------------------------------------------------------------- #
# POST /api/runs - 422 validation                                            #
# --------------------------------------------------------------------------- #


def test_post_runs_422_for_missing_kind(client):
    r = client.post("/api/runs", json={})
    assert r.status_code == 422


def test_post_runs_422_for_unknown_kind(client):
    r = client.post("/api/runs", json={"kind": "bogus"})
    assert r.status_code == 422


def test_post_runs_422_for_extra_field_on_baseline(client):
    """`extra='forbid'` on the request models means a typo in a field
    name surfaces here, not as a silently-dropped value."""
    r = client.post("/api/runs", json={"kind": "baseline", "color": "red"})
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# POST /api/runs/{db_id}/retry                                               #
# --------------------------------------------------------------------------- #


def test_retry_404_for_unknown_db_id(client):
    r = client.post("/api/runs/99999/retry")
    assert r.status_code == 404


def test_retry_409_when_original_kind_still_running(client):
    """If the original is still running and we retry it, the retry hits
    the same 409 idempotency rule (kind+date conflict). Pinning so a
    future bug that bypasses idempotency for retries gets caught."""
    r1 = client.post("/api/runs", json={"kind": "baseline"})
    db_id = r1.json()["db_id"]

    r2 = client.post(f"/api/runs/{db_id}/retry")
    assert r2.status_code == 409


def test_retry_succeeds_after_original_terminal(client, fake_spawn):
    """Retry after the original has finished spawns a new run with the
    same kind. The run_id is fresh - same args, new identity."""
    r1 = client.post("/api/runs", json={"kind": "baseline"})
    db_id_1 = r1.json()["db_id"]
    rid_1 = r1.json()["run_id"]

    db_path = settings.runs_db_path
    with dbmod.connection_scope(db_path) as conn:
        dbmod.mark_terminal(
            conn,
            db_id=db_id_1,
            status="done",
            finished_at=settings.get_current_datetime(),
            exit_code=0,
        )

    r2 = client.post(f"/api/runs/{db_id_1}/retry")
    assert r2.status_code == 202
    body = r2.json()
    assert body["db_id"] != db_id_1  # new row
    assert body["run_id"] != rid_1  # new ULID
    assert fake_spawn.call_count == 2


# --------------------------------------------------------------------------- #
# GET /api/runs/{db_id}/logs                                                 #
# --------------------------------------------------------------------------- #


def test_logs_404_for_unknown_db_id(client):
    r = client.get("/api/runs/99999/logs")
    assert r.status_code == 404


def test_logs_404_when_log_file_does_not_exist(client):
    """A row exists but its log file hasn't been created yet (e.g. the
    spawn raced with the request). Must 404, not 500."""
    r1 = client.post("/api/runs", json={"kind": "baseline"})
    db_id = r1.json()["db_id"]
    r2 = client.get(f"/api/runs/{db_id}/logs")
    assert r2.status_code == 404


def test_logs_returns_file_contents(client):
    """A real log file is read and returned as text/plain."""
    r1 = client.post("/api/runs", json={"kind": "baseline"})
    db_id = r1.json()["db_id"]
    log_path = settings.runs_log_dir / f"{db_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_bytes(b"hello\nworld\n")

    r2 = client.get(f"/api/runs/{db_id}/logs")
    assert r2.status_code == 200
    assert "text/plain" in r2.headers["content-type"]
    assert r2.content == b"hello\nworld\n"


def test_logs_tail_returns_only_last_n_bytes(client):
    """`?tail=5` returns the last 5 bytes; the rest of the file is dropped."""
    r1 = client.post("/api/runs", json={"kind": "baseline"})
    db_id = r1.json()["db_id"]
    log_path = settings.runs_log_dir / f"{db_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_bytes(b"abcdefghij")  # 10 bytes

    r2 = client.get(f"/api/runs/{db_id}/logs?tail=4")
    assert r2.status_code == 200
    assert r2.content == b"ghij"


def test_logs_tail_validates_max_bound(client):
    """The query-param `le` cap means tail beyond 1MB is 422, not silently
    truncated. Pre-fix a large `tail` would silently allocate memory."""
    r = client.get("/api/runs/1/logs?tail=999999999")
    assert r.status_code == 422


def test_logs_default_caps_at_1mb_even_for_giant_files(client, tmp_path):
    """No `tail` → return the whole file BUT capped at 1 MB. A 2 MB log
    must come back as exactly 1 MB. Stops a stuck route from OOMing the
    dashboard rendering a runaway Playwright trace."""
    r1 = client.post("/api/runs", json={"kind": "baseline"})
    db_id = r1.json()["db_id"]
    log_path = settings.runs_log_dir / f"{db_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_bytes(b"x" * (2 * 1024 * 1024))

    r2 = client.get(f"/api/runs/{db_id}/logs")
    assert r2.status_code == 200
    assert len(r2.content) == 1024 * 1024


# --------------------------------------------------------------------------- #
# Round-3 regression tests for routes / models / discriminator wiring        #
# --------------------------------------------------------------------------- #


def test_H2_report_request_rejects_impossible_date(client):
    """`{"kind": "report", "date": "31-02-2099"}` MUST 422 at the model
    layer, not propagate the bogus date to require_complete_run.

    Pre-fix the date string was passed through verbatim and showed up in
    a confusing 412 response ("No comparator runs found for 31-02-2099").
    """
    r = client.post("/api/runs", json={"kind": "report", "date": "31-02-2099"})
    assert r.status_code == 422


def test_H2_report_request_rejects_path_traversal_in_date(client):
    """Path-traversal-shaped date values MUST 422. Defense-in-depth even
    though `require_complete_run` only does `.exists()` (no read)."""
    r = client.post("/api/runs", json={"kind": "report", "date": "../../etc/passwd"})
    assert r.status_code == 422


def test_H2_report_request_accepts_real_date(client):
    """A real DD-MM-YYYY date passes validation (then hits 412 because no
    comparator artifacts exist - proves we got past the model)."""
    r = client.post("/api/runs", json={"kind": "report", "date": "01-01-2099"})
    assert r.status_code == 412  # no comparator data - but date passed validation


def test_M3_openapi_emits_discriminator_for_run_request(client):
    """Pydantic + FastAPI must generate a `oneOf` with a `discriminator`
    on the kind field - otherwise the openapi-typescript generator can't
    produce typed unions for the React frontend."""
    schema = client.get("/openapi.json").json()
    runs_post = schema["paths"]["/api/runs"]["post"]
    body_schema_ref = runs_post["requestBody"]["content"]["application/json"]["schema"]
    # The body is a $ref to the union OR an inline oneOf - accept either shape.
    body_schema = body_schema_ref
    if "$ref" in body_schema_ref:
        ref = body_schema_ref["$ref"].split("/")[-1]
        body_schema = schema["components"]["schemas"][ref]
    assert "oneOf" in body_schema or "anyOf" in body_schema, (
        "RunRequest must serialize as a oneOf/anyOf for the frontend type generator"
    )
    assert "discriminator" in body_schema, (
        "the union must have a discriminator so openapi-typescript "
        "can emit a typed dispatch"
    )
    assert body_schema["discriminator"]["propertyName"] == "kind"


def test_H5_runrow_renders_with_null_pid_pgid(client):
    """A row whose subprocess never spawned (e.g. spawn raised
    FileNotFoundError) has NULL pid/pgid/started_at. The wire model must
    serialize those as `null`, not crash."""
    r1 = client.post("/api/runs", json={"kind": "baseline"})
    db_id = r1.json()["db_id"]
    db_path = settings.runs_db_path
    # Force the row back to NULL pid/pgid + status=failed (simulating the
    # "spawn raised" path that the fake_spawn doesn't otherwise exercise).
    with dbmod.connection_scope(db_path) as conn:
        conn.execute(
            "UPDATE runs SET pid = NULL, pgid = NULL, pid_start_time = NULL, "
            "started_at = NULL, status = 'failed', error = 'simulated' "
            "WHERE id = ?",
            (db_id,),
        )

    r2 = client.get(f"/api/runs/{db_id}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["pid"] is None
    assert body["pgid"] is None
    assert body["pid_start_time"] is None
    assert body["started_at"] is None
    assert body["error"] == "simulated"


def test_M4_retry_preserves_args_beyond_kind(client, fake_spawn):
    """Retry of a comparator with explicit baseline_run_id MUST preserve
    that field through the round-trip - args_json → request → spawn.
    Pre-fix only `kind` was checked, so a regression dropping args
    silently would slip through.
    """
    # Set up upstream artifacts so the comparator passes precondition.
    today = settings.get_current_date()
    _seed_complete_run(settings.baseline_dir, today, kind="baseline")
    _seed_complete_run(settings.current_dir, today, kind="current")

    r1 = client.post(
        "/api/runs",
        json={
            "kind": "comparator",
            "baseline_run_id": "01HARDCODED0000000000000A0",
            "current_run_id": "01HARDCODED0000000000000B0",
        },
    )
    assert r1.status_code == 202
    db_id = r1.json()["db_id"]
    # Force terminal so retry isn't blocked by 409.
    with dbmod.connection_scope(settings.runs_db_path) as conn:
        dbmod.mark_terminal(
            conn,
            db_id=db_id,
            status="done",
            finished_at=settings.get_current_datetime(),
            exit_code=0,
        )

    r2 = client.post(f"/api/runs/{db_id}/retry")
    assert r2.status_code == 202

    # Verify the new row has the same args_json (not just same kind).
    new_db_id = r2.json()["db_id"]
    with dbmod.connection_scope(settings.runs_db_path) as conn:
        new_row = dbmod.get_run(conn, new_db_id)
    import json as _json

    new_args = _json.loads(new_row["args_json"])
    assert new_args["baseline_run_id"] == "01HARDCODED0000000000000A0"
    assert new_args["current_run_id"] == "01HARDCODED0000000000000B0"


def test_M4_500_path_marks_row_failed_with_error(client, monkeypatch):
    """When `runner.spawn_run` raises FileNotFoundError (e.g. python
    interpreter missing from PATH), the route MUST mark the row failed
    with the error message - not leave it in `pending` to confuse
    the operator."""

    async def _spawn_raises(**_):
        raise FileNotFoundError("[Errno 2] No such file or directory: 'python'")

    monkeypatch.setattr(runner, "spawn_run", _spawn_raises)

    r = client.post("/api/runs", json={"kind": "baseline"})
    assert r.status_code == 500
    assert "spawn" in r.json()["detail"].lower()

    # Find the row that got created. There's only one for this date+kind.
    with dbmod.connection_scope(settings.runs_db_path) as conn:
        rows, _ = dbmod.list_runs(conn, kind="baseline")
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "failed"
    assert "spawn failed" in row["error"]


# --------------------------------------------------------------------------- #
# Lifespan integration: recovery + sync ordering                             #
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# DELETE /api/runs/{db_id} + POST /api/runs/bulk-delete                      #
# --------------------------------------------------------------------------- #


def _seed_artifacts_for(db_id: int, run_id: str, kind: str = "baseline") -> Path:
    """Write a fake on-disk run dir + log file so we can verify the
    delete route's cleanup helper actually removes them."""
    today = settings.get_current_date()
    kind_root = {
        "baseline": settings.baseline_dir,
        "current": settings.current_dir,
        "comparator": settings.comparator_dir,
        "report": settings.report_dir,
    }[kind]
    run_dir = kind_root / today / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text("{}", encoding="utf-8")
    (run_dir / "x.txt").write_text("hi", encoding="utf-8")
    log_path = settings.runs_log_dir / f"{db_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("subprocess output\n", encoding="utf-8")
    return run_dir


def test_delete_run_204_removes_db_row_and_on_disk_artifacts(client, fake_spawn):
    """Spawn a run, force terminal, delete, verify everything's gone."""
    r = client.post("/api/runs", json={"kind": "baseline"})
    assert r.status_code == 202
    db_id = r.json()["db_id"]
    run_id = r.json()["run_id"]

    db_path = settings.runs_db_path
    with dbmod.connection_scope(db_path) as conn:
        dbmod.mark_terminal(
            conn,
            db_id=db_id,
            status="done",
            finished_at=settings.get_current_datetime(),
            exit_code=0,
        )

    # Materialize artifacts the delete route should clean up.
    run_dir = _seed_artifacts_for(db_id, run_id, kind="baseline")
    assert run_dir.exists()

    r = client.delete(f"/api/runs/{db_id}")
    assert r.status_code == 204

    # DB row gone.
    with dbmod.connection_scope(db_path) as conn:
        assert dbmod.get_run(conn, db_id) is None
    # On-disk dir gone.
    assert not run_dir.exists()
    # Log file gone.
    assert not (settings.runs_log_dir / f"{db_id}.log").exists()


def test_delete_run_404_for_unknown_id(client):
    r = client.delete("/api/runs/99999")
    assert r.status_code == 404


def test_delete_run_409_for_running_row(client, fake_spawn):
    """An in-flight subprocess is still writing to the artifact dir;
    the delete route must refuse with 409 instead of orphaning the
    runner. Operator waits for terminal status, then deletes."""
    r = client.post("/api/runs", json={"kind": "baseline"})
    db_id = r.json()["db_id"]
    # fake_spawn leaves the row at `running` (it calls mark_running but
    # not mark_terminal). Try to delete it.
    r = client.delete(f"/api/runs/{db_id}")
    assert r.status_code == 409
    assert "in-flight" in r.json()["detail"].lower()
    # Row MUST still be there.
    with dbmod.connection_scope(settings.runs_db_path) as conn:
        assert dbmod.get_run(conn, db_id) is not None


def test_delete_run_204_when_artifacts_already_missing(client, fake_spawn):
    """Cleanup is best-effort: a row whose on-disk dir was already
    removed (e.g. operator manually `rm -rf`d data/) still deletes
    cleanly. No 500 from a missing rmtree target."""
    r = client.post("/api/runs", json={"kind": "baseline"})
    db_id = r.json()["db_id"]
    with dbmod.connection_scope(settings.runs_db_path) as conn:
        dbmod.mark_terminal(
            conn,
            db_id=db_id,
            status="failed",
            finished_at=settings.get_current_datetime(),
            exit_code=1,
        )
    # Don't seed any artifacts - directory simply doesn't exist.
    r = client.delete(f"/api/runs/{db_id}")
    assert r.status_code == 204


def test_bulk_delete_returns_per_id_outcomes(client, fake_spawn):
    """Best-effort bulk: returns deleted / skipped_not_found /
    skipped_in_flight as separate lists so the frontend can show a
    structured summary."""
    # Seed 3 rows: one terminal (deletable), one in-flight, one we'll
    # never create (not_found).
    r1 = client.post("/api/runs", json={"kind": "baseline"})
    r2 = client.post("/api/runs", json={"kind": "current"})
    db_path = settings.runs_db_path
    with dbmod.connection_scope(db_path) as conn:
        dbmod.mark_terminal(
            conn,
            db_id=r1.json()["db_id"],
            status="done",
            finished_at=settings.get_current_datetime(),
            exit_code=0,
        )
        # r2 left at running.

    r = client.post(
        "/api/runs/bulk-delete",
        json={"db_ids": [r1.json()["db_id"], r2.json()["db_id"], 99999]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] == [r1.json()["db_id"]]
    assert body["skipped_in_flight"] == [r2.json()["db_id"]]
    assert body["skipped_not_found"] == [99999]


def test_bulk_delete_422_for_empty_list(client):
    """Empty db_ids list = client bug = 422 (not a silent 200)."""
    r = client.post("/api/runs/bulk-delete", json={"db_ids": []})
    assert r.status_code == 422


def test_bulk_delete_idempotent_on_resubmit(client, fake_spawn):
    """Re-submitting the same db_ids after success returns them in
    `skipped_not_found` instead of failing - matches the operator's
    intuition that 'delete X' is idempotent."""
    r = client.post("/api/runs", json={"kind": "baseline"})
    db_id = r.json()["db_id"]
    with dbmod.connection_scope(settings.runs_db_path) as conn:
        dbmod.mark_terminal(
            conn,
            db_id=db_id,
            status="done",
            finished_at=settings.get_current_datetime(),
            exit_code=0,
        )

    first = client.post("/api/runs/bulk-delete", json={"db_ids": [db_id]})
    assert first.json()["deleted"] == [db_id]
    second = client.post("/api/runs/bulk-delete", json={"db_ids": [db_id]})
    assert second.json()["deleted"] == []
    assert second.json()["skipped_not_found"] == [db_id]


def test_lifespan_recovers_orphaned_running_row(tmp_path, monkeypatch):
    """A row left in `running` from a 'previous' dashboard instance must be
    transitioned to `interrupted` by the lifespan's startup recovery path -
    BEFORE sync runs (so sync doesn't double-insert)."""
    db_path = _wire_settings(tmp_path, monkeypatch)
    dbmod.init_db(db_path)
    with dbmod.connection_scope(db_path) as conn:
        db_id = dbmod.insert_pending_run(
            conn,
            run_id=new_run_id(),
            kind="baseline",
            args={},
            command=[],
            date_dir="01-01-2099",
            created_at="01-01-2099 00:00:00",
        )
        dbmod.mark_running(
            conn,
            db_id=db_id,
            pid=2**22 - 1,
            pgid=2**22 - 1,
            pid_start_time="999999",  # bogus → won't be killed
            started_at="01-01-2099 00:00:01",
        )

    with _client_with(db_path) as _:
        with dbmod.connection_scope(db_path) as conn:
            row = dbmod.get_run(conn, db_id)
    assert row["status"] == "interrupted"


# --------------------------------------------------------------------------- #
# Test isolation: drain in-flight watcher tasks so they don't bleed across   #
# test boundaries. Uses runner._active_watchers (the strong-ref set added    #
# in round-3 fix C1) instead of the previous hacky cancel-everything pattern #
# that would have masked unhandled exceptions inside watchers.               #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _empty_watcher_set_between_tests():
    """Reset `runner._active_watchers` between tests so a leaked watcher
    from one test (e.g. an unintended real spawn) doesn't contaminate
    the next.

    This file uses `fake_spawn` everywhere, so the set should be empty at
    the start of every test - pin that as an explicit invariant. At
    teardown we just clear the set; the underlying Tasks belong to
    whatever event loop pytest-asyncio used and are gone by then.

    The previous fixture cancelled tasks at teardown, which masked
    unhandled exceptions inside watchers - replaced (round-3 H3 fix) so
    a watcher failure now surfaces as a test failure during the run,
    not as silent log noise.
    """
    assert not runner._active_watchers, (
        "watcher set leaked from a previous test - investigate before pinning."
    )
    yield
    runner._active_watchers.clear()
