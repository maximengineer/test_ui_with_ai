"""End-to-end tests for the B.2 lock workflow.

Pins the visible behavior of the precondition wiring:
  - A second crawler invocation refuses while the first holds a live lock
    (simulated by leaving a self-PID lock in a sibling .tmp- dir).
  - The lock file is auto-removed on clean completion of a real crawl,
    so a second invocation works fine afterward.
  - The compare orchestrator raises PreconditionFailed (with a useful
    message) when no complete baseline run exists.

These complement test_locks.py + test_preconditions.py (which test the
units in isolation) by exercising the actual crawler.main + compare paths.
"""

from __future__ import annotations


import pytest

from test_ui.cli import Orchestrator
from test_ui.common.locks import LOCK_FILENAME, write_lock
from test_ui.common.preconditions import PreconditionFailed
from test_ui.config import settings
from test_ui.crawler import engine as crawler_engine


@pytest.fixture
def crawler_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "timezone", "Europe/Dublin")
    return tmp_path


# ---------------------------------------------------------------------------
# Crawler refuses if a live lock for the same kind+date exists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crawler_refuses_when_live_lock_exists(crawler_setup, monkeypatch):
    """Pre-seed a sibling .tmp- dir with a self-PID lock; crawler.main
    must refuse with PreconditionFailed before doing any work."""
    output_dir = crawler_setup / "baseline"

    # Pre-seed: a hostile/concurrent run holding a live lock.
    date_str = settings.get_current_date()
    date_dir = output_dir / date_str
    busy_tmp = date_dir / ".tmp-01HXX0000000000000000000A0"
    busy_tmp.mkdir(parents=True)
    write_lock(busy_tmp, command="competing-crawler")

    # Patch save_assets so we can detect if the crawler erroneously got past
    # the precondition check and started doing work.
    started = {"flag": False}

    async def _should_not_run(self, url, name, output_dir):
        started["flag"] = True

    monkeypatch.setattr(crawler_engine.CrawlerEngine, "save_assets", _should_not_run)

    sites = [{"url": "https://example.com"}]
    with pytest.raises(PreconditionFailed, match="Another baseline run"):
        await crawler_engine.main(sites, str(output_dir), is_baseline=True)

    assert started["flag"] is False, "crawler must not run while lock is held"


@pytest.mark.asyncio
async def test_crawler_runs_after_stale_lock_cleared(crawler_setup, monkeypatch):
    """Post-success: lock is removed, so a second invocation isn't blocked.

    Drives one full crawl (with patched save_assets so we don't hit the
    network), then verifies that the published run has no .lock file
    inside it (the lock was released before atomic_run_dir's rename).
    """
    output_dir = crawler_setup / "baseline"

    async def _noop_save(self, url, name, output_dir):
        return None

    monkeypatch.setattr(crawler_engine.CrawlerEngine, "save_assets", _noop_save)

    sites = [{"url": "https://example.com"}]
    await crawler_engine.main(sites, str(output_dir), is_baseline=True)

    # Find the published run dir.
    date_str = settings.get_current_date()
    date_dir = output_dir / date_str
    published = [
        c for c in date_dir.iterdir() if c.is_dir() and not c.name.startswith(".tmp-")
    ]
    assert len(published) >= 1
    run_dir = published[0]
    assert not (run_dir / LOCK_FILENAME).exists(), (
        f"published run dir must not contain a .lock (found at {run_dir})"
    )

    # And a second crawl must succeed (no leftover lock blocking us).
    await crawler_engine.main(sites, str(output_dir), is_baseline=True)


# ---------------------------------------------------------------------------
# Comparator orchestrator refuses if no complete baseline/current
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compare_with_baseline_refuses_when_no_baseline(tmp_path, monkeypatch):
    """No baseline directory at all → clear PreconditionFailed message.

    Exercises Orchestrator.compare_with_baseline (not the Click command),
    so we can assert the exception type directly without going through
    Click's Abort wrapping.
    """
    import httpx

    monkeypatch.setattr(settings, "report_dir", tmp_path / "report")
    monkeypatch.setattr(settings, "comparator_dir", tmp_path / "comparator")

    async with httpx.AsyncClient() as client:
        orch = Orchestrator(client=client, ai_analyzer_url="http://test.local")
        with pytest.raises(PreconditionFailed, match="No complete baseline run"):
            await orch.compare_with_baseline(
                baseline_dir=tmp_path / "missing-baseline",
                current_dir=tmp_path / "missing-current",
                output_dir=tmp_path / "out",
                sites=[{"url": "https://example.com"}],
            )


@pytest.mark.asyncio
async def test_compare_with_baseline_refuses_when_baseline_exists_but_no_current(
    tmp_path, monkeypatch
):
    """Baseline complete + current missing → error mentions current, not baseline."""
    import httpx

    from test_ui.common.manifest import Manifest, write_manifest

    # Seed a complete baseline run.
    baseline_root = tmp_path / "baseline"
    baseline_date = baseline_root / "01-01-2099"
    baseline_run = baseline_date / "01HXX0000000000000000000A0"
    baseline_run.mkdir(parents=True)
    write_manifest(
        baseline_run,
        Manifest(
            run_id="01HXX0000000000000000000A0",
            kind="baseline",
            started_at="01-01-2099 00:00:00",
            status="complete",
            finished_at="01-01-2099 00:00:01",
        ),
    )

    monkeypatch.setattr(settings, "report_dir", tmp_path / "report")
    monkeypatch.setattr(settings, "comparator_dir", tmp_path / "comparator")

    async with httpx.AsyncClient() as client:
        orch = Orchestrator(client=client, ai_analyzer_url="http://test.local")
        with pytest.raises(PreconditionFailed, match="No complete current run"):
            await orch.compare_with_baseline(
                baseline_dir=baseline_root,
                current_dir=tmp_path / "missing-current",
                output_dir=tmp_path / "out",
                sites=[{"url": "https://example.com"}],
            )


# ---------------------------------------------------------------------------
# enhanced-report orchestrator refuses if no complete comparator run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_enhanced_report_refuses_when_comparator_failed(
    tmp_path, monkeypatch
):
    """A failed comparator run (status='failed', no others complete) must
    block enhanced-report with a precise hint about the failure."""
    import httpx

    from test_ui.common.manifest import Manifest, write_manifest

    comparator_root = tmp_path / "comparator"
    date = "01-01-2099"
    failed_run = comparator_root / date / "01HXX0000000000000000000A0"
    failed_run.mkdir(parents=True)
    write_manifest(
        failed_run,
        Manifest(
            run_id="01HXX0000000000000000000A0",
            kind="comparator",
            started_at="01-01-2099 00:00:00",
            status="failed",
            finished_at="01-01-2099 00:00:01",
        ),
    )

    monkeypatch.setattr(settings, "report_dir", tmp_path / "report")
    monkeypatch.setattr(settings, "comparator_dir", comparator_root)

    async with httpx.AsyncClient() as client:
        orch = Orchestrator(client=client, ai_analyzer_url="http://test.local")
        with pytest.raises(PreconditionFailed, match="status='failed'"):
            await orch.generate_enhanced_report(
                comparator_root=comparator_root,
                report_date=date,
            )


def test_enhanced_report_cli_handles_precondition_failed_cleanly(tmp_path, monkeypatch):
    """The `enhanced-report` Click command must NOT prefix PreconditionFailed
    messages with "Enhanced report generation failed:" - that's misleading
    (the failure is BEFORE generation, not during it).

    Pin the post-B.2-review fix: PreconditionFailed gets the same `❌ <msg>`
    format as the other Click commands.
    """
    from click.testing import CliRunner

    from test_ui.cli import cli
    from test_ui.common.manifest import Manifest, write_manifest

    monkeypatch.setattr(settings, "report_dir", tmp_path / "report")

    # Seed a comparator date with a single FAILED run - require_complete_run
    # will then raise with the failed-status hint. (An empty date dir would
    # just produce "no complete comparator run" and not exercise this branch.)
    comparator_dir = tmp_path / "comparator"
    failed_run = comparator_dir / "01-01-2099" / "01HXX0000000000000000000A0"
    failed_run.mkdir(parents=True)
    write_manifest(
        failed_run,
        Manifest(
            run_id="01HXX0000000000000000000A0",
            kind="comparator",
            started_at="01-01-2099 00:00:00",
            status="failed",
            finished_at="01-01-2099 00:00:01",
        ),
    )

    sites_file = tmp_path / "sites.yml"
    sites_file.write_text("sites:\n  - url: https://example.com\n    name: x\n")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--sites-file",
            str(sites_file),
            "enhanced-report",
            "--comparator-data",
            str(comparator_dir),
            "--date",
            "01-01-2099",
        ],
    )

    assert result.exit_code != 0, (
        f"expected non-zero exit on precondition failure; got output={result.output!r}"
    )
    # The PreconditionFailed message should appear unprefixed by the
    # generic "Enhanced report generation failed:" string.
    assert "No complete comparator run" in result.output
    assert "Enhanced report generation failed:" not in result.output, (
        "PreconditionFailed should not be wrapped in the generic-failure prefix"
    )


@pytest.mark.asyncio
async def test_published_runs_have_no_lock_file(tmp_path, monkeypatch):
    """The published `<run_id>/` dir must NEVER contain a `.lock` file -
    the lock is released *before* atomic publication renames .tmp- to final.

    Pinning this catches a future regression where someone moves the
    `acquire_lock` outside the `atomic_run_dir` context (which would
    leave .lock files in published runs forever).
    """
    monkeypatch.setattr(settings, "timezone", "Europe/Dublin")
    output_dir = tmp_path / "current"

    async def _noop_save(self, url, name, output_dir):
        return None

    monkeypatch.setattr(crawler_engine.CrawlerEngine, "save_assets", _noop_save)
    await crawler_engine.main(
        [{"url": "https://x"}], str(output_dir), is_baseline=False
    )

    # All published runs (any name not starting with .tmp-) must be lock-free.
    for date_dir in (output_dir).iterdir():
        for run_dir in date_dir.iterdir():
            if run_dir.is_dir() and not run_dir.name.startswith(".tmp-"):
                assert not (run_dir / LOCK_FILENAME).exists(), (
                    f"published run {run_dir} has a stale .lock file"
                )
