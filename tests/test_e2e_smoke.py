"""End-to-end smoke test for the report-generation pipeline (Phase A.2).

Drives the orchestration manually (discovery → per-URL processing) against a
synthetic comparator tree on disk + a respx-mocked AI analyzer service.
Asserts each per-URL result file is the expected shape (typed result_type)
and that the writer's stale-file clearing is working.

**Does NOT exercise HTML rendering.** The renderer's Jinja template references
fields that don't exist in the calculator output (e.g. `confidence_metrics.
average_confidence` vs. the actual `confidence_metrics.ai_confidence.average`)
— it raises `UndefinedError` on every render. This is a pre-existing bug
flagged for A.3 to fix as part of the html_renderer split. Characterization
tests should pin behavior we want to preserve, not bake in brokenness, so we
just don't test the render path.

This test is the safety net for Phase A.3 — the god-object refactor must not
change the observable per-URL persistence behavior pinned here.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from test_ui.config import settings
from test_ui.cli import _open_orchestrator


# ---------------------------------------------------------------------------
# Helpers — build a synthetic comparator tree on disk
# ---------------------------------------------------------------------------


def _seed_comparator_tree(
    comparator_root: Path, date: str, example_diffs_dir: Path
) -> dict[str, Path]:
    """Lay out three URLs under `<comparator_root>/<date>/`:
      - one with changes (will hit the mocked AI → analysis_success)
      - one with changes that will hit ai_disabled (when settings flips)
      - one without changes (NoChangesMarker path)
    Returns a dict: {url_name: dir}.
    """
    base = comparator_root / date
    base.mkdir(parents=True, exist_ok=True)

    def _with_changes(name: str) -> Path:
        url_dir = base / name
        url_dir.mkdir(parents=True, exist_ok=True)
        diffs = url_dir / "diffs"
        diffs.mkdir(exist_ok=True)
        for fname in (
            "change_summary.json",
            "html_changes.json",
            "css_changes.json",
            "js_changes.json",
        ):
            (diffs / fname).write_text((example_diffs_dir / fname).read_text())
        # comparison_results.json with screenshot paths that don't exist —
        # the loader gracefully handles missing screenshots.
        cr = {
            "metadata": {
                "url": f"https://test.example/{name}",
                "baseline_path": "/tmp/missing-baseline",
                "current_path": "/tmp/missing-current",
            },
            "result": {
                "changes_detected": True,
                "screenshot": {
                    "visual_changes": True,
                    "ssim_score": 0.85,
                    "diff_image_path": "",
                },
                "dom": {"has_changes": True},
                "assets": {
                    "css": {"has_changes": True},
                    "js": {"has_changes": False},
                    "media": {"has_changes": False},
                },
            },
        }
        (url_dir / "comparison_results.json").write_text(json.dumps(cr))
        return url_dir

    def _no_changes(name: str) -> Path:
        url_dir = base / name
        url_dir.mkdir(parents=True, exist_ok=True)
        cr = {
            "metadata": {"url": f"https://test.example/{name}"},
            "result": {
                "changes_detected": False,
                "screenshot": {"visual_changes": False, "ssim_score": 1.0},
                "dom": {"has_changes": False},
                "assets": {
                    "css": {"has_changes": False},
                    "js": {"has_changes": False},
                    "media": {"has_changes": False},
                },
            },
        }
        (url_dir / "comparison_results.json").write_text(json.dumps(cr))
        return url_dir

    return {
        "url_changes_a": _with_changes("url_changes_a"),
        "url_changes_b": _with_changes("url_changes_b"),
        "url_no_changes_c": _no_changes("url_no_changes_c"),
    }


def _mocked_ai_response_body(request_id: str) -> dict:
    """Deterministic AIAnalysisResponse body the mocked AI service returns."""
    return {
        "schema_version": "2026-04-30.1",
        "result_type": "analysis_success",
        "request_id": request_id,
        "model": "test-model",
        "prompt_sha256": "f" * 64,
        "overall_severity": "WARNING",
        "business_impact": "MEDIUM",
        "detailed_analysis": {
            "visual_changes": ["Hero section moved 40px down"],
            "functional_impact": ["No interactive features affected"],
            "technical_correlation": ["CSS .hero { margin-top } increased"],
        },
        "recommendations": {
            "immediate_actions": [],
            "review_items": ["Confirm intentional layout shift"],
            "acceptance_criteria": "Accept if hero positioning is part of the design refresh",
        },
        "confidence_score": 0.85,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_data_dirs(tmp_path, monkeypatch):
    """Redirect settings.report_dir + comparator_dir to a tmp tree per test."""
    report_dir = tmp_path / "report"
    comparator_dir = tmp_path / "comparator"
    report_dir.mkdir()
    comparator_dir.mkdir()
    monkeypatch.setattr(settings, "report_dir", report_dir)
    monkeypatch.setattr(settings, "comparator_dir", comparator_dir)
    monkeypatch.setattr(settings, "ai_analyzer_service_url", "http://test.local")
    return {"report": report_dir, "comparator": comparator_dir}


async def _drive_orchestration(orchestrator, comparator_root: Path, date: str) -> None:
    """Drive the real `Orchestrator.generate_enhanced_report` end-to-end.

    Phase B.1: this calls the full pipeline including atomic publication
    + HTML render. The pre-A.3 helper bypassed HTML render to dodge a
    template/data-shape bug; that bug was fixed in A.3, so we exercise the
    whole flow here.
    """
    await orchestrator.generate_enhanced_report(
        comparator_root=comparator_root,
        report_date=date,
    )


def _published_report_run_dir(report_root: Path, date: str) -> Path:
    """Resolve the test's freshly-published report run dir.

    `report_root/<date>/` contains exactly one ULID subdir (the test only
    publishes once); return it. Uses the production finder so test mirrors
    real-world resolution.
    """
    from test_ui.comparator.finder import find_latest_run_dir_in_date

    run_root = find_latest_run_dir_in_date(report_root / date)
    assert run_root is not None, (
        f"no published report run found in {report_root / date}"
    )
    return run_root


def _make_real_screenshots(
    baseline_root: Path, current_root: Path, url_names: list[str]
) -> None:
    """Lay down minimal valid PNGs at the paths process_single_url expects."""
    from PIL import Image

    for root in (baseline_root, current_root):
        for name in url_names:
            url_dir = root / name
            url_dir.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (10, 10), "red").save(url_dir / "screenshot.png")


@pytest.mark.asyncio
async def test_e2e_smoke_pipeline_success_path(
    isolated_data_dirs, example_diffs_dir, tmp_path
):
    """Drive the per-URL pipeline against a mocked AI returning analysis_success.
    Pin: each with-changes URL gets ai_analysis.json with the typed shape; the
    no-changes URL gets no_changes.json. Stale-file clearing means no orphan
    ai_error.json or other crossover."""
    date = "01-01-2099"
    seeded = _seed_comparator_tree(
        isolated_data_dirs["comparator"], date, example_diffs_dir
    )

    # Real screenshots so process_single_url's screenshot-loader doesn't fail.
    # The seed function set baseline_path/current_path in comparison_results.json
    # to /tmp/missing-* — overwrite those with paths that have real PNGs.
    baseline_root = tmp_path / "real-baseline"
    current_root = tmp_path / "real-current"
    _make_real_screenshots(
        baseline_root, current_root, ["url_changes_a", "url_changes_b"]
    )
    for name in ("url_changes_a", "url_changes_b"):
        cr_path = seeded[name] / "comparison_results.json"
        cr = json.loads(cr_path.read_text())
        cr["metadata"]["baseline_path"] = str(baseline_root)
        cr["metadata"]["current_path"] = str(current_root)
        cr_path.write_text(json.dumps(cr))

    with respx.mock(base_url="http://test.local") as mock:

        def _ai_handler(request):
            body = json.loads(request.content)
            return httpx.Response(
                200, json=_mocked_ai_response_body(body["request_id"])
            )

        mock.post("/api/compare").mock(side_effect=_ai_handler)

        async with _open_orchestrator() as orchestrator:
            await _drive_orchestration(
                orchestrator, isolated_data_dirs["comparator"], date
            )

    run_root = _published_report_run_dir(isolated_data_dirs["report"], date)
    for name in ("url_changes_a", "url_changes_b"):
        url_dir = run_root / name
        assert (url_dir / "ai_analysis.json").exists(), (
            f"{name}: missing ai_analysis.json"
        )
        assert not (url_dir / "ai_error.json").exists(), f"{name}: stale ai_error.json"
        assert not (url_dir / "no_changes.json").exists(), (
            f"{name}: stray no_changes.json"
        )
        data = json.loads((url_dir / "ai_analysis.json").read_text())
        assert data["result_type"] == "analysis_success"
        assert data["overall_severity"] == "WARNING"
        assert data["model"] == "test-model"

    nc_dir = run_root / "url_no_changes_c"
    assert (nc_dir / "no_changes.json").exists(), "no_changes URL missing marker"
    assert not (nc_dir / "ai_analysis.json").exists()
    nc = json.loads((nc_dir / "no_changes.json").read_text())
    assert nc["result_type"] == "no_changes"


@pytest.mark.asyncio
async def test_e2e_pipeline_handles_ai_error(
    isolated_data_dirs, example_diffs_dir, tmp_path
):
    """When the AI service returns a typed error, ai_error.json is written
    (no stale ai_analysis.json), and the pipeline does NOT crash."""
    date = "02-01-2099"
    seeded = _seed_comparator_tree(
        isolated_data_dirs["comparator"], date, example_diffs_dir
    )

    # Real screenshots so we get past the screenshot-loading step and actually
    # exercise the AI-call → error-response → ai_error.json path.
    baseline_root = tmp_path / "real-baseline"
    current_root = tmp_path / "real-current"
    _make_real_screenshots(
        baseline_root, current_root, ["url_changes_a", "url_changes_b"]
    )
    for name in ("url_changes_a", "url_changes_b"):
        cr_path = seeded[name] / "comparison_results.json"
        cr = json.loads(cr_path.read_text())
        cr["metadata"]["baseline_path"] = str(baseline_root)
        cr["metadata"]["current_path"] = str(current_root)
        cr_path.write_text(json.dumps(cr))

    with respx.mock(base_url="http://test.local") as mock:

        def _err_handler(request):
            body = json.loads(request.content)
            return httpx.Response(
                502,
                json={
                    "schema_version": "2026-04-30.1",
                    "result_type": "analysis_error",
                    "request_id": body["request_id"],
                    "model": "test-model",
                    "prompt_sha256": "f" * 64,
                    "error_type": "provider_error",
                    "retryable": False,
                    "details": "test-induced provider error",
                },
            )

        mock.post("/api/compare").mock(side_effect=_err_handler)

        async with _open_orchestrator() as orchestrator:
            await _drive_orchestration(
                orchestrator, isolated_data_dirs["comparator"], date
            )

    run_root = _published_report_run_dir(isolated_data_dirs["report"], date)
    for name in ("url_changes_a", "url_changes_b"):
        url_dir = run_root / name
        assert (url_dir / "ai_error.json").exists(), f"{name}: missing ai_error.json"
        assert not (url_dir / "ai_analysis.json").exists(), (
            f"{name}: stale ai_analysis.json"
        )
        data = json.loads((url_dir / "ai_error.json").read_text())
        assert data["result_type"] == "analysis_error"
        assert data["error_type"] == "provider_error"


@pytest.mark.asyncio
async def test_e2e_pipeline_ai_disabled(
    isolated_data_dirs, example_diffs_dir, monkeypatch
):
    """AFR_AI_ENABLED=false: ai_disabled.json written, AI service NOT called."""
    date = "03-01-2099"
    _seed_comparator_tree(isolated_data_dirs["comparator"], date, example_diffs_dir)
    monkeypatch.setattr(settings, "ai_enabled", False)

    # respx route registered solely to detect call attempts. assert_all_called
    # disabled because the WHOLE POINT is that this route SHOULD NOT be called.
    with respx.mock(base_url="http://test.local", assert_all_called=False) as mock:
        ai_route = mock.post("/api/compare")

        async with _open_orchestrator() as orchestrator:
            await _drive_orchestration(
                orchestrator, isolated_data_dirs["comparator"], date
            )

        assert not ai_route.called, "AI service was called despite AFR_AI_ENABLED=false"

    run_root = _published_report_run_dir(isolated_data_dirs["report"], date)
    for name in ("url_changes_a", "url_changes_b"):
        url_dir = run_root / name
        assert (url_dir / "ai_disabled.json").exists(), (
            f"{name}: missing ai_disabled.json"
        )
        assert not (url_dir / "ai_analysis.json").exists()
        assert not (url_dir / "ai_error.json").exists()
        data = json.loads((url_dir / "ai_disabled.json").read_text())
        assert data["result_type"] == "ai_disabled"
