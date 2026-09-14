"""``dp db schema impact``: what breaks if this schema goes.

`schema drop` already shows what a schema *contains*. What it cannot show is
what elsewhere depends on it, and that is the half that surprises people: a
dashboard nobody opened this week, an ingestion that lands at 03:00. Both
systems already know, and neither is ever asked before the drop.

No database connection is opened. The question is not what is in the schema.

Superset and Airbyte are optional here in two senses. A platform may not run
one of them -- then that half is skipped and the other half still answers --
and a `dataplat[db]`-only install has no httpx at all, which is why the
imports are guarded rather than assumed.
"""

from __future__ import annotations

import json
import os

import typer
from rich import box
from rich.console import Console
from rich.table import Table

from dataplat.cli._exit import fail
from dataplat.cli._options import JsonOption
from dataplat.cli._render import cell
from dataplat.core.errors import AuthError, ConfigError, ServiceError
from dataplat.services.impact import (
    ConnectionRef,
    DatasetRef,
    DestinationRef,
    connections_into,
    datasets_referencing,
    destinations_writing_to,
)

try:
    from dataplat.services.airbyte.client import build_authenticated_client
    from dataplat.services.airbyte.connections import list_connections
    from dataplat.services.airbyte.destinations import list_destinations
    from dataplat.services.superset.client import (
        build_client,
        get_auth_config_from_env,
        login,
    )
    from dataplat.services.superset.datasets import iter_datasets

    HTTP_AREAS_AVAILABLE = True
except ImportError:  # pragma: no cover - dataplat[db] without [bi]/[ingest]
    HTTP_AREAS_AVAILABLE = False

console = Console()


def summarize(schemas: list[str]) -> tuple[list[str], list[str]]:
    """One line per schema something depends on, plus notes for what was skipped.

    Never raises. This is advisory, and a check that can block a drop is worse
    than no check at all: an operator who cannot drop a schema because an
    unrelated system is down will pass whatever flag silences it, and then
    never see the check again.
    """
    if not HTTP_AREAS_AVAILABLE:
        return [], ["dependants not checked: dataplat[bi,ingest] is not installed"]

    lines: list[str] = []
    notes: list[str] = []
    for schema in schemas:
        try:
            datasets, superset_note = _superset_datasets(schema)
            destinations, connections, airbyte_note = _airbyte_writers(schema)
        except Exception as exc:  # noqa: BLE001 - advisory, never fatal
            notes.append(f"{schema}: dependants could not be checked ({exc})")
            continue

        for note in (superset_note, airbyte_note):
            if note and note not in notes:
                notes.append(note)

        parts = []
        if datasets:
            parts.append(f"{len(datasets)} Superset dataset(s)")
        if destinations:
            parts.append(f"{len(destinations)} Airbyte destination(s)")
        if connections:
            parts.append(f"{len(connections)} connection(s) writing into it")
        if parts:
            lines.append(
                f"{schema}: {', '.join(parts)} — `dp db schema impact {schema}`"
            )
    return lines, notes


def _superset_datasets(schema: str) -> tuple[tuple[DatasetRef, ...], str | None]:
    """Datasets referencing ``schema``, or a reason there is no answer."""
    if not os.getenv("SUPERSET_BASE_URL"):
        return (), "superset: not configured (SUPERSET_BASE_URL unset)"

    cfg = get_auth_config_from_env()
    with build_client() as client:
        token = login(client, cfg.base_url, cfg.username, cfg.password)
        datasets = list(iter_datasets(client, cfg.base_url, token))
    return datasets_referencing(datasets, schema), None


def _airbyte_writers(
    schema: str,
) -> tuple[tuple[DestinationRef, ...], tuple[ConnectionRef, ...], str | None]:
    """Destinations landing in ``schema`` and the connections that use them."""
    if not os.getenv("AIRBYTE_BASE_URL"):
        return (), (), "airbyte: not configured (AIRBYTE_BASE_URL unset)"

    client, base_url = build_authenticated_client()
    try:
        destinations = destinations_writing_to(
            list(list_destinations(client, base_url)), schema
        )
        if not destinations:
            return (), (), None
        connections = connections_into(
            list(list_connections(client, base_url)),
            [d.destination_id for d in destinations],
        )
    finally:
        client.close()
    return destinations, connections, None


def impact_command(
    schema: str = typer.Argument(..., help="Schema name to check"),
    as_json: bool = JsonOption,
):
    """Report what outside the database depends on a schema."""
    if not HTTP_AREAS_AVAILABLE:
        fail(
            ConfigError(
                "This command reads Superset and Airbyte; install "
                "dataplat[bi,ingest] to use it."
            ),
            console=console,
        )

    try:
        datasets, superset_note = _superset_datasets(schema)
        destinations, connections, airbyte_note = _airbyte_writers(schema)
    except (AuthError, ConfigError, ServiceError) as exc:
        fail(exc, console=console)

    notes = [note for note in (superset_note, airbyte_note) if note]

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "schema": schema,
                    "datasets": [vars(d) for d in datasets],
                    "destinations": [vars(d) for d in destinations],
                    "connections": [vars(c) for c in connections],
                    "skipped": notes,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return

    console.print(f"\n[bold]What depends on schema[/bold] {cell(schema)}\n")

    if datasets:
        table = Table(
            show_header=True,
            header_style="bold cyan",
            box=box.SIMPLE_HEAVY,
            expand=True,
        )
        table.add_column("Superset dataset", style="cyan")
        table.add_column("Database")
        table.add_column("Found via", justify="right")
        for dataset in datasets:
            table.add_row(
                cell(dataset.name),
                cell(dataset.database),
                # Worth naming: a dataset found only through its SQL is one no
                # amount of clicking around Superset's schema filter reveals.
                "schema field" if dataset.via == "schema" else "SQL",
            )
        console.print(table)

    for destination in destinations:
        console.print(
            f"[cyan]Airbyte destination[/cyan] {cell(destination.name)} "
            f"[dim](database {cell(destination.database)})[/dim]"
        )
    for connection in connections:
        console.print(
            f"  [dim]connection[/dim] {cell(connection.name)} "
            f"[dim]({cell(connection.status)})[/dim]"
        )

    for note in notes:
        console.print(f"[dim]{cell(note)}[/dim]")

    total = len(datasets) + len(destinations) + len(connections)
    if total == 0:
        console.print("[green]Nothing found that references it.[/green]")
    else:
        console.print(f"\n[yellow]{total} reference(s)[/yellow]")
