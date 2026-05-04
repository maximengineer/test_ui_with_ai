"""Wire shapes for the dashboard API (Phase C.1).

These Pydantic models are the *contract* between the React frontend and the
FastAPI backend. They show up in the generated `/openapi.json`, which the
frontend consumes via `openapi-typescript` to produce TypeScript types.

Convention:
  - `*Out` suffix → response body (what the API returns).
  - `*In` suffix  → request body (what the API accepts).
  - Bare names (`Site`, `RunRow`) for stable nouns the schema reuses.

We intentionally do NOT reuse `test_ui.common.sites.Site` directly. The
internal model has `extra="forbid"` and a strict id pattern; the API
response shape is a *projection* — fewer constraints, no validation
overhead, free to evolve independently if the wire ever needs to add
metadata the on-disk YAML doesn't have.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator

from test_ui.config import settings

from .db import RUN_KINDS_TUPLE, RUN_SOURCES_TUPLE, RUN_STATUSES_TUPLE


# Re-export as Literal types so FastAPI generates a closed enum in OpenAPI.
RunKind = Literal["baseline", "current", "comparator", "report"]
RunStatus = Literal["pending", "running", "done", "failed", "interrupted"]
RunSource = Literal["dashboard", "discovered", "cli"]


# Defensive: the Literals above are duplicated from db.py's tuples for
# OpenAPI ergonomics. Verify the duplication at import time so a future
# tuple edit can't silently desync the wire schema from the DB CHECK
# constraints. RuntimeError (not assert) so this survives `python -O`,
# matching the discipline applied to runtime checks elsewhere in the
# package. `get_args()` is the modern, public typing API for this.
def _verify_literal_matches(name: str, literal_type, tuple_value):
    if set(get_args(literal_type)) != set(tuple_value):
        raise RuntimeError(
            f"wire-schema desync: {name} Literal={get_args(literal_type)} "
            f"!= db tuple={tuple_value}. Update both sides together."
        )


_verify_literal_matches("RunKind", RunKind, RUN_KINDS_TUPLE)
_verify_literal_matches("RunStatus", RunStatus, RUN_STATUSES_TUPLE)
_verify_literal_matches("RunSource", RunSource, RUN_SOURCES_TUPLE)


# --------------------------------------------------------------------------- #
# Sites                                                                      #
# --------------------------------------------------------------------------- #


class SiteOut(BaseModel):
    """A site as returned by `GET /api/sites`."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    url: str


class SiteCreateIn(BaseModel):
    """`POST /api/sites` body. `id` is auto-generated server-side from the
    slugified name + numeric dedup suffix; the client never sets it.

    `max_length` caps mirror the underlying `Site` model — defending at
    the wire boundary too means a 100KB URL gets rejected with a clear
    422 instead of bloating sites.yml at the persistence layer.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2048)


class SiteUpdateIn(BaseModel):
    """`PATCH /api/sites/{id}` body. Both fields optional — operator can
    rename without changing the URL or vice versa. `id` is immutable
    (changing it would invalidate existing per-site data dirs); the URL
    path component is authoritative."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    url: str | None = Field(default=None, min_length=1, max_length=2048)


# --------------------------------------------------------------------------- #
# Dates / runs                                                               #
# --------------------------------------------------------------------------- #


class DatesOut(BaseModel):
    """Per-kind list of date directories present on disk.

    The frontend uses this to populate date pickers without scanning
    the filesystem itself. Dates are DD-MM-YYYY strings, sorted descending
    (newest first) so the UI can default to the most recent.
    """

    model_config = ConfigDict(extra="forbid")

    baseline: list[str] = Field(default_factory=list)
    current: list[str] = Field(default_factory=list)
    comparator: list[str] = Field(default_factory=list)
    report: list[str] = Field(default_factory=list)


class RunRow(BaseModel):
    """One row from `runs`, projected for the wire.

    `args_json` and `command_json` are exposed as parsed objects (not raw
    strings) — saves the frontend a JSON.parse and gives openapi-typescript
    a typed shape to work with.
    """

    model_config = ConfigDict(extra="forbid")

    id: int
    run_id: str
    kind: RunKind
    status: RunStatus
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    date_dir: str | None = None
    args: dict = Field(default_factory=dict)
    command: list[str] = Field(default_factory=list)
    exit_code: int | None = None
    error: str | None = None
    pid: int | None = None
    pgid: int | None = None
    pid_start_time: str | None = None
    source: RunSource


class RunListOut(BaseModel):
    """Paginated list response."""

    model_config = ConfigDict(extra="forbid")

    items: list[RunRow]
    total: int


# --------------------------------------------------------------------------- #
# Health                                                                     #
# --------------------------------------------------------------------------- #


class HealthOut(BaseModel):
    """Quick liveness probe for the dashboard.

    `db_ok` False → SQLite open or migration failed; the dashboard is
    running in a degraded state and most routes will 500.
    `ai_analyzer_ok` is checked with a 2s timeout — the dashboard never
    hangs on a slow analyzer; a False here just disables AI-dependent
    UI affordances client-side.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    db_ok: bool
    ai_analyzer_ok: bool


# --------------------------------------------------------------------------- #
# Sync                                                                       #
# --------------------------------------------------------------------------- #


class SyncOut(BaseModel):
    """`POST /api/sync` result. `synced` is the count of NEW rows inserted.

    Re-running sync should be a no-op (returning `synced=0`) once the DB
    has caught up to the on-disk state — the test pins this idempotency.
    """

    model_config = ConfigDict(extra="forbid")

    synced: int
    scanned: int


# --------------------------------------------------------------------------- #
# Job-runner request/response shapes (Phase C.1 second slice).               #
# --------------------------------------------------------------------------- #
#
# `RunRequest` is a discriminated union over `kind`. Each kind has different
# required inputs:
#   - baseline / current: just the kind
#   - comparator:         needs to know which baseline + current to diff
#                         (defaults to "latest complete" for each)
#   - report:             needs the comparator date to summarize
#
# Pydantic auto-routes incoming JSON to the right subclass based on `kind`,
# so a `POST /api/runs` body of `{"kind": "comparator", "baseline_run_id": ...}`
# fails 422 unless it matches `ComparatorRunRequest` exactly. This is much
# friendlier than a flat model with `Optional[...]` everywhere.


class _BaseRunRequest(BaseModel):
    """Common fields for every run request shape."""

    model_config = ConfigDict(extra="forbid")


class BaselineRunRequest(_BaseRunRequest):
    kind: Literal["baseline"]


class CurrentRunRequest(_BaseRunRequest):
    kind: Literal["current"]


class ComparatorRunRequest(_BaseRunRequest):
    kind: Literal["comparator"]
    # Both default to "use the latest complete run for today's date". Pinning
    # specific run_ids is reserved for re-running an old comparison; the
    # dashboard MVP doesn't surface that capability yet.
    baseline_run_id: str | None = None
    current_run_id: str | None = None


class ReportRunRequest(_BaseRunRequest):
    kind: Literal["report"]
    date: str | None = None  # DD-MM-YYYY; None → latest comparator date

    @field_validator("date")
    @classmethod
    def _validate_date_shape(cls, v: str | None) -> str | None:
        """Reject anything that isn't a real DD-MM-YYYY date.

        Without this, "31-02-2099" / "hello" / "../../etc/passwd" all
        passed validation and ended up in `require_complete_run` as a
        path component, producing a misleading "no comparator runs found
        for ../../etc/passwd" error. Validating at the model layer
        surfaces the typo as a 422 immediately.
        """
        if v is None:
            return v
        try:
            datetime.strptime(v, settings.date_format)
        except ValueError as e:
            raise ValueError(
                f"date must be a real {settings.date_format} date, got {v!r}"
            ) from e
        return v


# Discriminated union. FastAPI uses this in the route signature; Pydantic
# routes the JSON body to the right subclass based on the `kind` field.
RunRequest = (
    BaselineRunRequest | CurrentRunRequest | ComparatorRunRequest | ReportRunRequest
)


class RunSpawnedOut(BaseModel):
    """202 response from `POST /api/runs`. `db_id` lets the frontend poll
    `/api/runs/{db_id}` for status; `run_id` lets it correlate against the
    on-disk run dir as soon as the subprocess produces it."""

    model_config = ConfigDict(extra="forbid")

    db_id: int
    run_id: str
    status: RunStatus  # always "running" at this point — pinned for the wire


# --------------------------------------------------------------------------- #
# Report drill-in shapes (Phase C.2 second slice).                           #
# --------------------------------------------------------------------------- #
#
# A report run dir on disk looks like:
#   data/report/<date>/<run_id>/
#     <url_id>/
#       ai_analysis.json | ai_error.json | no_changes.json | ai_disabled.json
#       structured_data.json
#       screenshots/{baseline,current,visual_diff}.png
#     aggregated_analysis.json
#     enhanced_report.html
#     manifest.json
#
# These wire models project that into JSON the React UI can render.


class ReportSummaryOut(BaseModel):
    """`GET /api/reports/{date}/{run_id}` — top-level summary. `run_id`,
    `started_at`, `finished_at`, `url_count` come from the manifest;
    `severity_counts` is computed from the per-URL files."""

    model_config = ConfigDict(extra="forbid")

    date: str
    run_id: str
    started_at: str
    finished_at: str | None
    url_count: int
    # Counts of {CRITICAL, WARNING, SAFE, error, no_changes, ai_disabled, unknown}.
    severity_counts: dict[str, int] = Field(default_factory=dict)


# Report-level result types match the four mutually-exclusive per-URL files
# in test_ui/report/loader.py:RESULT_FILENAMES. `unknown` covers the
# defensive case where a URL dir exists but has no result file at all
# (would be a real bug; we surface it instead of crashing the listing).
ReportResultType = Literal[
    "analysis_success",
    "analysis_error",
    "no_changes",
    "ai_disabled",
    "unknown",
]


class ReportUrlSummary(BaseModel):
    """One row in the `/api/reports/{date}/{run_id}/urls` listing.

    `severity` is only populated for `result_type='analysis_success'` —
    the other result types don't have a meaningful severity. The frontend
    can surface a colored pill keyed off either `result_type` (always
    present) or `severity` (success-only)."""

    model_config = ConfigDict(extra="forbid")

    url_id: str
    result_type: ReportResultType
    severity: str | None = None  # CRITICAL / WARNING / SAFE
    # The URL the analysis ran against — operator-friendly when the id is
    # a slug (e.g. "department-of-health" doesn't tell you what site that
    # is until you look at the URL).
    url: str | None = None


class ReportUrlsOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ReportUrlSummary]


class ReportUrlDetail(BaseModel):
    """`GET /api/reports/{date}/{run_id}/url?id=<url_id>` — the per-URL
    AI analysis JSON, returned verbatim from disk.

    Typed as `dict` because the AI analysis schema lives in
    `test_ui/contracts/` and varies by `result_type` — wiring up a full
    discriminated union here would duplicate ~200 LOC of pydantic models
    that already exist in the contracts package, with the only payoff
    being typed access in the React drill-in (which renders it as JSON
    anyway). Acceptable type-loss for the MVP; tighten if the UI needs
    typed access."""

    model_config = ConfigDict(extra="forbid")

    url_id: str
    result_type: ReportResultType
    analysis: dict
    # Echoes structured_data.json if present — the diff payload the AI saw.
    structured_data: dict | None = None
    # Which screenshot kinds exist on disk (the SCREENSHOT route returns
    # the bytes; this lets the UI know which `which=` values to request).
    screenshots: list[Literal["baseline", "current", "visual_diff"]] = Field(
        default_factory=list
    )


__all__ = [
    "RunKind",
    "RunStatus",
    "RunSource",
    "SiteOut",
    "SiteCreateIn",
    "SiteUpdateIn",
    "DatesOut",
    "RunRow",
    "RunListOut",
    "HealthOut",
    "SyncOut",
    "BaselineRunRequest",
    "CurrentRunRequest",
    "ComparatorRunRequest",
    "ReportRunRequest",
    "RunRequest",
    "RunSpawnedOut",
    "ReportSummaryOut",
    "ReportResultType",
    "ReportUrlSummary",
    "ReportUrlsOut",
    "ReportUrlDetail",
]
