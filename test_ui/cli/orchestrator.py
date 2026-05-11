"""Orchestrator class - coordinates crawler, comparator, and report stages.

Split out of `cli.py` in the post-B.3 cleanup so the Click commands live
elsewhere. Each method is a thin shell around an engine + the run_context
lifecycle CM (introduced in the same cleanup pass).
"""

from __future__ import annotations

from pathlib import Path

import httpx
from rich.console import Console

from ..comparator.engine import ComparatorEngine
from ..config import settings
from ..crawler.engine import main as crawler_main
from ..report.generator import ReportGenerator


# Local Console for orchestrator status messages. The Click commands have
# their own Console in commands.py - sharing a single instance via import
# would create a back-edge in the dependency graph for no real win
# (Console() is cheap; both write to the same stdout).
console = Console()


class Orchestrator:
    """Orchestrates the testing workflow.

    Construction requires an httpx.AsyncClient. Phase A.1.7 made client
    ownership explicit at the CLI boundary - the Click command opens the
    client via `async with`, passes it here, and the pool is cleanly closed
    when the orchestration ends. This eliminates the leak that existed when
    ReportGenerator was constructing its own client and never closing it.
    """

    def __init__(self, *, client: httpx.AsyncClient, ai_analyzer_url: str | None = None):
        self.ai_analyzer_url = ai_analyzer_url or settings.ai_analyzer_service_url
        self.client = client
        self.comparator = ComparatorEngine()
        self.reporter = ReportGenerator(settings, self.ai_analyzer_url, client=client)

    async def create_baseline(
        self,
        sites: list,
        output_dir: str,
        is_baseline: bool = True,
        *,
        run_id: str | None = None,
    ) -> bool:
        """Create baseline snapshots with date-based directory structure.

        `run_id` is forwarded to `crawler.engine.main` so the dashboard can
        pre-allocate the ULID it spawned this subprocess for. None means the
        engine generates one itself (CLI default).
        """
        return await crawler_main(
            sites, output_dir, is_baseline=is_baseline, run_id=run_id
        )

    async def create_current(
        self, sites: list, output_dir: str, *, run_id: str | None = None
    ) -> bool:
        """Create current snapshots with date-based directory structure."""
        return await crawler_main(sites, output_dir, is_baseline=False, run_id=run_id)

    async def compare_with_baseline(
        self,
        baseline_dir: Path,
        current_dir: Path,
        output_dir: Path,
        sites: list,
        *,
        run_id: str | None = None,
    ):
        """Runs comparison and saves the results to a file.

        B.2 precondition: refuses if either baseline or current has no
        complete run available. Pre-B.2 silently fell back to the kind root
        itself (which contains date dirs, not url dirs), causing every site
        to report missing_baseline/missing_current - a confusing failure
        mode replaced here with a single clear error.
        """
        from ..common.preconditions import PreconditionFailed

        # output_dir is deprecated - comparator creates its own per-URL structure.

        actual_baseline_dir = self.comparator.find_latest_baseline(baseline_dir)
        if actual_baseline_dir is None:
            raise PreconditionFailed(
                f"No complete baseline run found under {baseline_dir}. "
                f"Run `afr snapshot --output {baseline_dir}` first."
            )
        console.print(f"[cyan]Using latest baseline: {actual_baseline_dir.name}[/cyan]")
        baseline_path = actual_baseline_dir

        actual_current_dir = self.comparator.find_latest_current(current_dir)
        if actual_current_dir is None:
            raise PreconditionFailed(
                f"No complete current run found under {current_dir}. "
                f"Run `afr current --output {current_dir}` first."
            )
        console.print(f"[cyan]Using latest current: {actual_current_dir.name}[/cyan]")
        current_path = actual_current_dir

        console.print(
            f"[cyan]Comparing baseline '{baseline_path}' with current '{current_path}'...[/cyan]"
        )
        comparison_results = self.comparator.compare_all(
            baseline_dir=baseline_path,
            current_dir=current_path,
            sites=sites,
            run_id=run_id,
        )

        # The comparator now returns a list of results, one per URL
        # Each URL has its own comparison_results.json file already saved
        console.print(
            f"[green]Comparison complete for {len(comparison_results)} URLs[/green]"
        )

        for result in comparison_results:
            url = result["metadata"]["url"]
            output_file = result["metadata"]["output_path"] + "/comparison_results.json"
            console.print(f"[cyan]  {url} -> {output_file}[/cyan]")

        return comparison_results

    async def generate_enhanced_report(
        self,
        comparator_root: Path,
        report_date: str,
        *,
        run_id: str | None = None,
    ):
        """Generate enhanced AI-powered HTML report; publish under report_dir/<date>/<run_id>/.

        Phase B.1: opens an atomic publication context for the report's own
        run_id, writes per-URL data + the final HTML inside it, and renames
        to the final path on success. Manifest records the source comparator
        run for provenance.

        Pre-B.1 took an `output_dir` parameter that was created but never
        actually written to (the report has always written under
        `settings.report_dir`). Dropped here to remove confusion - callers
        that need a separate output location should override `settings.report_dir`.
        """
        from ..common.preconditions import require_complete_run
        from ..common.run_context import run_context
        from ..common.run_id import is_valid_run_id, new_run_id
        from ..common.run_record import write_run_record
        from ..comparator.finder import update_latest_symlink

        # Workflow precondition: require a complete comparator run for this
        # date. Returns the run dir on success (which we use for source
        # provenance), or raises PreconditionFailed with a user-facing
        # error message naming the actual status of the latest run.
        comparator_run_dir = require_complete_run(
            Path(comparator_root), report_date, kind_label="comparator"
        )
        # is_valid_run_id guard handles the legacy fallback case (date dir
        # name like '01-01-2099' is not a ULID, so source stays empty
        # rather than recording a junk value).
        comparator_run_id = (
            comparator_run_dir.name
            if is_valid_run_id(comparator_run_dir.name)
            else None
        )

        console.print(
            f"[cyan]🔍 Discovering comparison data for {report_date}...[/cyan]"
        )

        discovery_result = self.reporter.discover_comparison_data(
            comparator_root, report_date
        )
        urls_with_changes = discovery_result["with_changes"]
        urls_without_changes = discovery_result["without_changes"]

        console.print(
            f"[blue]📊 Found {len(urls_with_changes)} URLs with changes, "
            f"{len(urls_without_changes)} without changes[/blue]"
        )

        if not urls_with_changes and not urls_without_changes:
            console.print(
                f"[yellow]⚠️  No comparison data found for {report_date}[/yellow]"
            )
            return

        report_date_dir = settings.report_dir / report_date
        report_date_dir.mkdir(parents=True, exist_ok=True)
        # Caller (e.g. dashboard) may pre-allocate a run_id; otherwise we
        # generate one. Validated as a real ULID so a typo lands here, not
        # in directory naming downstream.
        if run_id is None:
            run_id = new_run_id()
        elif not is_valid_run_id(run_id):
            raise ValueError(f"run_id={run_id!r} is not a valid ULID")
        source_run_ids = {"comparator": comparator_run_id} if comparator_run_id else {}

        write_run_record(
            run_id,
            kind="report",
            args={
                "comparator_root": str(comparator_root),
                "report_date": report_date,
                "source_run_ids": source_run_ids,
                "urls_with_changes": len(urls_with_changes),
                "urls_without_changes": len(urls_without_changes),
            },
        )

        with run_context(
            report_date_dir,
            run_id,
            kind="report",
            command="enhanced-report",
            source_run_ids=source_run_ids,
        ) as ctx:
            console.print(
                f"[cyan]📝 Processing {len(urls_without_changes)} URLs without changes...[/cyan]"
            )
            self.reporter.process_urls_without_changes(
                urls_without_changes, ctx.run_root
            )

            if urls_with_changes:
                console.print(
                    f"[cyan]🤖 Processing {len(urls_with_changes)} URLs with AI analysis...[/cyan]"
                )
                for i, url_data in enumerate(urls_with_changes, 1):
                    console.print(
                        f"[blue]🔄 Processing {url_data['url_name']} "
                        f"({i}/{len(urls_with_changes)})...[/blue]"
                    )
                    try:
                        result = await self.reporter.process_single_url(
                            url_data, ctx.run_root
                        )
                        severity = result.get("ai_analysis", {}).get(
                            "overall_severity", "UNKNOWN"
                        )
                        console.print(
                            f"[green]   ✅ Completed - Severity: {severity}[/green]"
                        )
                    except Exception as e:
                        console.print(f"[red]   ❌ Failed: {str(e)}[/red]")
                        # Error path is already inside process_single_url
                        continue

            console.print(
                "[cyan]📈 Generating cross-URL analysis and enhanced report...[/cyan]"
            )
            enhanced_report_path = await self.reporter.generate_enhanced_report(
                ctx.run_root, report_date
            )
            ctx.complete(url_count=len(urls_with_changes) + len(urls_without_changes))

        try:
            update_latest_symlink(report_date_dir, run_id)
        except Exception as e:
            console.print(
                f"[yellow]⚠ Could not update report 'latest' symlink: {e}[/yellow]"
            )

        # The path returned to the caller now points into the published run dir.
        published_path = report_date_dir / run_id / enhanced_report_path.name
        console.print(f"[green]🎉 Report run {run_id} published[/green]")
        console.print(f"[blue]📄 Report location: {published_path}[/blue]")
        return published_path


__all__ = ["Orchestrator"]
