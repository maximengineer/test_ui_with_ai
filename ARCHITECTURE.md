# Architecture

System-level tour of the AI-assisted UI regression tool. Read this first
to orient. For *how to run it* see [README.md](README.md). For *known
follow-ups and intentional out-of-scope items* see [BACKLOG.md](BACKLOG.md).
The decision log from when the current shape was being built is archived
at [docs/history/REFACTOR_AND_DASHBOARD_PLAN.md](docs/history/REFACTOR_AND_DASHBOARD_PLAN.md).

---

## What it is

Captures snapshots of web pages at two points in time, diffs the
visual + structural differences, and sends the structured diff plus
screenshots to an AI for a per-page severity verdict. Output is an HTML
report (CLI flow) or a browser dashboard (long-running flow). Designed
for sites the operator does *not* control (regulator pages, public
gov/info sites) where the operator wants to know what changed without
having to eyeball every page.

The pipeline is **four sequential stages** plus an AI step:

```
sites.yml → baseline → current → comparator → report (+ AI) → HTML/dashboard
            capture    capture    diff         render
```

Stages are *kinds*: `baseline`, `current`, `comparator`, `report`. Each
kind writes to its own dir under `data/<kind>/<DD-MM-YYYY>/<run_id>/`.
Stages are independent processes; the dashboard sequences them, the CLI
expects you to invoke them in order.

---

## Top-level component map

| Path | What it is | Deeper docs |
|---|---|---|
| [test_ui/](test_ui/) | Python CLI + library: capture, compare, render. Entry point [test_ui/__main__.py](test_ui/__main__.py). | this file (sections below) |
| [ai_analyzer/](ai_analyzer/) | Node + Express service. OpenAI-compatible HTTP wrapper around any chat-completions provider (default: OpenRouter + qwen/qwen3.6-plus). | header docstring of [ai_analyzer/server.js](ai_analyzer/server.js) |
| [dashboard/](dashboard/) | FastAPI backend + React SPA for triggering runs and viewing reports in a browser. Linux-only (reads `/proc`). | [dashboard/README.md](dashboard/README.md) |
| [schemas/](schemas/) | **Generated** JSON Schema files. Cross-language contract (Pydantic → ajv). Don't edit by hand. | [schemas/README.md](schemas/README.md) |
| [scripts/](scripts/) | One-off operator utilities: schema export, data-layout migrations, baseline tampering for framework validation, orphan cleanup. | one docstring per script |
| [tests/](tests/) | Pytest suite. `not slow` runs in <30s; `slow` adds OpenCV + golden tests (~17s more). Goldens in `tests/fixtures/golden/`. | [pyproject.toml](pyproject.toml) `[tool.pytest.ini_options]` |
| [docs/](docs/) | Internal docs: input data shapes, crawler determinism baseline, historical decision logs. | [docs/data_shapes.md](docs/data_shapes.md), [docs/determinism.md](docs/determinism.md), [docs/history/](docs/history/) |
| [data/](data/) | Per-run artifact tree. Layout: `data/<kind>/<DD-MM-YYYY>/<run_id>/<site_id>/`. Plus `data/runs/<id>.run.json` operator records and `data/dashboard.db` SQLite. | this file ("Data layout") |

---

## test_ui — Python pipeline

Single-responsibility submodules. No file is meant to exceed ~400 LOC.

| Module | Purpose | Key entry |
|---|---|---|
| [test_ui/cli/](test_ui/cli/) | Click commands + lifecycle CMs. Each command opens an httpx client, instantiates `Orchestrator`, dispatches. Per-URL retry lives in `cli/retry.py`. | `cli.commands.cli` |
| [test_ui/cli/orchestrator.py](test_ui/cli/orchestrator.py) | Owns the `httpx.AsyncClient` lifecycle and dispatches to comparator + reporter. The "what runs in what order" boundary. | `Orchestrator` |
| [test_ui/crawler/engine.py](test_ui/crawler/engine.py) | Crawl4AI wrapper. Captures screenshot + DOM + assets per site, writes to `data/baseline/...` or `data/current/...` | `crawl_sites()` |
| [test_ui/comparator/](test_ui/comparator/) | Diff baseline vs current per URL. Split by responsibility: `screenshots.py` (SSIM + visual diff), `dom.py` (BeautifulSoup HTML diff), `assets.py` (CSS/JS/media), `summary.py` (aggregate change_summary), `finder.py` (date+run-dir scanning). `engine.py` is the thin orchestrator (~150 LOC). | `comparator.engine.compare_sites()` |
| [test_ui/report/](test_ui/report/) | Discover comparator output → call AI per URL → aggregate → render Jinja HTML. `discovery.py`, `loader.py`, `ai_client.py`, `aggregator.py`, `confidence.py`, `html_renderer.py`. Templates in `report/templates/`. | `report.generator.ReportGenerator` |
| [test_ui/contracts/ai_contract.py](test_ui/contracts/ai_contract.py) | **Pydantic v2 source of truth** for the AI request/response wire shape. Discriminated union on `result_type`: `analysis_success`, `analysis_error`, `no_changes`, `ai_disabled`. JSON Schema in `schemas/` is generated from this. | `AIAnalysisRequest`, `AIAnalysisResponse`, `AIAnalysisError` |
| [test_ui/common/](test_ui/common/) | Shared infrastructure: `url_id.py` (canonical URL→dirname), `run_id.py` (ULID generation), `manifest.py` + `publish.py` (atomic per-run publication), `locks.py` + `preconditions.py` (workflow integrity), `sites.py` (sites.yml loader), `images.py` (WebP base64 helpers), `run_record.py` (per-run JSON sidecar). | one file per concern |
| [test_ui/config.py](test_ui/config.py) | Pydantic `Settings`. All env vars `AFR_`-prefixed. Single source for paths, AI flags, viewport, timezone. | `settings` singleton |

---

## Data flow end-to-end

```
        sites.yml                                   .env
            │                                         │
            ▼                                         ▼
  ┌──────────────────┐                      ┌──────────────────┐
  │ test_ui crawler  │                      │ ai_analyzer      │
  │ (Crawl4AI)       │                      │ (Node + OpenAI   │
  │                  │                      │  SDK → OpenRouter│
  │ writes:          │                      │  → Qwen 3.6 Plus)│
  │  baseline/...    │                      └──────────────────┘
  │  current/...     │                                ▲
  └──────────────────┘                                │
            │                                         │ HTTP
            ▼                                         │ POST /api/compare
  ┌──────────────────┐                                │ {AIAnalysisRequest}
  │ test_ui          │                                │
  │ comparator       │                                │
  │                  │                                │
  │ writes:          │                                │
  │  comparator/...  │                                │
  │   diffs/*.json   │                                │
  └──────────────────┘                                │
            │                                         │
            ▼                                         │
  ┌──────────────────┐    one HTTP call per URL       │
  │ test_ui report   │ ──────────────────────────────►│
  │                  │                                │
  │ writes:          │ ◄──────────────────────────────┘
  │  report/...      │    {AIAnalysisResponse | AIAnalysisError
  │   ai_*.json      │     | NoChangesMarker | AIDisabledMarker}
  │   *.html         │
  └──────────────────┘
```

The dashboard wraps this same flow: it spawns the same CLI commands as
subprocesses, tracks them in SQLite, and serves the rendered reports.
See [dashboard/README.md](dashboard/README.md) for the dashboard-specific
layout.

---

## Cross-language contract

| Component | Where | Notes |
|---|---|---|
| **Source of truth** | [test_ui/contracts/ai_contract.py](test_ui/contracts/ai_contract.py) (Pydantic v2) | `ConfigDict(extra='forbid')` on every model. Discriminated union on `result_type`. |
| **Generated JSON Schema** | [schemas/*.schema.json](schemas/) | Produced by `python scripts/export_schemas.py`. CI fails on drift. |
| **Python validator** | Pydantic `model_validate()` in [test_ui/report/ai_client.py](test_ui/report/ai_client.py) | Validates inbound responses; emits `AIAnalysisError` on schema violation. |
| **Node validator** | `ajv` (strict mode) in [ai_analyzer/server.js](ai_analyzer/server.js) | Validates inbound requests; rejects with typed error body. |
| **Contract smoke test** | [tests/test_contracts.py](tests/test_contracts.py) + fixtures in `tests/fixtures/contracts/` | One pytest test iterates fixtures, validates with Pydantic AND subprocess-calls Node validator, asserts both agree. |

If you're touching a wire shape, you change `ai_contract.py`, then
`scripts/export_schemas.py`, then commit both. Forgetting the regen
fails CI.

---

## Data layout

```
data/
├── baseline/<DD-MM-YYYY>/<run_id>/<site_id>/
│   ├── screenshot.png
│   ├── index.html              # captured DOM
│   ├── css/*.css               # downloaded stylesheets
│   ├── js/*.js                 # downloaded scripts
│   └── metadata.json
├── current/<DD-MM-YYYY>/<run_id>/<site_id>/...   # same shape as baseline
├── comparator/<DD-MM-YYYY>/<run_id>/<site_id>/
│   ├── comparison_results.json
│   └── diffs/
│       ├── change_summary.json   # AI-facing master summary
│       ├── html_changes.json
│       ├── css_changes.json
│       ├── js_changes.json
│       └── visual_diff.png
├── report/<DD-MM-YYYY>/<run_id>/<site_id>/
│   ├── ai_analysis.json | ai_error.json | no_changes.json | ai_disabled.json
│   ├── structured_data.json
│   └── screenshots/
├── runs/<db_id>.run.json + <db_id>.log    # per-run sidecar + subprocess log
└── dashboard.db                            # SQLite (WAL)
```

Each per-run dir carries a `manifest.json` with status, source_run_ids,
and a content hash. `latest` symlinks point at the most recent
*complete* run per kind+date. Runs publish atomically via a `.tmp-<run_id>`
rename. See [test_ui/common/manifest.py](test_ui/common/manifest.py)
+ [test_ui/common/publish.py](test_ui/common/publish.py).

Run-status vocab is intentionally split by layer:
- manifest (`test_ui/common/manifest.py`): `running|complete|failed|interrupted`
- dashboard DB/API (`dashboard/api/lifecycle.py`): `pending|running|done|failed|interrupted`

The only renamed success value is `complete <-> done`; mapping + transition
rules live centrally in [dashboard/api/lifecycle.py](dashboard/api/lifecycle.py).

The full per-file shape is in [docs/data_shapes.md](docs/data_shapes.md).

---

## Where to look for X

| Question | Look here |
|---|---|
| How is a site captured? | [test_ui/crawler/engine.py](test_ui/crawler/engine.py) |
| How is the DOM diffed? | [test_ui/comparator/dom.py](test_ui/comparator/dom.py) — `compare_dom()` and `compare_key_attributes()` |
| How is the screenshot diffed? | [test_ui/comparator/screenshots.py](test_ui/comparator/screenshots.py) |
| What gets sent to the AI? | [test_ui/report/ai_client.py](test_ui/report/ai_client.py) — `AIClient.create_request()` |
| What does the AI prompt say? | [ai_analyzer/prompts/system.txt](ai_analyzer/prompts/system.txt) |
| How are URLs canonicalized to dirnames? | [test_ui/common/url_id.py](test_ui/common/url_id.py) |
| Where is the env-var contract? | [test_ui/config.py](test_ui/config.py) (`AFR_*`) + [.env.example](.env.example) |
| How does the dashboard spawn subprocesses? | [dashboard/api/runner.py](dashboard/api/runner.py) |
| Where are the test goldens? | [tests/fixtures/golden/](tests/fixtures/golden/) |
| What's the URL noise normalizer? | [test_ui/comparator/assets.py](test_ui/comparator/assets.py) — `normalize_volatile_urls()` + `_VOLATILE_URL_PATTERNS` |
| How do I validate my framework changes end-to-end? | `make tamper-baseline` + [scripts/tamper_baseline.py](scripts/tamper_baseline.py) |
| What does the AI response look like on success vs failure? | [test_ui/contracts/ai_contract.py](test_ui/contracts/ai_contract.py) and example fixtures in [tests/fixtures/contracts/](tests/fixtures/contracts/) |
| How are crawler determinism gaps tracked? | [docs/determinism.md](docs/determinism.md) |

---

## What's not here

Things you might expect to find but don't:

- **Authentication / multi-tenancy / RBAC.** Single-user, dashboard binds `127.0.0.1` by default.
- **Streaming uploads.** Screenshots cross the Python ↔ Node boundary as base64-in-JSON. Bounded at 50 MB body / 10 MB per image.
- **Real-time log streaming.** Dashboard polls `/api/runs/{id}` every 2s. No WebSocket / SSE.
- **Run cancellation API.** Dashboard restart kills running subprocesses (SIGTERM → 5s → SIGKILL); no in-flight cancel button.
- **Windows support for the dashboard.** The job runner reads `/proc/<pid>/stat` for PID-recycling defense. CLI works on macOS; dashboard is Linux-only (or Linux-in-Docker).

The full deferred list is in [BACKLOG.md "Intentionally out of scope"](BACKLOG.md#intentionally-out-of-scope).

---

## Current Improvement Priorities

The architecture is functional, but the next improvements should stay focused
on correctness, safety, and maintainability rather than another broad rewrite.
The active source of truth for deferred work is [BACKLOG.md](BACKLOG.md).

| Priority | Area | Why it matters |
|---|---|---|
| 1 | Security signal extraction and deterministic report floors | AI explanations can under-rate security-sensitive diffs. Keep expanding structured HTML/CSS/JS detectors and report-side floors with focused tests. |
| 2 | Dashboard-triggered crawl safety | Static URL validation blocks private/link-local/loopback targets by default. Crawler-time preflight also checks DNS-resolved private targets and redirect chains before browser/resource fetches. Docker runs can additionally enable container-level egress allowlisting with `AFR_EGRESS_ALLOWLIST_ENABLED=true` and `docker-compose.egress.yml`. |
| 3 | Sensitive artifact controls | Structured text diffs are redacted for obvious secrets before AI calls and report persistence; `AFR_AI_REDACT_SCREENSHOTS=true` omits screenshot base64 from AI requests. Raw crawl/comparator artifacts remain unredacted; use `AFR_AI_ENABLED=false` unless approved controls exist. |
| 4 | Remaining hotspot decomposition | Avoid new god objects. Current hotspots are `dashboard/api/db.py`, `dashboard/api/runner.py`, `test_ui/crawler/engine.py`, and large React page modules. |
| 5 | Frontend/backend contract drift | Keep OpenAPI snapshot checks and generated TypeScript types current; avoid hand-written literal unions where generated types can carry the contract. |
| 6 | Crawler determinism | Improve noise controls only when a real false-positive pattern appears. The current baseline is documented in [docs/determinism.md](docs/determinism.md). |

Do not create a second architecture plan document for this list. Add concrete
future work to [BACKLOG.md](BACKLOG.md), and update this file only when the
implemented architecture changes.
