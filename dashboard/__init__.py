"""Dashboard package (Milestone C).

Two halves:
  - `dashboard.api` — FastAPI app + SQLite job runner state
  - `dashboard.web` — React SPA built into static assets (Phase C.2)

Designed to share the existing `test_ui` machinery (Orchestrator, manifests,
locks) rather than duplicate it. The dashboard *triggers* runs by spawning
the same `python -m test_ui ...` subprocess a CLI user would; it does not
reimplement the pipeline.
"""
