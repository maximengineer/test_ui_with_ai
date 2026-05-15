"""Health route domain module."""

from __future__ import annotations

import httpx
from fastapi import APIRouter
from loguru import logger

from test_ui.config import settings

from .db import connection_scope
from .models import HealthOut


health_router = APIRouter(prefix="/api", tags=["health"])


@health_router.get("/health", response_model=HealthOut)
def get_health() -> HealthOut:
    """Liveness check. Never raises - degraded states surface as `False`s."""
    db_ok = False
    if settings.runs_db_path is not None:
        try:
            with connection_scope(settings.runs_db_path) as probe:
                probe.execute("SELECT 1").fetchone()
                db_ok = True
        except Exception as e:
            logger.warning(f"health: DB probe failed: {type(e).__name__}: {e}")

    ai_ok = False
    try:
        with httpx.Client(timeout=2.0) as client:
            resp = client.get(f"{settings.ai_analyzer_service_url}/health")
            ai_ok = resp.status_code == 200
    except Exception:
        ai_ok = False

    return HealthOut(ok=db_ok, db_ok=db_ok, ai_analyzer_ok=ai_ok)


__all__ = ["health_router", "get_health"]
