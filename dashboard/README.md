# AI Frontend Regression Dashboard

Local-first web UI for the `test_ui` AI-assisted UI regression pipeline.
Replaces the operator-CLI workflow with a browser: add URLs (single or
bulk-paste), click **Run all** to sequence baseline → current →
comparator → report in one shot (or run any stage individually), view
the result on a session-grouped Runs table, drill into per-URL reports
with side-by-side baseline/current/diff screenshots and the AI
severity rollup.

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

### Local dev (Linux only - fast edit loop)

```bash
make dashboard-dev
```

Parallels uvicorn (with `--reload`) and the Vite dev server via
`concurrently`. Open http://localhost:5173 - Vite proxies `/api/*` to
the backend on `:8080`.

For backend-only iteration (no SPA), `make dashboard-dev-backend`.

### Docker (cross-platform - Mac/Windows operators use this)

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
| `AFR_VISUAL_SIMILARITY_THRESHOLD` | `0.95` | SSIM threshold for `visual_changes`. SSIM ≥ this is treated as no change (above the WebP-encoding noise floor of ~0.999). Tighten toward 1.0 for stricter regression detection; loosen for noisier corpora. |

## Architecture notes

**Subprocess isolation.** The dashboard never imports the crawler /
comparator / report generators directly - it spawns
`python -m test_ui <command> --run-id <ULID> ...` as a separate process
group (`start_new_session=True`). Crash isolation: a Playwright segfault
takes down the subprocess, the dashboard stays up. Cleanup safety: on
restart, `recover_orphaned_runs` SIGTERMs each PGID it identifies as
ours via `/proc/<pid>/stat`'s start-time check (defense against PID
recycling).

**Same-origin SPA.** In production the React bundle and the JSON API
are served from the same FastAPI instance. No CORS, no separate static
host. The Vite dev server (port `5173`) is dev-only - it proxies
`/api/*` to the backend on `:8080`.

**Run-id pre-allocation.** The dashboard generates a ULID before
spawning, passes it via `--run-id`, and tracks the row by that ID. The
CLI accepts the flag transparently (auto-generates one when absent -
the CLI use case).

**Sessions view (frontend-derived).** The Runs page shows one row per
*session* (= one cycle of baseline + current + comparator + report)
rather than one row per backend `runs` record. Sessions are inferred
client-side in `dashboard/web/src/lib/sessions.ts` by grouping rows by
`date_dir` and pairing positionally within each date - no schema
change. Per-session bulk delete maps to N×4 individual `db_id`
deletes via `POST /api/runs/bulk-delete` (per-id outcomes returned so
in-flight rows are reported as skipped, not failed).

**Run all chain (frontend-orchestrated).** The blue "Run all" button
on `/runs` walks the four stages sequentially: spawn baseline → poll
`/api/runs/{db_id}` every 2s until terminal → spawn current → ... →
report. The state machine lives in `useRunAll` (browser memory). 409
conflicts (an active run for kind+date already exists) are adopted -
the chain attaches to the existing row rather than failing. The
chain is browser-side by design: each stage shows up as its own
visible row in the table (preserving per-stage retry/delete/log
visibility) and there's no stateful supervisor in the API process.
Trade-off: closing the tab or restarting the dashboard mid-chain
stops the next-stage spawn (the in-flight subprocess on the server
keeps running; only the chain's progression stops).

## Tests

```bash
poetry run pytest tests/test_dashboard_*.py
```

~196 dashboard tests across DB layer, sync, runner (subprocess +
recovery), routes (sites CRUD including bulk add/delete + runs
including bulk delete + reports + health), SPA serving, and an
end-to-end TestClient happy-path (`test_dashboard_api_workflow.py`).

For the CI surface, see `.github/workflows/ci.yml` - `dashboard-openapi-drift`
fails if the committed `schemas/dashboard-openapi.json` snapshot
disagrees with `app.openapi()` from the live backend (which would mean
the React TypeScript types are stale).

### End-to-end pipeline validation against external sites

Pytest covers the dashboard's own logic. To validate the **comparator +
report** pipeline against sites you don't control (e.g. external
gov.ie URLs that don't change between runs), use
[`scripts/tamper_baseline.py`](../scripts/tamper_baseline.py). It
injects 19 deterministic synthetic mutations into a fresh baseline -
one mutation pattern per site - so the next `current` + `comparator`
run produces a real diff the framework has to detect.

Mutation coverage spans:

- **Visual** (4): drastic rectangle, subtle text overlay, uniform RGB
  shift, tiny corner pixel
- **HTML** (7): structural insertion, attribute mutation, `href`
  hijack (phishing sim), `<script>` injection (XSS sim), meta-tag
  changes (SEO), hidden content (`display:none` / offscreen),
  critical text change
- **CSS** (5): real value change, equivalent rewrite (`#fff` →
  `white`), rule reorder, `@media` breakpoint shift, `!important`
- **JS** (3): marker function, behavior change (operator flip),
  format-only (whitespace)

One site is left untouched as an environmental-noise control.

**Workflow** - per cycle, on a fresh baseline:

```bash
# 1. Capture clean baseline (via dashboard or `make baseline`).
# 2. Inject mutations - script needs root inside the container because
#    baseline dirs are written by the dashboard's subprocess as root:
cat scripts/tamper_baseline.py \
  | docker exec -i test_ui_with_ai-dashboard-1 python -

# 3. In the dashboard at /runs, click these in order (NOT "Run all" -
#    that spawns a fresh baseline and wipes the tampering):
#       Run current  →  Run comparator  →  Run report

# 4. Open /reports and audit each site against the manifest the script
#    printed in step 2.
```

**Manifest format.** Each mutation entry carries an `expected` field
(`should_flag`, `should_not_flag`, or `edge_case`). After the report
finishes, cross-check `/reports` against the manifest:

| Manifest `expected` | `/reports` shows | Verdict |
|---|---|---|
| `should_flag` | flagged | ✓ working |
| `should_flag` | `no_changes` | **false negative** - framework missed a real change |
| `should_not_flag` | flagged | **false positive** - framework is too noisy |
| `edge_case` | either | design choice; document the answer |

The control site (numerically the highest-id site) MUST come back as
`no_changes`. If it doesn't, environmental noise (timezone-dependent
content, dynamic ads, A/B variants) is producing diffs and you need
to fix that before trusting any other audit result.

The script is re-runnable any time after a fresh baseline; it does
not back up the baseline because re-snapshotting from gov.ie is the
faster path to a clean state. See the script's docstring for full
per-pattern details and override env vars.

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
- **Run all is browser-orchestrated.** The chain progression lives in
  the SPA tab; closing the tab or recreating the dashboard container
  stops the next-stage spawn (the in-flight subprocess finishes
  normally on the server). For a chain that survives those, a
  backend supervisor task would be needed. Acceptable for the
  local-operator use case where someone is watching the run anyway.
