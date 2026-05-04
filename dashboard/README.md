# AI Frontend Regression Dashboard

Local-first web UI for the `test_ui` AI-assisted UI regression pipeline.
Replaces the operator-CLI workflow with a browser: add URLs, click
"Run Baseline" → "Run Current" → "Compare" → "Generate Report",
view per-URL drill-ins with side-by-side baseline/current/diff
screenshots and the AI severity rollup.

> **Linux-only.** The dashboard reads `/proc/<pid>/stat` for PID-recycling
> defense. Mac/Windows operators use the Docker path (which runs the
> dashboard in a Linux container). See `dashboard/api/main.py:_require_linux`.

## Layout

```
dashboard/
├── api/        # FastAPI backend
│   ├── main.py     # app factory + lifespan
│   ├── routes.py   # sites + runs + reports + health (single file for now)
│   ├── runner.py   # subprocess job runner: spawn, watch, recover
│   ├── db.py       # SQLite schema + migrations + row helpers
│   ├── sync.py     # backfill `runs` table from on-disk manifests
│   ├── spa.py      # mounts the React bundle (production same-origin)
│   └── models.py   # Pydantic wire shapes
└── web/        # React SPA (Vite + TypeScript + Tailwind)
    ├── src/
    │   ├── api/    # openapi-typescript-generated client + TanStack Query hooks
    │   ├── components/
    │   └── pages/  # Sites, Runs, Reports
    └── dist/       # built bundle (gitignored; produced by `npm run build`)
```

## Running the dashboard

### Local dev (Linux only — fast edit loop)

```bash
make dashboard-dev
```

Parallels uvicorn (with `--reload`) and the Vite dev server via
`concurrently`. Open http://localhost:5173 — Vite proxies `/api/*` to
the backend on `:8080`.

For backend-only iteration (no SPA), `make dashboard-dev-backend`.

### Docker (cross-platform — Mac/Windows operators use this)

```bash
make dashboard-docker     # docker compose up -d dashboard
make dashboard-logs       # tail the container
make dashboard-down       # stop dashboard + ai-analyzer
```

Open http://localhost:8080. The Docker image builds the React bundle
in Stage 1 (Node 20) and serves it from Stage 2's FastAPI (Python 3.13)
at the same origin as `/api/*`.

`make dashboard-build` force-rebuilds the image (e.g. after editing
`Dockerfile.dashboard` or bumping `pyproject.toml` / `package.json`).

## Configuration

All env vars are namespaced `AFR_*`. Common ones:

| Var | Default | Purpose |
|---|---|---|
| `AFR_DASHBOARD_PORT` | `8080` | uvicorn port |
| `AFR_DASHBOARD_BIND` | `127.0.0.1` | uvicorn bind address (`0.0.0.0` in Docker) |
| `AFR_DASHBOARD_DEV_MODE` | unset | When truthy, installs the Vite-dev CORS middleware |
| `AFR_DASHBOARD_MAX_CONCURRENT_RUNS` | `2` | Cap on simultaneously-spawned crawl/compare/report subprocesses (each is ~500 MB Chromium) |
| `AFR_DATA_ROOT` | `data` | Where artifact directories + `dashboard.db` live |
| `AFR_AI_ANALYZER_SERVICE_URL` | `http://ai-analyzer:3000` | Reachable from inside Docker network |

## Architecture notes

**Subprocess isolation.** The dashboard never imports the crawler /
comparator / report generators directly — it spawns
`python -m test_ui <command> --run-id <ULID> ...` as a separate process
group (`start_new_session=True`). Crash isolation: a Playwright segfault
takes down the subprocess, the dashboard stays up. Cleanup safety: on
restart, `recover_orphaned_runs` SIGTERMs each PGID it identifies as
ours via `/proc/<pid>/stat`'s start-time check (defense against PID
recycling).

**Same-origin SPA.** In production the React bundle and the JSON API
are served from the same FastAPI instance. No CORS, no separate static
host. The Vite dev server (port `5173`) is dev-only — it proxies
`/api/*` to the backend on `:8080`.

**Run-id pre-allocation.** The dashboard generates a ULID before
spawning, passes it via `--run-id`, and tracks the row by that ID. The
CLI accepts the flag transparently (auto-generates one when absent —
the CLI use case).

## Tests

```bash
poetry run pytest tests/test_dashboard_*.py
```

432 tests across DB layer, sync, runner (subprocess + recovery), routes
(sites CRUD + reports + health), SPA serving, and an end-to-end
TestClient happy-path (`test_dashboard_api_workflow.py`).

For the CI surface, see `.github/workflows/ci.yml` — `dashboard-openapi-drift`
fails if the committed `schemas/dashboard-openapi.json` snapshot
disagrees with `app.openapi()` from the live backend (which would mean
the React TypeScript types are stale).

## Known limitations (planned for later milestones)

- **No data retention.** `runs` table + on-disk dirs grow monotonically;
  see `dashboard/api/db.py` `TODO(retention)` for the planned approach.
- **No authentication.** The startup warning when binding non-loopback
  is the only access control. MVP-acceptable for localhost / trusted-LAN
  use.
- **Image size ~2 GB.** Bundles Playwright Chromium because the spawned
  subprocesses crawl from inside the dashboard container. A
  separate-container architecture is possible but much more complex.
- **Zipped-wheel deployment unsupported.** `dashboard/api/routes.py`
  needs a stable `sites.yml` path; under a wheel the bundled YAML is in
  a zipfile. Switch to editable install for now.
