# AI-assisted UI regression analysis tool

A tool for capturing baseline / current snapshots of web pages, comparing visual and structural differences, and (optionally) generating AI-assisted reports about what changed and why.

> **Status:** undergoing a multi-milestone refactor. See [`REFACTOR_AND_DASHBOARD_PLAN.md`](REFACTOR_AND_DASHBOARD_PLAN.md) for the work in progress and the per-milestone deliverables.

## Current capabilities and limitations

**What works today:**

- CLI capture of baselines and current snapshots via [Crawl4AI](https://github.com/unclecode/crawl4ai), with deterministic asset naming.
- Visual diffing (SSIM-based) and structural diffing (HTML / CSS / JS) between baseline and current.
- Generation of an HTML report from the comparison data, with an optional AI analysis step (Google Gemini).
- Docker Compose layout with a Python service and a Node AI-analyzer service.

**What is being fixed in the current refactor:**

- The AI-analysis step today does **not** pass meaningful structured diff data to Gemini. The model effectively gets screenshots plus a few counts. Phase A.1 of the plan rewires this so the model actually sees CSS / JS / HTML changes.
- Two large modules (`test_ui/report/generator.py`, `test_ui/comparator/engine.py`) are being broken into single-responsibility submodules.
- Test coverage and CI are being added; the project currently has effectively none.
- A web dashboard is planned (Milestone C) but does not exist yet.

**Do not use this as an unattended deployment gate** until Milestone A is complete. Until then, treat the AI output as a polished narrative over noisy diffs (see [`docs/determinism.md`](docs/determinism.md) for the sources of noise that aren't yet controlled).

## Privacy and data sent to the AI provider

When AI analysis is enabled, screenshots and structured page data (DOM, CSS, JS diffs) are sent to the configured AI provider. **Do not run this against pages containing secrets, regulated data, customer PII, or confidential internal content** unless you have approved data-processing controls in place.

To disable AI calls entirely (the report generator will still run; URLs get an `ai_disabled.json` marker file instead of an `ai_analysis.json`):

```bash
export AFR_AI_ENABLED=false
```

## Quick start

There are two supported paths: Docker (recommended) and local development.

### Docker

Requires Docker and Docker Compose. Get a Gemini API key from [Google AI Studio](https://aistudio.google.com/app/apikey).

```bash
cp .env.example .env
# Edit .env and set GEMINI_API_KEY=<your-key>

# Optional: edit test_ui/sites.yml to point at your URLs.

docker compose up -d ai-analyzer    # only the ai-analyzer service needs to be running ahead of time

# Step through the workflow (each `make` target builds the test-ui image on demand):
make baseline                       # capture initial snapshots
make current                        # capture current snapshots
make compare                        # generate diffs
make report                         # AI-assisted HTML report
```

Or in one go: `make test-full`.

Reports land in `data/report/<DD-MM-YYYY>/`. Look for `enhanced_analysis_report.html`.

### Local development (no Docker for the Python side)

```bash
python -m venv .venv
source .venv/bin/activate
pip install poetry && poetry install

# AI analyzer still runs in Docker - it's a small Node service:
docker compose up -d ai-analyzer

cp .env.example .env
# Edit .env and set GEMINI_API_KEY=<your-key>

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
| `GEMINI_API_KEY` | - | Google Gemini API key. Required if AI is enabled. |
| `GEMINI_MODEL` | `gemini-2.5-pro` | Model to use. |
| `AFR_AI_ANALYZER_SERVICE_URL` | `http://ai-analyzer:3000` | URL of the Node AI service. Set to `http://localhost:3000` for local dev. |
| `AFR_AI_ENABLED` | `true` | Set `false` to skip AI calls entirely. |
| `AFR_AI_CONCURRENCY` | `3` | Max concurrent AI requests per orchestration. |
| `AFR_DATA_ROOT` | `data` | Root directory for all artifacts. Per-kind paths derive from this unless overridden. |
| `AFR_BASELINE_DIR`, `AFR_CURRENT_DIR`, `AFR_COMPARATOR_DIR`, `AFR_REPORT_DIR` | derived from `AFR_DATA_ROOT` | Override individual artifact roots if needed. |
| `AFR_VIEWPORT_WIDTH`, `AFR_VIEWPORT_HEIGHT` | `1920`, `1080` | Browser viewport. |
| `AFR_BROWSER_HEADLESS` | `true` | Headless browser mode. |
| `AFR_TIMEZONE` | `Europe/Dublin` | Timezone for date directory naming. |

> **Breaking change (Phase A.0.4):** the previously-supported unprefixed `AI_ANALYZER_SERVICE_URL` is no longer accepted. Use `AFR_AI_ANALYZER_SERVICE_URL`. The application will print a deprecation warning if the old name is set.

## Site configuration

Edit [`test_ui/sites.yml`](test_ui/sites.yml):

```yaml
sites:
  - name: "Homepage"
    url: "https://your-site.com"
  - name: "About"
    url: "https://your-site.com/about"
```

> Milestone B will add stable site IDs (`id: <slug>`) so renames don't break historical mapping. Today, site `name` is used as the key.

## Data layout

```
data/
├── baseline/<DD-MM-YYYY>/<url_dir>/
│   ├── screenshot.png
│   ├── dom.html
│   ├── assets/
│   └── metadata.json
├── current/<DD-MM-YYYY>/<url_dir>/
├── comparator/<DD-MM-YYYY>/<url_dir>/
│   ├── comparison_results.json
│   └── diffs/
│       ├── change_summary.json
│       ├── html_changes.json
│       ├── css_changes.json
│       ├── js_changes.json
│       └── visual_diff.png
└── report/<DD-MM-YYYY>/<url_dir>/
    ├── ai_analysis.json
    ├── structured_data.json
    └── screenshots/
```

> Milestone B introduces immutable per-execution `run_id` directories under each date dir to prevent same-day runs from overwriting each other.

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
make clean-all      # Remove all data
make help           # Show all targets
```

## Project layout

```
test_ui/
├── cli.py              # CLI entrypoint (Click)
├── config.py           # Settings (Pydantic)
├── crawler/engine.py   # Crawl4AI wrapper + asset downloading
├── comparator/engine.py # Visual + structural diffing
├── report/generator.py # AI-analysis orchestration + HTML rendering
├── contracts/          # Pydantic models for the AI request/response (Phase A.1)
├── common/             # Shared utilities (Phase A.3)
└── utils/              # Image compression, etc.

ai_analyzer/
└── server.js           # Node + Express + Google Generative AI

schemas/                # Generated JSON Schema (Phase A.1)
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

The work-in-progress refactor plan is in [`REFACTOR_AND_DASHBOARD_PLAN.md`](REFACTOR_AND_DASHBOARD_PLAN.md). Open issues and pull requests should reference the relevant milestone (A / B / C) and phase.

## License

MIT - see [LICENSE](LICENSE) (if present).
