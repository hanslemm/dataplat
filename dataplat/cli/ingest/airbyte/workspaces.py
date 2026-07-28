"""Airbyte workspaces CLI commands."""

from __future__ import annotations

import json

import typer
from rich import box
from rich.console import Console
from rich.table import Table

from dataplat.cli._exit import fail
from dataplat.cli._render import cell
from dataplat.core.errors import AuthError, ConfigError, ServiceError
from dataplat.services.airbyte.client import build_authenticated_client
from dataplat.services.airbyte.workspaces import get_workspace, list_workspaces

app = typer.Typer(
    name="workspaces", help="Manage Airbyte workspaces", no_args_is_help=True
)
console = Console()


@app.command("list")
def list_workspaces_cmd(
    format: str = typer.Option(
        "table", "--format", "-f", help="Output format: table or json"
    ),
):
    """List Airbyte workspaces."""
    try:
        client, base_url = build_authenticated_client()
    except (ConfigError, AuthError) as exc:
        fail(exc, console=console)

    try:
        workspaces = list(list_workspaces(client, base_url))

        if not workspaces:
            console.print("[yellow]No workspaces found[/yellow]")
            return

        if format == "json":
            console.print(cell(json.dumps(workspaces, indent=2, ensure_ascii=False)))
            return

        table = Table(
            show_header=True,
            header_style="bold cyan",
            box=box.SIMPLE_HEAVY,
            expand=True,
        )
        table.add_column("Name", style="cyan")
        table.add_column("Workspace ID", style="dim")

        for ws in workspaces:
            table.add_row(
                cell(ws.get("name", "N/A")),
                cell(ws.get("workspaceId", "N/A")),
            )

        console.print(table)
        console.print(f"\n[dim]Total: {len(workspaces)} workspace(s)[/dim]")

    except ServiceError as exc:
        fail(exc, console=console)
    finally:
        client.close()


@app.command("get")
def get_workspace_cmd(
    workspace_id: str = typer.Option(..., "--workspace-id", "-w", help="Workspace ID"),
):
    """Get workspace details."""
    try:
        client, base_url = build_authenticated_client()
    except (ConfigError, AuthError) as exc:
        fail(exc, console=console)

    try:
        workspace = get_workspace(client, base_url, workspace_id)
        console.print(cell(json.dumps(workspace, indent=2, ensure_ascii=False)))
    except ServiceError as exc:
        fail(exc, console=console)
    finally:
        client.close()
