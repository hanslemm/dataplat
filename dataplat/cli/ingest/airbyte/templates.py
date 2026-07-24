"""Airbyte template/skeleton generation CLI commands."""

from __future__ import annotations

import json

import typer
from rich.console import Console

from dataplat.core.errors import AuthError, ConfigError, ServiceError
from dataplat.services.airbyte.client import build_authenticated_client
from dataplat.services.airbyte.definitions import (
    list_destination_definitions,
    list_source_definitions,
)

app = typer.Typer(name="templates", help="Generate Airbyte configuration skeletons", no_args_is_help=True)
console = Console()


def _find_definition(definitions: list, definition_id: str, id_key: str) -> dict | None:
    """Find a definition by its ID."""
    for d in definitions:
        if d.get(id_key) == definition_id or d.get("definitionId") == definition_id:
            return d
    return None


def _spec_to_skeleton(spec: dict) -> dict:
    """Convert a JSON schema spec to a skeleton dict with placeholder values."""
    properties = spec.get("properties") or spec.get("connectionSpecification", {}).get("properties", {})
    if not properties:
        return {}

    skeleton: dict = {}
    for key, schema in properties.items():
        prop_type = schema.get("type")
        if isinstance(prop_type, list):
            prop_type = next((t for t in prop_type if t != "null"), prop_type[0])

        if schema.get("enum"):
            skeleton[key] = schema["enum"][0]
        elif prop_type == "object":
            nested = _spec_to_skeleton(schema)
            skeleton[key] = nested if nested else {}
        elif prop_type == "array":
            skeleton[key] = []
        elif prop_type == "boolean":
            skeleton[key] = False
        elif prop_type == "integer" or prop_type == "number":
            skeleton[key] = 0
        else:
            skeleton[key] = f"<{key}>"

    return skeleton


def _write_output(data: dict, output: str | None) -> None:
    """Print JSON to stdout or write to a file."""
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        console.print(f"[green]Template written to {output}[/green]")
    else:
        print(text)


@app.command("source")
def source_template(
    definition_id: str = typer.Option(..., "--definition-id", "-d", help="Source definition ID"),
    workspace_id: str = typer.Option(..., "--workspace-id", "-w", help="Workspace ID"),
    output: str | None = typer.Option(None, "--output", "-o", help="Write template to this file path"),
):
    """Generate a JSON config skeleton for a source connector."""
    try:
        client, base_url = build_authenticated_client()
    except (ConfigError, AuthError) as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(code=1)

    try:
        definitions = list(list_source_definitions(client, base_url, workspace_id))
    except ServiceError as exc:
        console.print(f"[red]Error fetching source definitions: {exc}[/red]")
        raise typer.Exit(code=1)
    finally:
        client.close()

    definition = _find_definition(definitions, definition_id, "sourceDefinitionId")
    if not definition:
        console.print(f"[red]Error: source definition {definition_id!r} not found in workspace {workspace_id}[/red]")
        raise typer.Exit(code=1)

    spec = definition.get("spec") or definition.get("connectionSpecification") or {}
    skeleton = _spec_to_skeleton(spec)
    _write_output(skeleton, output)


@app.command("destination")
def destination_template(
    definition_id: str = typer.Option(..., "--definition-id", "-d", help="Destination definition ID"),
    workspace_id: str = typer.Option(..., "--workspace-id", "-w", help="Workspace ID"),
    output: str | None = typer.Option(None, "--output", "-o", help="Write template to this file path"),
):
    """Generate a JSON config skeleton for a destination connector."""
    try:
        client, base_url = build_authenticated_client()
    except (ConfigError, AuthError) as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(code=1)

    try:
        definitions = list(list_destination_definitions(client, base_url, workspace_id))
    except ServiceError as exc:
        console.print(f"[red]Error fetching destination definitions: {exc}[/red]")
        raise typer.Exit(code=1)
    finally:
        client.close()

    definition = _find_definition(definitions, definition_id, "destinationDefinitionId")
    if not definition:
        console.print(f"[red]Error: destination definition {definition_id!r} not found in workspace {workspace_id}[/red]")
        raise typer.Exit(code=1)

    spec = definition.get("spec") or definition.get("connectionSpecification") or {}
    skeleton = _spec_to_skeleton(spec)
    _write_output(skeleton, output)


@app.command("connection")
def connection_template(
    output: str | None = typer.Option(None, "--output", "-o", help="Write template to this file path"),
):
    """Generate a JSON skeleton for creating a connection."""
    skeleton = {
        "sourceId": "<source ID>",
        "destinationId": "<destination ID>",
        "name": "<connection name>",
        "schedule": {"scheduleType": "manual"},
        "namespaceDefinition": "source",
        "status": "active",
    }
    _write_output(skeleton, output)
