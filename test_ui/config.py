"""Configuration management for the regression tester."""

import os
import warnings
from datetime import datetime
from pathlib import Path
from typing import Self

import pytz
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Breaking change as of Phase A.0.4: the unprefixed AI_ANALYZER_SERVICE_URL is no
# longer accepted. Warn loudly if a user still has it set so they know to rename.
if os.environ.get("AI_ANALYZER_SERVICE_URL") and not os.environ.get(
    "AFR_AI_ANALYZER_SERVICE_URL"
):
    warnings.warn(
        "AI_ANALYZER_SERVICE_URL is no longer supported. "
        "Rename to AFR_AI_ANALYZER_SERVICE_URL in your environment / .env file. "
        "The unprefixed value is being ignored.",
        DeprecationWarning,
        stacklevel=2,
    )


class Settings(BaseSettings):
    # Crawler settings
    crawler_timeout: int = 45  # Increased for 30-40 pages
    crawler_workers: int = 3  # Parallel crawling
    browser_headless: bool = True
    viewport_width: int = 1920
    viewport_height: int = 1080

    # AI Analyzer settings - overridable by AFR_AI_ANALYZER_SERVICE_URL.
    # The unprefixed AI_ANALYZER_SERVICE_URL was removed in Phase A.0.4
    # (see startup warning at top of this module).
    ai_analyzer_service_url: str = "http://ai-analyzer:3000"
    ai_analyzer_timeout: int = 30

    # Comparison thresholds
    visual_similarity_threshold: float = 0.95  # SSIM score
    # Min area (px²) of any single connected diff region to count as a
    # visual change. Catches small-but-distinct localized mutations
    # (e.g. an 80x80 painted rect = 6400 px² has SSIM mean ~0.99 - too
    # high to trip the SSIM threshold above - but produces one obvious
    # contour). Default 50 px² ≈ a 7x7 mark; tweak via
    # AFR_VISUAL_MIN_CONTOUR_AREA.
    visual_min_contour_area: int = 50
    css_change_threshold: int = 5  # Number of CSS property changes
    js_size_change_threshold: float = 0.1  # 10% size change

    # Report settings
    report_include_screenshots: bool = True
    report_max_diff_size: int = 1000  # Max chars of diff to show

    # Date/Time settings - Ireland/Dublin timezone
    timezone: str = "Europe/Dublin"
    date_format: str = "%d-%m-%Y"  # Irish format: DD-MM-YYYY
    datetime_format: str = "%d-%m-%Y %H:%M:%S"  # Irish format with time

    # Data layout. Override AFR_DATA_ROOT to relocate everything; the per-kind
    # paths derive from data_root unless individually overridden via their own
    # AFR_* env vars.
    data_root: Path = Path("data")
    baseline_dir: Path | None = None
    current_dir: Path | None = None
    comparator_dir: Path | None = None
    report_dir: Path | None = None
    runs_db_path: Path | None = None
    runs_log_dir: Path | None = None

    # AI gating. Set AFR_AI_ENABLED=false to skip AI calls entirely
    # (writes ai_disabled.json marker files; useful for sensitive sites).
    # Phase A.1.9 wires this into the report generator.
    ai_enabled: bool = True
    # Concurrency cap for AI requests (Phase A.1.10). Default 3.
    ai_concurrency: int = 3
    # Maximum dimension (width or height) for screenshots sent to the AI
    # analyzer. Full-resolution 1920x1080 PNGs can exceed 500 KB each in
    # base64, burning quota and latency. 800 px preserves enough detail for
    # visual change detection while cutting payload size by ~60-80%.
    ai_max_screenshot_dimension: int = 800

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="AFR_",
        # "ignore" so non-AFR_ env vars (e.g. OPENROUTER_API_KEY consumed
        # by the Node analyzer) don't make Settings construction fail.
        # Trade-off: typos in AFR_* vars are silently ignored too. If that
        # becomes painful, switch to "forbid" and add the non-AFR vars as
        # fields here.
        extra="ignore",
    )

    @model_validator(mode="after")
    def _fill_path_defaults(self) -> Self:
        """Derive per-kind paths from data_root when not explicitly set.

        Each path field defaults to None and is filled here so users can
        either override AFR_DATA_ROOT (and have everything follow) or
        override individual AFR_<KIND>_DIR vars (which take precedence).
        """
        if self.baseline_dir is None:
            self.baseline_dir = self.data_root / "baseline"
        if self.current_dir is None:
            self.current_dir = self.data_root / "current"
        if self.comparator_dir is None:
            self.comparator_dir = self.data_root / "comparator"
        if self.report_dir is None:
            self.report_dir = self.data_root / "report"
        if self.runs_db_path is None:
            self.runs_db_path = self.data_root / "dashboard.db"
        if self.runs_log_dir is None:
            self.runs_log_dir = self.data_root / "runs"
        return self

    def get_current_date(self) -> str:
        """Get current date in Ireland/Dublin timezone formatted as DD-MM-YYYY."""
        dublin_tz = pytz.timezone(self.timezone)
        dublin_time = datetime.now(dublin_tz)
        return dublin_time.strftime(self.date_format)

    def get_current_datetime(self) -> str:
        """Get current datetime in Ireland/Dublin timezone formatted as DD-MM-YYYY HH:MM:SS."""
        dublin_tz = pytz.timezone(self.timezone)
        dublin_time = datetime.now(dublin_tz)
        return dublin_time.strftime(self.datetime_format)


settings = Settings()
