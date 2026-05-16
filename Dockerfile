FROM python:3.13 AS dependencies

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
  build-essential \
  curl git ca-certificates \
  # Dependencies for Pillow and scikit-image
  libjpeg-dev libpng-dev libwebp-dev \
  # Minimal system libraries for opencv-python-headless
  libglib2.0-0 libgomp1 \
  # Dependencies for Playwright
  libnspr4 \
  libnss3 \
  libdbus-1-3 \
  libatk1.0-0 \
  libatk-bridge2.0-0 \
  libcups2 \
  libxkbcommon0 \
  libatspi2.0-0 \
  libxcomposite1 \
  libxdamage1 \
  libxfixes3 \
  libxrandr2 \
  libgbm1 \
  libasound2 \
  # Cleanup
  && rm -rf /var/lib/apt/lists/*

# Install Poetry
# Poetry 2.x required: pyproject.toml is PEP 621 (`[project]` table);
# Poetry 1.8 errors with "fields ['authors', 'description', 'name',
# 'version'] are required in package mode" because it only reads the
# legacy `[tool.poetry]` keys.
RUN pip install --no-cache-dir poetry==2.1.4

WORKDIR /app

# Copy dependency files first (these change less frequently)
COPY pyproject.toml poetry.lock* ./

# Install Python dependencies in a separate layer.
# `--no-root` skips installing the project itself; source is COPY'd into
# the runtime stage below and the entry point (`python -m test_ui.cli`)
# finds it via WORKDIR. Required because the project's `[project].name`
# (`ai-frontend-regression-tester`) doesn't match either Python package
# directory, so Poetry can't install the project without explicit
# `[tool.poetry].packages` config + the source already being present.
RUN poetry config virtualenvs.create false \
  && poetry install --only main --no-root --no-interaction --no-ansi

# Install Playwright browsers in a separate layer (this is expensive)
RUN playwright install chromium

FROM dependencies AS runtime

# Create canonical artifact roots. Keep these aligned with Settings defaults
# (test_ui/config.py): baseline/current/comparator/report + runs metadata.
RUN mkdir -p /data/baseline /data/current /data/comparator /data/report /data/runs

# Copy application code last (this changes most frequently)
# For development, this will be overridden by volume mount
COPY test_ui ./test_ui

ENTRYPOINT ["python", "-m", "test_ui.cli"]
