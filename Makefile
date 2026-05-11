.PHONY: baseline current compare report report-date clean-baseline clean-current clean-reports clean-comparator clean-all clean-orphans test test-local-baseline test-local-current test-local-comparator test-local-report test-local-report-date test-local-report-with-ai test-ai-analyzer test-full test-local-full test-existing-data audit dashboard-dev dashboard-dev-backend dashboard-docker dashboard-build dashboard-logs dashboard-down tamper-baseline help

# === Individual Step Commands ===

# # Step 1: Create baseline snapshots in data/baseline/dd-mm-yyyy/
baseline:
	$(MAKE) clean-baseline
	docker compose run --rm test-ui snapshot -o /data/baseline

# Step 2: Crawl the current state of websites into data/current/dd-mm-yyyy/
current:
	$(MAKE) clean-current
	docker compose run --rm test-ui current -o /data/current

# Step 3: Compare the baseline with the current state and save results
compare:
	docker compose run --rm test-ui compare -b /data/baseline -c /data/current

# Step 4: Generate enhanced AI-powered HTML report from comparator data
report:
	docker compose up -d ai-analyzer
	docker compose run --rm test-ui enhanced-report --comparator-data /data/comparator

# Step 4 Alternative: Generate report from specific date
report-date:
	@if [ -z "$(DATE)" ]; then echo "Usage: make report-date DATE=DD-MM-YYYY"; exit 1; fi
	docker compose up -d ai-analyzer
	docker compose run --rm test-ui enhanced-report --comparator-data /data/comparator --date $(DATE)

# === Utility Commands ===

# Clean baseline
clean-baseline:
	sudo rm -rf data/baseline

# Clean current data
clean-current:
	sudo rm -rf data/current

# Clean comparator data
clean-comparator:
	sudo rm -rf data/comparator

# Clean reports
clean-reports:
	sudo rm -rf data/report

clean-all:
	sudo rm -rf data

# === Testing ===

# Run the unit/golden test suite (fast tests only by default).
test:
	.venv/bin/pytest

# Audit source for hardcoded data paths (see scripts/audit_paths.py).
audit:
	.venv/bin/python scripts/audit_paths.py

# Test locally without Docker (for debugging) - create baseline
test-local-baseline:
	.venv/bin/python -m test_ui.cli --sites-file test_ui/sites.yml snapshot --output data/baseline

# Test locally without Docker (for debugging) - create current
test-local-current:
	.venv/bin/python -m test_ui.cli --sites-file test_ui/sites.yml current --output data/current

test-local-comparator:
	.venv/bin/python -m test_ui.cli --sites-file test_ui/sites.yml compare --baseline data/baseline --current data/current

# Test locally - generate enhanced report (requires AI analyzer running)
test-local-report:
	@echo "Starting AI analyzer service..."
	docker compose up -d ai-analyzer
	@echo "Waiting for AI analyzer to be ready..."
	@sleep 5
	@echo "Generating enhanced report..."
	AFR_AI_ANALYZER_SERVICE_URL=http://localhost:3000 .venv/bin/python -m test_ui.cli --sites-file test_ui/sites.yml enhanced-report --comparator-data data/comparator
	@echo "Enhanced report generated!"

# Test locally - generate report from specific date
test-local-report-date:
	@if [ -z "$(DATE)" ]; then echo "Usage: make test-local-report-date DATE=DD-MM-YYYY"; exit 1; fi
	@echo "Starting AI analyzer service..."
	docker compose up -d ai-analyzer
	@echo "Waiting for AI analyzer to be ready..."
	@sleep 5
	@echo "Generating enhanced report for $(DATE)..."
	AFR_AI_ANALYZER_SERVICE_URL=http://localhost:3000 .venv/bin/python -m test_ui.cli --sites-file test_ui/sites.yml enhanced-report --comparator-data data/comparator --date $(DATE)
	@echo "Enhanced report for $(DATE) generated!"

# Test AI Analyzer service
test-ai-analyzer:
	curl -X GET http://localhost:3000/health

# Full test cycle with enhanced reporting
test-full: clean-all baseline current compare report

# Full local test cycle  
test-local-full: clean-all test-local-baseline test-local-current test-local-comparator test-local-report

# Test locally with correct AI analyzer URL
test-local-report-with-ai:
	@echo "Testing with AI analyzer connection..."
	@if ! curl -s http://localhost:3000/health > /dev/null; then echo "AI analyzer not running on localhost:3000. Start it first."; exit 1; fi
	AFR_AI_ANALYZER_SERVICE_URL=http://localhost:3000 .venv/bin/python -m test_ui.cli --sites-file test_ui/sites.yml enhanced-report --comparator-data data/comparator
	@echo "Enhanced report with AI analysis completed!"

# Quick test with existing data
test-existing-data:
	@echo "Testing with existing comparator data..."
	@if [ ! -d "data/comparator" ]; then echo "No comparator data found. Run 'make compare' first."; exit 1; fi
	$(MAKE) test-local-report
	@echo "Test completed! Check data/report/ for results."

# === Dashboard (Phase C.1) ===

# Host-native dev: uvicorn (--reload) + Vite dev server in parallel
# via `concurrently` (devDep in dashboard/web). LINUX ONLY - see
# `_require_linux` in dashboard/api/main.py. Mac/Windows operators
# use `make dashboard-docker` instead.
#
# Backend: http://localhost:8080 (uvicorn auto-reloads on Python edits)
# Frontend: http://localhost:5173 (Vite proxies /api → :8080; HMR for TS)
#
# AFR_DASHBOARD_DEV_MODE=true (set in package.json's dev:full script)
# enables the CORS middleware so the Vite dev server can hit the
# backend cross-origin.
dashboard-dev:
	npm --prefix dashboard/web run dev:full

# Backend-only host-native uvicorn (no Vite). For when you only want
# to drive the API directly (e.g. with curl) and don't need the SPA.
dashboard-dev-backend:
	AFR_DASHBOARD_DEV_MODE=true .venv/bin/uvicorn dashboard.api:app --host 127.0.0.1 --port 8080 --reload

# Cross-platform: launch the dashboard in Docker. Brings up ai-analyzer
# alongside it (the dashboard's healthcheck depends on analyzer-healthy).
# Use `make dashboard-logs` to tail; `make dashboard-down` to stop.
dashboard-docker:
	docker compose up -d dashboard
	@echo ""
	@echo "Dashboard:    http://localhost:8080/api/health"
	@echo "AI analyzer:  http://localhost:3000/health"
	@echo "Logs:         make dashboard-logs"
	@echo "Stop:         make dashboard-down"

# Force-rebuild the dashboard image (e.g. after editing the Dockerfile or
# bumping pyproject.toml). Source-code edits don't need a rebuild because
# `dashboard/` and `test_ui/` are mounted read-write in compose.
dashboard-build:
	docker compose build dashboard

dashboard-logs:
	docker compose logs -f dashboard

dashboard-down:
	docker compose stop dashboard ai-analyzer

# Inject 19 deterministic synthetic mutations into the latest baseline
# run, so the comparator + report pipeline can be validated end-to-end
# against external sites you don't control (e.g. gov.ie URLs that don't
# change between runs). See `scripts/tamper_baseline.py` docstring for
# the full per-pattern audit table + workflow.
#
# Pipes the script through `docker exec -i python -` because the baseline
# dirs are owned by `root` (the dashboard's subprocess writes them as
# root inside the container). Requires `make dashboard-docker` to have
# the dashboard container running.
#
# After running, in the dashboard at /runs:
#   1. Check today's session
#   2. Click the (now-slate) Run current button to spawn current
#   3. Repeat for comparator, then report
# Then audit /reports against the manifest the script printed.
tamper-baseline:
	@cat scripts/tamper_baseline.py \
	  | docker exec -i test_ui_with_ai-dashboard-1 python -

# Reap orphan artifacts that survived deletion (e.g. `.run.json` files
# whose runs were sync'd from manifests then deleted, empty date dirs
# from before the prune-empty-date-dir cleanup landed). User-invisible
# (the dashboard's read paths already filter both classes), but they
# burn disk + slow `ls`. Pass AFR_CLEANUP_DRY_RUN=1 to scan first.
clean-orphans:
	@cat scripts/cleanup_orphans.py \
	  | docker exec -i test_ui_with_ai-dashboard-1 python -

# Help command showing all available Make targets
help:
	@echo "=== AI-Powered UI Regression Testing Makefile ==="
	@echo ""
	@echo "🔄 WORKFLOW COMMANDS:"
	@echo "  baseline          - Step 1: Create baseline snapshots"  
	@echo "  current           - Step 2: Create current snapshots"
	@echo "  compare           - Step 3: Compare baseline vs current"
	@echo "  report            - Step 4: Generate enhanced AI report (Docker)"
	@echo "  report-date       - Step 4: Generate report for specific date (Usage: make report-date DATE=DD-MM-YYYY)"
	@echo ""
	@echo "🧪 LOCAL TESTING COMMANDS:"
	@echo "  test-local-baseline         - Local baseline creation (no Docker)"
	@echo "  test-local-current          - Local current snapshots (no Docker)"
	@echo "  test-local-comparator       - Local comparison (no Docker)"
	@echo "  test-local-report           - Local enhanced report generation"
	@echo "  test-local-report-date      - Local report for date (Usage: make test-local-report-date DATE=DD-MM-YYYY)"
	@echo "  test-local-report-with-ai   - Test report with AI analyzer connection"
	@echo ""
	@echo "🔄 FULL TESTING CYCLES:"
	@echo "  test-full          - Complete cycle (baseline → current → compare → report)"
	@echo "  test-local-full    - Complete local cycle (no Docker for main steps)"  
	@echo "  test-existing-data - Quick test with existing comparator data"
	@echo ""
	@echo "🧹 CLEANUP COMMANDS:"
	@echo "  clean-baseline     - Remove baseline data"
	@echo "  clean-current      - Remove current data"
	@echo "  clean-comparator   - Remove comparator data"
	@echo "  clean-reports      - Remove reports data"
	@echo "  clean-all          - Remove all data"
	@echo ""
	@echo "🔧 UTILITY COMMANDS:"
	@echo "  test               - Run the unit/golden test suite (fast tests only)"
	@echo "  audit              - Check source for hardcoded data paths (Phase A.0.3)"
	@echo "  test-ai-analyzer   - Check AI analyzer service health"
	@echo "  help               - Show this help message"
	@echo ""
	@echo "📊 DASHBOARD COMMANDS (Phase C):"
	@echo "  dashboard-dev          - Host-native uvicorn + Vite (Linux only)"
	@echo "  dashboard-dev-backend  - Host-native uvicorn ONLY (no SPA)"
	@echo "  dashboard-docker       - Launch dashboard in Docker (cross-platform)"
	@echo "  dashboard-build        - Rebuild the dashboard image"
	@echo "  dashboard-logs         - Tail the dashboard container logs"
	@echo "  dashboard-down         - Stop dashboard + ai-analyzer containers"
	@echo "  tamper-baseline        - Inject synthetic changes for end-to-end pipeline validation"
	@echo "  clean-orphans          - Reap orphan .run.json files + empty date dirs"
	@echo ""
	@echo "📦 ENV VARS (see .env.example for full list):"
	@echo "  AFR_AI_ENABLED=false   - Skip AI calls; write ai_disabled.json markers"
	@echo "  AFR_AI_CONCURRENCY=N   - Cap concurrent AI requests (default 3)"
	@echo "  AFR_DATA_ROOT=/path    - Relocate all artifact directories"
	@echo ""
	@echo "ℹ️  See README.md for current capabilities/limitations and"
	@echo "    REFACTOR_AND_DASHBOARD_PLAN.md for the in-progress refactor."
