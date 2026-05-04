"""Direct unit tests for `generate_global_recommendations`.

The deep-dive audit caught (I4) that the function was counting errors by
the legacy `overall_severity == "ERROR"` predicate, which post-A.1.8 is
always 0 (errors live in `AIAnalysisError(result_type="analysis_error")`
now, with severity constrained to CRITICAL/WARNING/SAFE). The fix now
counts by `result_type == "analysis_error"`. Pre-fix this had only
transitive coverage via the report goldens - pin it directly here.
"""

from __future__ import annotations

from test_ui.report.aggregator import generate_global_recommendations


def _success(severity: str) -> dict:
    """Synthesize a per-URL result dict for a successful analysis."""
    return {
        "ai_analysis": {
            "result_type": "analysis_success",
            "overall_severity": severity,
        }
    }


def _error(error_type: str = "provider_error") -> dict:
    """Synthesize a per-URL result dict for an analysis error."""
    return {
        "ai_analysis": {
            "result_type": "analysis_error",
            "error_type": error_type,
        }
    }


def test_critical_count_drives_immediate_actions():
    recs = generate_global_recommendations([_success("CRITICAL"), _success("WARNING")])
    assert any("critical" in s.lower() for s in recs["immediate_actions"])
    assert recs["immediate_actions"], "critical → must populate immediate_actions"


def test_warning_count_drives_strategic_actions():
    recs = generate_global_recommendations([_success("WARNING"), _success("WARNING")])
    assert any("warning" in s.lower() for s in recs["strategic_actions"])


def test_error_count_uses_result_type_not_severity():
    """The post-fix predicate. Pre-fix used `overall_severity == "ERROR"`,
    which is impossible under the current contract (severity Literal is
    CRITICAL/WARNING/SAFE only). This test would have failed on the
    pre-fix code: `_error()` produces no overall_severity field, so
    error_count was always 0 and process_improvements stayed empty."""
    recs = generate_global_recommendations([_error(), _error()])
    assert recs["process_improvements"], (
        "two analysis_error results must trigger process_improvements"
    )
    assert any("2 analysis error" in s.lower() for s in recs["process_improvements"])


def test_zero_errors_leaves_process_improvements_empty():
    """Sanity: if no errors, no process_improvements rec fires."""
    recs = generate_global_recommendations([_success("SAFE"), _success("WARNING")])
    assert recs["process_improvements"] == []


def test_single_error_uses_singular_grammar():
    """Pin the singular-vs-plural string handling."""
    recs = generate_global_recommendations([_error()])
    msg = recs["process_improvements"][0]
    assert "1 analysis error" in msg
    assert "errors" not in msg.lower(), "singular form expected"


def test_legacy_overall_severity_ERROR_does_NOT_count():
    """Defensive: the legacy `overall_severity: "ERROR"` shape (only
    persisted by A.1.5 and earlier) MUST NOT count as an error post-fix.
    Old goldens with that shape should be loaded as zero errors so the
    operator gets accurate counts based on the new contract."""
    legacy_error_record = {"ai_analysis": {"overall_severity": "ERROR"}}
    recs = generate_global_recommendations([legacy_error_record])
    assert recs["process_improvements"] == [], (
        "legacy severity=ERROR records must NOT count as analysis errors"
    )


def test_monitoring_suggestions_always_populated():
    """Sanity: monitoring suggestions are emitted regardless of inputs."""
    recs = generate_global_recommendations([])
    assert recs["monitoring_suggestions"], (
        "must emit at least one monitoring suggestion"
    )
