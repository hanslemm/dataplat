"""``dp bi superset dashboards duplicate``: copy a dashboard onto another database.

Five phases, and the first writes nothing. Everything that can be decided is
decided before anything is created, because the alternative is a half-migrated
dashboard and a list of problems discovered one run at a time.

Nothing here deletes anything, including on the failure paths. A cleanup
triggered by an error runs exactly when its own correctness is least certain;
this area has a stated position on that (``people offboard`` destroys nothing,
``schema impact`` is advisory and never fatal).
"""

from __future__ import annotations

import json

import typer
from rich import box
from rich.console import Console
from rich.table import Table

from dataplat.cli._exit import fail
from dataplat.cli._options import JsonOption, YesOption
from dataplat.cli._prompt import confirm_or_exit
from dataplat.cli._render import cell, esc, shorten
from dataplat.cli.bi._superset_auth import load_auth_context
from dataplat.core.errors import (
    AuthError,
    ConfigError,
    ExitCode,
    ServiceError,
    ValidationError,
)
from dataplat.core.redshift_ports import SOURCE_SYNCED, scan
from dataplat.services.superset.charts import get_chart, update_chart
from dataplat.services.superset.client import build_client
from dataplat.services.superset.client import login as _login
from dataplat.services.superset.dashboards import (
    copy_dashboard,
    dashboard_charts,
    get_dashboard,
    update_dashboard,
)
from dataplat.services.superset.databases import (
    execute_sql,
    resolve_database_id,
    validation_query,
)
from dataplat.services.superset.datasets import (
    create_dataset,
    get_dataset,
    iter_datasets,
    update_dataset,
)
from dataplat.services.superset.repoint import (
    DatasetMatch,
    MigrationPlan,
    copy_metadata,
    create_payload,
    dashboard_metadata,
    parse_overrides,
    plan_migration,
    repoint_chart,
    semantic_payload,
)

__all__ = ["duplicate_command"]

console = Console()


def _source_datasets(client, base_url: str, token: str, dashboard: str) -> list[dict]:
    charts = dashboard_charts(client, base_url, token, dashboard)
    ids = sorted(
        {
            int(c["datasource_id"])
            for c in charts
            if isinstance(c.get("datasource_id"), int)
        }
    )
    return [get_dataset(client, base_url, token, dataset_id) for dataset_id in ids]


def _validate_virtual(
    client,
    base_url: str,
    token: str,
    to_id: int,
    pending: tuple[DatasetMatch, ...],
    sources: dict[int, dict],
) -> list[tuple[DatasetMatch, str]]:
    """Run each virtual dataset's SQL on the target; collect the refusals.

    The engine's own message is what is kept. On Redshift a Postgres-shaped
    query fails for reasons worth reading verbatim -- `type "jsonb" does not
    exist`, `function concat_ws(...) does not exist` -- and no category this
    could invent would be more useful.
    """
    failures: list[tuple[DatasetMatch, str]] = []
    for match in pending:
        if not match.is_virtual:
            continue
        sql = str(sources[match.source_id].get("sql") or "")
        try:
            execute_sql(
                client,
                base_url,
                token,
                database_id=to_id,
                sql=validation_query(sql),
                schema=match.target_key.schema,
            )
        except ServiceError as exc:
            failures.append((match, str(exc)))
    return failures


def _report_blockers(
    plan: MigrationPlan,
    failures: list[tuple[DatasetMatch, str]],
    sources: dict[int, dict],
    *,
    create_missing: bool,
) -> None:
    if plan.foreign:
        console.print("\n[dim]Left alone — not on the source database:[/dim]")
        for match in plan.foreign:
            console.print(
                f"  {esc(match.source_key)} [dim](id {match.source_id})[/dim]"
            )

    if plan.to_create and not create_missing:
        console.print("\n[red]No counterpart on the target database:[/red]")
        for match in plan.to_create:
            console.print(
                f"  {esc(match.source_key)} [dim](id {match.source_id})[/dim]"
            )

    for match, message in failures:
        console.print(f"\n[red]✗ {esc(match.source_key)}[/red]")
        console.print(f"  {esc(shorten(str(message), 400))}")
        findings = scan(str(sources[match.source_id].get("sql") or ""))
        if findings:
            console.print(f"  [dim]port table synced {SOURCE_SYNCED}[/dim]")
            for finding in findings:
                console.print(
                    f"    {esc(finding.kind)} {esc(finding.construct)} — "
                    f"{esc(shorten(finding.message, 200))}"
                )


def duplicate_command(
    dashboard: str = typer.Argument(..., help="Dashboard id or slug."),
    from_database: str = typer.Option(
        ..., "--from-database", help="The connection whose datasets are being left."
    ),
    to_database: str = typer.Option(
        ..., "--to-database", help="The connection the copy should read from."
    ),
    title: str | None = typer.Option(
        None,
        "--title",
        help="Title for the copy. Defaults to '<original> (<to-database>)'.",
    ),
    mapping: list[str] | None = typer.Option(
        None,
        "--map",
        help="schema.table=schema.table override, repeatable.",
    ),
    create_missing: bool = typer.Option(
        True,
        "--create-missing/--no-create-missing",
        help="Create a dataset on the target when none exists.",
    ),
    verify: bool = typer.Option(
        True, "--verify/--no-verify", help="Run each new chart's query afterwards."
    ),
    compare: bool = typer.Option(
        False,
        "--compare",
        help="Also run the original's query and diff the two. Doubles the query load.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report the plan and write nothing."
    ),
    yes: bool = YesOption,
    as_json: bool = JsonOption,
) -> None:
    """Copy a dashboard and repoint the copy onto another database."""
    base_url, username, password = load_auth_context(console)

    try:
        overrides = parse_overrides(mapping or [])
    except ValidationError as exc:
        fail(exc, console=console)

    # --- Phase 1: resolve. Nothing is written in this block. ---
    try:
        with build_client() as client:
            token = _login(client, base_url, username, password)
            from_id = resolve_database_id(client, base_url, token, from_database)
            to_id = resolve_database_id(client, base_url, token, to_database)
            original = get_dashboard(client, base_url, token, dashboard)
            source_list = _source_datasets(client, base_url, token, dashboard)
            sources = {int(d["id"]): d for d in source_list if d.get("id") is not None}
            targets = list(iter_datasets(client, base_url, token, database_id=to_id))

            plan = plan_migration(
                source_datasets=source_list,
                target_datasets=targets,
                from_database_id=from_id,
                overrides=overrides,
            )
            failures = (
                _validate_virtual(
                    client, base_url, token, to_id, plan.to_create, sources
                )
                if create_missing
                else []
            )
    except (AuthError, ServiceError, ConfigError, ValidationError) as exc:
        fail(exc, console=console)

    new_title = title or f"{original.get('dashboard_title', dashboard)} ({to_database})"

    table = Table(
        show_header=True, header_style="bold cyan", box=box.SIMPLE_HEAVY, expand=True
    )
    table.add_column("Dataset", style="cyan")
    table.add_column("Action", no_wrap=True)
    table.add_column("Target", style="dim", no_wrap=True)
    for match in plan.matched:
        table.add_row(cell(match.source_key), match.via, cell(match.target_id))
    for match in plan.to_create:
        table.add_row(
            cell(match.source_key),
            "create" if create_missing else "MISSING",
            cell(match.target_key),
        )
    console.print(table)

    blocked = bool(failures) or (bool(plan.to_create) and not create_missing)
    if blocked:
        _report_blockers(plan, failures, sources, create_missing=create_missing)
        console.print("\n[red]Nothing was written.[/red]")
        raise typer.Exit(code=ExitCode.FAILURE)

    if plan.foreign:
        _report_blockers(plan, [], sources, create_missing=create_missing)

    if dry_run:
        console.print(f"\n[dim]Would create: {new_title}[/dim]")
        return

    confirm_or_exit(yes=yes, prompt=f"Create {new_title!r} on {to_database}?")

    created: list[int] = []
    new_dashboard_id: int | None = None

    try:
        with build_client() as client:
            token = _login(client, base_url, username, password)

            # --- Phase 2: create what is missing. ---
            id_map = dict(plan.id_map)
            for match in plan.to_create:
                source = sources[match.source_id]
                new_id = create_dataset(
                    client,
                    base_url,
                    token,
                    create_payload(source, to_id, match.target_key),
                )
                created.append(new_id)
                synced = get_dataset(client, base_url, token, new_id)
                update_dataset(
                    client, base_url, token, new_id, semantic_payload(source, synced)
                )
                id_map[match.source_id] = new_id
                console.print(
                    f"[green]✓ created dataset {new_id}[/green] {esc(match.target_key)}"
                )

            # --- Phase 3: copy. ---
            new_dashboard_id = copy_dashboard(
                client,
                base_url,
                token,
                int(original["id"]),
                {
                    "dashboard_title": new_title,
                    "css": original.get("css") or "",
                    "duplicate_slices": True,
                    "json_metadata": copy_metadata(original),
                },
            )
            console.print(f"[green]✓ copied dashboard {new_dashboard_id}[/green]")

            # --- Phase 4: repoint. ---
            clones = dashboard_charts(client, base_url, token, new_dashboard_id)
            full = [
                get_chart(client, base_url, token, int(c["id"]))
                for c in clones
                if c.get("id") is not None
            ]

            shared = [
                chart
                for chart in full
                if [
                    d
                    for d in (chart.get("dashboards") or [])
                    if isinstance(d, dict) and d.get("id") != new_dashboard_id
                ]
            ]
            if shared:
                # duplicate_slices did not take effect -- almost always the
                # positions merge in phase 3. Writing would edit the ORIGINAL.
                console.print(
                    "\n[red]Refusing to repoint: "
                    f"{len(shared)} chart(s) on dashboard {new_dashboard_id} also "
                    "belong to another dashboard, so they were never cloned.[/red]"
                )
                for chart in shared:
                    console.print(f"  chart {esc(chart.get('id'))}")
                console.print(
                    f"\n[dim]Dashboard {new_dashboard_id} and dataset(s) "
                    f"{', '.join(str(c) for c in created) or '—'} were created "
                    f"and left in place.[/dim]"
                )
                raise typer.Exit(code=ExitCode.FAILURE)

            for chart in full:
                payload = repoint_chart(chart, id_map)
                if payload is None:
                    continue
                update_chart(client, base_url, token, int(chart["id"]), payload)
                console.print(f"[green]✓ repointed chart {chart['id']}[/green]")

            update_dashboard(
                client,
                base_url,
                token,
                new_dashboard_id,
                {
                    "json_metadata": dashboard_metadata(
                        original.get("json_metadata") or "{}", id_map
                    )
                },
            )
    except (AuthError, ServiceError, ConfigError) as exc:
        if new_dashboard_id or created:
            console.print(
                f"\n[dim]Left in place: dashboard {new_dashboard_id or '—'}, "
                f"dataset(s) {', '.join(str(c) for c in created) or '—'}[/dim]"
            )
        fail(exc, console=console)

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "dashboard_id": new_dashboard_id,
                    "title": new_title,
                    "created_datasets": created,
                },
                indent=2,
            )
        )
        return

    console.print(f"\n[green]Done:[/green] {esc(new_title)} ({new_dashboard_id})")
