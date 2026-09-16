"""``dp bi superset dashboards datasets``: what a dashboard reads, and whether
the other warehouse has it.

The question to ask BEFORE migrating anything. It writes nothing, so it can be
run against production while someone is still deciding.
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
from dataplat.core.errors import AuthError, ConfigError, ServiceError, ValidationError
from dataplat.core.redshift_ports import SOURCE_SYNCED, scan
from dataplat.services.superset.client import build_client
from dataplat.services.superset.client import login as _login
from dataplat.services.superset.dashboards import dashboard_charts
from dataplat.services.superset.databases import resolve_database_id
from dataplat.services.superset.datasets import get_dataset, iter_datasets
from dataplat.services.superset.repoint import DatasetKey, plan_migration

__all__ = ["datasets_command"]

console = Console()


def _read_source_datasets(
    client, base_url: str, token: str, dashboard: str
) -> list[dict]:
    charts = dashboard_charts(client, base_url, token, dashboard)
    ids = sorted(
        {
            int(chart["datasource_id"])
            for chart in charts
            if isinstance(chart.get("datasource_id"), int)
        }
    )
    return [get_dataset(client, base_url, token, dataset_id) for dataset_id in ids]


def datasets_command(
    dashboard: str = typer.Argument(..., help="Dashboard id or slug."),
    from_database: str | None = typer.Option(
        None,
        "--from-database",
        help="Treat only datasets on this connection as movable.",
    ),
    to_database: str | None = typer.Option(
        None, "--to-database", help="Check whether this connection has a counterpart."
    ),
    as_json: bool = JsonOption,
) -> None:
    """What a dashboard reads, and whether a target database has it."""
    base_url, username, password = load_auth_context(console)

    try:
        with build_client() as client:
            token = _login(client, base_url, username, password)
            sources = _read_source_datasets(client, base_url, token, dashboard)

            from_id = (
                resolve_database_id(client, base_url, token, from_database)
                if from_database
                else None
            )
            targets: list[dict] = []
            if to_database:
                to_id = resolve_database_id(client, base_url, token, to_database)
                targets = list(
                    iter_datasets(client, base_url, token, database_id=to_id)
                )
    except (AuthError, ServiceError, ConfigError, ValidationError) as exc:
        fail(exc, console=console)

    # Without a target there is nothing to check against, so every dataset is
    # simply listed. With one, the plan answers it -- and `from_id` may be
    # None, which means "every dataset is a candidate", the right default for
    # a report that is asked before anyone has named a source connection.
    if to_database is None:
        rows = [
            {
                "dataset": str(DatasetKey.of(d)),
                "source_id": d.get("id"),
                "virtual": bool(d.get("sql")),
                "target_id": None,
                "status": "unchecked",
            }
            for d in sources
        ]
    else:
        plan = plan_migration(
            source_datasets=sources,
            target_datasets=targets,
            from_database_id=from_id,
            overrides={},
        )
        groups = (
            (plan.matched, "exists"),
            (plan.to_create, "missing"),
            (plan.foreign, "other database"),
        )
        rows = [
            {
                "dataset": str(match.source_key),
                "source_id": match.source_id,
                "virtual": match.is_virtual,
                "target_id": match.target_id if status == "exists" else None,
                "status": status,
            }
            for matches, status in groups
            for match in matches
        ]

    findings = {
        int(d["id"]): scan(str(d.get("sql") or "")) for d in sources if d.get("sql")
    }

    if as_json:
        for row in rows:
            source_id = row["source_id"]
            row["port_findings"] = [
                {"kind": f.kind, "construct": f.construct, "message": f.message}
                for f in findings.get(int(source_id) if source_id else 0, ())
            ]
        typer.echo(json.dumps(rows, indent=2, ensure_ascii=False))
        return

    if not rows:
        console.print("[yellow]This dashboard reads no datasets[/yellow]")
        return

    table = Table(
        show_header=True, header_style="bold cyan", box=box.SIMPLE_HEAVY, expand=True
    )
    table.add_column("Dataset", style="cyan")
    table.add_column("Source", style="dim", no_wrap=True)
    table.add_column("Kind", no_wrap=True)
    table.add_column("On target", no_wrap=True)

    for row in sorted(rows, key=lambda r: str(r["dataset"])):
        on_target = (
            str(row["target_id"])
            if row["target_id"]
            else ("—" if row["status"] == "unchecked" else row["status"])
        )
        table.add_row(
            cell(row["dataset"]),
            cell(row["source_id"]),
            "virtual" if row["virtual"] else "physical",
            cell(on_target),
        )

    console.print(table)

    flagged = {k: v for k, v in findings.items() if v}
    if flagged:
        console.print(
            f"\n[yellow]Virtual SQL worth reading before it moves[/yellow] "
            f"[dim](port table synced {SOURCE_SYNCED})[/dim]"
        )
        for dataset_id, items in sorted(flagged.items()):
            console.print(f"  [cyan]dataset {dataset_id}[/cyan]")
            for finding in items:
                console.print(
                    f"    {cell(finding.kind)} {cell(finding.construct)} — "
                    f"{cell(finding.message, max_length=160)}"
                )
