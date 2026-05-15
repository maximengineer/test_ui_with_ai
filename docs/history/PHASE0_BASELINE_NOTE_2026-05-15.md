# Phase 0 Baseline Note (2026-05-15)

Scope: record baseline metrics + verification gates for
`docs/ARCHITECTURE_IMPROVEMENT_PLAN.md` Phase 0.

## Environment Snapshot

- Date: 2026-05-15
- Repo HEAD: `77af3ea`
- Artifact root sampled: `data/`

## Hotspot Size Snapshot

Largest code files (excluding `node_modules`) at capture time:

- `dashboard/web/src/api/schema.gen.ts`: 1514 LOC
- `dashboard/api/db.py`: 682 LOC
- `dashboard/api/runner.py`: 659 LOC
- `dashboard/web/src/pages/runs/RunsPageView.tsx`: 627 LOC
- `test_ui/crawler/engine.py`: 566 LOC
- `test_ui/common/sites.py`: 553 LOC
- `dashboard/web/src/pages/SitesPage.tsx`: 549 LOC
- `dashboard/api/models.py`: 482 LOC
- `test_ui/report/generator.py`: 442 LOC
- `dashboard/api/routes_runs.py`: 421 LOC
- `dashboard/web/src/pages/ReportsPage.tsx`: 418 LOC

## Verification Gates

Commands and outcomes:

1. `.venv/bin/pytest -q`
   - Result: pass
   - Runtime: `real 8.10s`
2. `.venv/bin/pytest -q -m slow`
   - Result: pass
   - Runtime: `real 3.41s`
3. `.venv/bin/python scripts/audit_paths.py`
   - Result: `audit_paths: clean (no hardcoded data/ paths in source).`
   - Runtime: `real 0.05s`

Collection snapshot:

- `pytest --collect-only`: `495/593 tests collected (98 deselected)`
- `pytest --collect-only -m slow`: `98/593 tests collected (495 deselected)`

## Representative Pipeline Timings (Manifest-Derived)

Measured from existing `data/<kind>/<date>/<run_id>/manifest.json` samples
(`finished_at - started_at`) under `data/11-05-2026`:

- `baseline`: 159s (`data/baseline/11-05-2026/01KRC38EF7B4DYAJFV9CSR96P8/manifest.json`)
- `current`: 160s (`data/current/11-05-2026/01KRC3ND4QWYG24KTDBD6KN0RX/manifest.json`)
- `comparator`: 5s (`data/comparator/11-05-2026/01KRC7MY3RFW4TZMEMDACDXZJ7/manifest.json`)
- `report`: 1867s (`data/report/11-05-2026/01KRC7RX9GB6Y3V7C8MV0NSWPS/manifest.json`)

Note: these are single-run samples, useful as baseline order-of-magnitude,
not stable SLOs.

## Scope Constraints (Reaffirmed)

- No artifact layout changes outside canonical
  `data/<kind>/<DD-MM-YYYY>/<run_id>/...`.
- No cross-language AI contract breaks without schema updates.
- No CLI surface changes without explicit migration/user note.
