"""Focused unit tests for report.generator edge paths."""

from __future__ import annotations

import json

import pytest

from test_ui.cli import _open_orchestrator
from test_ui.config import settings
from test_ui.report.generator import _synthesize_timeout_response


@pytest.mark.asyncio
async def test_process_single_url_surfaces_comparator_error_without_ai_call(
    tmp_path, monkeypatch
):
    """Comparator error payloads should become explicit ai_error records."""
    monkeypatch.setattr(settings, "ai_enabled", True)
    # If the code accidentally tries to call AI, this unreachable endpoint
    # would trigger a transport error and fail the assertions below.
    monkeypatch.setattr(settings, "ai_analyzer_service_url", "http://127.0.0.1:1")

    run_root = tmp_path / "report" / "01-01-2099" / "run"
    run_root.mkdir(parents=True)
    url_dir = tmp_path / "comparator" / "01-01-2099" / "site-1"
    url_dir.mkdir(parents=True)

    url_data = {
        "url_name": "site-1",
        "url_dir": url_dir,
        "structured_data_path": None,
        "has_changes": True,
        "comparison_data": {
            "metadata": {"url": "https://example.test/site-1"},
            "result": {
                "error": "missing_baseline",
                "message": "Site not found in baseline",
            },
        },
    }

    async with _open_orchestrator() as orch:
        out = await orch.reporter.process_single_url(url_data, run_root)

    assert out["processing_status"] == "error"
    assert out["ai_analysis"]["result_type"] == "analysis_error"
    assert out["ai_analysis"]["error_type"] == "config_error"
    assert "Comparator error for site-1" in out["ai_analysis"]["details"]
    assert "missing_baseline" in out["ai_analysis"]["details"]

    saved = run_root / "site-1" / "ai_error.json"
    assert saved.exists()
    payload = json.loads(saved.read_text(encoding="utf-8"))
    assert payload["result_type"] == "analysis_error"


def test_timeout_fallback_keeps_analysis_success_shape_contract():
    """Timeout fallback should still satisfy analysis_success field expectations."""
    response = _synthesize_timeout_response(
        request_id="req-123",
        structured_data={},
    )
    assert response["result_type"] == "analysis_success"
    assert isinstance(response["prompt_sha256"], str)
    assert len(response["prompt_sha256"]) == 64


@pytest.mark.asyncio
async def test_process_single_url_reports_structured_data_contract_error(
    tmp_path, monkeypatch
):
    """Invalid/missing diff JSON should fail before AI call with clear error details."""
    monkeypatch.setattr(settings, "ai_enabled", True)
    monkeypatch.setattr(settings, "ai_analyzer_service_url", "http://127.0.0.1:1")

    run_root = tmp_path / "report" / "01-01-2099" / "run"
    run_root.mkdir(parents=True)
    url_dir = tmp_path / "comparator" / "01-01-2099" / "site-1"
    diffs_dir = url_dir / "diffs"
    diffs_dir.mkdir(parents=True)

    def _w(name: str, payload: dict) -> None:
        (diffs_dir / name).write_text(json.dumps(payload), encoding="utf-8")

    _w(
        "change_summary.json",
        {
            "overall_assessment": {},
            "change_categories": {},
            "affected_components": [],
            "recommendation": "",
            "ai_analysis_priority": "low",
        },
    )
    _w(
        "html_changes.json",
        {
            "changes_detected": False,
            "change_types": [],
            "changes": [],
            "summary": {},
        },
    )
    _w(
        "css_changes.json",
        {
            "changes_detected": False,
            "change_types": [],
            "files_changed": [],
            "changes": [],
            "summary": {},
        },
    )
    # Intentionally omit js_changes.json.

    url_data = {
        "url_name": "site-1",
        "url_dir": url_dir,
        "structured_data_path": diffs_dir,
        "has_changes": True,
        "comparison_data": {
            "metadata": {"url": "https://example.test/site-1"},
            "result": {"changes_detected": True},
        },
    }

    async with _open_orchestrator() as orch:
        out = await orch.reporter.process_single_url(url_data, run_root)

    assert out["processing_status"] == "error"
    assert out["ai_analysis"]["result_type"] == "analysis_error"
    assert out["ai_analysis"]["error_type"] == "config_error"
    assert "Structured diff files missing/invalid" in out["ai_analysis"]["details"]
    assert "js_changes.json: file not found" in out["ai_analysis"]["details"]

    saved = run_root / "site-1" / "ai_error.json"
    assert saved.exists()
    payload = json.loads(saved.read_text(encoding="utf-8"))
    assert payload["result_type"] == "analysis_error"
