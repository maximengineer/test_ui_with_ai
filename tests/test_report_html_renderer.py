"""Focused unit tests for report.html_renderer template behavior."""

from __future__ import annotations

from pathlib import Path

from test_ui.report import html_renderer


def test_render_error_panel_uses_retry_url_command():
    """The HTML error panel should point to the real retry CLI command."""
    template_data = {
        "timestamp": "01-01-2099 00:00:00",
        "report_date": "01-01-2099",
        "aggregation": {
            "summary": {
                "total_urls_analyzed": 1,
                "critical_issues": 0,
                "warnings": 0,
                "safe_changes": 0,
                "errors": 1,
            },
            "confidence_metrics": {
                "success_rate": 0.0,
                "ai_confidence": {"average": 0.0, "min": 0.0, "max": 0.0},
            },
            "patterns": {},
            "recommendations": {},
        },
        "url_results": [
            {
                "url": "site-1",
                "ai_analysis": {
                    "result_type": "analysis_error",
                    "error_type": "provider_error",
                    "retryable": True,
                    "details": "upstream timeout",
                },
                "structured_data": {},
                "report_path": Path("/tmp/report/site-1"),
                "screenshots_available": [],
            }
        ],
        "total_urls": 1,
        "has_critical": False,
        "has_warnings": False,
        "has_errors": True,
        "confidence_level": "low",
        "system_status": "error",
    }

    rendered = html_renderer.render(template_data)
    assert "afr retry-url --date 01-01-2099 --url site-1" in rendered
    assert "--only" not in rendered
