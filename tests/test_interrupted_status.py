"""Tests for the `interrupted` manifest status path (Phase B.1).

Background: the crawler / comparator / report orchestrators all wrap their
work in a `try/except (KeyboardInterrupt, SystemExit)` block that calls
`fail_manifest(..., status="interrupted")`. Genuine exceptions land in a
separate `except BaseException` block with `status="failed"`.

Without coverage, a future regression that swaps the except-order (which
matters: KeyboardInterrupt IS a BaseException) silently maps Ctrl-C to
"failed" - losing the operator-vs-system distinction that B.2 lock-recovery
will rely on.

These tests drive the crawler with a `save_assets` that raises the
appropriate exception type, and assert the manifest written into the
preserved tmp dir reflects the right status.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from test_ui.common.manifest import read_manifest
from test_ui.config import settings
from test_ui.crawler import engine as crawler_engine


@pytest.fixture
def crawler_output(tmp_path, monkeypatch):
    """Make settings.timezone usable without forcing real-network setup."""
    monkeypatch.setattr(settings, "timezone", "Europe/Dublin")
    return tmp_path


def _find_tmp_run_dir(date_dir: Path) -> Path:
    """The crawler leaves `.tmp-<run_id>/` in place when it raises."""
    candidates = [c for c in date_dir.iterdir() if c.name.startswith(".tmp-")]
    assert len(candidates) == 1, (
        f"expected 1 .tmp- dir, got {[c.name for c in candidates]}"
    )
    return candidates[0]


@pytest.mark.asyncio
async def test_crawler_keyboard_interrupt_writes_interrupted_status(
    crawler_output, monkeypatch
):
    """Ctrl-C during crawl → manifest.status == 'interrupted', tmp dir preserved.

    Patches `CrawlerEngine.save_assets` to raise KeyboardInterrupt on the
    first URL. Verifies the interrupted-vs-failed branch in
    crawler/engine.py:main correctly classifies it.
    """

    async def _interrupting_save_assets(self, url, name, output_dir):
        raise KeyboardInterrupt("simulated user interrupt")

    monkeypatch.setattr(
        crawler_engine.CrawlerEngine,
        "save_assets",
        _interrupting_save_assets,
    )

    sites = [{"url": "https://example.com"}]
    output_dir = crawler_output / "baseline"

    with pytest.raises(KeyboardInterrupt):
        await crawler_engine.main(sites, str(output_dir), is_baseline=True)

    # The crawler creates date_dir/<.tmp-run_id>/manifest.json with status=interrupted.
    date_dir = next(output_dir.iterdir())
    tmp_run_dir = _find_tmp_run_dir(date_dir)

    manifest = read_manifest(tmp_run_dir)
    assert manifest.status == "interrupted", (
        f"KeyboardInterrupt must map to 'interrupted', got '{manifest.status}'"
    )
    assert manifest.kind == "baseline"
    assert manifest.finished_at is not None


@pytest.mark.asyncio
async def test_crawler_runtime_exception_writes_failed_status(
    crawler_output, monkeypatch
):
    """A genuine error (RuntimeError outside the per-URL try/except) → status='failed'.

    Pin the symmetry: regular exceptions still produce 'failed', not
    'interrupted'. Catches the case where someone collapses the two except
    branches into one and breaks the distinction.

    Note: the per-URL save_assets exceptions are caught locally inside the
    main() loop and only logged. To force the outer except to fire, we
    patch `complete_manifest` so it raises. After the post-B.3 cleanup, the
    crawler's lifecycle is owned by `common.run_context`, which imported
    `complete_manifest` from `common.manifest` at module-load time -
    `RunContext.complete()` calls the imported reference, so we patch
    where `run_context` actually looks it up (NOT on the source manifest
    module).
    """
    from test_ui.common import run_context as run_context_module

    def _failing_complete(*args, **kwargs):
        raise RuntimeError("simulated complete-manifest failure")

    monkeypatch.setattr(run_context_module, "complete_manifest", _failing_complete)

    sites = [{"url": "https://example.com"}]
    output_dir = crawler_output / "current"

    async def _noop_save(self, url, name, output_dir):
        return None

    monkeypatch.setattr(crawler_engine.CrawlerEngine, "save_assets", _noop_save)

    with pytest.raises(RuntimeError, match="simulated complete-manifest failure"):
        await crawler_engine.main(sites, str(output_dir), is_baseline=False)

    date_dir = next(output_dir.iterdir())
    tmp_run_dir = _find_tmp_run_dir(date_dir)

    manifest = read_manifest(tmp_run_dir)
    assert manifest.status == "failed", (
        f"RuntimeError must map to 'failed', got '{manifest.status}'"
    )
    assert manifest.kind == "current"
