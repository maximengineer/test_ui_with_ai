# Refactor + Dashboard Implementation Plan

> **Historical record (archived).** All three milestones have shipped.
> For current architecture see [`ARCHITECTURE.md`](../../ARCHITECTURE.md).
> For still-open follow-ups (deferred deps, latent bugs, intentional
> out-of-scope items) see [`BACKLOG.md`](../../BACKLOG.md).
> This file is preserved as the decision log: locked decisions,
> rationale, risks, and per-phase implementation flags from when the
> work was in flight.

**Status:** Approved - ready for Milestone A.
**Estimated effort:** 23–28 working days, single engineer focused. Three independently shippable milestones.
**Author:** Claude (senior engineer review of existing codebase, revised four times after self-critique and one external AI critique).

---

## What changed in this revision

Following an external review, the plan absorbed the following corrections that I'd missed in earlier drafts:

- **Run identity:** date-only directories (`DD-MM-YYYY`) aren't sufficient identity. Two runs the same day overwrite each other. Introduces immutable `run_id` per execution; date stays as grouping/display. New layout: `data/baseline/DD-MM-YYYY/<run_id>/<url_dir>/`.
- **Retry actually works:** `runs` table now stores `args_json`/`command_json`. Without these, `/api/runs/{id}/retry` was impossible.
- **Workflow integrity:** per-artifact manifests + locks + preconditions, not just per-`kind` idempotency. `compare` requires complete `baseline` and `current`; `report` requires complete `compare`.
- **Stable site IDs:** `sites.yml` gains `id: <slug>` (immutable) alongside `name` (display) and `url`. Renames don't break historical mapping.
- **Process groups:** dashboard cleanup uses session/PGID + start-time verification, not bare PID. Avoids killing unrelated PID-recycled processes.
- **Result discriminator:** AI output JSON gains `result_type`, `schema_version`, `model`, `request_id`. Consumers don't infer type by filename or shape.
- **AI opt-out:** `AFR_AI_ENABLED=false` skips AI calls and writes `ai_disabled.json`. For sensitive sites where you don't want to ship DOM/screenshots to a third party.
- **README day-one rewrite:** README claims contradict reality today. Fix now, not at the end of Phase 1.
- **Three-milestone split:** Pipeline truthful (A) → Durable artifact model (B) → Dashboard (C). A and B are valuable on their own.

I pushed back on (and did *not* include): ADR formalism, RunExecutor interface for hypothetical future, full SSRF protection, audit-event table, DOM redaction. These are incremental additions for after MVP if needed.

---

## Goals

1. **Make the AI pipeline actually work.** Today, structured diff data the Python side gathers never reaches the model in any meaningful form ([ai_analyzer/server.js:115-153](ai_analyzer/server.js#L115)). After Milestone A, CSS/JS/HTML changes are what the model analyzes - not three screenshots and a count.
2. **Make the codebase maintainable.** Break the two god-objects ([report/generator.py](test_ui/report/generator.py) 1,678 LOC, [comparator/engine.py](test_ui/comparator/engine.py) 1,390 LOC) into single-responsibility modules. Add the test coverage that doesn't exist.
3. **One contract between Python and Node.** Pydantic v2 source of truth → JSON Schema → ajv. Validated cross-language in CI.
4. **Durable artifact and run identity.** Every execution has an immutable `run_id`. Concurrent or repeated runs can't corrupt each other's data.
5. **Ship a dashboard.** React + FastAPI for managing URLs, triggering runs, viewing reports - replacing manual `sites.yml` editing and Makefile invocations.

## Non-goals

- Rewriting the crawler or visual-diff algorithm. (One import line is touched in Milestone A for URL canonicalization. No behavior change.)
- Production-grade auth, multi-tenancy, RBAC. Single-user dashboard, defaults to localhost-only.
- Replacing Crawl4AI, the AI provider, or any other external dependency.
- Kubernetes deployment. The README claims this; the plan removes the claim.
- Deprecating the `enhanced_report` CLI command. CLI and dashboard both stay.
- DOM/screenshot redaction. Out of scope; opt-out via `AFR_AI_ENABLED=false` instead.
- Subprocess re-attachment after dashboard restart. SIGTERM + interrupted-status only.
- `RunExecutor` abstract interface for a hypothetical second job runner. YAGNI.

---

## Decisions (locked)

| # | Decision | Rationale |
|---|---|---|
| 1 | Date format stays `DD-MM-YYYY` for human-facing grouping | User preference. Run identity sits *under* date dirs. |
| 2 | Identity: immutable `run_id = ULID` per execution | Sortable, URL-safe, generation-collision-free. Path becomes `data/<kind>/DD-MM-YYYY/<run_id>/...` |
| 3 | Dashboard: React + FastAPI | Vite + TypeScript + Tailwind. |
| 4 | Job runner: subprocess + SQLite + process groups | Dashboard owns state machine. CLI stays unaware. |
| 5 | AI prompt: `ai_analyzer/prompts/system.txt` | Mounted as Docker volume. SHA-256 in API responses, persisted in output, exposed via `/health`. |
| 6 | Site IDs: `id` (immutable slug), `name` (display), `url` | Renames don't break history. |
| 7 | Dashboard binds `127.0.0.1` by default | Opt into LAN binding via env var with a startup warning. |
| 8 | Dead `report` CLI command | Delete in A.3. |

## Directory & naming conventions (locked)

| Path | Purpose |
|---|---|
| `test_ui/contracts/` | Pydantic v2 model **source of truth** (`ai_contract.py`). |
| `test_ui/common/` | Shared utilities (`url_id.py`, `images.py`, `run_id.py`). |
| `schemas/` | **Generated** JSON Schema. Consumed by Node ajv. CI verifies sync. |
| `docs/data_shapes.md` | Documentation of input data shapes (`html_changes.json` etc.). |
| `docs/determinism.md` | What Crawl4AI determinism controls we use today. |
| `tests/fixtures/golden/` | Golden output snapshots. |
| `tests/fixtures/contracts/` | Cross-language schema test fixtures. |
| `tests/fixtures/example_diffs/` | Sample `html_changes.json` for tests + Phase A.1 verification. |
| `data/<kind>/DD-MM-YYYY/<run_id>/...` | Per-run artifact tree. `<kind>` ∈ baseline / current / comparator / report. |
| `data/<kind>/DD-MM-YYYY/<run_id>/manifest.json` | Run manifest: status, started_at, finished_at, source_run_ids, file count, checksum. |
| `data/runs/<id>.log` | Subprocess merged stdout+stderr. |

---

# MILESTONE A - Pipeline truthful and safe (10–12 days)

**Goal:** Stop the AI theater. Stop misleading users. Make the codebase maintainable. Add the missing tests. Ship-able on its own.

## Phase A.0 - Foundation (Day 1–2)

- **A.0.1 README day-one rewrite.** Today's README claims "production-ready, enterprise-grade," "no sensitive data sent to AI services," "Kubernetes-ready." None of these are true today. Replace opening with factual current-state language. Add a "Current limitations" section. Replace the privacy claim with: *"AI analysis sends screenshots and structured page data to the configured AI provider. Do not run against pages with secrets, PII, or confidential content unless you have approved data-processing controls. Set `AFR_AI_ENABLED=false` to disable AI calls entirely."* Remove the Kubernetes and "memory-efficient streaming" claims. Splits Quick Start into two tested paths: Docker (`docker compose up --build` + `make test-full`) and Local (venv + Node + ai-analyzer + report).
- **A.0.2 Verify environment.** Pydantic v2 confirmed in [config.py:2](test_ui/config.py#L2). Verify `ajv` (v8) + `ajv-formats`, `respx`, `pytest-asyncio`, `openapi-typescript`, `vitest`, and `python-ulid` install on Python 3.13 / Node 20 in clean env.
- **A.0.3 Add paths config + audit script.** Extend `Settings` with `data_root`, `baseline_dir`, `current_dir`, `comparator_dir`, `report_dir`, `runs_db_path`, `runs_log_dir`, `ai_enabled` (bool, default true). All overridable via `AFR_*`. `scripts/audit_paths.py` regex-greps `.py` files for hardcoded `data/` paths; gracefully skips dirs that don't exist; non-zero exit on hits. Wire into `make audit` and CI.
- **A.0.4 Consolidate AI service URL env var.** Drop the unprefixed `AI_ANALYZER_SERVICE_URL` alias. **Breaking change:** add a startup warning if the old variable is set, instructing the user to rename. Update `.env.example` and README.
- **A.0.5 Crawler determinism doc.** `docs/determinism.md` enumerates what we already control via Crawl4AI (viewport size, headless mode, timeout) and what we don't (timezone, animations, A/B test stabilization, fonts, cookie state). Sets a baseline for future work without expanding scope now.
- **A.0.6 Scaffolding directories.** Create `schemas/`, `test_ui/contracts/`, `test_ui/common/`, `docs/`, all with `__init__.py` where applicable. Add `schemas/README.md` stating the files are generated.
- **A.0.7 Wire up tests.** Verify `ruff` not already a dep; add via Poetry along with `pytest`, `pytest-asyncio`, `pytest-cov`, `respx`. **Pin** `opencv-python` and `scikit-image` to exact versions. Configure `pyproject.toml`:
  ```toml
  [tool.pytest.ini_options]
  markers = ["slow: tests that take >5s; skipped by default"]
  addopts = "-q -m 'not slow'"
  ```
- **A.0.8 Comment the date sort + Makefile.** One-line comment at [cli.py:279](test_ui/cli.py#L279). Add `make test`, `make audit`.

**Deliverable:** Honest README. Clean foundation. CLI behavior unchanged. `make test` and `make audit` pass.

## Phase A.1 - Fix the AI pipeline (Day 3–6)

### A.1.1 Verify input data shapes
Read 3–5 real `html_changes.json` etc. and document in `docs/data_shapes.md`. Per-change severity? Realistic max payload size? Fallback for fresh installs: example diffs in `tests/fixtures/example_diffs/`.

### A.1.2 Pydantic models with discriminator + version
Create `test_ui/contracts/ai_contract.py`:
- `AIAnalysisRequest` - `schema_version` ("2026-04-30.1"), `request_id` (UUID), url, structured_data with full change arrays, screenshots typed `Base64Bytes`. Uses `ConfigDict(extra='forbid')`.
- `AIAnalysisResponse` - `result_type: Literal["analysis_success"]`, `schema_version`, `request_id`, `model`, `prompt_sha256`, overall_severity (`Literal["CRITICAL","WARNING","SAFE"]`), business_impact, detailed_analysis, recommendations, confidence_score.
- `AIAnalysisError` - `result_type: Literal["analysis_error"]`, `schema_version`, `request_id`, `model` (optional, may be null if provider not reached), `prompt_sha256` (Optional), `error_type`, `retryable`, `details`.
- `NoChangesMarker` - `result_type: Literal["no_changes"]`, `schema_version`, `checked_at`.
- `AIDisabledMarker` - `result_type: Literal["ai_disabled"]`, `schema_version`, `checked_at`. Written when `AFR_AI_ENABLED=false`.

`scripts/export_schemas.py` writes JSON Schema for each. CI checks drift (Phase A.4).

### A.1.3 Mount schemas + prompts into Node container
`docker-compose.yml`:
```yaml
ai-analyzer:
  volumes:
    - ./schemas:/app/schemas:ro
    - ./ai_analyzer/prompts:/app/prompts:ro
```
For local-dev, `server.js` reads `SCHEMAS_DIR` (default `./schemas` relative to `__dirname`) and `PROMPTS_DIR` (default `./prompts`). No magic fallback chains.

### A.1.4 Rewrite Node prompt construction with bounded payload
- Add `ajv` v8 + `ajv-formats` to `ai_analyzer/package.json`.
- Replace hand-rolled validation with ajv against `schemas/ai_request.schema.json`. Compile schema once at startup. Configure `{strict: true, allErrors: true}`.
- Send full structured data - but with **bounded defaults** (overcorrection from earlier draft):
  - Max request body: 30 MB.
  - Max changes per category: 200 (configurable via env). Prioritization: structural HTML changes → content changes → CSS/JS by `total_changes` desc.
  - Max code snippet length: 2000 chars (was `substring(0, 200)`).
  - Counts of dropped/truncated items recorded in response metadata.
- Image validation: decoded PNG/JPEG must start with magic bytes (`\x89PNG` / `\xff\xd8\xff`); decoded size ≤ 10 MB per image.

### A.1.5 System prompt to file, hash everything, expose via /health
- `ai_analyzer/prompts/system.txt`. Move current prompt content into it.
- Node loads at startup; **fail loudly if missing**.
- Compute SHA-256 of prompt file and concatenated schema files at startup.
- Every API response includes `prompt_sha256` and `schemas_sha256`.
- `/health` returns `{ok, prompt_sha256, schemas_sha256, model}`.

### A.1.6 Phase-A contract smoke test (moved early per critique)
Don't wait until Phase A.4 to discover Pydantic ↔ ajv disagreement. One smoke test now:
- One valid sample, one invalid sample in `tests/fixtures/contracts/`.
- pytest test validates with Pydantic, then `subprocess.run(['node', 'ai_analyzer/scripts/validate.js', fixture])` for ajv.
- Fail loudly if validators disagree. Catches `format`, `null`/`Optional`, `$defs`, additional-properties mismatches *before* you depend on them.
- Full contract test matrix lands in A.4.

### A.1.7 Fix httpx.AsyncClient leak - CLI owns client
Each Click command in [cli.py](test_ui/cli.py) wraps with:
```python
async def _run():
    async with httpx.AsyncClient(timeout=settings.ai_analyzer_timeout) as client:
        orchestrator = Orchestrator(
            client=client,
            ai_analyzer_url=settings.ai_analyzer_service_url,
        )
        await orchestrator.generate_enhanced_report(...)
asyncio.run(_run())
```
All 5 Click commands updated. `Orchestrator.__init__` accepts `client` and `ai_analyzer_url`, passes to `ReportGenerator`.

### A.1.8 Stop fabricating fake AI analyses for failures
- Node returns HTTP non-2xx with `AIAnalysisError`-shaped body on failure.
- Python's `ai_client` returns `Union[AIAnalysisResponse, AIAnalysisError]`.
- Aggregator handles all four `result_type` values.
- **Per-URL retry CLI:** `python -m test_ui retry-url --date <DD-MM-YYYY> --run <run_id> --url <url-name>` re-runs AI for one URL. Used by Phase 1 "see logs" message and (Milestone C) dashboard retry button.

### A.1.9 No-changes and AI-disabled marker files
- No detected changes → `no_changes.json` (`NoChangesMarker` shape).
- `AFR_AI_ENABLED=false` → skip AI call entirely, write `ai_disabled.json` (`AIDisabledMarker` shape).
- **Migration:** `scripts/migrate_no_changes.py` scans existing `data/report/<date>/<url>/ai_analysis.json`, renames synthetic-no-change instances to `no_changes.json`. Idempotent. Keep the script in `scripts/`.

### A.1.10 HTTP 429 retry, Retry-After, semaphore
- Treat 429 as retryable (currently lumped with non-retryable 4xx at [generator.py:374](test_ui/report/generator.py#L374)).
- `asyncio.Semaphore(N)` at AI client level. Default N=3, configurable via `AFR_AI_CONCURRENCY`.

**Deliverable:** Real `ai_analysis.json` references CSS selectors / JS code in `detailed_analysis.technical_correlation`. `prompt_sha256` + `schemas_sha256` on `/health`. `result_type`, `schema_version`, `request_id`, `model` in every JSON output. AI failures produce `AIAnalysisError`-shaped JSON. `AFR_AI_ENABLED=false` produces `ai_disabled.json`. `retry-url` CLI works. Smoke contract test green. README accurate.

## Phase A.2 - Characterization tests (Day 7)

Tests that pin down current behavior, before refactoring:
- **E2E smoke (CLI path):** fake comparator tree + respx-mocked AI service → `Orchestrator.generate_enhanced_report()` → assert HTML report contains expected sections. Wraps execution in `respx.mock(base_url=...)` context, constructs `httpx.AsyncClient` *inside* (per A.1.7 ownership pattern).
- **Golden ai_analysis.json snapshot** (deterministic via mocked AI). Refactor must not change byte-for-byte (timestamps + `request_id` normalized).
- **Comparator golden:** synthetic before/after pair → assert `comparison_results.json` and `diffs/*.json` match stored golden. Float comparisons via `pytest.approx(rel=1e-3)`. Binary outputs (visual_diff.png) checked for existence + size-bounds, not byte-equality. Marked `@pytest.mark.slow`.
- **Updating goldens:** `pytest --update-golden` flag (~10 lines via `pytest_addoption`).

These must pass before any A.3 code change is committed.

## Phase A.3 - Refactor god-objects (Day 8–10)

### Split `test_ui/comparator/`
`engine.py` → thin orchestrator (~150 LOC). Extract:
- `comparator/screenshots.py` - SSIM + visual diff. **Hard import** of `cv2`/`skimage`; no silent `CV2_AVAILABLE = False`.
- `comparator/dom.py` - BeautifulSoup HTML diff.
- `comparator/assets.py` - CSS/JS/media diffing.
- `comparator/finder.py` - date-dir + run-dir scanning, baseline/current pairing.
- `test_ui/common/url_id.py` - single canonical `url_to_dirname()`. Crawler ([crawler/engine.py:17-24](test_ui/crawler/engine.py)) and comparator (duplicated at [engine.py:88-93](test_ui/comparator/engine.py#L88) and [105-109](test_ui/comparator/engine.py#L105)) both import from here.
- `test_ui/common/images.py` - moves from `test_ui/utils/image_compression.py`. Functionally unchanged.

### Split `test_ui/report/`
`generator.py` → thin orchestrator (~200 LOC). Extract:
- `report/discovery.py` - `discover_comparison_data` and friends.
- `report/loader.py` - Each URL has exactly one of: `ai_analysis.json` (success), `ai_error.json` (failure - split per critique), `no_changes.json`, `ai_disabled.json`. Loader returns one of the typed Pydantic objects via `result_type` discriminator. (Note: the critique recommended splitting success and error into separate files. Adopting that - `ai_error.json` is now its own filename. Cleaner than overloading `ai_analysis.json`.)
- `report/ai_client.py` - HTTP client wrapper, retry, semaphore, schema validation. Accepts injected client (per A.1.7).
- `report/aggregator.py` - `aggregate_analyses`. Handles all four result types.
- `report/html_renderer.py` - Jinja templating. Templates move to `test_ui/report/templates/`. **"AI analysis failed" badge ships here**, with retry-url CLI invocation displayed for copy.

### Other cleanup
- Delete dead `report` CLI command: [cli.py:235-246](test_ui/cli.py#L235), `Orchestrator.generate_report` ([cli.py:85-100](test_ui/cli.py#L85)), Makefile target referencing it.
- Investigate multi-signal change detection at [generator.py:42-54](test_ui/report/generator.py#L42-L54). Time-box to half a day. If unresolved, document with comment + file follow-up issue.
- No file in `test_ui/` exceeds 400 LOC.

**Deliverable:** Single-responsibility modules. Goldens green. No functional change vs end of A.1.

## Phase A.4 - Expand test coverage + CI (Day 11–12)

1. **AI client tests** (highest value). Mock with `respx`. Cover: 200, 429-with-Retry-After, 500-with-retry, timeout, malformed JSON, schema-invalid response. Each lands in correct error bucket. Test that semaphore caps in-flight at N (using `asyncio.Event`).
2. **Cross-language schema contract test matrix.** Builds on the A.1.6 smoke. Multiple fixtures in `tests/fixtures/contracts/`. Single pytest test iterates: validates with Pydantic, subprocess-calls Node validator (`node ai_analyzer/scripts/validate.js <fixture>`), parses single-char stdout, asserts both agree with `<scenario>.expected.json`. No intermediate result files; no test ordering hazard. Strict mode on both sides.
3. **URL canonicalization tests** for `common/url_id.py`. Edge cases: trailing slashes, query strings, ports, unicode, www stripping, fragments.
4. **Discovery tests** with `tmp_path` fixtures.
5. **Comparator unit tests** (Pillow-generated synthetic before/after for SSIM, crafted HTML pairs for DOM diff). Marked `@pytest.mark.slow`.
6. **Schema export drift test:** rerun export, assert `git diff --exit-code schemas/`.

### CI workflow

**Node 20 setup is critical** - Debian Bookworm's `apt-get install nodejs` gets Node 18. Use NodeSource:

```yaml
jobs:
  python-tests:
    runs-on: ubuntu-latest
    container: python:3.13-bookworm
    steps:
      - uses: actions/checkout@v4
      - run: |
          apt-get update
          apt-get install -y libgl1 libglib2.0-0 curl ca-certificates gnupg
          curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
          apt-get install -y nodejs
      - run: pip install poetry && poetry install
      - run: poetry run pytest -q
```

Concrete jobs:
- `lint`: `ruff check test_ui/ scripts/ tests/ dashboard/api/` (dashboard/api added in C). `ruff format --check`.
- `audit`: `python scripts/audit_paths.py`.
- `test-fast`: `pytest -q` (default skips slow). Target <30s.
- `test-slow`: `pytest -q -m slow`. Target <90s (aspirational; OpenCV ops aren't fast).
- `schema-drift`: `python scripts/export_schemas.py && git diff --exit-code schemas/`.
- `node-validate`: `cd ai_analyzer && npm ci && npm test` (Vitest with ajv contract tests).
- `web-typecheck` (Phase C): `cd dashboard/web && npm ci && npm run typecheck && npm run build`.

**Milestone A deliverable:** Honest README; AI pipeline truthful; modular code; tests + CI green. **Shippable on its own.** Stop here if needed.

---

# MILESTONE B - Durable artifact and run model (5–7 days)

**Goal:** Run identity + workflow integrity. Two runs the same day no longer overwrite each other. Concurrent kinds can't corrupt shared state. Site renames don't break history.

## Phase B.1 - Run identity + manifests (Day 13–14)

- **B.1.1 `run_id` generation.** `test_ui/common/run_id.py` exposes `new_run_id()` returning a ULID (sortable, URL-safe, 26 chars). One module, one source of truth.
- **B.1.2 New on-disk layout.** `data/<kind>/DD-MM-YYYY/<run_id>/<url_dir>/...`. Date kept as grouping; `run_id` is identity. Each run also writes `data/<kind>/DD-MM-YYYY/<run_id>/manifest.json`:
  ```json
  {
    "schema_version": "2026-04-30.1",
    "run_id": "01HXYZ...",
    "kind": "baseline",
    "started_at": "...",
    "finished_at": "...",
    "status": "running | complete | failed | interrupted",
    "source_run_ids": {"baseline": "...", "current": "..."},  // for compare/report
    "url_count": 40,
    "files_sha256": "<hash of sorted file path + size list>"
  }
  ```
- **B.1.3 Atomic publication.** Each run writes to a `.tmp-<run_id>` directory and renames to final on completion. Half-written runs never appear under their final path.
- **B.1.4 Latest pointer.** `data/<kind>/DD-MM-YYYY/latest` symlink to the most recent *complete* `<run_id>` directory. `find_latest_baseline()` etc. follow this; falls back to scanning if symlink is missing (legacy / cross-platform).
- **B.1.5 Crawler + comparator + report changes.**
  - Crawler: write to `data/<kind>/DD-MM-YYYY/<run_id>/<url_dir>/`. Generate `run_id` at start, write manifest with `status="running"`, update on completion.
  - Comparator: takes `--baseline-run <run_id>` and `--current-run <run_id>`. Defaults to "latest complete." Writes `data/comparator/DD-MM-YYYY/<run_id>/...` with `source_run_ids` populated.
  - Report: takes `--comparator-run <run_id>`. Writes under its own `run_id`.
  - Old date-only paths still readable for migration grace.
- **B.1.6 Migration script.** `scripts/migrate_run_layout.py` walks existing `data/<kind>/DD-MM-YYYY/<url_dir>/...`, generates a synthetic `run_id` from the directory mtime, moves files into `data/<kind>/DD-MM-YYYY/<run_id>/<url_dir>/`, writes a manifest with `source="migrated"`. Idempotent. Keep in `scripts/`.

## Phase B.2 - Workflow preconditions + per-artifact locks (Day 15–16)

- **B.2.1 Lock files.** Each run-in-progress writes `data/<kind>/DD-MM-YYYY/<run_id>/.lock` containing `{pid, pgid, hostname, started_at, command}`. Removed on completion.
- **B.2.2 Workflow precondition checks** baked into the CLI:
  - `compare` requires *complete* baseline + current manifests. Refuses with clear error if missing or `status != "complete"`.
  - `report` requires complete `comparator` manifest.
  - `baseline` / `current` refuse to start if a lock for the same `kind` + `date` exists with a live PGID.
- **B.2.3 Stale lock recovery.** On lock-acquire, if existing lock's PGID is not alive (verified via `os.kill(pgid, 0)` plus start-time check from `/proc/<pid>/stat` on Linux), log a warning and proceed. Otherwise refuse.

## Phase B.3 - Stable site IDs (Day 17)

- **B.3.1 New `sites.yml` format:**
  ```yaml
  sites:
    - id: homepage-prod
      name: Homepage
      url: https://example.com
  ```
- **B.3.2 Loader migration.** `scripts/migrate_sites_ids.py`: for each site lacking `id`, generate from `name` (slugified, deduplicated). Writes back to `sites.yml` preserving comments via `ruamel.yaml` (not `pyyaml` which loses comments). Idempotent.
- **B.3.3 URL-dir naming derives from `id`,** not `name`. Crawler updated. Existing dirs under `data/<kind>/<date>/<run_id>/` use the URL canonicalization function (`url_to_dirname`); the migration in B.1.6 also reconciles old name-based dirs to id-based.
- **B.3.4 `args_json` and `command_json` in run records.** Even pre-dashboard, the CLI writes a `data/runs/<run_id>.run.json` with full invocation arguments, so future retry has the data it needs. Trivial; one write per run.

**Milestone B deliverable:** Multiple runs the same day no longer overwrite. Manifests + locks + atomic publication prevent partial-data confusion. Site renames preserve history. Migration scripts handle existing data.

---

# MILESTONE C - Dashboard (8–10 days)

**Goal:** Web UI for managing URLs, triggering runs, viewing reports. MVP. **Depends on Milestones A and B.**

## Phase C.1 - Backend (Day 18–22)

### Stack
- FastAPI app at `dashboard/api/`. Pure JSON. Reuses `Orchestrator`. CORS for `http://localhost:5173` in dev.
- SQLite at `settings.runs_db_path` (default `data/dashboard.db`). **WAL mode + busy_timeout.**
- Job runner: dashboard owns state machine via `asyncio.subprocess`, **process group cleanup with start-time verification**.
- **Binds `127.0.0.1` by default.** LAN binding requires `AFR_DASHBOARD_BIND=0.0.0.0`; logs a startup warning when binding non-loopback.

### SQLite schema and migrations

```sql
CREATE TABLE IF NOT EXISTS runs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id        TEXT NOT NULL UNIQUE,    -- ULID per Milestone B.1
  kind          TEXT NOT NULL CHECK (kind IN ('baseline','current','compare','report')),
  status        TEXT NOT NULL CHECK (status IN ('pending','running','done','failed','interrupted')),
  created_at    TEXT NOT NULL,
  started_at    TEXT,
  finished_at   TEXT,
  date_dir      TEXT,                    -- DD-MM-YYYY
  args_json     TEXT NOT NULL,           -- request body that triggered the run
  command_json  TEXT NOT NULL,           -- actual argv used
  exit_code     INTEGER,
  error         TEXT,
  pid           INTEGER,
  pgid          INTEGER,                 -- process group for safe cleanup
  pid_start_time TEXT,                   -- /proc/<pid>/stat[22] at spawn; verifies identity later
  source        TEXT NOT NULL DEFAULT 'dashboard' CHECK (source IN ('dashboard','discovered','cli'))
);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_run_id ON runs(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_kind_date ON runs(kind, date_dir);
```

`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;` at every connection open.

Migrations: `PRAGMA user_version` based. `dashboard/api/db.py` has `MIGRATIONS: list[Callable]`. No Alembic.

### Job runner

Subprocess spawned with `start_new_session=True` (Linux/macOS) to create a new process group. Windows: documented as not supported in MVP.

1. `POST /api/runs` handler:
   - Validates body (Pydantic discriminated union per kind).
   - **Idempotency:** if `pending` or `running` row exists with same `kind` AND date, return `409`.
   - **Workflow precondition check** (per Milestone B.2): `compare` requires complete baseline+current; `report` requires complete comparator. `412 Precondition Failed` if not met.
   - INSERT row: `status='pending', created_at=now(), args_json=<body>, run_id=<new ULID>`.
   - Open log file at `runs_log_dir / f"{db_id}.log"`, hold handle.
   - `process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'test_ui', <command>, '--run-id', run_id, *args, stdout=log_fd, stderr=asyncio.subprocess.STDOUT, start_new_session=True)`.
   - Capture `pgid = os.getpgid(process.pid)` and `pid_start_time` from `/proc/<pid>/stat` (Linux only; falls back to current time on macOS).
   - **Race-safe UPDATE:** `UPDATE runs SET status='running', pid=?, pgid=?, pid_start_time=?, started_at=?, command_json=? WHERE id=? AND status='pending'`. Guard prevents overwriting if subprocess already exited and `_watch` updated to terminal.
   - Schedule `asyncio.create_task(_watch(process, db_id, log_fd))`.
   - Return `202 {db_id, run_id, status}`.
2. `_watch`:
   - `exit_code = await process.wait()`. Close log_fd.
   - `UPDATE runs SET status=?, finished_at=?, exit_code=?, error=? WHERE id=? AND status NOT IN ('done','failed','interrupted')`.
3. **Dashboard startup recovery:**
   - `SELECT id, pgid, pid_start_time FROM runs WHERE status IN ('pending','running')`.
   - For each row: verify the PGID is still ours by checking the leader PID's start time matches `pid_start_time`. If yes → SIGTERM the **process group** (`os.killpg(pgid, SIGTERM)`); wait 5s; SIGKILL if still alive. If start time doesn't match (PID recycled), do *not* kill - log and skip.
   - Mark all such rows `status='interrupted', error='dashboard restarted'`.
   - Run existing-data sync (always, idempotent).

### Existing-data sync

Scans `data/<kind>/DD-MM-YYYY/<run_id>/manifest.json` files. For each not in `runs`, INSERT synthetic row with `source='discovered', status='done'`. Reads timestamps from manifest, not directory mtime. Performance note: scales linearly with historical date+run dirs.

### Sites identification

Uses `id` from `sites.yml` (per Milestone B.3). API routes use `id`, not `name`. Renames are pure metadata changes; URLs don't change.

### API routes

All `/api/`-prefixed. Pydantic discriminated unions.

| Method | Path | Body / params | Returns |
|---|---|---|---|
| `GET`    | `/api/sites`                              | - | `[{id, name, url}]` |
| `POST`   | `/api/sites`                              | `{name, url}` (id auto-generated) | `201 {id, name, url}` or `409` |
| `PATCH`  | `/api/sites/{id}`                         | `{name?, url?}` (id immutable) | `200 {id, name, url}` |
| `DELETE` | `/api/sites/{id}`                         | - | `204` |
| `GET`    | `/api/dates`                              | - | `{baseline:[...], current:[...], comparator:[...], report:[...]}` |
| `GET`    | `/api/runs/by-date/{date}`                | - | runs for a date with `run_id` and `kind` |
| `POST`   | `/api/runs`                               | `RunRequest` (discriminated by `kind`) | `202 {db_id, run_id, status}` or `409`/`412` |
| `GET`    | `/api/runs?kind=&status=&limit=&offset=`  | - | `{items, total}` |
| `GET`    | `/api/runs/{db_id}`                       | - | full run row including `args_json` |
| `GET`    | `/api/runs/{db_id}/logs?tail=N`           | optional tail; **N capped at 1MB** | `text/plain` |
| `POST`   | `/api/runs/{db_id}/retry`                 | - | `202`; reads `args_json`, spawns new run with same args |
| `POST`   | `/api/sync`                               | - | `{synced: <count>}` |
| `GET`    | `/api/health`                             | - | `{ok, db_ok, ai_analyzer_ok}` (degraded if AI analyzer down - uses 2s timeout, never hangs) |
| `GET`    | `/api/reports/{date}/{run_id}`            | - | summary metadata |
| `GET`    | `/api/reports/{date}/{run_id}/urls`       | - | `[{url_id, ai_status, severity?}]` |
| `GET`    | `/api/reports/{date}/{run_id}/url?id=...` | site `id` as query param | per-URL JSON |
| `GET`    | `/api/reports/{date}/{run_id}/screenshot?url_id=&which=baseline\|current\|diff` | query params | `image/png` |
| `GET`    | `/openapi.json`                           | - | OpenAPI schema |
| `GET`    | `/`, `/assets/*`, `/{spa-route}`          | - | static; catch-all returns `index.html` |

**Path traversal protection:** screenshot/log endpoints validate `date`, `run_id`, `url_id` parameters. Resolve final path, assert it's inside the configured root directory; reject with 400 if not. Tested in Phase C.3.

**Route ordering:** API routes registered *before* the SPA catch-all. The catch-all only matches what no other handler did.

## Phase C.2 - Frontend (Day 23–25)

- React SPA at `dashboard/web/`. Vite + TypeScript + React Router + Tailwind.
- State: TanStack Query for server state (caching, polling). Polling cadence: `/api/runs/{id}` every 2s while `status=running`.
- API client: `openapi-typescript` consumes `/openapi.json`. Budget half a day for hand-tuning generated types.
- Pages: Sites (CRUD), Runs (list + detail + logs), Reports (list by date+run, per-URL drill-in with screenshots side-by-side).

## Phase C.3 - Verification + automated dashboard test (Day 26–27)

- **Manual browser flow:** add URL → "Run Baseline" → wait → "Run Current" → "Compare" → "Generate Report" → view report → drill into per-URL analysis. No terminal needed.
- **Automated API smoke test** (added per critique - Playwright still deferred): pytest test using FastAPI `TestClient` exercises the full happy path: POST /api/sites, POST /api/runs (mocked subprocess), GET /api/runs/{id}, GET /api/reports/.../urls. Catches API-level breakage without browser.
- **Path traversal tests:** `..`-escape attempts on screenshot/log endpoints return 400.
- **Manual restart test:** start a long-running fake run, kill the dashboard, restart, verify `interrupted` status appears in UI.

### Docker + Makefile

- **`Dockerfile.dashboard`** (deliverable): multi-stage. Stage 1 builds React with Node 20. Stage 2 Python image copies bundle + runs uvicorn.
- `docker-compose.yml`: ai-analyzer gets schema + prompt volumes; new `dashboard` service exposes 8080, mounts `./data:/app/data`.
- `Makefile`:
  - `make dashboard-dev` - uvicorn `--reload` + Vite dev server in parallel via `concurrently` (installed as `dashboard/web/` devDependency: `npm i -D concurrently`). Cleaner than bash `& wait` + signal trap.
  - `make dashboard` - production build + uvicorn.

**Milestone C deliverable:** Manual + automated browser flow complete. `Dockerfile.dashboard` exists and builds. Dashboard restart kills process group safely (SIGTERM then SIGKILL after 5s) verifying PID identity first. `409`/`412` correct on conflicting and precondition-failing runs. Path traversal blocked. Bound to `127.0.0.1` by default.

---

## Risks & mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Run identity migration breaks existing data layout | Medium | B.1.6 migration script tested on a copy of real data first. Keeps old paths readable until migration runs. |
| Site ID migration breaks existing URL-dir paths | Medium | B.3.2 reuses canonical `url_to_dirname()`; B.1.6 walks both old name-based and new id-based dirs. |
| Phase A.1 changes AI output severity distribution | High | Expected. A.2 goldens pin the new behavior. |
| A.3 refactor regressions | Medium | A.2 characterization tests written *before* refactor. |
| Token budget overshoots provider limits | Low | A.1.4 hard caps (30MB body, 200 changes/category). |
| Provider rate limits | Medium | A.1.10 client-side semaphore + Retry-After. |
| Subprocess orphans on dashboard restart | Mitigated | Process groups + start-time verification. Documented limitation: partial run data left on disk. |
| OpenAPI → TypeScript codegen produces awkward types | Medium | Half a day to hand-tune; worst case write 5–10 types manually. |
| Schema drift Pydantic ↔ JSON Schema | Low | A.4 CI `schema-drift` job. |
| Pydantic ↔ ajv format/null mismatches | Medium | A.1.6 contract smoke test moved into Phase A.1, not waiting until A.4. |
| Comparator goldens flap on lib upgrade | Low | Pinned versions (A.0.7) + tolerance-based float comparison + size-bound binary check (A.2). |
| Phase C React work uncovers missing API endpoints | Medium | Build one screen end-to-end before next; surface gaps fast. |
| Effort slippage | Medium–High | Milestones A and B independently shippable. Ship A alone if C slips. |
| Existing JSON consumers break on new file split | Low | A.1.9 migration script for no-changes; new `ai_error.json` filename is additive (success path is unchanged). |
| Old `AI_ANALYZER_SERVICE_URL` env var breaks user envs | Low | A.0.4 startup warning with rename instructions. |
| Dashboard PID-recycling kills wrong process | Mitigated | C.1 verifies start time matches before SIGKILL; skips if mismatch. |
| Windows users blocked by `start_new_session` | Accepted | Documented as MVP unsupported on Windows; CLI (which doesn't need this) still works. |

---

## Deferred dependency upgrades

These were considered during the Phase A.0/A.1 dep audit and intentionally held back. Each one needs to be addressed in the named phase, not as background drift.

| Upgrade | Current | Target | Defer to | Why |
|---|---|---|---|---|
| `@google/generative-ai` → `@google/genai` | 0.1.1 (legacy SDK) | 1.51.0 (current SDK) | **A.1.4** | The legacy SDK is no longer Google's recommended client. Migration is a real API change (different import, different request/response shape). Doing it as part of the prompt-construction rewrite avoids touching server.js twice. |
| `express` 4 → 5 | 4.18.2 | 5.2.1 | **A.1.4** | Express 5 changes async error handling and middleware semantics. Bundling with the server.js rewrite means we don't touch routing twice. |
| `pytest` 8 → 9 | 8.4.1 | 9.0.3 | **A.4** | Pytest 9 has deprecation removals that may affect our test config. Easiest to bump alongside the CI workflow setup in A.4 so we can adjust both at once. |
| `lxml` 5 → 6 | 5.4.0 | 6.1.0 | **upstream-blocked** | Crawl4ai 0.8.6's transitive deps (patchright) require `lxml < 6`. Cannot bump until upstream relaxes. Tracked here so we re-check on the next crawl4ai release. |
| pyproject `[tool.poetry.*]` → PEP 621 `[project]` | tool.poetry style | project table | **A.4** | Poetry 2.x deprecates `[tool.poetry.name/version/description/authors]` in favor of the standard `[project]` table. Currently advisory (`poetry check` warns; install still works). Migrate alongside CI workflow setup. |

---

## Implementation flags (raised during build, deferred to named phases)

Issues discovered while implementing earlier phases that should be addressed in later phases. Each flag is tied to the specific phase that owns it so it doesn't get lost across sessions.

| Flag | Found in | Defer to | Description |
|---|---|---|---|
| ~~`_validate_ai_response` accepts `'ERROR'` as a valid `overall_severity`~~ | ~~A.1.5 review~~ | **RESOLVED in A.1.8** | The whole `_validate_ai_response` method was deleted in A.1.8 along with the synthetic-error pattern. Pydantic's `Literal["CRITICAL","WARNING","SAFE"]` enforces the constraint via the typed `AIAnalysisResponse` model. |
| ~~Python `create_ai_request` still produces OLD-shape payload~~ | ~~A.1.5~~ | **RESOLVED in A.1.8** | `create_ai_request` now builds via `AIAnalysisRequest.model_dump()` - request shape exactly matches the contract that ajv strict-mode validates. Verified end-to-end. |
| ~~Comparator emits `affected_components` via `list(set(...))` - non-deterministic order~~ | ~~A.2 review~~ | **RESOLVED in A.3** | `comparator/summary.py` now emits `sorted(set(...))`. The Phase A.2 test workaround (sorting before compare) is now redundant but kept as defense-in-depth. |
| ~~HTML renderer template references fields the calculator doesn't produce~~ | ~~A.2 review~~ | **RESOLVED in A.3** | Template moved to `report/templates/enhanced_report.html.j2` and rewritten to read `confidence_metrics.ai_confidence.{average,min,max}` (the actual shape). Also rewrote per-URL section to branch on the `result_type` discriminator (analysis_success / analysis_error / no_changes / ai_disabled) - including a dedicated "AI analysis failed" panel that the critique called out as missing. |
| Orchestrator class methods accidentally nested inside `_open_orchestrator` | A.2 review | **RESOLVED in A.2** | A.1.7 multi-line edit placed `_open_orchestrator` between `Orchestrator.__init__` and the rest of the methods, leaving 5 methods (`create_baseline`, `create_current`, `compare_with_baseline`, `generate_report`, `generate_enhanced_report`) at the wrong indentation level - they became nested inside the async generator and were not on the class at all. CLI commands using `orchestrator.<method>(...)` would have failed at runtime. Fixed in A.2 by moving `_open_orchestrator` after the class closes; methods now correctly bound. The earlier "end-to-end" verifications dodged this by calling `orchestrator.reporter.<method>` directly instead of `orchestrator.<method>`. |
| ~~Comparator `change_summary.change_categories.content` always all-`False`~~ | ~~A.1.1 review~~ | **RESOLVED in A.3** | Fixed the consumer (`comparator/summary.py`) to read the nested keys dom.py actually emits: `title.changed`, `content.significant_change`, `len(structure.element_changes) > 0`. Comparator golden regenerated; the synthetic fixture now correctly reports `title_changed: true` and `structure_changed: true`. |
| ~~Multi-signal change detection~~ | ~~early review~~ | **RESOLVED in A.3** | Investigated and simplified. The pre-A.3 discovery code OR-ed `changes_detected` against five per-category flags as defense-in-depth, but a trace of `comparator/engine.py:_compare_single_site` confirms `changes_detected = any(per-category flags)` in every successful return path - error paths return neither field. So the OR was always redundant. `report/discovery.py` now trusts the top-level flag; comment captures the historical context. |
| ~~Dead `report` Click command (broken - calls non-existent `reporter.generate`)~~ | ~~early review~~ | **RESOLVED in A.3** | `Orchestrator.generate_report`, the `report` Click command, and the `ReportGenerator.generate()` legacy method are all deleted. Makefile `report:` target was already pointing at `enhanced-report` so it kept working. |
| Prompt prioritization rules duplicated: code + `prompts/system.txt` | A.1.5 | **future** | The prioritization order ("structural HTML first, then content, then CSS/JS") is enforced in `server.js` (`prioritizeStructuredData`) and *also* described in `ai_analyzer/prompts/system.txt`. If we ever change one, the other must change too. Consider templating the prompt at startup with constants from server.js so they can't drift. |
| ~~`.github/workflows/regression-test.yml` was deleted (broken)~~ | ~~A.1 review~~ | **RESOLVED in A.4** | Fresh `.github/workflows/ci.yml` ships six parallel jobs: `lint` (ruff check + format), `audit` (audit_paths.py), `test-fast`, `test-slow`, `schema-drift` (re-export + git diff --exit-code), `node-validate` (npm test syntax check). Pinned to ubuntu-24.04 + Python 3.13 + Node 24 (matches ai_analyzer/Dockerfile). Concurrency group cancels superseded runs. |
| `url_id.url_to_dirname` has four latent bugs | A.4 url_id tests | **future** | Pinned in `tests/test_url_id.py` with explicit "LATENT BUG" docstrings: (1) `www.` is replaced *globally* not just at the prefix anchor - `www.foo.www.bar.com` → `foo.bar.com`; (2) port appears in dirname as `host:port_path` (colon is illegal on Windows / NTFS); (3) host case is preserved verbatim - `EXAMPLE.com` and `example.com` collide on case-insensitive filesystems and are split on case-sensitive ones; (4) percent-encoded paths are not decoded - `caf%C3%A9` and `café` produce different dirnames for the same URL. Real-world impact today is near-zero (Linux filesystems, no Windows users, no port collisions in sites.yml). A future canonicalizer pass should `.lower()` the netloc and `unquote()` the path. |
| `assess_element_impact` has unused `change_type` and dead `'medium'` branch | A.4 comparator units | **future** | `dom.assess_element_impact(tag, change_type, count_diff)` ignores `change_type` entirely; the HIGH_IMPACT branch returns `'high' if count_diff > 0 else 'medium'`, but every caller passes `abs(...)` of a non-zero diff so the `'medium'` arm is dead. Pinned in `test_assess_element_impact_high_impact_branch_is_unconditional`. Cleanup is either deletion (drop the param + dead arm) or implementing the asymmetric heuristic the function name implies (medium severity on remove, high on add). |
| `[tool.poetry.scripts] afr = ...:cli` works but PEP 621 migration deferred | A.0 dep audit | **A.4** *(already in deferred-deps table)* | Poetry 2.x deprecates `[tool.poetry.*]` fields in favor of `[project]` table. `poetry check` warns. Migrate alongside CI workflow. |

---

## Out of scope, deferred

- Streaming uploads instead of base64-in-JSON for screenshots.
- Color-aware visual diffing.
- Server-side rate limiting on the AI analyzer.
- Replacing Crawl4AI custom asset-downloading regex code in [crawler/engine.py:110-153](test_ui/crawler/engine.py#L110).
- Comprehensive crawler determinism (timezone pinning, animation disabling, A/B stabilization, font consistency, masking dynamic regions). Phase A.0.5 documents only.
- "Diff across arbitrary dates / runs" feature.
- Notification integrations (Slack/email).
- Real-time log streaming via SSE/WebSocket. Polling instead.
- Multi-user auth and RBAC.
- Subprocess re-attachment after dashboard restart (would need wrapper-script + exit-marker design).
- Run cancellation API (`POST /api/runs/{id}/cancel`).
- Playwright browser tests for the dashboard. C.3 has API-level smoke only.
- DOM/screenshot redaction. Opt-out via `AFR_AI_ENABLED=false` instead.
- SSRF protection on dashboard-triggered crawls (block link-local, RFC1918, metadata endpoints).
- `RunExecutor` abstract interface (no second implementation in sight; YAGNI).
- `run_events` audit table. Log file per run is sufficient.
- ADR formalism. Decision table is the artifact.
- Windows support for the dashboard subprocess job runner.

---

## Order of operations

```
MILESTONE A - Pipeline truthful and safe (10–12 days)
  Day 1-2    A.0  Foundation (README day-one, env verify, paths, scaffolding)
  Day 3-6    A.1  AI pipeline + schema-as-contract + retry-url + early contract smoke
  Day 7      A.2  Characterization tests
  Day 8-10   A.3  Refactor god-objects
  Day 11-12  A.4  Test coverage + CI

MILESTONE B - Durable artifact and run model (5–7 days)
  Day 13-14  B.1  Run identity (run_id, manifests, atomic publication, latest pointer)
  Day 15-16  B.2  Workflow preconditions + per-artifact locks
  Day 17     B.3  Stable site IDs + sites.yml migration

MILESTONE C - Dashboard (8–10 days)
  Day 18-22  C.1  Backend (FastAPI, SQLite WAL, process groups, path traversal)
  Day 23-25  C.2  React frontend
  Day 26-27  C.3  Manual + automated API verification
```

A and B independently shippable. C depends on A + B.

---

## Per-milestone deliverable checklist

**Milestone A:**
- README accurately describes current capabilities (no production-ready / Kubernetes claims).
- `make audit` returns zero hits across source dirs that exist.
- `AFR_AI_ENABLED=false` skips AI calls and writes `ai_disabled.json`.
- Real `ai_analysis.json` references actual CSS selectors / JS code in `detailed_analysis.technical_correlation`.
- Every JSON output carries `result_type`, `schema_version`, `model`, `request_id`. `prompt_sha256` and `schemas_sha256` on `/health`.
- `python -m test_ui retry-url --date X --run R --url Y` works end-to-end.
- Cross-language schema smoke test green in Phase A.1; full matrix in A.4.
- No file in `test_ui/` exceeds 400 LOC. Dead `report` command gone.
- `test-fast` <30s, CI green.

**Milestone B:**
- Two `baseline` runs the same day produce two distinct `<run_id>` directories. Neither overwrites the other.
- `compare` refuses to start if no complete baseline + current exists, with a clear error message.
- A run interrupted mid-execution leaves a `.lock` file with a dead PGID; the next run detects this and proceeds.
- `sites.yml` has stable `id` per site. Renaming a site doesn't change any directory path.
- Migration scripts (`migrate_run_layout.py`, `migrate_sites_ids.py`) ran successfully on existing data.

**Milestone C:**
- Dashboard binds `127.0.0.1` by default; LAN-binding logs a warning.
- Manual browser flow completes end-to-end.
- API-level smoke test exercises POST /api/sites, POST /api/runs, GET /api/runs/{id}, GET /api/reports endpoints.
- `409` on duplicate concurrent kind+date; `412` on workflow precondition failure.
- Dashboard restart kills the process group (SIGTERM → 5s → SIGKILL) only after verifying PID start-time matches recorded value.
- Path traversal attempts on screenshot / log endpoints return 400.
- `Dockerfile.dashboard` builds and the container runs end-to-end.
