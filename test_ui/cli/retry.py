"""`retry-url` Click command - single-URL retry of a previously-failed AI run.

Split out of `commands.py` because it accounts for ~150 LOC of the 444-line
file (38%) and has a meaningfully distinct shape from the other commands
(it resolves a single URL's data + publishes a single-URL report run).
The HTML report's "AI analysis failed" badge points users at this command,
so it has its own ergonomic concerns (stable id semantics, friendly error
messages) that aren't shared with the rest of the CLI.

Registered against the same `cli` group via the standard Click decorator
trick - importing this module attaches the command to the group.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import click

from ..config import settings
from .commands import _open_orchestrator, cli, console


@cli.command(name="retry-url")
@click.option(
    "--date",
    "-d",
    required=True,
    help="Date directory under data/comparator (e.g., 30-04-2026)",
)
@click.option(
    "--url",
    "-u",
    required=True,
    help=(
        "Site id (matches the per-site dir under <date>/<run_id>/, "
        "e.g. 'home-page' or 'budget-2025'). "
        "Pre-B.3 layouts used url-derived names like 'gov.ie_en_about'; "
        "those still work via the legacy fallback."
    ),
)
@click.option(
    "--comparator-data",
    "-c",
    type=click.Path(exists=True, file_okay=False),
    default=None,
    help="Override comparator root (defaults to settings.comparator_dir).",
)
@click.pass_context
def retry_url(ctx, date, url, comparator_data):
    """Re-run AI analysis for a single URL after a previous failure.

    Phase B.1: drills through `<comparator_root>/<date>/<run_id>/<url>/` to
    find the existing diff data, then publishes a fresh single-URL report
    run at `<report_dir>/<date>/<run_id>/` (atomic publication + manifest,
    same as a full report run).
    """
    from ..common.preconditions import PreconditionFailed, require_complete_run
    from ..common.run_context import run_context
    from ..common.run_id import is_valid_run_id, new_run_id
    from ..common.run_record import write_run_record
    from ..comparator.finder import update_latest_symlink

    comparator_root = (
        Path(comparator_data) if comparator_data else settings.comparator_dir
    )

    # Resolve the latest complete comparator run; raises with a friendly
    # message if missing or status != complete (covers running/failed/etc).
    try:
        comparator_run_dir = require_complete_run(
            comparator_root, date, kind_label="comparator"
        )
    except PreconditionFailed as e:
        console.print(f"[red]❌ {e}[/red]")
        raise click.Abort() from e

    url_dir = comparator_run_dir / url
    if not url_dir.exists():
        console.print(f"[red]❌ No comparator data at {url_dir}[/red]")
        raise click.Abort()

    diffs_dir = url_dir / "diffs"
    if not diffs_dir.exists():
        console.print(
            f"[red]❌ No diffs directory at {diffs_dir} "
            "(comparator detected no changes for this URL - nothing to analyze).[/red]"
        )
        raise click.Abort()

    # Pull comparison_results.json so the screenshot loader can find baseline
    # and current paths from the recorded metadata. Optional - falls back to
    # diffs/visual_diff.png if missing.
    comparison_file = url_dir / "comparison_results.json"
    comparison_data = None
    if comparison_file.exists():
        comparison_data = json.loads(comparison_file.read_text(encoding="utf-8"))

    url_data = {
        "url_name": url,
        "url_dir": url_dir,
        "structured_data_path": diffs_dir,
        "has_changes": True,
        "comparison_data": comparison_data,
    }

    # Each retry is its own report run. Source provenance points at the
    # comparator run we just resolved when it has a canonical run_id.
    source_run_ids: dict[str, str] = {}
    if is_valid_run_id(comparator_run_dir.name):
        source_run_ids["comparator"] = comparator_run_dir.name

    report_date_dir = settings.report_dir / date
    report_date_dir.mkdir(parents=True, exist_ok=True)
    run_id = new_run_id()

    write_run_record(
        run_id,
        kind="report",
        args={
            "retry_url": url,
            "report_date": date,
            "comparator_root": str(comparator_root),
            "source_run_ids": source_run_ids,
        },
    )

    async def _run():
        async with _open_orchestrator() as orchestrator:
            with run_context(
                report_date_dir,
                run_id,
                kind="report",
                command=f"retry-url {url} {date}",
                source_run_ids=source_run_ids,
            ) as ctx:
                result = await orchestrator.reporter.process_single_url(
                    url_data, ctx.run_root
                )
                ctx.complete(url_count=1)

            try:
                update_latest_symlink(report_date_dir, run_id)
            except Exception as e:
                console.print(
                    f"[yellow]⚠ Could not update report 'latest' symlink: {e}[/yellow]"
                )

            ai = result.get("ai_analysis", {})
            rt = ai.get("result_type", "unknown")
            if rt == "analysis_success":
                sev = ai.get("overall_severity")
                console.print(
                    f"[green]✅ Retried {url} for {date} → {sev} (run {run_id})[/green]"
                )
                console.print(
                    f"[blue]   Wrote {result['report_path']}/ai_analysis.json[/blue]"
                )
            elif rt == "analysis_error":
                err = ai.get("error_type", "unknown")
                details = ai.get("details", "")[:120]
                console.print(
                    f"[yellow]⚠ Retried {url} for {date} → AIAnalysisError({err}): {details}[/yellow]"
                )
                console.print(
                    f"[blue]   Wrote {result['report_path']}/ai_error.json[/blue]"
                )
            else:
                console.print(f"[red]Unexpected result_type: {rt}[/red]")

    asyncio.run(_run())


__all__ = ["retry_url"]
