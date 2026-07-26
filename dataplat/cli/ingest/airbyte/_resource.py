"""Factory building the sources/destinations Typer apps.

Sources and destinations expose the same five commands with the same
behavior; only the subject id flag and a couple of field names differ.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable

import typer
from rich import box
from rich.console import Console
from rich.table import Table

from dataplat.cli._options import YesOption
from dataplat.cli._prompt import confirm_or_exit
from dataplat.cli._render import cell, esc
from dataplat.cli.ingest.airbyte._common import airbyte_client

console = Console()


def _load_config(config_path: str) -> dict:
    try:
        if config_path == "-":
            raw = sys.stdin.read()
        else:
            with open(config_path, encoding="utf-8") as f:
                raw = f.read()
        return json.loads(raw)
    except FileNotFoundError:
        console.print(f"[red]Error: config file not found: {esc(config_path)}[/red]")
        raise typer.Exit(code=2)
    except json.JSONDecodeError as exc:
        console.print(f"[red]Error: invalid JSON in config: {esc(exc)}[/red]")
        raise typer.Exit(code=2)


def make_resource_app(
    *,
    kind: str,
    plural: str,
    id_flag: str,
    id_short: str,
    id_key: str,
    connector_keys: tuple[str, str],
    list_fn: Callable,
    get_fn: Callable,
    create_fn: Callable,
    update_fn: Callable,
    delete_fn: Callable,
) -> typer.Typer:
    """Build the Typer app for one resource kind ("source"/"destination")."""
    app = typer.Typer(
        name=plural, help=f"Manage Airbyte {plural}", no_args_is_help=True
    )
    id_help = f"{kind.capitalize()} ID"

    @app.command("list")
    def list_cmd(
        workspace_id: str | None = typer.Option(
            None, "--workspace-id", "-w", help="Filter by workspace ID"
        ),
        format: str = typer.Option(
            "table", "--format", "-f", help="Output format: table or json"
        ),
        include_deleted: bool = typer.Option(
            False, "--include-deleted", help=f"Include deleted {plural}"
        ),
    ):
        with airbyte_client() as (client, base_url):
            workspace_ids = [workspace_id] if workspace_id else None
            items = list(
                list_fn(
                    client,
                    base_url,
                    workspace_ids=workspace_ids,
                    include_deleted=include_deleted,
                )
            )

            if not items:
                console.print(f"[yellow]No {plural} found[/yellow]")
                return

            if format == "json":
                console.print(cell(json.dumps(items, indent=2, ensure_ascii=False)))
                return

            table = Table(
                show_header=True,
                header_style="bold cyan",
                box=box.SIMPLE_HEAVY,
                expand=True,
            )
            table.add_column("Name", style="cyan")
            table.add_column(id_help, style="dim")
            table.add_column("Connector")
            table.add_column("Workspace ID", style="dim")

            for item in items:
                table.add_row(
                    cell(item.get("name", "N/A")),
                    cell(item.get(id_key, "N/A")),
                    cell(
                        item.get(connector_keys[0], item.get(connector_keys[1], "N/A"))
                    ),
                    cell(item.get("workspaceId", "N/A")),
                )

            console.print(table)
            console.print(f"\n[dim]Total: {len(items)} {kind}(s)[/dim]")

    list_cmd.__doc__ = f"List Airbyte {plural}."

    @app.command("get")
    def get_cmd(
        resource_id: str = typer.Option(..., id_flag, id_short, help=id_help),
    ):
        with airbyte_client() as (client, base_url):
            item = get_fn(client, base_url, resource_id)
            console.print(cell(json.dumps(item, indent=2, ensure_ascii=False)))

    get_cmd.__doc__ = f"Get {kind} details."

    @app.command("create")
    def create_cmd(
        name: str = typer.Option(..., "--name", "-n", help=f"{kind.capitalize()} name"),
        definition_id: str = typer.Option(
            ..., "--definition-id", help=f"{kind.capitalize()} definition ID"
        ),
        workspace_id: str = typer.Option(
            ..., "--workspace-id", "-w", help="Workspace ID"
        ),
        config_path: str = typer.Option(
            ..., "--config", help="Path to JSON config file, or '-' for stdin"
        ),
    ):
        configuration = _load_config(config_path)
        with airbyte_client() as (client, base_url):
            item = create_fn(
                client, base_url, name, workspace_id, definition_id, configuration
            )
            console.print(cell(json.dumps(item, indent=2, ensure_ascii=False)))

    create_cmd.__doc__ = f"Create a new Airbyte {kind}."

    @app.command("update")
    def update_cmd(
        resource_id: str = typer.Option(..., id_flag, id_short, help=id_help),
        name: str | None = typer.Option(None, "--name", "-n", help=f"New {kind} name"),
        config_path: str | None = typer.Option(
            None, "--config", help="Path to JSON config file, or '-' for stdin"
        ),
    ):
        if not name and not config_path:
            console.print(
                "[red]Error: at least one of --name or --config must be provided[/red]"
            )
            raise typer.Exit(code=2)

        updates: dict = {}
        if name:
            updates["name"] = name
        if config_path:
            updates["configuration"] = _load_config(config_path)

        with airbyte_client() as (client, base_url):
            item = update_fn(client, base_url, resource_id, updates)
            console.print(cell(json.dumps(item, indent=2, ensure_ascii=False)))

    update_cmd.__doc__ = f"Update an Airbyte {kind}."

    @app.command("delete")
    def delete_cmd(
        resource_id: str = typer.Option(..., id_flag, id_short, help=id_help),
        yes: bool = YesOption,
    ):
        # The prompt reaches the terminal through click, which does not parse
        # markup, so resource_id must stay unescaped here.
        confirm_or_exit(
            yes=yes, prompt=f"Delete {kind} {resource_id}?", console=console
        )

        with airbyte_client() as (client, base_url):
            delete_fn(client, base_url, resource_id)
            console.print(
                f"[green]{kind.capitalize()} {esc(resource_id)} deleted[/green]"
            )

    delete_cmd.__doc__ = f"Delete an Airbyte {kind}."

    return app
