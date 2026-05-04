"""Per-run manifest: the metadata file every run writes (Phase B.1.2).

Each run produces `data/<kind>/<date>/<run_id>/manifest.json` describing what
was executed, when, by whom, and how it relates to upstream runs. This is the
single source of truth for "is this run usable" - `compare` and `report` both
refuse to operate on runs whose manifest doesn't say `status="complete"`.

The manifest is written twice:
  1. At run start, with `status="running"` and `finished_at=None`.
  2. At run end, mutated to `status="complete"` (or `"failed"` /
     `"interrupted"`) and `finished_at` set.

Callers wrap their work in `try/except BaseException`, calling
`fail_manifest(...)` with the appropriate status before re-raising:
KeyboardInterrupt/SystemExit → `"interrupted"`, anything else → `"failed"`.
A run whose manifest is still `"running"` after the process exits indicates
a hard crash (SIGKILL, OOM kill, host reboot) where the exception handler
never ran - the lock-file recovery logic in B.2 reaps those by checking
process liveness.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..config import settings


# Bump when the manifest shape changes in a backwards-incompatible way.
# Format mirrors the AI contract version.
MANIFEST_SCHEMA_VERSION = "2026-04-30.1"

MANIFEST_FILENAME = "manifest.json"

Kind = Literal["baseline", "current", "comparator", "report"]
Status = Literal["running", "complete", "failed", "interrupted"]


class Manifest(BaseModel):
    """The on-disk manifest schema. Written as `manifest.json`.

    `source_run_ids` is populated only for derivative kinds:
      - comparator: {"baseline": "...", "current": "..."}
      - report:     {"comparator": "..."}
    For baseline/current it stays empty.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = MANIFEST_SCHEMA_VERSION
    run_id: str
    kind: Kind
    started_at: str  # DD-MM-YYYY HH:MM:SS, settings.get_current_datetime() format
    finished_at: str | None = None
    status: Status
    source_run_ids: dict[str, str] = Field(default_factory=dict)
    url_count: int = 0
    files_sha256: str | None = None  # set on completion; see compute_files_sha256


def write_manifest(run_dir: Path, manifest: Manifest) -> Path:
    """Write `manifest.json` to `run_dir`. Overwrites any existing file."""
    path = run_dir / MANIFEST_FILENAME
    path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return path


def read_manifest(run_dir: Path) -> Manifest:
    """Read and validate `manifest.json` from `run_dir`. Raises on missing/invalid."""
    path = run_dir / MANIFEST_FILENAME
    if not path.exists():
        raise FileNotFoundError(f"No manifest at {path}")
    return Manifest.model_validate_json(path.read_text(encoding="utf-8"))


def start_manifest(
    run_dir: Path, kind: Kind, run_id: str, source_run_ids: dict[str, str] | None = None
) -> Manifest:
    """Create + persist a fresh `status="running"` manifest. Returns the model."""
    manifest = Manifest(
        run_id=run_id,
        kind=kind,
        started_at=settings.get_current_datetime(),
        status="running",
        source_run_ids=source_run_ids or {},
    )
    write_manifest(run_dir, manifest)
    return manifest


def complete_manifest(run_dir: Path, *, url_count: int) -> Manifest:
    """Re-read, mutate to status='complete', recompute checksum, write back.

    Files-sha256 is computed *after* all the run's outputs are on disk so
    it's a stable fingerprint of the deliverables. Skips the manifest itself
    (would be circular).

    **Note for test authors**: callers (`run_context`, the engines) import
    this function with `from .manifest import complete_manifest`, which
    rebinds the name into their module's namespace at import time.
    Monkeypatching `manifest.complete_manifest` in a test does NOT affect
    those callers - patch the caller's module instead (e.g. for
    `run_context`, `monkeypatch.setattr("test_ui.common.run_context.complete_manifest", ...)`).
    """
    manifest = read_manifest(run_dir)
    manifest.status = "complete"
    manifest.finished_at = settings.get_current_datetime()
    manifest.url_count = url_count
    manifest.files_sha256 = compute_files_sha256(run_dir)
    write_manifest(run_dir, manifest)
    return manifest


def fail_manifest(
    run_dir: Path,
    *,
    status: Status = "failed",
    run_id: str | None = None,
    kind: Kind | None = None,
) -> Manifest | None:
    """Mark a run as failed/interrupted. Always tries to leave a manifest behind.

    Used in exception handlers. Doesn't compute the file checksum because
    a failed run's output is not stable / inspectable.

    Failure modes:
      - Manifest exists + parseable → mutate status, write back.
      - Manifest missing → write a fresh stub at the requested status if
        `run_id` and `kind` are provided; otherwise return None.
      - Manifest exists but unreadable / fails validation → write a fresh
        stub, preserving the file as `manifest.json.corrupt-<timestamp>`
        for post-mortem.

    Returns the persisted Manifest, or None if we couldn't write anything.
    Catches narrow I/O / parse exceptions only - unexpected errors propagate
    so the caller's exception handler still sees them.
    """
    manifest: Manifest | None = None
    try:
        manifest = read_manifest(run_dir)
    except FileNotFoundError:
        pass  # manifest never written; we'll synthesize below if we can
    except (json.JSONDecodeError, ValidationError, OSError) as e:
        # Preserve the corrupt file so we can debug post-mortem, then fall
        # through to writing a fresh stub.
        corrupt_path = run_dir / MANIFEST_FILENAME
        if corrupt_path.exists():
            backup = run_dir / f"{MANIFEST_FILENAME}.corrupt-{int(_unix_now())}"
            try:
                corrupt_path.rename(backup)
            except OSError:
                pass  # best-effort
        # Don't return - fall through to write a fresh stub below.
        # Note `e` is intentionally not re-raised: the caller is already
        # handling the original failure that triggered us.
        del e

    if manifest is None:
        if run_id is None or kind is None:
            return None
        manifest = Manifest(
            run_id=run_id,
            kind=kind,
            started_at=settings.get_current_datetime(),
            status=status,
            finished_at=settings.get_current_datetime(),
        )
    else:
        manifest.status = status
        manifest.finished_at = settings.get_current_datetime()
    write_manifest(run_dir, manifest)
    return manifest


def _unix_now() -> float:
    """Time helper isolated so tests can monkeypatch it."""
    import time

    return time.time()


def compute_files_sha256(run_dir: Path) -> str:
    """Hash the sorted list of (relative_path, size) tuples for every file in `run_dir`.

    Excludes:
      - `manifest.json` itself (chicken-and-egg)
      - `manifest.json.corrupt-<ts>` backups left by `fail_manifest` when the
        prior manifest was unreadable - these are debug debris, not run output
      - `.lock` files (cleaned up before publish, but defensive)
      - `.tmp-*` workspace dirs

    The hash detects post-publication tampering / partial writes without
    having to re-hash large image bytes - size + path is enough for a
    manifest-level integrity check. The Pydantic `files_sha256` field stores
    just the digest hex.
    """
    h = hashlib.sha256()
    files: list[tuple[str, int]] = []
    for p in sorted(run_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(run_dir).as_posix()
        if (
            rel == MANIFEST_FILENAME
            or rel.startswith(f"{MANIFEST_FILENAME}.corrupt-")
            or rel.endswith(".lock")
            or "/.tmp-" in f"/{rel}"
            or rel.startswith(".tmp-")
        ):
            continue
        files.append((rel, p.stat().st_size))
    for rel, size in files:
        h.update(f"{rel}\0{size}\n".encode())
    return h.hexdigest()


__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "MANIFEST_FILENAME",
    "Manifest",
    "Kind",
    "Status",
    "write_manifest",
    "read_manifest",
    "start_manifest",
    "complete_manifest",
    "fail_manifest",
    "compute_files_sha256",
]
