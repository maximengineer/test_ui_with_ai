"""Direct unit tests for report.confidence edge cases."""

from __future__ import annotations

from test_ui.report.confidence import calculate_confidence_metrics


def _full_success(*, confidence: float = 1.0) -> dict:
    return {
        "processing_status": "success",
        "ai_analysis": {
            "overall_severity": "SAFE",
            "business_impact": "LOW",
            "confidence_score": confidence,
            "detailed_analysis": {
                "visual_changes": ["v"],
                "functional_impact": ["f"],
                "technical_correlation": ["t"],
            },
            "recommendations": {
                "immediate_actions": ["none"],
                "review_items": ["none"],
                "acceptance_criteria": "ok",
            },
        },
        "structured_data": {
            "html_changes": {
                "changes": [
                    {"code_snippet": "<a>1</a>"},
                    {"code_snippet": "<a>2</a>"},
                    {"code_snippet": "<a>3</a>"},
                    {"code_snippet": "<a>4</a>"},
                    {"code_snippet": "<a>5</a>"},
                ]
            },
            "css_changes": {"changes": []},
            "js_changes": {"changes": []},
            "metadata": {"x": 1},
        },
        "screenshots_available": ["baseline", "current", "visual_diff"],
    }


def test_confidence_applies_small_sample_penalty():
    """Sample sizes < 3 are penalized in composite confidence."""
    one = calculate_confidence_metrics([_full_success()])
    three = calculate_confidence_metrics(
        [_full_success(), _full_success(), _full_success()]
    )

    assert one["composite_confidence"] == 0.8
    assert three["composite_confidence"] == 1.0
    assert three["composite_confidence"] > one["composite_confidence"]


def test_confidence_handles_mixed_success_error_and_marker_runs():
    """Errors/markers should not crash metrics and should affect rates."""
    success = _full_success(confidence=0.9)
    error = {
        "processing_status": "error",
        "ai_analysis": {"result_type": "analysis_error", "error_type": "provider_error"},
        "structured_data": {},
        "screenshots_available": [],
    }
    marker = {
        "processing_status": "no_changes",
        "ai_analysis": {"result_type": "no_changes"},
        "structured_data": {},
        "screenshots_available": [],
    }

    metrics = calculate_confidence_metrics([success, error, marker])

    assert metrics["total_urls"] == 3
    assert metrics["successful_analyses"] == 1
    assert metrics["success_rate"] == 0.333
    assert metrics["quality_indicators"]["error_rate"] == 0.333
    assert any("High error rate: 1/3 URLs failed processing" in w for w in metrics["validation_warnings"])
