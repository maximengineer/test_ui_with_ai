"""Focused unit tests for report.loader helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_ui.config import settings
from test_ui.report import loader


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _valid_analysis_success(*, request_id: str) -> dict:
    return {
        "schema_version": "2026-04-30.1",
        "result_type": "analysis_success",
        "request_id": request_id,
        "model": "test-model",
        "prompt_sha256": "0" * 64,
        "overall_severity": "SAFE",
        "business_impact": "LOW",
        "detailed_analysis": {
            "visual_changes": ["none"],
            "functional_impact": ["none"],
            "technical_correlation": ["none"],
        },
        "recommendations": {
            "immediate_actions": [],
            "review_items": ["none"],
            "acceptance_criteria": "none",
        },
        "confidence_score": 0.9,
    }


def _valid_structured_data_parts() -> dict[str, dict]:
    return {
        "change_summary.json": {
            "overall_assessment": {},
            "change_categories": {},
            "affected_components": [],
            "recommendation": "",
            "ai_analysis_priority": "low",
        },
        "html_changes.json": {
            "changes_detected": False,
            "change_types": [],
            "changes": [],
            "summary": {},
        },
        "css_changes.json": {
            "changes_detected": False,
            "change_types": [],
            "files_changed": [],
            "changes": [],
            "summary": {},
        },
        "js_changes.json": {
            "changes_detected": False,
            "change_types": [],
            "files_changed": [],
            "changes": [],
            "summary": {},
        },
    }


def test_load_all_url_results_sorts_url_dirs_stably(tmp_path):
    """Result order should be deterministic regardless of filesystem order."""
    run_root = tmp_path / "report-run"
    run_root.mkdir()

    # Create out of lexical order on purpose.
    for name in ("z-site", "a-site", "m-site"):
        url_dir = run_root / name
        url_dir.mkdir()
        _write_json(
            url_dir / "ai_analysis.json",
            _valid_analysis_success(request_id=f"req-{name}"),
        )
        _write_json(url_dir / "structured_data.json", {})

    loaded = loader.load_all_url_results(run_root)
    assert [row["url"] for row in loaded] == ["a-site", "m-site", "z-site"]


def test_load_all_url_results_skips_invalid_typed_analysis_payload(tmp_path):
    """Typed result files with invalid contract shape are skipped."""
    run_root = tmp_path / "report-run"
    run_root.mkdir()

    bad_dir = run_root / "bad"
    bad_dir.mkdir()
    # Invalid analysis_success: missing required fields.
    _write_json(
        bad_dir / "ai_analysis.json",
        {
            "result_type": "analysis_success",
            "overall_severity": "WARNING",
        },
    )
    _write_json(bad_dir / "structured_data.json", {})

    good_dir = run_root / "good"
    good_dir.mkdir()
    _write_json(
        good_dir / "ai_analysis.json",
        _valid_analysis_success(request_id="req-1"),
    )
    _write_json(good_dir / "structured_data.json", {})

    loaded = loader.load_all_url_results(run_root)
    assert [row["url"] for row in loaded] == ["good"]


def test_load_all_url_results_keeps_legacy_payload_without_result_type(tmp_path):
    """Pre-A.1.8 report files without result_type remain loadable."""
    run_root = tmp_path / "report-run"
    run_root.mkdir()
    url_dir = run_root / "legacy"
    url_dir.mkdir()
    _write_json(
        url_dir / "ai_analysis.json",
        {
            "overall_severity": "WARNING",
            "analysis_type": "legacy",
        },
    )
    _write_json(url_dir / "structured_data.json", {})

    loaded = loader.load_all_url_results(run_root)
    assert len(loaded) == 1
    assert loaded[0]["url"] == "legacy"
    assert loaded[0]["processing_status"] == "success"


def test_load_structured_data_raises_clear_error_when_required_file_missing(tmp_path):
    """Missing required comparator diff files should fail early with filename context."""
    diffs = tmp_path / "diffs"
    diffs.mkdir()
    parts = _valid_structured_data_parts()
    for fname, payload in parts.items():
        if fname == "js_changes.json":
            continue
        _write_json(diffs / fname, payload)

    with pytest.raises(ValueError, match="js_changes.json: file not found"):
        loader.load_structured_data(diffs)


def test_load_structured_data_raises_on_contract_validation_error(tmp_path):
    """Malformed structured diff JSON should fail at loader boundary validation."""
    diffs = tmp_path / "diffs"
    diffs.mkdir()
    parts = _valid_structured_data_parts()
    # Break required contract field.
    del parts["html_changes.json"]["changes_detected"]
    for fname, payload in parts.items():
        _write_json(diffs / fname, payload)

    with pytest.raises(ValueError, match="html_changes.changes_detected"):
        loader.load_structured_data(diffs)


def test_load_screenshots_can_omit_ai_base64_when_redaction_enabled(
    tmp_path, monkeypatch
):
    """Screenshot redaction keeps local paths but omits AI payload bodies."""
    monkeypatch.setattr(settings, "ai_redact_screenshots", True)
    url_dir = tmp_path / "site"
    diffs = url_dir / "diffs"
    diffs.mkdir(parents=True)
    # Minimal valid 1x1 PNG.
    (diffs / "visual_diff.png").write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
            "1f15c4890000000a49444154789c6360000002000100ffff030000060005"
            "57bfab0000000049454e44ae426082"
        )
    )

    screenshots = loader.load_screenshots(url_dir)

    assert "visual_diff_path" in screenshots
    assert screenshots["visual_diff_redacted"] is True
    assert "visual_diff_b64" not in screenshots
