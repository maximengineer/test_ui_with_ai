# Architecture Improvement Recommendations and Phased Implementation Plan

This plan turns architecture findings into a step-by-step, reversible execution path.
It prioritizes low-risk changes first, keeps behavior stable, and adds guardrails before broad refactors.

## Goals

- Reduce architectural complexity without changing core user-facing behavior.
- Strengthen contracts between pipeline stages (capture -> comparator -> report -> dashboard).
- Improve maintainability by splitting oversized modules along clear boundaries.
- Remove stale documentation and legacy compatibility paths that create confusion.
- Preserve deterministic artifacts and existing run/data layout unless explicitly migrated.

## Critical Corrections to Previous Draft

1. **Run status model assumption corrected.**
   - A single literal status enum cannot be shared 1:1 across manifest + dashboard because dashboard has `pending` (pre-spawn) while manifest does not.
   - Correct approach: one canonical lifecycle model + explicit subset mapping + transition tests.
2. **Comparator core decomposition added as a first-class phase.**
   - `test_ui/comparator/dom.py` and `test_ui/comparator/assets.py` are among the largest/riskiest modules and are central to product correctness.
3. **Operational backlog items brought into plan scope.**
   - Data retention strategy (`dashboard/api/db.py` TODO) and zipped-wheel `sites.yml` resolution TODO are now planned as explicit implementation items.
4. **Verification gates aligned to actual repo/CI surface.**
   - Gates now reference existing tests/jobs (`test_dashboard_*`, schema drift, openapi drift, Node tests), not generic placeholders.

## Priority Recommendations

1. Align docs with implemented architecture and data layout.
2. Formalize run lifecycle contracts (state model, mappings, transitions).
3. Decompose comparator core modules (`dom.py`, `assets.py`) before broader dashboard refactors.
4. Strengthen typed internal contracts at comparator -> report boundaries.
5. Decompose oversized dashboard backend/frontend modules with API compatibility guarantees.
6. Reduce frontend/backend type drift (especially literal enums) via contract generation strategy.
7. Retire legacy fallbacks with explicit migration-readiness criteria.
8. Harden operations: safer cleanup, retention policy, packaging path stability.

## Phased Plan

### Phase 0: Baseline, Safety, and Scope Freeze

#### Objectives

- Establish a stable baseline before structural changes.
- Make risk visible and prevent accidental regressions.

#### Implementation steps

1. Capture baseline metrics:
   - module file sizes for current hotspots
   - current fast/slow test status
   - representative pipeline timings (`baseline/current/compare/report`)
2. Confirm canonical architecture sources:
   - system architecture: `ARCHITECTURE.md`
   - deferred/out-of-scope: `BACKLOG.md`
3. Define no-go constraints:
   - no artifact layout changes
   - no cross-language AI contract breaks
   - no CLI surface break without migration note

#### Verification gates

- `pytest -q`
- `pytest -q -m slow`
- `python scripts/audit_paths.py`

#### Exit criteria

- Baseline metrics recorded in a short implementation note.
- Scope constraints documented and agreed.

#### Phase 0 Slice A (Implemented)

- Baseline metrics and verification-gate timings recorded in:
  - `docs/history/PHASE0_BASELINE_NOTE_2026-05-15.md`
- Includes:
  - hotspot file-size snapshot
  - `pytest -q`, `pytest -q -m slow`, and `scripts/audit_paths.py` outcomes + runtimes
  - manifest-derived representative pipeline timings
  - reaffirmed scope/no-go constraints

---

### Phase 1: Documentation and Reality Sync

#### Objectives

- Remove stale/contradictory documentation.
- Ensure run instructions and architecture docs match actual behavior.

#### Implementation steps

1. Update stale sections in:
   - `README.md`
   - `docs/data_shapes.md`
2. Remove obsolete milestone-language that contradicts implemented dashboard/contract state.
3. Align data layout docs with current run-id-based layout.
4. Add a concise “doc drift checklist” to PR workflow for architecture-affecting changes.

#### Verification gates

- `pytest -q tests/test_schema_drift.py`
- `pytest -q tests/test_dashboard_routes.py::test_openapi_includes_all_routes`

#### Exit criteria

- README/docs reflect current system behavior and paths.
- No known stale “planned/in-progress” claims for already-shipped features.

---

### Phase 2: Run Lifecycle Contract Alignment

#### Objectives

- Eliminate ambiguity in run states and transitions across manifest, DB, and API.
- Keep existing behavior but make mapping rules explicit and testable.

#### Implementation steps

1. Define canonical lifecycle model (state machine) with explicit subset rules:
   - dashboard-only pre-run state: `pending`
   - shared in-flight/terminal states: `running|failed|interrupted`
   - success mapping: manifest `complete` <-> dashboard `done`
2. Keep mapping adapters centralized (`sync.py`-style), not scattered in route logic.
3. Add transition invariants (allowed state transitions) and encode in tests.
4. Update comments/docs so model is obvious to future contributors.

#### Verification gates

- `pytest -q tests/test_dashboard_db.py tests/test_dashboard_sync.py tests/test_interrupted_status.py`

#### Exit criteria

- State semantics are explicit, tested, and consistently applied.
- No hidden/implicit status translation in feature code.

---

### Phase 3: Comparator Core Decomposition

#### Objectives

- Reduce complexity in comparator-critical hotspots.
- Preserve output contract and deterministic behavior while splitting responsibilities.

#### Implementation steps

1. Split `test_ui/comparator/dom.py` into focused units:
   - extraction/normalization
   - structural/content/attribute diffing
   - summarization/serialization helpers
2. Split `test_ui/comparator/assets.py` into focused units:
   - URL normalization/volatile pattern handling
   - CSS/JS/media differ logic
   - summary serializers
3. Preserve `engine.py` orchestration contract and output file shapes.
4. Keep deterministic ordering and sorting guarantees explicit.

#### Verification gates

- `pytest -q tests/test_comparator_units.py tests/test_comparator_golden.py`
- `pytest -q tests/test_finder.py tests/test_discovery.py`

#### Exit criteria

- Comparator hotspots are decomposed with stable output contracts.
- Golden/unit tests pass without fixture drift except intentional updates.

---

### Phase 4: Internal Typed Contracts (Comparator -> Report)

#### Objectives

- Reduce brittle dict-based handoffs.
- Make schema drift and missing fields fail early with actionable errors.

#### Implementation steps

1. Introduce typed internal models for comparator output consumed by report.
2. Standardize report-side typed envelopes for per-URL outcomes.
3. Remove ad-hoc sentinel dict branching where typed variants can be used.
4. Validate at module boundaries (`loader`/`generator`) and keep errors deterministic.

#### Verification gates

- `pytest -q tests/test_report_loader.py tests/test_report_generator.py tests/test_report_html_renderer.py tests/test_report_confidence.py`
- `pytest -q tests/test_ai_client.py`

#### Exit criteria

- Comparator/report handoff is typed and validated.
- Error handling paths are explicit, deterministic, and covered.

---

### Phase 5: Dashboard Backend Decomposition

#### Objectives

- Split oversized API modules while preserving endpoint behavior.
- Make routing, services, and filesystem concerns separable.

#### Implementation steps

1. Decompose `dashboard/api/routes.py` into domain routers:
   - runs
   - reports
   - sites
   - health/system
2. Move non-routing logic to service modules with clear ownership.
3. Consolidate path-safety helpers into reusable utilities.
4. Keep wire models and HTTP behavior backward compatible.

#### Verification gates

- `pytest -q tests/test_dashboard_routes.py tests/test_dashboard_jobruns.py tests/test_dashboard_reports.py tests/test_dashboard_api_workflow.py`

#### Exit criteria

- No single multi-domain route module remains.
- Endpoint compatibility preserved.

---

### Phase 6: Dashboard Frontend Decomposition and Type Drift Reduction

#### Objectives

- Reduce complexity in page-level React files.
- Strengthen frontend/backend type alignment.

#### Implementation steps

1. Split `RunsPage.tsx` into container + presentational + action modules.
2. Split `api/hooks.ts` by domain (runs/reports/sites/system).
3. Reduce hand-maintained literal unions where possible by improving OpenAPI schema emission strategy.
4. Keep existing UX/flows stable unless fixing defects.

#### Verification gates

- `cd dashboard/web && npm run build`
- `pytest -q tests/test_dashboard_spa.py tests/test_dashboard_routes.py`
- OpenAPI drift gate equivalent to CI snapshot check.

#### Exit criteria

- Frontend hotspot files decomposed.
- Type drift risk reduced, especially around run/report literal types.

---

### Phase 7: Legacy Compatibility Retirement

#### Objectives

- Remove migration-era fallback logic that increases cognitive load.
- Keep removals data-driven and reversible.

#### Implementation steps

1. Inventory all fallback paths (finder/discovery/sites/url_id etc.).
2. Define objective removal criteria per fallback:
   - migration scripts complete
   - historical artifact checks pass
   - replacement tests exist
3. Remove one fallback family per PR with rollback note.

#### Verification gates

- Targeted tests per fallback family.
- Regression checks against representative historical artifacts where needed.

#### Exit criteria

- Only operationally required fallbacks remain.
- Remaining shims are documented with explicit removal triggers.

#### Phase 7 Slice A (Implemented)

- Retired legacy read-path fallback for comparator/report run discovery:
  - removed `<date>/<url_dir>/` fallback from `test_ui/comparator/finder.py`
  - aligned `test_ui/report/discovery.py` to require canonical `<date>/<run_id>/...`
  - aligned `test_ui/common/preconditions.py::require_complete_run` to reject legacy layout with a migration hint
- Added/updated targeted tests to pin new behavior (`tests/test_finder.py`, `tests/test_discovery.py`, `tests/test_preconditions.py`).
- Rollback note:
  - revert this slice and re-enable legacy branches in finder/discovery/preconditions.
  - preferred operational path is migration via `scripts/migrate_run_layout.py` rather than rollback.

---

### Phase 8: Operational Safety, Retention, and Packaging Hardening

#### Objectives

- Reduce destructive-command risk.
- Address known operational TODOs (retention + packaged `sites.yml` path stability).

#### Implementation steps

1. Replace or guard `sudo rm -rf` cleanup paths in `Makefile`.
2. Define retention strategy for `runs` DB + artifact dirs (manual route or background policy).
3. Resolve zipped-wheel `sites.yml` persistence strategy (`settings.data_root/.cache` approach).
4. Improve troubleshooting docs for AI-disabled and analyzer-unavailable states.

#### Verification gates

- `pytest -q tests/test_dashboard_db.py tests/test_dashboard_runner.py tests/test_dashboard_entrypoint.py`
- Make-target smoke checks for cleanup and run workflows.

#### Exit criteria

- Destructive operations are explicit and safer.
- Retention and packaging TODOs are converted to implemented behavior or tracked ADR-level decisions.

#### Phase 8 Slice A (Implemented)

- Replaced `sudo rm -rf` cleanup targets with non-privileged `rm -rf -- ...` and added an explicit guard for `clean-all` (`CONFIRM_CLEAN_ALL=1`).
- Added manual retention primitives in `dashboard/api/db.py`:
  - `find_prunable_runs(...)` (age/source/status filtered selection)
  - `prune_runs_by_id(...)` (defensive delete that ignores in-flight rows)
- Added operator-facing retention script `scripts/prune_runs.py` (+ `make prune-runs`) to prune old terminal rows and matching artifact/log directories.
- Hardened packaged `sites.yml` subprocess resolution via `runner._resolve_sites_file()` cache path and tests.
- Updated docs for AI-disabled/analyzer-unavailable behavior and current packaging/retention posture.

---

## Execution Strategy

- Deliver in small PRs per phase (or sub-phase for large files).
- Each PR must include:
  - scope statement
  - compatibility note
  - tests added/updated
  - rollback note (for risky changes)
- Avoid mixing comparator decomposition and dashboard decomposition in one PR.

## Suggested Order of Work

1. Phase 0
2. Phase 1
3. Phase 2
4. Phase 3
5. Phase 4
6. Phase 5
7. Phase 6
8. Phase 7
9. Phase 8

Rationale: first remove ambiguity and lock lifecycle semantics, then stabilize core comparator/report boundaries, then refactor dashboard layers, then retire compatibility debt, then harden operations.

## Success Metrics

- Fewer oversized modules in comparator/dashboard hotspots.
- Fewer fallback/legacy branches in hot paths.
- Stable or improved test pass rate and runtime.
- Zero artifact layout regressions.
- Reduced doc/code drift incidents.
- Retention/packaging TODOs closed or explicitly deferred with owner + trigger.

## Non-goals

- Rewriting the crawler determinism model in this track.
- Changing AI provider abstraction semantics.
- Introducing major new dependencies unless strictly justified.
