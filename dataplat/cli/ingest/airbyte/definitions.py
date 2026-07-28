"""Airbyte connector definitions CLI commands."""

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
from dataplat.services.airbyte.definitions import (
    list_destination_definitions,
    list_source_definitions,
)

app = typer.Typer(
    name="definitions", help="List Airbyte connector definitions", no_args_is_help=True
)
console = Console()


@app.command("list-sources")
def list_source_definitions_cmd(
    workspace_id: str = typer.Option(..., "--workspace-id", "-w", help="Workspace ID"),
    format: str = typer.Option(
        "table", "--format", "-f", help="Output format: table or json"
    ),
):
    """List available source connector definitions for a workspace."""
    try:
        client, base_url = build_authenticated_client()
    except (ConfigError, AuthError) as exc:
        fail(exc, console=console)

    try:
        definitions = list(list_source_definitions(client, base_url, workspace_id))

        if not definitions:
            console.print("[yellow]No source definitions found[/yellow]")
            return

        if format == "json":
            console.print(cell(json.dumps(definitions, indent=2, ensure_ascii=False)))
            return

        table = Table(
            show_header=True,
            header_style="bold cyan",
            box=box.SIMPLE_HEAVY,
            expand=True,
        )
        table.add_column("Name", style="cyan")
        table.add_column("Definition ID", style="dim")
        table.add_column("Docker Image")
        table.add_column("Doc URL", style="dim")

        for d in definitions:
            docker = d.get("dockerRepository", "N/A")
            tag = d.get("dockerImageTag")
            docker_image = f"{docker}:{tag}" if tag else docker
            def_id = d.get("sourceDefinitionId", d.get("definitionId", "N/A"))
            table.add_row(
                cell(d.get("name", "N/A")),
                cell(def_id),
                cell(docker_image),
                cell(d.get("documentationUrl", "N/A")),
            )

        console.print(table)
        console.print(f"\n[dim]Total: {len(definitions)} definition(s)[/dim]")

    except ServiceError as exc:
        fail(exc, console=console)
    finally:
        client.close()


@app.command("list-destinations")
def list_destination_definitions_cmd(
    workspace_id: str = typer.Option(..., "--workspace-id", "-w", help="Workspace ID"),
    format: str = typer.Option(
        "table", "--format", "-f", help="Output format: table or json"
    ),
):
    """List available destination connector definitions for a workspace."""
    try:
        client, base_url = build_authenticated_client()
    except (ConfigError, AuthError) as exc:
        fail(exc, console=console)

    try:
        definitions = list(list_destination_definitions(client, base_url, workspace_id))

        if not definitions:
            console.print("[yellow]No destination definitions found[/yellow]")
            return

        if format == "json":
            console.print(cell(json.dumps(definitions, indent=2, ensure_ascii=False)))
            return

        table = Table(
            show_header=True,
            header_style="bold cyan",
            box=box.SIMPLE_HEAVY,
            expand=True,
        )
        table.add_column("Name", style="cyan")
        table.add_column("Definition ID", style="dim")
        table.add_column("Docker Image")
        table.add_column("Doc URL", style="dim")

        for d in definitions:
            docker = d.get("dockerRepository", "N/A")
            tag = d.get("dockerImageTag")
            docker_image = f"{docker}:{tag}" if tag else docker
            def_id = d.get("destinationDefinitionId", d.get("definitionId", "N/A"))
            table.add_row(
                cell(d.get("name", "N/A")),
                cell(def_id),
                cell(docker_image),
                cell(d.get("documentationUrl", "N/A")),
            )

        console.print(table)
        console.print(f"\n[dim]Total: {len(definitions)} definition(s)[/dim]")

    except ServiceError as exc:
        fail(exc, console=console)
    finally:
        client.close()
