#!/usr/bin/env python3
"""
CLI orchestrator for AI Frontend Regression Tester.
Coordinates crawler, comparator, and report generation.
"""
import asyncio
import shutil
import tempfile
import json
from pathlib import Path
import yaml  # Import YAML library

import click
from rich.console import Console

from .crawler.engine import main as crawler_main
from .comparator.engine import ComparatorEngine
from .report.generator import ReportGenerator
from .config import settings

console = Console()

def load_sites(sites_file: str) -> list:
    """Loads site configurations from a YAML file."""
    with open(sites_file, 'r') as f:
        return yaml.safe_load(f)

class Orchestrator:
    """Orchestrates the testing workflow."""

    def __init__(self, gemini_url: str = None):
        self.gemini_url = gemini_url or settings.ai_analyzer_service_url
        self.comparator = ComparatorEngine()
        # Pass gemini_url to ReportGenerator
        self.reporter = ReportGenerator(settings, self.gemini_url)

    async def create_baseline(self, sites: list, output_dir: str, is_baseline: bool = True) -> bool:
        """Create baseline snapshots with date-based directory structure."""
        return await crawler_main(sites, output_dir, is_baseline=is_baseline)
        
    async def create_current(self, sites: list, output_dir: str) -> bool:
        """Create current snapshots with date-based directory structure."""
        return await crawler_main(sites, output_dir, is_baseline=False)

    async def compare_with_baseline(self, baseline_dir: Path, current_dir: Path, output_dir: Path, urls: list):
        """Runs comparison and saves the results to a file."""
        # Note: output_dir is deprecated - comparator creates its own per-URL directory structure

        # Check if baseline_dir contains date subdirectories
        actual_baseline_dir = self.comparator.find_latest_baseline(baseline_dir)
        if actual_baseline_dir:
            console.print(f"[cyan]Using latest baseline: {actual_baseline_dir.name}[/cyan]")
            baseline_path = actual_baseline_dir
        else:
            # Fallback to direct baseline directory (legacy support)
            baseline_path = baseline_dir
        
        # Check if current_dir contains date subdirectories
        actual_current_dir = self.comparator.find_latest_current(current_dir)
        if actual_current_dir:
            console.print(f"[cyan]Using latest current: {actual_current_dir.name}[/cyan]")
            current_path = actual_current_dir
        else:
            # Fallback to direct current directory (legacy support)
            current_path = current_dir
            
        console.print(f"[cyan]Comparing baseline '{baseline_path}' with current '{current_path}'...[/cyan]")
        comparison_results = self.comparator.compare_all(
            baseline_dir=baseline_path,
            current_dir=current_path,
            urls=urls
        )

        # The comparator now returns a list of results, one per URL
        # Each URL has its own comparison_results.json file already saved
        console.print(f"[green]Comparison complete for {len(comparison_results)} URLs[/green]")
        
        for result in comparison_results:
            url = result["metadata"]["url"]
            output_file = result["metadata"]["output_path"] + "/comparison_results.json"
            console.print(f"[cyan]  {url} -> {output_file}[/cyan]")
        
        return comparison_results

    async def generate_report(self, results_file: Path, output_dir: Path):
        """Generates an HTML report from comparison results."""
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        console.print(f"[cyan]Generating report from '{results_file}'...[/cyan]")
        with open(results_file, "r") as f:
            comparison_results = json.load(f)

        report_path = await self.reporter.generate(
            comparisons=comparison_results,
            output_dir=output_path
        )

        console.print(f"[green]Report generated at {report_path}[/green]")
        return report_path
    
    async def generate_enhanced_report(self, comparator_root: Path, report_date: str, output_dir: Path):
        """Generates enhanced AI-powered HTML report with cross-URL analysis."""
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        console.print(f"[cyan]🔍 Discovering comparison data for {report_date}...[/cyan]")
        
        # Discover URLs with and without changes
        discovery_result = self.reporter.discover_comparison_data(comparator_root, report_date)
        urls_with_changes = discovery_result["with_changes"]
        urls_without_changes = discovery_result["without_changes"]
        
        console.print(f"[blue]📊 Found {len(urls_with_changes)} URLs with changes, {len(urls_without_changes)} without changes[/blue]")
        
        if not urls_with_changes and not urls_without_changes:
            console.print(f"[yellow]⚠️  No comparison data found for {report_date}[/yellow]")
            return
        
        # Process URLs without changes first (faster, no AI needed)
        console.print(f"[cyan]📝 Processing {len(urls_without_changes)} URLs without changes...[/cyan]")
        no_change_results = self.reporter.process_urls_without_changes(urls_without_changes, report_date)
        
        all_url_results = no_change_results.copy()
        
        # Process URLs with changes using AI analysis
        if urls_with_changes:
            console.print(f"[cyan]🤖 Processing {len(urls_with_changes)} URLs with AI analysis...[/cyan]")
            
            for i, url_data in enumerate(urls_with_changes, 1):
                console.print(f"[blue]🔄 Processing {url_data['url_name']} ({i}/{len(urls_with_changes)})...[/blue]")
                
                try:
                    result = await self.reporter.process_single_url(url_data, report_date)
                    all_url_results.append(result)
                    
                    severity = result.get("ai_analysis", {}).get("overall_severity", "UNKNOWN")
                    console.print(f"[green]   ✅ Completed - Severity: {severity}[/green]")
                    
                except Exception as e:
                    console.print(f"[red]   ❌ Failed: {str(e)}[/red]")
                    # Error handling is already built into process_single_url
                    continue
        
        # Generate aggregated analysis and enhanced report
        console.print(f"[cyan]📈 Generating cross-URL analysis and enhanced report...[/cyan]")
        
        try:
            enhanced_report_path = await self.reporter.generate_enhanced_report(report_date)
            console.print(f"[green]🎉 Enhanced report generated successfully![/green]")
            console.print(f"[blue]📄 Report location: {enhanced_report_path}[/blue]")
            return enhanced_report_path
            
        except Exception as e:
            console.print(f"[red]❌ Enhanced report generation failed: {str(e)}[/red]")
            raise

@click.group()
@click.option('--sites-file', default='sites.yml', help='Path to the YAML file with sites to test.')
@click.pass_context
def cli(ctx, sites_file):
    """AI-powered frontend regression testing tool."""
    ctx.obj = {}
    try:
        with open(sites_file, 'r') as f:
            ctx.obj['sites'] = yaml.safe_load(f)
        ctx.obj['sites_file'] = sites_file
    except FileNotFoundError:
        console.print(f"[red]Error: Sites file not found at '{sites_file}'[/red]")
        raise click.Abort()
    except yaml.YAMLError as e:
        console.print(f"[red]Error parsing YAML file '{sites_file}': {e}[/red]")
        raise click.Abort()


@cli.command()
@click.option('--output', '-o', type=click.Path(), required=True, help='Output directory for baseline')
@click.option('--force', is_flag=True, help='Force overwrite existing baseline')
@click.pass_context
def snapshot(ctx, output, force):
    """Create baseline snapshots of URLs with date-based structure."""
    sites = ctx.obj['sites']['sites']  # Extract the sites list from the YAML structure
    output_path = Path(output)

    if output_path.exists() and any(output_path.iterdir()) and not force:
        console.print(f"[yellow]Baseline directory '{output}' is not empty. Use --force to overwrite.[/yellow]")
        return

    orchestrator = Orchestrator()
    asyncio.run(orchestrator.create_baseline(sites, output, is_baseline=True))
    console.print(f"[green]Baseline created successfully in '{output}' with date-based structure[/green]")


@cli.command()
@click.option('--output', '-o', type=click.Path(), required=True, help='Output directory for current snapshots')
@click.option('--force', is_flag=True, help='Force overwrite existing current snapshots')
@click.pass_context
def current(ctx, output, force):
    """Create current snapshots of URLs with date-based structure."""
    sites = ctx.obj['sites']['sites']  # Extract the sites list from the YAML structure
    output_path = Path(output)

    if output_path.exists() and any(output_path.iterdir()) and not force:
        console.print(f"[yellow]Current directory '{output}' is not empty. Use --force to overwrite.[/yellow]")
        return

    orchestrator = Orchestrator()
    asyncio.run(orchestrator.create_current(sites, output))
    console.print(f"[green]Current snapshots created successfully in '{output}' with date-based structure[/green]")


@cli.command()
@click.option('--baseline', '-b', type=click.Path(exists=True, file_okay=False), required=True, help='Baseline directory')
@click.option('--current', '-c', type=click.Path(exists=True, file_okay=False), required=True, help='Current version directory')
@click.option('--output', '-o', type=click.Path(), required=False, help='Output directory for reports and results (deprecated - comparator creates per-URL structure)')
@click.pass_context
def compare(ctx, baseline, current, output):
    """Compares baseline with current, saving results."""
    sites = ctx.obj['sites']['sites']  # Extract the sites list from the YAML structure
    urls = [site['url'] for site in sites]  # Extract URLs for comparison
    
    # If no output directory is specified, use a dummy path since comparator creates its own structure
    if output is None:
        output = Path("data/reports")  # Dummy path, not actually used
    
    orchestrator = Orchestrator()
    asyncio.run(orchestrator.compare_with_baseline(
        baseline_dir=Path(baseline),
        current_dir=Path(current),
        output_dir=Path(output),
        urls=urls
    ))


@cli.command()
@click.option('--results-file', '-r', type=click.Path(exists=True, dir_okay=False), required=True, help='Path to comparison_results.json file')
@click.option('--output', '-o', type=click.Path(), required=True, help='Output directory for the final report')
@click.pass_context
def report(ctx, results_file, output):
    """Generates the final HTML report from comparison results."""
    orchestrator = Orchestrator()
    # The generate_report method in Orchestrator is now async
    asyncio.run(orchestrator.generate_report(
        results_file=Path(results_file),
        output_dir=Path(output)
    ))


@cli.command()
@click.option('--comparator-data', '-c', type=click.Path(exists=True, file_okay=False), required=True, help='Path to data/comparator directory')
@click.option('--date', '-d', help='Specific date to generate report for (e.g., 01-09-2025). If not provided, uses latest available.')
@click.option('--output', '-o', type=click.Path(), required=True, help='Output directory for the enhanced report')
@click.pass_context
def enhanced_report(ctx, comparator_data, date, output):
    """Generates enhanced AI-powered HTML report from comparator data with cross-URL analysis."""
    try:
        orchestrator = Orchestrator()
        
        # Convert paths to Path objects
        comparator_root = Path(comparator_data)
        output_dir = Path(output)
        
        # Ensure output directory exists
        output_dir.mkdir(parents=True, exist_ok=True)
        
        console.print("[blue]🤖 Starting Enhanced AI-Powered Report Generation[/blue]")
        
        if date:
            console.print(f"[cyan]📅 Using specified date: {date}[/cyan]")
            report_date = date
        else:
            # Find the latest date in comparator directory
            available_dates = [d.name for d in comparator_root.iterdir() if d.is_dir()]
            if not available_dates:
                console.print(f"[red]❌ No comparison data found in {comparator_data}[/red]")
                raise click.Abort()
            
            # Sort dates and get the latest (assumes DD-MM-YYYY format)
            available_dates.sort(key=lambda x: x.split('-')[::-1])  # Convert to YYYY-MM-DD for sorting
            report_date = available_dates[-1]
            console.print(f"[cyan]📅 Using latest available date: {report_date}[/cyan]")
        
        # Check if the date directory exists
        date_dir = comparator_root / report_date
        if not date_dir.exists():
            console.print(f"[red]❌ No comparison data found for date {report_date} in {comparator_data}[/red]")
            raise click.Abort()
        
        asyncio.run(orchestrator.generate_enhanced_report(
            comparator_root=comparator_root,
            report_date=report_date,
            output_dir=output_dir
        ))
        
        console.print(f"[green]✅ Enhanced report generated successfully in '{output_dir}'[/green]")
        console.print(f"[blue]📊 View your enhanced AI analysis report: {output_dir}/enhanced_analysis_report.html[/blue]")
        
    except Exception as e:
        console.print(f"[red]❌ Enhanced report generation failed: {str(e)}[/red]")
        raise click.Abort()


if __name__ == '__main__':
    cli()