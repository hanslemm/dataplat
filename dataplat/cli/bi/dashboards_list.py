"""``dp bi superset dashboards list``: find the id, and what it reads.

``--database`` is the reason this exists as more than a convenience: "which
dashboards still read from DataOcean" is the question a warehouse retirement
actually asks, and nothing else in the area answers it.
"""

from __future__ import annotations

import json

import typer
from rich import box
from rich.console import Console
from rich.table import Table

from dataplat.cli._exit import fail
from dataplat.cli._options import JsonOption
from dataplat.cli._render import cell
from dataplat.cli.bi._superset_auth import load_auth_context
from dataplat.core.errors import AuthError, ConfigError, ServiceError
from dataplat.services.superset.client import build_client
from dataplat.services.superset.client import login as _login
from dataplat.services.superset.dashboards import dashboard_charts, iter_dashboards
from dataplat.services.superset.databases import resolve_database_id
from dataplat.services.superset.datasets import iter_datasets

__all__ = ["list_command"]

console = Console()


def _owner_names(dashboard: dict) -> str:
    owners = dashboard.get("owners") or []
    return ", ".join(
        " ".join(
            part for part in (owner.get("first_name"), owner.get("last_name")) if part
        )
        for owner in owners
        if isinstance(owner, dict)
    )


def list_command(
    title: str | None = typer.Option(
        None, "--title", help="Only dashboards whose title contains this."
    ),
    database: str | None = typer.Option(
        None,
        "--database",
        help="Only dashboards reading a dataset on this database connection.",
    ),
    as_json: bool = JsonOption,
) -> None:
    """List Superset dashboards."""
    base_url, username, password = load_auth_context(console)

    needle = title.lower() if title else None

    try:
        with build_client() as client:
            token = _login(client, base_url, username, password)
            dashboards = [
                d
                for d in iter_dashboards(client, base_url, token)
                if needle is None or needle in str(d.get("dashboard_title", "")).lower()
            ]

            if database:
                # Superset cannot filter dashboards by database, so the join
                # happens here: a dashboard qualifies when any of its charts
                # reads one of that connection's datasets. Narrowed by --title
                # first, because this costs one request per dashboard.
                database_id = resolve_database_id(client, base_url, token, database)
                dataset_ids = {
                    int(d["id"])
                    for d in iter_datasets(
                        client, base_url, token, database_id=database_id
                    )
                    if d.get("id") is not None
                }
                dashboards = [
                    d
                    for d in dashboards
                    if any(
                        chart.get("datasource_id") in dataset_ids
                        for chart in dashboard_charts(
                            client, base_url, token, int(d["id"])
                        )
                    )
                ]
    except (AuthError, ServiceError, ConfigError) as exc:
        fail(exc, console=console)

    if as_json:
        typer.echo(json.dumps(dashboards, indent=2, ensure_ascii=False))
        return

    if not dashboards:
        console.print("[yellow]No dashboards found[/yellow]")
        return

    table = Table(
        show_header=True, header_style="bold cyan", box=box.SIMPLE_HEAVY, expand=True
    )
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Title", style="cyan")
    table.add_column("Status")
    table.add_column("Owners", style="dim")

    for dashboard in sorted(
        dashboards, key=lambda d: str(d.get("dashboard_title", ""))
    ):
        table.add_row(
            cell(dashboard.get("id", "")),
            cell(dashboard.get("dashboard_title", "")),
            cell(dashboard.get("status", "")),
            cell(_owner_names(dashboard)),
        )

    console.print(table)
    console.print(f"\n[dim]Total: {len(dashboards)} dashboard(s)[/dim]")
