"""Airbyte tag CLI commands."""

from __future__ import annotations

import json

import typer
from rich.console import Console

from dataplat.core.errors import AuthError, ConfigError, ServiceError
from dataplat.services.airbyte.client import build_authenticated_client
from dataplat.services.airbyte.tags import create_tag, list_tags

app = typer.Typer(name="tags", help="Manage Airbyte tags", no_args_is_help=True)
console = Console()


@app.command("list")
def list_tags_cmd():
    """List all available Airbyte tags."""
    try:
        client, base_url = build_authenticated_client()
    except (ConfigError, AuthError) as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(code=1)

    try:
        tags = list_tags(client, base_url)
        console.print(json.dumps(tags, indent=2, ensure_ascii=False))
    except ServiceError as exc:
        console.print(f"[red]Error listing tags: {exc}[/red]")
        raise typer.Exit(code=1)
    finally:
        client.close()


@app.command("create")
def create_tag_cmd(
    name: str = typer.Option(..., "--name", help="Tag name"),
    workspace_id: str | None = typer.Option(None, "--workspace-id", help="Workspace ID (if required)"),
    color: str | None = typer.Option(None, "--color", help="Tag color hex without '#', e.g. 75DCFF"),
):
    """Create a new Airbyte tag."""
    try:
        client, base_url = build_authenticated_client()
    except (ConfigError, AuthError) as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(code=1)

    try:
        tag = create_tag(client, base_url, name, workspace_id, color)
        console.print(json.dumps(tag, indent=2, ensure_ascii=False))
    except ServiceError as exc:
        console.print(f"[red]Error creating tag: {exc}[/red]")
        raise typer.Exit(code=1)
    finally:
        client.close()
