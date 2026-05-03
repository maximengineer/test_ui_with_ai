.PHONY: baseline current compare report report-date clean-baseline clean-current clean-reports clean-comparator clean-all test test-local-baseline test-local-current test-local-comparator test-local-report test-local-report-date test-local-report-with-ai test-ai-analyzer test-full test-local-full test-existing-data audit help

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
	@echo "📦 ENV VARS (see .env.example for full list):"
	@echo "  AFR_AI_ENABLED=false   - Skip AI calls; write ai_disabled.json markers"
	@echo "  AFR_AI_CONCURRENCY=N   - Cap concurrent AI requests (default 3)"
	@echo "  AFR_DATA_ROOT=/path    - Relocate all artifact directories"
	@echo ""
	@echo "ℹ️  See README.md for current capabilities/limitations and"
	@echo "    REFACTOR_AND_DASHBOARD_PLAN.md for the in-progress refactor."
