# AI-assisted UI regression analysis tool

A tool for capturing baseline / current snapshots of web pages, comparing visual and structural differences, and (optionally) generating AI-assisted reports about what changed and why.

> **New here?** [`ARCHITECTURE.md`](ARCHITECTURE.md) is the system-level tour — what each component does, how data flows through the pipeline, where to look for X. Read that before diving into code.
>
> Forward-looking work (deferred deps, latent bugs, intentional out-of-scope) lives in [`BACKLOG.md`](BACKLOG.md). The original multi-milestone refactor plan that built the current state is archived in [`docs/history/`](docs/history/) as a decision log.

## Current capabilities and limitations

**What works today:**

- CLI capture of baselines and current snapshots via [Crawl4AI](https://github.com/unclecode/crawl4ai), with deterministic asset naming.
- Visual diffing (SSIM-based) and structural diffing (HTML / CSS / JS) between baseline and current.
- Enhanced HTML report generation from comparator output, with optional AI analysis (OpenAI-compatible provider; default is OpenRouter + qwen/qwen3.6-plus).
- AI request/response contract validation across Python (Pydantic) and Node (ajv) via generated schemas in [`schemas/`](schemas/).
- Dashboard for triggering runs and browsing reports (`dashboard/`), backed by FastAPI + React.
- CI coverage for linting, tests, schema drift, and dashboard OpenAPI drift.

**Current limitations:**

- AI output should not be treated as an unattended deployment gate yet; use it as assisted analysis, not automated approval.
- Crawler determinism is intentionally partial (animations/dynamic content/timezone variance can still introduce noise). See [`docs/determinism.md`](docs/determinism.md).
- Remaining architecture/security follow-ups are tracked in [`BACKLOG.md`](BACKLOG.md). The live architecture source is [`ARCHITECTURE.md`](ARCHITECTURE.md); historical plans live under [`docs/history/`](docs/history/).

## Privacy and data sent to the AI provider

When AI analysis is enabled, screenshots and structured page data (DOM, CSS, JS diffs) are sent to the configured AI provider. **Do not run this against pages containing secrets, regulated data, customer PII, or confidential internal content** unless you have approved data-processing controls in place.

By default, obvious secrets in structured text diffs are masked before the AI request (`AFR_AI_REDACT_STRUCTURED_DATA=true`) and before `data/report/.../structured_data.json` is written (`AFR_REPORT_REDACT_STRUCTURED_DATA=true`). Screenshots are sent to AI unless `AFR_AI_REDACT_SCREENSHOTS=true`, which keeps local report screenshots but omits screenshot base64 from the AI request.

Raw crawler/comparator artifacts can still contain page DOM, CSS, JS, and screenshots. For sensitive pages, disable AI calls entirely unless approved controls are in place.

To disable AI calls entirely (the report generator will still run; URLs get an `ai_disabled.json` marker file instead of an `ai_analysis.json`):

```bash
export AFR_AI_ENABLED=false
```

## Quick start

There are two supported paths: Docker (recommended) and local development.

### Docker

Requires Docker and Docker Compose. Get an OpenRouter API key from [openrouter.ai/keys](https://openrouter.ai/keys).

```bash
cp .env.example .env
# Edit .env and set OPENROUTER_API_KEY=<your-key>

# Optional: edit test_ui/sites.yml to point at your URLs.

docker compose up -d ai-analyzer    # only the ai-analyzer service needs to be running ahead of time

# Step through the workflow (each `make` target builds the test-ui image on demand):
make baseline                       # capture initial snapshots
make current                        # capture current snapshots
make compare                        # generate diffs
make report                         # AI-assisted HTML report
```

Or in one go: `make test-full`.

Reports are published under `data/report/<DD-MM-YYYY>/<run_id>/` with one
`enhanced_analysis_report.html` per report run.

### Local development (no Docker for the Python side)

```bash
python -m venv .venv
source .venv/bin/activate
pip install poetry && poetry install

# AI analyzer still runs in Docker - it's a small Node service:
docker compose up -d ai-analyzer

cp .env.example .env
# Edit .env and set OPENROUTER_API_KEY=<your-key>

make test-local-full                # baseline → current → compare → report
```

### Running tests

```bash
make test          # fast unit tests (default skips slow tests)
make audit         # check for hardcoded data/ paths in source
```

## Configuration

All runtime settings are read from environment variables prefixed `AFR_` (or from `.env`). See [`test_ui/config.py`](test_ui/config.py) for the full list. Common settings:

| Variable | Default | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | - | OpenRouter API key. Required if AI is enabled. |
| `AFR_AI_MODEL` | `qwen/qwen3.6-plus` | OpenRouter model slug (provider/model). |
| `AFR_AI_BASE_URL` | `https://openrouter.ai/api/v1` | OpenAI-compatible base URL. Override to point at a different provider. |
| `AFR_AI_ANALYZER_SERVICE_URL` | `http://ai-analyzer:3000` | URL of the Node AI service. Set to `http://localhost:3000` for local dev. |
| `AFR_AI_ENABLED` | `true` | Set `false` to skip AI calls entirely. |
| `AFR_AI_REDACT_STRUCTURED_DATA` | `true` | Mask obvious secrets in structured HTML/CSS/JS diff strings before AI analysis. |
| `AFR_AI_REDACT_SCREENSHOTS` | `false` | Keep local report screenshots but omit screenshot base64 from AI requests. |
| `AFR_REPORT_REDACT_STRUCTURED_DATA` | `true` | Mask obvious secrets in `data/report/.../structured_data.json`. Comparator/crawler artifacts remain raw. |
| `AFR_AI_CONCURRENCY` | `3` | Max concurrent AI requests per orchestration. |
| `AFR_DATA_ROOT` | `data` | Root directory for all artifacts. Per-kind paths derive from this unless overridden. |
| `AFR_BASELINE_DIR`, `AFR_CURRENT_DIR`, `AFR_COMPARATOR_DIR`, `AFR_REPORT_DIR` | derived from `AFR_DATA_ROOT` | Override individual artifact roots if needed. |
| `AFR_VIEWPORT_WIDTH`, `AFR_VIEWPORT_HEIGHT` | `1920`, `1080` | Browser viewport. |
| `AFR_BROWSER_HEADLESS` | `true` | Headless browser mode. |
| `AFR_TIMEZONE` | `Europe/Dublin` | Timezone for date directory naming. |
| `AFR_ALLOW_PRIVATE_SITE_URLS` | `false` | Allow private/link-local/loopback crawl targets in `sites.yml`. Keep false unless intentionally testing internal sites. |
| `AFR_SITE_URL_CHECK_DNS` | `true` | Before crawling, block public hostnames that resolve to private/reserved IPs. |
| `AFR_SITE_URL_CHECK_REDIRECTS` | `true` | Before crawling/downloading assets, validate redirect targets. |
| `AFR_SITE_URL_REDIRECT_LIMIT` | `5` | Maximum redirect hops followed by crawler preflight checks. |
| `AFR_EGRESS_ALLOWLIST_ENABLED` | `false` | Docker-only: enable container firewall egress allowlisting for `test-ui` and `dashboard`. Requires `docker-compose.egress.yml`. |
| `AFR_EGRESS_ALLOWLIST` | empty | Comma/space-separated extra hostnames, URLs, IPs, or CIDRs allowed for outbound traffic. Site hosts are included separately by default. |
| `AFR_EGRESS_ALLOWLIST_INCLUDE_SITES` | `true` | Include exact hosts from `sites.yml` in the Docker egress allowlist. |
| `AFR_EGRESS_SITES_FILE` | auto | Optional path to the `sites.yml` file used when deriving egress allowlist hosts. |

> **Breaking change:** the previously-supported unprefixed `AI_ANALYZER_SERVICE_URL` is no longer accepted. Use `AFR_AI_ANALYZER_SERVICE_URL`. The application will print a deprecation warning if the old name is set.

### Docker egress allowlist

Set `AFR_EGRESS_ALLOWLIST_ENABLED=true` to make the `test-ui` and `dashboard`
containers program an `iptables` OUTPUT allowlist at startup. The allowlist
uses resolved IPs for:

- exact hosts from `sites.yml` (`AFR_EGRESS_ALLOWLIST_INCLUDE_SITES=true`)
- `AFR_AI_ANALYZER_SERVICE_URL` so reports can reach the internal analyzer
- extra entries in `AFR_EGRESS_ALLOWLIST` for CDNs/API hosts or CIDRs
- Docker resolver nameservers and loopback

This is intentionally stricter than crawler URL validation. External assets on
CDNs will be blocked unless their host/IP/CIDR is added to
`AFR_EGRESS_ALLOWLIST`.

The sandbox needs `NET_ADMIN` only when enabled, so the base Compose file does
not grant it. Run with the explicit override:

```bash
export COMPOSE_FILE=docker-compose.yml:docker-compose.egress.yml
export AFR_EGRESS_ALLOWLIST_ENABLED=true
export AFR_EGRESS_ALLOWLIST="cdn.example.com,api.example.com,203.0.113.0/24"

docker compose build test-ui
make dashboard-build
```

With `COMPOSE_FILE` exported, existing Make targets use the override too.

## Troubleshooting AI analysis

- `AFR_AI_ENABLED=false`: report generation still succeeds, but per-URL output uses `ai_disabled.json` (not `ai_analysis.json`).
- Analyzer unavailable (`AFR_AI_ENABLED=true` + analyzer down/unreachable): report generation still completes with per-URL `ai_error.json` files.
- Dashboard health check: `GET /api/health` returns `ai_analyzer_ok=false` when the analyzer is unreachable.

## Site configuration

Edit [`test_ui/sites.yml`](test_ui/sites.yml):

```yaml
sites:
  - id: "1"
    name: "Homepage"
    url: "https://your-site.com"
  - id: "2"
    name: "About"
    url: "https://your-site.com/about"
```

`id` is the stable on-disk key (`data/.../<site_id>/...`). Keep `id` stable
across renames and URL edits to preserve history continuity.

## Data layout

```
data/
├── baseline/<DD-MM-YYYY>/<run_id>/<site_id>/
│   ├── screenshot.png
│   ├── index.html
│   ├── css/
│   ├── js/
│   └── metadata.json
├── current/<DD-MM-YYYY>/<run_id>/<site_id>/
├── comparator/<DD-MM-YYYY>/<run_id>/<site_id>/
│   ├── comparison_results.json
│   └── diffs/
│       ├── change_summary.json
│       ├── html_changes.json
│       ├── css_changes.json
│       ├── js_changes.json
│       └── visual_diff.png
└── report/<DD-MM-YYYY>/<run_id>/<site_id>/
    ├── ai_analysis.json | ai_error.json | no_changes.json | ai_disabled.json
    ├── structured_data.json
    └── screenshots/

data/runs/
├── <db_id>.run.json
└── <db_id>.log

data/dashboard.db
```

## Make targets

```
make baseline       # Step 1: snapshot (Docker)
make current        # Step 2: snapshot (Docker)
make compare        # Step 3: diff baseline vs current (Docker)
make report         # Step 4: AI-assisted HTML report (Docker)
make test-full      # All four in sequence (Docker)
make test-local-*   # Local equivalents (no Docker for the Python side)

make test           # Run fast unit tests
make audit          # Check source for hardcoded data/ paths
make clean-all CONFIRM_CLEAN_ALL=1  # Remove all data (destructive)
make prune-runs     # Manual retention prune for old terminal runs
make help           # Show all targets
```

## Project layout

```
test_ui/
├── __main__.py         # module entrypoint (`python -m test_ui`)
├── cli/                # Click commands + orchestrator
├── config.py           # Settings (Pydantic)
├── crawler/engine.py   # Crawl4AI wrapper + asset downloading
├── comparator/         # Visual + structural diffing
├── report/             # AI-analysis orchestration + HTML rendering
├── contracts/          # Pydantic models for AI request/response
├── common/             # Shared utilities
└── utils/              # Image compression, etc.

ai_analyzer/
└── server.js           # Node + Express + OpenAI-compatible API wrapper

dashboard/              # FastAPI backend + React frontend
schemas/                # Generated JSON Schemas + dashboard OpenAPI snapshot
docs/                   # Internal documentation
scripts/                # One-off utilities (audit, migrations, schema export)
tests/                  # Pytest suite + fixtures
```

## Contributing / development

```bash
poetry install
make test                           # run the fast suite
make audit                          # check for hardcoded paths
.venv/bin/ruff check .              # lint
```

### Documentation drift checklist

When changing architecture-affecting behavior, update docs and generated
contracts in the same PR:

1. Update `README.md` / `ARCHITECTURE.md` / `docs/data_shapes.md` for data-layout, run-state, or workflow changes.
2. If `test_ui/contracts/ai_contract.py` changed, run `python scripts/export_schemas.py` and commit `schemas/*.schema.json`.
3. If dashboard API models/routes changed, refresh `schemas/dashboard-openapi.json` and regenerate frontend types:
   - `python -c "import json; from dashboard.api.main import create_app; print(json.dumps(create_app(dev_mode=False).openapi(), indent=2))" > schemas/dashboard-openapi.json`
   - `cd dashboard/web && npm run gen:api`

Open issues and pull requests should reference [`BACKLOG.md`](BACKLOG.md) when the work is a known follow-up; for context on past architectural decisions see the archived [`docs/history/REFACTOR_AND_DASHBOARD_PLAN.md`](docs/history/REFACTOR_AND_DASHBOARD_PLAN.md).

## License

MIT - see [LICENSE](LICENSE) (if present).
