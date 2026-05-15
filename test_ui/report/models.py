"""Typed internal contracts at report boundaries (Phase 4).

These models are intentionally *internal* to the Python report pipeline:
- comparator diff JSON -> report loader (`StructuredDataEnvelope`)
- per-URL report outcome envelope (`URLResultEnvelope`)

The cross-process wire contract remains in `test_ui.contracts.ai_contract`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from ..contracts.ai_contract import (
    AIAnalysisError,
    AIAnalysisResponse,
    AIDisabledMarker,
    AnalysisOutput,
    CSSChanges,
    ChangeSummary,
    HTMLChanges,
    JSChanges,
    NoChangesMarker,
    StructuredData,
)


class _StrictModel(BaseModel):
    """Base class for strict internal report-boundary contracts."""

    model_config = ConfigDict(extra="forbid")


class StructuredDataEnvelope(_StrictModel):
    """Comparator structured diff data as loaded by the report stage."""

    change_summary: ChangeSummary
    html_changes: HTMLChanges
    css_changes: CSSChanges
    js_changes: JSChanges
    metadata: dict[str, Any] = Field(default_factory=dict)
    visual_diff_image: str | None = None

    def to_ai_structured_data(self) -> StructuredData:
        """Drop loader-only fields and return the strict AI-request shape."""
        return StructuredData(
            change_summary=self.change_summary,
            html_changes=self.html_changes,
            css_changes=self.css_changes,
            js_changes=self.js_changes,
        )


ProcessingStatus = Literal["success", "error", "no_changes", "ai_disabled"]
LegacyProcessingStatus = Literal["success", "error"]
DerivedProcessingStatus = ProcessingStatus | LegacyProcessingStatus

_STRUCTURED_DATA_ADAPTER = TypeAdapter(StructuredDataEnvelope)
_ANALYSIS_OUTPUT_ADAPTER = TypeAdapter(AnalysisOutput)


def validate_structured_data_envelope(payload: dict[str, Any]) -> StructuredDataEnvelope:
    """Validate comparator payload consumed by the report stage."""
    return _STRUCTURED_DATA_ADAPTER.validate_python(payload)


def validate_structured_data_for_ai(payload: dict[str, Any]) -> StructuredData:
    """Validate and normalize loader payload to strict AI-request StructuredData."""
    return validate_structured_data_envelope(payload).to_ai_structured_data()


class URLResultView(BaseModel):
    """Typed view over per-URL result rows for aggregation/confidence logic.

    Accepts sparse legacy/test shapes (extra fields ignored) and derives a
    stable `processing_status` when absent.
    """

    model_config = ConfigDict(extra="ignore")

    url: str = "unknown"
    ai_analysis: dict[str, Any] = Field(default_factory=dict)
    structured_data: dict[str, Any] = Field(default_factory=dict)
    processing_status: str | None = None
    screenshots_available: list[str] = Field(default_factory=list)
    error: str | None = None

    @field_validator("ai_analysis", mode="before")
    @classmethod
    def _normalize_ai_analysis(cls, v: Any) -> dict[str, Any]:
        if isinstance(v, BaseModel):
            dumped = v.model_dump(mode="python")
            return dumped if isinstance(dumped, dict) else {}
        return v if isinstance(v, dict) else {}

    @field_validator("structured_data", mode="before")
    @classmethod
    def _normalize_structured_data(cls, v: Any) -> dict[str, Any]:
        return v if isinstance(v, dict) else {}

    @field_validator("screenshots_available", mode="before")
    @classmethod
    def _normalize_screenshots(cls, v: Any) -> list[str]:
        if isinstance(v, list):
            return [str(x) for x in v]
        return []

    @model_validator(mode="after")
    def _derive_processing_status(self) -> URLResultView:
        if self.processing_status is not None:
            return self
        rt = self.result_type
        if rt == "analysis_success":
            self.processing_status = "success"
        elif rt == "analysis_error":
            self.processing_status = "error"
        elif rt == "no_changes":
            self.processing_status = "no_changes"
        elif rt == "ai_disabled":
            self.processing_status = "ai_disabled"
        elif self.analysis_type == "no_changes_detected":
            self.processing_status = "no_changes"
        elif self.overall_severity == "ERROR":
            self.processing_status = "error"
        else:
            self.processing_status = "success"
        return self

    @property
    def result_type(self) -> str | None:
        rt = self.ai_analysis.get("result_type")
        return str(rt) if isinstance(rt, str) else None

    @property
    def analysis_type(self) -> str | None:
        at = self.ai_analysis.get("analysis_type")
        return str(at) if isinstance(at, str) else None

    @property
    def overall_severity(self) -> str | None:
        sev = self.ai_analysis.get("overall_severity")
        return str(sev) if isinstance(sev, str) else None

    @property
    def business_impact(self) -> str:
        impact = self.ai_analysis.get("business_impact")
        return str(impact) if isinstance(impact, str) else "UNKNOWN"

    @property
    def functional_impacts(self) -> list[str]:
        detailed = self.ai_analysis.get("detailed_analysis", {})
        if not isinstance(detailed, dict):
            return []
        impacts = detailed.get("functional_impact", [])
        return [str(x) for x in impacts] if isinstance(impacts, list) else []

    @property
    def confidence_score(self) -> float | None:
        val = self.ai_analysis.get("confidence_score")
        if isinstance(val, (int, float)) and 0 <= val <= 1:
            return float(val)
        return None

    @property
    def is_analysis_success(self) -> bool:
        return self.result_type == "analysis_success"

    @property
    def is_analysis_error(self) -> bool:
        return self.result_type == "analysis_error"

    @property
    def is_marker(self) -> bool:
        return self.result_type in {"no_changes", "ai_disabled"}

    @property
    def is_url_with_changes(self) -> bool:
        if self.is_analysis_success:
            return True
        return (
            self.processing_status == "success"
            and self.analysis_type != "no_changes_detected"
            and not self.result_type
        )

    @property
    def has_required_structured_blocks(self) -> bool:
        data = self.structured_data
        return all(key in data for key in ("html_changes", "css_changes", "js_changes"))


URLResultInput = URLResultView | dict[str, Any]


def coerce_result_views(rows: list[URLResultInput]) -> list[URLResultView]:
    """Normalize arbitrary per-URL rows into typed `URLResultView` objects."""
    out: list[URLResultView] = []
    for row in rows:
        if isinstance(row, URLResultView):
            out.append(row)
        else:
            out.append(URLResultView.model_validate(row))
    return out


class URLResultEnvelope(_StrictModel):
    """Typed per-URL outcome envelope consumed by aggregator + renderer."""

    url: str
    ai_analysis: AnalysisOutput
    structured_data: dict[str, Any] = Field(default_factory=dict)
    report_path: Path
    processing_status: ProcessingStatus
    screenshots_available: list[str] = Field(default_factory=list)
    timings: dict[str, float] | None = None
    error: str | None = None

    @model_validator(mode="after")
    def _validate_status_alignment(self) -> URLResultEnvelope:
        expected_by_type = {
            AIAnalysisResponse: "success",
            AIAnalysisError: "error",
            NoChangesMarker: "no_changes",
            AIDisabledMarker: "ai_disabled",
        }
        expected = expected_by_type.get(type(self.ai_analysis))
        if expected is not None and self.processing_status != expected:
            raise ValueError(
                "processing_status does not match ai_analysis result_type "
                f"(expected={expected}, got={self.processing_status})"
            )
        return self


class LegacyURLResultEnvelope(_StrictModel):
    """Back-compat envelope for pre-result_type report payloads."""

    url: str
    ai_analysis: dict[str, Any]
    structured_data: dict[str, Any] = Field(default_factory=dict)
    report_path: Path
    processing_status: LegacyProcessingStatus
    screenshots_available: list[str] = Field(default_factory=list)
    error: str | None = None


def build_url_result(
    *,
    url: str,
    ai_analysis: dict[str, Any],
    structured_data: dict[str, Any],
    report_path: Path,
    processing_status: ProcessingStatus,
    screenshots_available: list[str] | None = None,
    timings: dict[str, float] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Build + validate a typed per-URL result dict."""
    parsed_analysis = _ANALYSIS_OUTPUT_ADAPTER.validate_python(ai_analysis)
    row = URLResultEnvelope(
        url=url,
        ai_analysis=parsed_analysis,
        structured_data=structured_data,
        report_path=report_path,
        processing_status=processing_status,
        screenshots_available=screenshots_available or [],
        timings=timings,
        error=error,
    )
    return row.model_dump(mode="python", exclude_none=True)


def build_legacy_url_result(
    *,
    url: str,
    ai_analysis: dict[str, Any],
    structured_data: dict[str, Any],
    report_path: Path,
    processing_status: LegacyProcessingStatus,
    screenshots_available: list[str] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Build + validate a pre-result_type back-compat per-URL result dict."""
    row = LegacyURLResultEnvelope(
        url=url,
        ai_analysis=ai_analysis,
        structured_data=structured_data,
        report_path=report_path,
        processing_status=processing_status,
        screenshots_available=screenshots_available or [],
        error=error,
    )
    return row.model_dump(mode="python", exclude_none=True)


def format_validation_error(e: ValidationError) -> str:
    """Compact, deterministic validation error string for operator logs."""
    parts: list[str] = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err.get("loc", ()))
        msg = err.get("msg", "validation error")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


__all__ = [
    "StructuredDataEnvelope",
    "URLResultEnvelope",
    "LegacyURLResultEnvelope",
    "ProcessingStatus",
    "LegacyProcessingStatus",
    "validate_structured_data_envelope",
    "validate_structured_data_for_ai",
    "build_url_result",
    "build_legacy_url_result",
    "URLResultView",
    "URLResultInput",
    "coerce_result_views",
    "format_validation_error",
]
