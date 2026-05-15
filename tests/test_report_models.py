"""Focused tests for report.models typed row views."""

from __future__ import annotations

from test_ui.report import models


def test_url_result_view_derives_processing_status_from_result_type():
    row = models.URLResultView.model_validate(
        {
            "url": "site-a",
            "ai_analysis": {"result_type": "analysis_error", "error_type": "timeout"},
        }
    )
    assert row.processing_status == "error"
    assert row.is_analysis_error is True


def test_url_result_view_derives_legacy_no_changes_status():
    row = models.URLResultView.model_validate(
        {
            "url": "site-b",
            "ai_analysis": {"analysis_type": "no_changes_detected"},
        }
    )
    assert row.processing_status == "no_changes"
    assert row.is_url_with_changes is False


def test_coerce_result_views_accepts_mixed_row_inputs():
    typed = models.URLResultView.model_validate(
        {"url": "typed", "ai_analysis": {"result_type": "analysis_success"}}
    )
    rows = models.coerce_result_views(
        [
            typed,
            {"url": "dict", "ai_analysis": {"result_type": "ai_disabled"}},
        ]
    )
    assert len(rows) == 2
    assert rows[0].url == "typed"
    assert rows[1].processing_status == "ai_disabled"

