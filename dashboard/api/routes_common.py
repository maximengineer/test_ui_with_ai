"""Shared route helpers for dashboard API domain routers."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends
from loguru import logger

from test_ui.config import settings

from .db import connection_scope
from .models import RunRow


def get_db() -> sqlite3.Connection:
    """FastAPI dependency: yield a connection per request."""
    if settings.runs_db_path is None:
        raise RuntimeError(
            "settings.runs_db_path is None - Settings._fill_path_defaults "
            "did not run, or was overridden after construction."
        )
    with connection_scope(settings.runs_db_path) as conn:
        yield conn


DbDep = Annotated[sqlite3.Connection, Depends(get_db)]


def row_to_runrow(row: sqlite3.Row) -> RunRow:
    """Project a `runs` SQLite row into the wire model."""
    try:
        args = json.loads(row["args_json"]) if row["args_json"] else {}
    except json.JSONDecodeError:
        logger.warning(f"runs.id={row['id']}: corrupt args_json, substituting {{}}")
        args = {}
    try:
        command = json.loads(row["command_json"]) if row["command_json"] else []
    except json.JSONDecodeError:
        logger.warning(f"runs.id={row['id']}: corrupt command_json, substituting []")
        command = []

    return RunRow(
        id=row["id"],
        run_id=row["run_id"],
        kind=row["kind"],
        status=row["status"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        date_dir=row["date_dir"],
        args=args,
        command=command,
        exit_code=row["exit_code"],
        error=row["error"],
        pid=row["pid"],
        pgid=row["pgid"],
        pid_start_time=row["pid_start_time"],
        source=row["source"],
    )


def is_valid_date_dir(name: str) -> bool:
    """True iff `name` is a parseable DD-MM-YYYY date."""
    try:
        datetime.strptime(name, settings.date_format)
    except ValueError:
        return False
    return True


def date_dir_has_published_run(date_dir: Path) -> bool:
    """True if `date_dir` contains at least one real published run subdir."""
    try:
        for entry in date_dir.iterdir():
            if entry.name.startswith("."):
                continue
            if entry.is_symlink():
                continue
            try:
                if entry.is_dir():
                    return True
            except OSError:
                continue
    except (FileNotFoundError, PermissionError, NotADirectoryError):
        return False
    return False


def list_date_dirs(root: Path | None) -> list[str]:
    """Return DD-MM-YYYY dir names under `root`, newest first."""
    if root is None or not root.exists():
        return []
    try:
        entries = list(root.iterdir())
    except (FileNotFoundError, PermissionError) as e:
        logger.warning(f"dates: cannot list {root}: {type(e).__name__}: {e}")
        return []
    dates = [
        p.name
        for p in entries
        if p.is_dir()
        and not p.name.startswith(".")
        and is_valid_date_dir(p.name)
        and date_dir_has_published_run(p)
    ]

    def _key(s: str) -> str:
        d, m, y = s.split("-")
        return f"{y}-{m}-{d}"

    return sorted(dates, key=_key, reverse=True)


__all__ = [
    "get_db",
    "DbDep",
    "row_to_runrow",
    "is_valid_date_dir",
    "date_dir_has_published_run",
    "list_date_dirs",
]
