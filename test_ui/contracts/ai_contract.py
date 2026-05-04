"""Pydantic v2 source of truth for the Python ↔ Node AI-analyzer contract.

This module is authoritative. JSON Schema files in `schemas/` are generated
from these models by `scripts/export_schemas.py`. Do not hand-edit the
generated schemas; regenerate them.

Versioning is explicit via `schema_version`. Bump it when shape changes in
ways consumers must adapt to. The discriminator field `result_type`
distinguishes the four per-URL output types so consumers don't infer by
filename or shape.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


# Bump when the request/response shape changes in a backward-incompatible way.
# Format: <YYYY-MM-DD>.<sequence-within-day>.
SCHEMA_VERSION = "2026-04-30.1"


class _StrictModel(BaseModel):
    """Base for all contract models. Forbids unknown fields so drift is loud."""

    model_config = ConfigDict(extra="forbid")


# ============================================================================
# Input - structured comparator data (matches docs/data_shapes.md)
# ============================================================================
#
# The inner `changes` lists carry per-change records whose shape varies by
# `type`. Modeling them as `list[dict]` is a deliberate trade-off: top-level
# structure is enforced (catches drift in the producer), per-record structure
# is left flexible. If/when the comparator gets refactored to emit consistent
# per-record shapes, tighten these.


class ChangeSummary(_StrictModel):
    overall_assessment: dict
    change_categories: dict
    affected_components: list[str]
    recommendation: str
    ai_analysis_priority: Literal["none", "low", "medium", "high"]


class HTMLChanges(_StrictModel):
    changes_detected: bool
    change_types: list[str]
    changes: list[dict]
    summary: dict


class CSSChanges(_StrictModel):
    changes_detected: bool
    change_types: list[str]
    files_changed: list[str]
    changes: list[dict]
    summary: dict


class JSChanges(_StrictModel):
    changes_detected: bool
    change_types: list[str]
    files_changed: list[str]
    changes: list[dict]
    summary: dict


class StructuredData(_StrictModel):
    change_summary: ChangeSummary
    html_changes: HTMLChanges
    css_changes: CSSChanges
    js_changes: JSChanges


class Screenshots(_StrictModel):
    """Base64-encoded screenshot bytes. All optional; at least one expected.

    The Node side enforces image size + magic-byte validation in Phase A.1.4.
    Pydantic only validates the strings are valid base64.
    """

    baseline: str | None = None  # base64-encoded PNG/JPEG bytes
    current: str | None = None
    visual_diff: str | None = None


# ============================================================================
# Request
# ============================================================================


class AIAnalysisRequest(_StrictModel):
    schema_version: str = SCHEMA_VERSION
    request_id: str  # UUID4 generated client-side; correlates request → response
    url: str  # the URL being analyzed (the page, not the AI service)
    structured_data: StructuredData
    screenshots: Screenshots


# ============================================================================
# Response variants - discriminated union by `result_type`
# ============================================================================


class DetailedAnalysis(_StrictModel):
    visual_changes: list[str]
    functional_impact: list[str]
    technical_correlation: list[str]


class Recommendations(_StrictModel):
    immediate_actions: list[str]
    review_items: list[str]
    acceptance_criteria: str


class AIAnalysisResponse(_StrictModel):
    """Successful AI analysis. The model produced a verdict."""

    schema_version: str = SCHEMA_VERSION
    result_type: Literal["analysis_success"] = "analysis_success"
    request_id: str
    model: str  # e.g. "gemini-2.5-pro"
    prompt_sha256: str  # 64 hex chars
    overall_severity: Literal["CRITICAL", "WARNING", "SAFE"]
    business_impact: Literal["HIGH", "MEDIUM", "LOW", "NONE"]
    detailed_analysis: DetailedAnalysis
    recommendations: Recommendations
    confidence_score: float = Field(ge=0.0, le=1.0)


class AIAnalysisError(_StrictModel):
    """The AI call failed. This is NOT an analysis result; it's a typed failure.

    Distinct from AIAnalysisResponse so consumers can route failures to a
    "needs retry" bucket rather than treating them as a phantom severity.
    """

    schema_version: str = SCHEMA_VERSION
    result_type: Literal["analysis_error"] = "analysis_error"
    # Optional: if the request body failed to parse on the Node side, the server
    # can't echo back a request_id. Python correlates by None == "parse failure
    # before request_id was readable." Successful round-trips always carry it.
    request_id: str | None = None
    model: str | None = None  # null if provider was never reached
    prompt_sha256: str | None = None  # null if prompt failed to load
    error_type: Literal[
        "schema_invalid",  # request didn't match schema
        "timeout",  # network / provider timeout
        "rate_limited",  # 429 from provider
        "provider_error",  # 5xx or other from provider
        "response_invalid",  # provider returned malformed response
        "config_error",  # missing API key, missing prompt, etc.
        "unknown",  # catch-all
    ]
    retryable: bool
    details: str  # human-readable error context (safe to log)


class NoChangesMarker(_StrictModel):
    """Comparator detected no changes for this URL. AI was not invoked.

    Distinct from AIAnalysisResponse(severity=SAFE) - that would imply the AI
    looked at the page and decided it was safe. This says the AI never looked
    because there was nothing to look at.
    """

    schema_version: str = SCHEMA_VERSION
    result_type: Literal["no_changes"] = "no_changes"
    checked_at: str  # DD-MM-YYYY HH:MM:SS, from settings.get_current_datetime()


class AIDisabledMarker(_StrictModel):
    """AFR_AI_ENABLED=false skipped the AI call.

    Used for sensitive sites where DOM/screenshots shouldn't leave the local
    network. The comparator still ran; we just didn't ship the data to a
    third-party model.
    """

    schema_version: str = SCHEMA_VERSION
    result_type: Literal["ai_disabled"] = "ai_disabled"
    checked_at: str


# Discriminated union covering every per-URL output the report layer can emit.
# The aggregator and HTML renderer (Phase A.3) parse one of these per URL.
AnalysisOutput = Annotated[
    Union[AIAnalysisResponse, AIAnalysisError, NoChangesMarker, AIDisabledMarker],
    Field(discriminator="result_type"),
]


__all__ = [
    "SCHEMA_VERSION",
    "AIAnalysisRequest",
    "AIAnalysisResponse",
    "AIAnalysisError",
    "NoChangesMarker",
    "AIDisabledMarker",
    "AnalysisOutput",
    "StructuredData",
    "ChangeSummary",
    "HTMLChanges",
    "CSSChanges",
    "JSChanges",
    "Screenshots",
    "DetailedAnalysis",
    "Recommendations",
]
