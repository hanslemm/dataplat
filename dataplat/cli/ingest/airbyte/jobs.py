"""Airbyte jobs CLI commands: list, get, cancel."""

from __future__ import annotations

import json

import typer
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from dataplat.cli._options import YesOption, json_option
from dataplat.cli._prompt import confirm_or_exit
from dataplat.cli._render import cell, esc
from dataplat.cli.ingest.airbyte._common import airbyte_client
from dataplat.services.airbyte.jobs import cancel_job, get_job, list_jobs

app = typer.Typer(
    name="jobs", help="Inspect and cancel Airbyte jobs", no_args_is_help=True
)
console = Console()

_STATUS_STYLES = {
    "succeeded": "green",
    "running": "yellow",
    "pending": "yellow",
    "incomplete": "yellow",
    "failed": "red",
    "cancelled": "red",
}


def _style_status(status: str) -> Text:
    """Colour a job status without letting the API's value act as markup."""
    return cell(status, style=_STATUS_STYLES.get(status.lower(), ""))


@app.command("list")
def list_cmd(
    connection_id: str | None = typer.Option(
        None, "--connection-id", "-c", help="Filter by connection ID"
    ),
    status: str | None = typer.Option(
        None,
        "--status",
        help=(
            "Filter by status "
            "(pending, running, succeeded, failed, cancelled, incomplete)"
        ),
    ),
    job_type: str | None = typer.Option(
        None, "--job-type", help="Filter by type (sync, reset, refresh, clear)"
    ),
    limit: int = typer.Option(
        20, "--limit", "-n", min=1, help="Maximum number of jobs to show"
    ),
    as_json: bool = json_option("Emit JSON instead of a table"),
):
    """List recent jobs, newest first."""
    with airbyte_client() as (client, base_url):
        jobs = list_jobs(
            client,
            base_url,
            connection_id=connection_id,
            status=status,
            job_type=job_type,
            limit=limit,
        )

        if as_json:
            typer.echo(json.dumps(jobs, indent=2, ensure_ascii=False))
            return

        if not jobs:
            console.print("[yellow]No jobs found[/yellow]")
            return

        table = Table(show_header=True, header_style="bold cyan", box=box.SIMPLE_HEAVY)
        table.add_column("Job ID", justify="right")
        table.add_column("Connection", style="dim")
        table.add_column("Type")
        table.add_column("Status")
        table.add_column("Started", style="dim")
        table.add_column("Duration", style="dim")
        table.add_column("Rows synced", justify="right")

        for job in jobs:
            table.add_row(
                cell(job.get("jobId", "")),
                cell(job.get("connectionId", "")),
                cell(job.get("jobType", "")),
                _style_status(str(job.get("status", ""))),
                cell(job.get("startTime", "")),
                cell(job.get("duration", "")),
                cell(job.get("rowsSynced", "")),
            )

        console.print(table)
        console.print(f"\n[dim]{len(jobs)} job(s)[/dim]")


@app.command("get")
def get_cmd(job_id: str = typer.Argument(..., help="Job ID")):
    """Show a job as JSON."""
    with airbyte_client() as (client, base_url):
        job = get_job(client, base_url, job_id)
        typer.echo(json.dumps(job, indent=2, ensure_ascii=False))


@app.command("cancel")
def cancel_cmd(
    job_id: str = typer.Argument(..., help="Job ID to cancel"),
    yes: bool = YesOption,
):
    """Cancel a running job."""
    confirm_or_exit(yes=yes, prompt=f"Cancel job {job_id}?", console=console)
    with airbyte_client() as (client, base_url):
        cancel_job(client, base_url, job_id)
        console.print(f"[green]✓ Cancellation requested for job {esc(job_id)}[/green]")
