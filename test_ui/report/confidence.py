"""Confidence-metric calculation (Phase A.3 split from aggregator).

Pure functions. The composite confidence weights AI confidence (50%), data
quality (30%), and analysis completeness (20%), then penalizes very small
samples. The thresholds in `_determine_confidence_level` are heuristic - if
they're ever tightened, also revisit `html_renderer.build_template_data`'s
"high if composite >= 0.8 else medium" check, which mirrors them.
"""

from __future__ import annotations

from typing import Any

from . import models


def calculate_confidence_metrics(
    all_url_results: list[models.URLResultInput],
) -> dict[str, Any]:
    """Aggregate per-URL confidence into composite + level + indicators + warnings."""
    rows = models.coerce_result_views(all_url_results)
    confidence_scores: list[float] = []
    successful_analyses = 0
    data_quality_scores: list[float] = []
    analysis_completeness_scores: list[float] = []

    for result in rows:
        ai_analysis = result.ai_analysis
        structured_data = result.structured_data

        if result.processing_status == "success":
            successful_analyses += 1

            ai_confidence = result.confidence_score
            if ai_confidence is not None:
                confidence_scores.append(ai_confidence)

            data_quality_scores.append(
                _calculate_data_quality_score(structured_data, result)
            )
            analysis_completeness_scores.append(
                _calculate_analysis_completeness_score(ai_analysis)
            )

    if confidence_scores:
        avg_ai_confidence = sum(confidence_scores) / len(confidence_scores)
        min_ai_confidence = min(confidence_scores)
        max_ai_confidence = max(confidence_scores)
    else:
        avg_ai_confidence = min_ai_confidence = max_ai_confidence = 0.0

    if data_quality_scores:
        avg_data_quality = sum(data_quality_scores) / len(data_quality_scores)
        min_data_quality = min(data_quality_scores)
    else:
        avg_data_quality = min_data_quality = 0.0

    avg_analysis_completeness = (
        sum(analysis_completeness_scores) / len(analysis_completeness_scores)
        if analysis_completeness_scores
        else 0.0
    )

    composite_confidence = _calculate_composite_confidence(
        avg_ai_confidence,
        avg_data_quality,
        avg_analysis_completeness,
        len(rows),
    )
    success_rate = successful_analyses / len(rows) if rows else 0.0
    confidence_level = _determine_confidence_level(
        composite_confidence, min_data_quality, success_rate
    )

    return {
        "composite_confidence": round(composite_confidence, 3),
        "confidence_level": confidence_level,
        "ai_confidence": {
            "average": round(avg_ai_confidence, 3),
            "min": round(min_ai_confidence, 3),
            "max": round(max_ai_confidence, 3),
        },
        "data_quality": {
            "average": round(avg_data_quality, 3),
            "min": round(min_data_quality, 3),
        },
        "analysis_completeness": {"average": round(avg_analysis_completeness, 3)},
        "successful_analyses": successful_analyses,
        "total_urls": len(rows),
        "success_rate": round(success_rate, 3),
        "quality_indicators": _generate_quality_indicators(rows),
        "validation_warnings": _generate_validation_warnings(rows),
    }


def _calculate_data_quality_score(
    structured_data: dict[str, Any], result: models.URLResultView
) -> float:
    """Weighted composite: structured-data presence, screenshots, change detail, processing OK."""
    score = 0.0

    # Structured data completeness (40%)
    expected_keys = ["html_changes", "css_changes", "js_changes", "metadata"]
    present_keys = sum(
        1 for key in expected_keys if key in structured_data and structured_data[key]
    )
    score += (present_keys / len(expected_keys)) * 0.4

    # Screenshot availability (30%)
    screenshots_available = result.screenshots_available
    expected_screenshots = ["baseline", "current", "visual_diff"]
    screenshot_score = sum(
        1 for shot in expected_screenshots if shot in screenshots_available
    )
    score += (screenshot_score / len(expected_screenshots)) * 0.3

    # HTML changes with code snippets (20%)
    html_changes = structured_data.get("html_changes", {})
    if html_changes and isinstance(html_changes, dict):
        changes = html_changes.get("changes", [])
        if changes:
            detailed_changes = sum(
                1 for change in changes[:10] if change.get("code_snippet")
            )
            score += (min(detailed_changes, 5) / 5) * 0.2

    # Processing status (10%)
    if result.processing_status == "success":
        score += 0.1

    return min(score, 1.0)


def _calculate_analysis_completeness_score(ai_analysis: dict[str, Any]) -> float:
    """Weighted: required fields, detailed_analysis sub-fields, recommendations sub-fields."""
    score = 0.0

    required_fields = [
        "overall_severity",
        "business_impact",
        "detailed_analysis",
        "recommendations",
        "confidence_score",
    ]
    present_fields = sum(
        1
        for field in required_fields
        if field in ai_analysis and ai_analysis[field] is not None
    )
    score += (present_fields / len(required_fields)) * 0.6

    detailed_analysis = ai_analysis.get("detailed_analysis", {})
    if isinstance(detailed_analysis, dict):
        detail_fields = ["visual_changes", "functional_impact", "technical_correlation"]
        present_details = sum(
            1
            for field in detail_fields
            if isinstance(detailed_analysis.get(field, []), list)
            and len(detailed_analysis.get(field, [])) > 0
        )
        score += (present_details / len(detail_fields)) * 0.25

    recommendations = ai_analysis.get("recommendations", {})
    if isinstance(recommendations, dict):
        rec_fields = ["immediate_actions", "review_items", "acceptance_criteria"]
        present_recs = sum(
            1
            for field in rec_fields
            if field in recommendations and recommendations[field]
        )
        score += (present_recs / len(rec_fields)) * 0.15

    return min(score, 1.0)


def _calculate_composite_confidence(
    ai_confidence: float,
    data_quality: float,
    analysis_completeness: float,
    sample_size: int,
) -> float:
    """50% AI confidence + 30% data quality + 20% completeness, penalized for small samples."""
    base_score = ai_confidence * 0.5 + data_quality * 0.3 + analysis_completeness * 0.2

    # Penalize tiny samples - 1 or 2 URLs isn't a real signal.
    if sample_size < 3:
        size_penalty = 0.1 * (3 - sample_size)
        base_score = max(0.0, base_score - size_penalty)

    return min(base_score, 1.0)


def _determine_confidence_level(
    composite_confidence: float,
    min_data_quality: float,
    success_rate: float,
) -> str:
    """Map composite + min-quality + success-rate to HIGH / MEDIUM / LOW / VERY_LOW."""
    if composite_confidence >= 0.8 and min_data_quality >= 0.7 and success_rate >= 0.9:
        return "HIGH"
    if composite_confidence >= 0.6 and min_data_quality >= 0.5 and success_rate >= 0.7:
        return "MEDIUM"
    if composite_confidence >= 0.4 and success_rate >= 0.5:
        return "LOW"
    return "VERY_LOW"


def _generate_quality_indicators(
    all_url_results: list[models.URLResultInput],
) -> dict[str, float]:
    """Per-URL coverage rates: data, screenshots, detailed analysis, errors."""
    rows = models.coerce_result_views(all_url_results)
    indicators = {
        "data_completeness": 0.0,
        "screenshot_coverage": 0.0,
        "detailed_analysis_coverage": 0.0,
        "error_rate": 0.0,
    }
    if not rows:
        return indicators

    total_urls = len(rows)
    complete_data_count = 0
    screenshot_coverage_count = 0
    detailed_analysis_count = 0
    error_count = 0

    for result in rows:
        if result.has_required_structured_blocks:
            complete_data_count += 1

        screenshots_available = result.screenshots_available
        if len(screenshots_available) >= 2:  # baseline + current minimum
            screenshot_coverage_count += 1

        detailed = result.ai_analysis.get("detailed_analysis", {})
        if isinstance(detailed, dict) and any(
            isinstance(detailed.get(field, []), list)
            and len(detailed.get(field, [])) > 0
            for field in (
                "visual_changes",
                "functional_impact",
                "technical_correlation",
            )
        ):
            detailed_analysis_count += 1

        if result.processing_status == "error":
            error_count += 1

    indicators["data_completeness"] = round(complete_data_count / total_urls, 3)
    indicators["screenshot_coverage"] = round(screenshot_coverage_count / total_urls, 3)
    indicators["detailed_analysis_coverage"] = round(
        detailed_analysis_count / total_urls, 3
    )
    indicators["error_rate"] = round(error_count / total_urls, 3)

    return indicators


def _generate_validation_warnings(
    all_url_results: list[models.URLResultInput],
) -> list[str]:
    """Free-text warnings about high error rate, low confidence, missing screenshots, sparse data."""
    rows = models.coerce_result_views(all_url_results)
    warnings: list[str] = []
    if not rows:
        warnings.append("No URL results available for validation")
        return warnings

    total_urls = len(rows)
    error_count = sum(1 for r in rows if r.processing_status == "error")
    success_count = total_urls - error_count

    if error_count / total_urls > 0.2:
        warnings.append(
            f"High error rate: {error_count}/{total_urls} URLs failed processing"
        )

    low_confidence_count = 0
    for result in rows:
        confidence = result.confidence_score
        if confidence is not None and confidence < 0.6:
            low_confidence_count += 1

    if low_confidence_count > 0 and success_count > 0:
        if low_confidence_count / success_count > 0.3:
            warnings.append(
                f"Multiple URLs with low AI confidence: {low_confidence_count}/{success_count} "
                "successful analyses"
            )

    no_screenshot_count = sum(1 for r in rows if not r.screenshots_available)
    if no_screenshot_count > 0:
        warnings.append(
            f"Missing screenshots for {no_screenshot_count}/{total_urls} URLs"
        )

    incomplete_data_count = sum(
        1 for r in rows if not r.has_required_structured_blocks
    )
    if incomplete_data_count > total_urls * 0.25:
        warnings.append(
            f"Incomplete structured data for {incomplete_data_count}/{total_urls} URLs"
        )

    return warnings


__all__ = ["calculate_confidence_metrics"]
