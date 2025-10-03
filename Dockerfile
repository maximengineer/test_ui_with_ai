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
RUN pip install --no-cache-dir poetry==1.8.3

WORKDIR /app

# Copy dependency files first (these change less frequently)
COPY pyproject.toml poetry.lock* ./

# Install Python dependencies in a separate layer
RUN poetry config virtualenvs.create false \
  && poetry install --only main --no-interaction --no-ansi

# Install Playwright browsers in a separate layer (this is expensive)
RUN playwright install chromium

FROM dependencies AS runtime

# Create directories for data
RUN mkdir -p /data/snapshots /data/reports

# Copy application code last (this changes most frequently)
# For development, this will be overridden by volume mount
COPY test_ui ./test_ui

ENTRYPOINT ["python", "-m", "test_ui.cli"]