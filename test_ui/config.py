"""Configuration management for the regression tester."""
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, AliasChoices
from pathlib import Path
from datetime import datetime
import pytz

class Settings(BaseSettings):
    # Crawler settings
    crawler_timeout: int = 45  # Increased for 30-40 pages
    crawler_workers: int = 3   # Parallel crawling
    browser_headless: bool = True
    viewport_width: int = 1920
    viewport_height: int = 1080

    # AI Analyzer settings - can be overridden by AI_ANALYZER_SERVICE_URL env var
    ai_analyzer_service_url: str = Field(
        default="http://ai-analyzer:3000", 
        validation_alias=AliasChoices("AI_ANALYZER_SERVICE_URL", "AFR_AI_ANALYZER_SERVICE_URL")
    )
    ai_analyzer_timeout: int = 30

    # Comparison thresholds
    visual_similarity_threshold: float = 0.95  # SSIM score
    css_change_threshold: int = 5  # Number of CSS property changes
    js_size_change_threshold: float = 0.1  # 10% size change

    # Report settings
    report_include_screenshots: bool = True
    report_max_diff_size: int = 1000  # Max chars of diff to show
    
    # Date/Time settings - Ireland/Dublin timezone
    timezone: str = "Europe/Dublin"
    date_format: str = "%d-%m-%Y"  # Irish format: DD-MM-YYYY
    datetime_format: str = "%d-%m-%Y %H:%M:%S"  # Irish format with time

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="AFR_",
        extra="ignore"  # Allow extra fields from .env file
    )
        
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