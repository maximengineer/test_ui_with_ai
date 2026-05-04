"""Pydantic contract validation tests.

Phase A.1.2 deliverable. Verifies:
- The example diff fixtures parse against StructuredData (catches drift between
  the comparator's output shape and the contract).
- A full AIAnalysisRequest can be constructed.
- The discriminated AnalysisOutput union dispatches correctly.

The cross-language Pydantic↔ajv smoke test lives in test_contract_smoke.py
(Phase A.1.6) once Node tooling is wired up.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from pydantic import TypeAdapter, ValidationError
import pytest

from test_ui.contracts.ai_contract import (
    AIAnalysisError,
    AIAnalysisRequest,
    AIAnalysisResponse,
    AIDisabledMarker,
    AnalysisOutput,
    NoChangesMarker,
    SCHEMA_VERSION,
    StructuredData,
)


def _load_structured_data(example_diffs_dir: Path) -> StructuredData:
    return StructuredData(
        change_summary=json.loads(
            (example_diffs_dir / "change_summary.json").read_text()
        ),
        html_changes=json.loads((example_diffs_dir / "html_changes.json").read_text()),
        css_changes=json.loads((example_diffs_dir / "css_changes.json").read_text()),
        js_changes=json.loads((example_diffs_dir / "js_changes.json").read_text()),
    )


def test_example_fixtures_match_structured_data_shape(example_diffs_dir):
    """Comparator output shape and the contract agree.

    If this fails after a comparator change, either tighten the contract or
    update the fixtures. Don't widen the contract to `dict[str, Any]` to make
    this pass - that defeats the point of having a contract.
    """
    sd = _load_structured_data(example_diffs_dir)
    assert sd.html_changes.summary["total_changes"] == 5
    assert sd.css_changes.changes_detected is True
    assert sd.js_changes.changes_detected is False
    assert sd.change_summary.ai_analysis_priority == "high"


def test_ai_analysis_request_constructs_from_fixtures(example_diffs_dir):
    sd = _load_structured_data(example_diffs_dir)
    req = AIAnalysisRequest(
        request_id=str(uuid.uuid4()),
        url="https://example.com/about",
        structured_data=sd,
        screenshots={},  # all None defaults
    )
    assert req.schema_version == SCHEMA_VERSION
    assert req.url == "https://example.com/about"


def test_ai_analysis_request_rejects_unknown_field(example_diffs_dir):
    """extra='forbid' catches drift early."""
    sd = _load_structured_data(example_diffs_dir)
    with pytest.raises(ValidationError):
        AIAnalysisRequest(
            request_id=str(uuid.uuid4()),
            url="https://example.com",
            structured_data=sd,
            screenshots={},
            unknown_field="surprise",  # noqa
        )


def test_analysis_output_discriminator_routes_correctly():
    """Each result_type lands on the right Pydantic class."""
    adapter = TypeAdapter(AnalysisOutput)

    success = adapter.validate_python(
        {
            "result_type": "analysis_success",
            "request_id": str(uuid.uuid4()),
            "model": "gemini-2.5-pro",
            "prompt_sha256": "a" * 64,
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
                "acceptance_criteria": "",
            },
            "confidence_score": 0.95,
        }
    )
    assert isinstance(success, AIAnalysisResponse)

    err = adapter.validate_python(
        {
            "result_type": "analysis_error",
            "request_id": str(uuid.uuid4()),
            "error_type": "timeout",
            "retryable": True,
            "details": "request timed out after 60s",
        }
    )
    assert isinstance(err, AIAnalysisError)
    assert err.model is None  # optional, defaulted

    no_changes = adapter.validate_python(
        {
            "result_type": "no_changes",
            "checked_at": "30-04-2026 14:23:11",
        }
    )
    assert isinstance(no_changes, NoChangesMarker)

    disabled = adapter.validate_python(
        {
            "result_type": "ai_disabled",
            "checked_at": "30-04-2026 14:23:11",
        }
    )
    assert isinstance(disabled, AIDisabledMarker)


def test_confidence_score_bounded():
    """Confidence must be in [0, 1]; out-of-range rejected."""
    base = {
        "result_type": "analysis_success",
        "request_id": str(uuid.uuid4()),
        "model": "gemini-2.5-pro",
        "prompt_sha256": "a" * 64,
        "overall_severity": "SAFE",
        "business_impact": "NONE",
        "detailed_analysis": {
            "visual_changes": [],
            "functional_impact": [],
            "technical_correlation": [],
        },
        "recommendations": {
            "immediate_actions": [],
            "review_items": [],
            "acceptance_criteria": "",
        },
    }
    AIAnalysisResponse(**base, confidence_score=0.0)
    AIAnalysisResponse(**base, confidence_score=1.0)
    with pytest.raises(ValidationError):
        AIAnalysisResponse(**base, confidence_score=1.5)
    with pytest.raises(ValidationError):
        AIAnalysisResponse(**base, confidence_score=-0.1)


def test_error_type_must_be_known_enum():
    base = {
        "result_type": "analysis_error",
        "request_id": str(uuid.uuid4()),
        "retryable": True,
        "details": "x",
    }
    AIAnalysisError(**base, error_type="timeout")
    with pytest.raises(ValidationError):
        AIAnalysisError(**base, error_type="bogus_error_type")
