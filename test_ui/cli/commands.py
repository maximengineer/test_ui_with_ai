"""Click commands + the `cli` group + `_open_orchestrator` lifecycle CM.

Split out of `cli.py` in the post-B.3 cleanup so the Orchestrator class
has its own module. This file is a thin shell around the Orchestrator's
methods - each command opens an httpx client, instantiates the
Orchestrator via `_open_orchestrator`, and dispatches.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import click
import httpx
import yaml
from rich.console import Console

from ..config import settings
from .orchestrator import Orchestrator


console = Console()


@asynccontextmanager
async def _open_orchestrator():
    """Open an httpx client and yield a fresh Orchestrator using it.

    Single canonical place that owns the AI-analyzer client lifecycle.
    All 5 Click commands use this so the client is guaranteed to close
    cleanly even on exceptions. Phase A.1.7 - replaces the leaked client
    that ReportGenerator used to construct internally.
    """
    async with httpx.AsyncClient(timeout=settings.ai_analyzer_timeout) as client:
        yield Orchestrator(client=client, ai_analyzer_url=settings.ai_analyzer_service_url)


@click.group()
@click.option(
    "--sites-file",
    default="sites.yml",
    help="Path to the YAML file with sites to test.",
)
@click.pass_context
def cli(ctx, sites_file):
    """AI-powered frontend regression testing tool."""
    from pydantic import ValidationError

    from ..common.sites import load_sites as _load_sites_typed

    ctx.obj = {}
    try:
        # Phase B.3: load via the typed `Site` model (auto-fills `id` from
        # slugified `name` for legacy entries, with a WARNING). Convert back
        # to plain dicts for compatibility with the rest of the pipeline,
        # which still passes sites around as `dict[str, str]`.
        site_objs = _load_sites_typed(sites_file)
        ctx.obj["sites"] = {"sites": [s.model_dump() for s in site_objs]}
        ctx.obj["sites_file"] = sites_file
    except FileNotFoundError:
        console.print(f"[red]Error: Sites file not found at '{sites_file}'[/red]")
        raise click.Abort()
    except yaml.YAMLError as e:
        console.print(f"[red]Error parsing YAML file '{sites_file}': {e}[/red]")
        raise click.Abort()
    except (ValueError, ValidationError) as e:
        # Loader-rejection cases: duplicate ids, missing url, invalid id pattern,
        # unknown fields. The exception message already says what's wrong;
        # printing it directly is more useful than a Python traceback.
        console.print(f"[red]Error in '{sites_file}': {e}[/red]")
        raise click.Abort()


# Help text for the dashboard-only `--run-id` option. Hidden from the
# top-level `--help` is tempting (it's not for human use) but we leave
# it visible so the operator who notices it in `ps -ef` output can find
# its docs without grep-ing the source.
_RUN_ID_HELP = (
    "Pre-allocated ULID to use as this run's identifier. Intended for the "
    "dashboard to spawn subprocesses it can then track by ID. CLI users "
    "should omit this flag - the engine generates a fresh ULID."
)


@cli.command()
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    required=True,
    help="Output directory for baseline",
)
@click.option("--run-id", "run_id", default=None, help=_RUN_ID_HELP)
@click.pass_context
def snapshot(ctx, output, run_id):
    """Create baseline snapshots of URLs with date-based + per-run structure.

    Phase B.1: each invocation publishes a fresh `run_id` directory under
    `<output>/<date>/<run_id>/`, so multiple runs the same day no longer
    collide.

    Phase B.2: refuses to start if another baseline run for the same date
    holds a live `.lock`. Stale locks (dead holding process) are silently
    ignored.
    """
    from ..common.preconditions import PreconditionFailed

    sites = ctx.obj["sites"]["sites"]

    async def _run():
        async with _open_orchestrator() as orchestrator:
            await orchestrator.create_baseline(
                sites, output, is_baseline=True, run_id=run_id
            )

    try:
        asyncio.run(_run())
    except PreconditionFailed as e:
        console.print(f"[red]❌ {e}[/red]")
        raise click.Abort() from e
    except ValueError as e:
        # Engine raises ValueError on invalid --run-id; surface as a
        # friendly Click abort instead of a Python stack trace.
        console.print(f"[red]❌ {e}[/red]")
        raise click.Abort() from e
    console.print(
        f"[green]Baseline run published under '{output}/<date>/<run_id>/'[/green]"
    )


@cli.command()
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    required=True,
    help="Output directory for current snapshots",
)
@click.option("--run-id", "run_id", default=None, help=_RUN_ID_HELP)
@click.pass_context
def current(ctx, output, run_id):
    """Create current snapshots of URLs with date-based + per-run structure.

    Phase B.2: refuses to start if another current run for the same date
    holds a live `.lock`. Stale locks (dead holding process) are silently
    ignored.
    """
    from ..common.preconditions import PreconditionFailed

    sites = ctx.obj["sites"]["sites"]

    async def _run():
        async with _open_orchestrator() as orchestrator:
            await orchestrator.create_current(sites, output, run_id=run_id)

    try:
        asyncio.run(_run())
    except PreconditionFailed as e:
        console.print(f"[red]❌ {e}[/red]")
        raise click.Abort() from e
    except ValueError as e:
        # Engine raises ValueError on invalid --run-id.
        console.print(f"[red]❌ {e}[/red]")
        raise click.Abort() from e
    console.print(
        f"[green]Current run published under '{output}/<date>/<run_id>/'[/green]"
    )


@cli.command()
@click.option(
    "--baseline",
    "-b",
    type=click.Path(exists=True, file_okay=False),
    required=True,
    help="Baseline directory",
)
@click.option(
    "--current",
    "-c",
    type=click.Path(exists=True, file_okay=False),
    required=True,
    help="Current version directory",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    required=False,
    help="Output directory for reports and results (deprecated - comparator creates per-URL structure)",
)
@click.option("--run-id", "run_id", default=None, help=_RUN_ID_HELP)
@click.pass_context
def compare(ctx, baseline, current, output, run_id):
    """Compares baseline with current, saving results."""
    from ..common.preconditions import PreconditionFailed

    sites = ctx.obj["sites"]["sites"]  # Extract the sites list from the YAML structure

    # If no output directory is specified, use a dummy path since comparator creates its own structure
    if output is None:
        output = settings.report_dir  # Dummy - comparator builds its own per-URL paths

    async def _run():
        async with _open_orchestrator() as orchestrator:
            await orchestrator.compare_with_baseline(
                baseline_dir=Path(baseline),
                current_dir=Path(current),
                output_dir=Path(output),
                sites=sites,
                run_id=run_id,
            )

    try:
        asyncio.run(_run())
    except PreconditionFailed as e:
        console.print(f"[red]❌ {e}[/red]")
        raise click.Abort() from e
    except ValueError as e:
        # Engine raises ValueError on invalid --run-id.
        console.print(f"[red]❌ {e}[/red]")
        raise click.Abort() from e


@cli.command()
@click.option(
    "--comparator-data",
    "-c",
    type=click.Path(exists=True, file_okay=False),
    required=True,
    help="Path to data/comparator directory",
)
@click.option(
    "--date",
    "-d",
    help="Specific date to generate report for (e.g., 01-09-2025). If not provided, uses latest available.",
)
@click.option("--run-id", "run_id", default=None, help=_RUN_ID_HELP)
@click.pass_context
def enhanced_report(ctx, comparator_data, date, run_id):
    """Generate enhanced AI-powered HTML report; published under data/report/<date>/<run_id>/.

    Phase B.1 dropped the `--output` flag - the report has always written to
    `settings.report_dir` regardless of what was passed for `--output`. Override
    the destination via `AFR_REPORT_DIR=...` if you need to redirect.
    """
    from ..common.preconditions import PreconditionFailed

    try:
        comparator_root = Path(comparator_data)

        console.print("[blue]🤖 Starting Enhanced AI-Powered Report Generation[/blue]")

        if date:
            console.print(f"[cyan]📅 Using specified date: {date}[/cyan]")
            report_date = date
        else:
            # Find the latest date dir in the comparator root. Filter via
            # is_valid_date_dir so stray entries (e.g. a `.DS_Store` file or
            # a `latest` symlink in a non-date location) don't pollute the
            # sort with garbage that lex-orders unpredictably.
            from ..comparator.finder import is_valid_date_dir

            available_dates = [
                d.name
                for d in comparator_root.iterdir()
                if d.is_dir() and is_valid_date_dir(d.name)
            ]
            if not available_dates:
                console.print(
                    f"[red]❌ No comparison data found in {comparator_data}[/red]"
                )
                raise click.Abort()

            # DD-MM-YYYY → reversed parts give YYYY-MM-DD lexical sort.
            available_dates.sort(key=lambda x: x.split("-")[::-1])
            report_date = available_dates[-1]
            console.print(f"[cyan]📅 Using latest available date: {report_date}[/cyan]")

        # Check if the date directory exists
        date_dir = comparator_root / report_date
        if not date_dir.exists():
            console.print(
                f"[red]❌ No comparison data found for date {report_date} in {comparator_data}[/red]"
            )
            raise click.Abort()

        async def _run():
            async with _open_orchestrator() as orchestrator:
                await orchestrator.generate_enhanced_report(
                    comparator_root=comparator_root,
                    report_date=report_date,
                    run_id=run_id,
                )

        asyncio.run(_run())

        # The orchestrator already prints the published path with the run_id,
        # so we don't need to repeat it here.
        console.print(
            f"[green]✅ Enhanced report published under '{settings.report_dir}/{report_date}/<run_id>/'[/green]"
        )

    except PreconditionFailed as e:
        # Precondition failures (no complete comparator run, etc.) get the
        # same friendly format as the other commands - without the
        # misleading "Enhanced report generation failed:" prefix that the
        # generic Exception branch below would add.
        console.print(f"[red]❌ {e}[/red]")
        raise click.Abort() from e
    except ValueError as e:
        # Engine raises ValueError on invalid --run-id; same friendly
        # treatment as PreconditionFailed.
        console.print(f"[red]❌ {e}[/red]")
        raise click.Abort() from e
    except Exception as e:
        console.print(f"[red]❌ Enhanced report generation failed: {str(e)}[/red]")
        raise click.Abort() from e


# `retry-url` lives in `cli/retry.py` - import it via cli/__init__.py so
# Click registration happens at package-load time. Kept out of this file
# because (a) it's ~150 LOC of distinct shape, (b) commands.py was already
# at the 400-LOC ceiling, and (c) the HTML report deep-links to it so it
# has its own ergonomic concerns worth isolating.


if __name__ == "__main__":
    cli()


__all__ = ["cli", "console", "_open_orchestrator"]
