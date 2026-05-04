"""Golden snapshot test for ai_analysis.json output (Phase A.2).

Pins the byte-for-byte content of an `ai_analysis.json` file produced by
`ReportGenerator.process_single_url` for a known input + a deterministic
mocked AI response. Volatile fields (`request_id`, timestamps, screenshot
paths) are normalized before compare.

If A.3's refactor changes the output shape, this test fails - and that's the
signal to either:
  - update the test to reflect intentional new behavior, OR
  - run `pytest --update-golden` to accept the new output.

Either way, the change is visible in the diff. That's the whole point of a
characterization test.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from PIL import Image

from test_ui.cli import _open_orchestrator
from test_ui.config import settings


@pytest.fixture
def isolated_data_dirs(tmp_path, monkeypatch):
    report_dir = tmp_path / "report"
    comparator_dir = tmp_path / "comparator"
    report_dir.mkdir()
    comparator_dir.mkdir()
    monkeypatch.setattr(settings, "report_dir", report_dir)
    monkeypatch.setattr(settings, "comparator_dir", comparator_dir)
    monkeypatch.setattr(settings, "ai_analyzer_service_url", "http://test.local")
    return {"report": report_dir, "comparator": comparator_dir, "tmp": tmp_path}


def _seed_one_url(
    comparator_root: Path,
    date: str,
    url_name: str,
    example_diffs: Path,
    baseline_path: Path,
    current_path: Path,
) -> None:
    url_dir = comparator_root / date / url_name
    diffs = url_dir / "diffs"
    diffs.mkdir(parents=True)
    for fname in (
        "change_summary.json",
        "html_changes.json",
        "css_changes.json",
        "js_changes.json",
    ):
        (diffs / fname).write_text((example_diffs / fname).read_text())
    cr = {
        "metadata": {
            "url": f"https://test.example/{url_name}",
            "baseline_path": str(baseline_path),
            "current_path": str(current_path),
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


@pytest.mark.asyncio
async def test_golden_ai_analysis_success_shape(
    isolated_data_dirs, example_diffs_dir, golden_compare
):
    date = "01-01-2099"
    url_name = "test_demo_url"

    baseline_root = isolated_data_dirs["tmp"] / "baseline"
    current_root = isolated_data_dirs["tmp"] / "current"
    for root in (baseline_root, current_root):
        (root / url_name).mkdir(parents=True)
        Image.new("RGB", (10, 10), "red").save(root / url_name / "screenshot.png")

    _seed_one_url(
        isolated_data_dirs["comparator"],
        date,
        url_name,
        example_diffs_dir,
        baseline_root,
        current_root,
    )

    ai_body = {
        "schema_version": "2026-04-30.1",
        "result_type": "analysis_success",
        "request_id": "GOLDEN_REQ_ID_PLACEHOLDER",
        "model": "gemini-test-model",
        "prompt_sha256": "abc123" + "0" * 58,
        "overall_severity": "WARNING",
        "business_impact": "MEDIUM",
        "detailed_analysis": {
            "visual_changes": ["Hero section moved 40px down"],
            "functional_impact": ["No interactive features affected"],
            "technical_correlation": ["CSS .hero margin-top increased"],
        },
        "recommendations": {
            "immediate_actions": [],
            "review_items": ["Confirm intentional layout shift"],
            "acceptance_criteria": "Accept if hero positioning is part of design refresh",
        },
        "confidence_score": 0.85,
    }

    with respx.mock(base_url="http://test.local") as mock:

        def _handler(request):
            req_body = json.loads(request.content)
            response = dict(ai_body)
            response["request_id"] = req_body["request_id"]
            return httpx.Response(200, json=response)

        mock.post("/api/compare").mock(side_effect=_handler)

        # Phase B.1: process_single_url now takes a `run_root` instead of
        # `date` - tests construct one explicitly so assertions can find it
        # at a stable path (no ULID resolution).
        run_root = isolated_data_dirs["report"] / date / "test-run"
        run_root.mkdir(parents=True)

        async with _open_orchestrator() as orchestrator:
            url_data = {
                "url_name": url_name,
                "url_dir": isolated_data_dirs["comparator"] / date / url_name,
                "structured_data_path": isolated_data_dirs["comparator"]
                / date
                / url_name
                / "diffs",
                "has_changes": True,
                "comparison_data": json.loads(
                    (
                        isolated_data_dirs["comparator"]
                        / date
                        / url_name
                        / "comparison_results.json"
                    ).read_text()
                ),
            }
            await orchestrator.reporter.process_single_url(url_data, run_root)

    ai_file = run_root / url_name / "ai_analysis.json"
    assert ai_file.exists(), "ai_analysis.json not written"
    actual = json.loads(ai_file.read_text())
    golden_compare(actual, "ai_analysis_success.json", normalize=("request_id",))


@pytest.mark.asyncio
async def test_golden_ai_error_shape(
    isolated_data_dirs, example_diffs_dir, golden_compare, tmp_path
):
    date = "02-01-2099"
    url_name = "test_demo_url"

    baseline_root = tmp_path / "baseline"
    current_root = tmp_path / "current"
    for root in (baseline_root, current_root):
        (root / url_name).mkdir(parents=True)
        Image.new("RGB", (10, 10), "red").save(root / url_name / "screenshot.png")

    _seed_one_url(
        isolated_data_dirs["comparator"],
        date,
        url_name,
        example_diffs_dir,
        baseline_root,
        current_root,
    )

    with respx.mock(base_url="http://test.local") as mock:

        def _handler(request):
            req_body = json.loads(request.content)
            return httpx.Response(
                502,
                json={
                    "schema_version": "2026-04-30.1",
                    "result_type": "analysis_error",
                    "request_id": req_body["request_id"],
                    "model": "gemini-test-model",
                    "prompt_sha256": "abc123" + "0" * 58,
                    "error_type": "provider_error",
                    "retryable": False,
                    "details": "test-induced provider error",
                },
            )

        mock.post("/api/compare").mock(side_effect=_handler)

        run_root = isolated_data_dirs["report"] / date / "test-run"
        run_root.mkdir(parents=True)

        async with _open_orchestrator() as orchestrator:
            url_data = {
                "url_name": url_name,
                "url_dir": isolated_data_dirs["comparator"] / date / url_name,
                "structured_data_path": isolated_data_dirs["comparator"]
                / date
                / url_name
                / "diffs",
                "has_changes": True,
                "comparison_data": json.loads(
                    (
                        isolated_data_dirs["comparator"]
                        / date
                        / url_name
                        / "comparison_results.json"
                    ).read_text()
                ),
            }
            await orchestrator.reporter.process_single_url(url_data, run_root)

    ai_file = run_root / url_name / "ai_error.json"
    assert ai_file.exists(), "ai_error.json not written"
    actual = json.loads(ai_file.read_text())
    golden_compare(actual, "ai_error.json", normalize=("request_id",))


def test_golden_no_changes_shape(isolated_data_dirs, golden_compare):
    import asyncio

    date = "03-01-2099"
    url_name = "no_change_url"
    run_root = isolated_data_dirs["report"] / date / "test-run"
    run_root.mkdir(parents=True)

    async def _run():
        async with _open_orchestrator() as orch:
            orch.reporter.process_urls_without_changes(
                [{"url_name": url_name}], run_root
            )

    asyncio.run(_run())
    nc_file = run_root / url_name / "no_changes.json"
    assert nc_file.exists()
    actual = json.loads(nc_file.read_text())
    golden_compare(actual, "no_changes.json", normalize=("checked_at",))
