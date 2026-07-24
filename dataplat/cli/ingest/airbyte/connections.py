"""Airbyte connections CLI commands."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import typer
from rich import box
from rich.console import Console
from rich.table import Table

from dataplat.cli.ingest.airbyte._common import airbyte_client
from dataplat.cli.ingest.airbyte._cursor import (
    parse_target_date,
    plan_cursor_rewrites,
)
from dataplat.cli.ingest.airbyte.enums import (
    ConnectionStatus,
    DataResidency,
    NamespaceDefinition,
    ScheduleType,
    SchemaUpdatesBehavior,
)
from dataplat.cli.ingest.airbyte.tui import TEXTUAL_AVAILABLE, ConnectionsApp
from dataplat.core.errors import AuthError, ConfigError
from dataplat.services.airbyte.client import (
    build_authenticated_client,
    split_cron_timezone,
    validate_cron_expression,
)
from dataplat.services.airbyte.connections import (
    build_web_backend_updates,
    connection_has_active_streams,
    create_connection,
    delete_connection,
    get_connection,
    get_connection_state,
    get_job,
    list_connections,
    patch_connection,
    trigger_sync_job,
    update_connection_state,
    update_connection_web_backend,
)
from dataplat.services.airbyte.jobs import list_jobs
from dataplat.services.airbyte.tags import TagResolver, merge_tags

app = typer.Typer(name="connections", help="Manage Airbyte connections", no_args_is_help=True)
console = Console()

YesOption = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt.")


def _confirm_bulk(summary: str, yes: bool) -> None:
    """Require confirmation before a bulk mutation. Non-interactive needs --yes."""
    if yes:
        return
    console.print(f"[yellow]{summary}[/yellow]")
    if sys.stdin.isatty():
        if typer.confirm("Proceed?", default=False):
            return
        console.print("[yellow]Aborted.[/yellow]")
        raise typer.Exit(code=1)
    console.print(
        "[red]Error: refusing bulk operation without confirmation. "
        "Pass --yes/-y in non-interactive contexts.[/red]"
    )
    raise typer.Exit(code=1)

BUSY_STATUSES = {"running", "pending", "incomplete"}


def _connection_is_busy(client, base_url, conn_id: str) -> bool:
    """True if the connection has a running/pending/incomplete job."""
    for job in list_jobs(client, base_url, connection_id=conn_id, limit=5):
        if (job.get("status") or "").lower() in BUSY_STATUSES:
            return True
    return False


def _backup_path(backup_dir: str, conn_id: str) -> Path:
    """Path of the per-connection state backup file: <backup-dir>/<conn-id>.json."""
    return Path(backup_dir) / f"{conn_id}.json"


def _print_cursor_plan(conn_label: str, actions: list[dict]) -> None:
    """Render the per-stream rewrite/skip plan for one connection."""
    console.print(f"- {conn_label}")
    if not actions:
        console.print("  [dim]no cursor state to change[/dim]")
        return
    for a in actions:
        stream = f"{a['namespace']}.{a['stream']}" if a.get("namespace") else a["stream"]
        action = a["action"]
        if action.startswith("rewrite") or action == "reset:count":
            console.print(
                f"  [green]{action}[/green] {stream}:{a['key']} {a['old']} -> {a['new']}"
            )
        else:
            console.print(
                f"  [yellow]{action}[/yellow] {stream}:{a['key']} ({a['old']!r})"
            )


def _matches_filters(
    connection: dict,
    source_id: str | None,
    destination_id: str | None,
) -> bool:
    if connection.get("status") != "active":
        return False
    if source_id and connection.get("sourceId") != source_id:
        return False
    return not (
        destination_id and connection.get("destinationId") != destination_id
    )

@app.command("list")
def list_connections_cmd(
    status_filter: ConnectionStatus | None = typer.Option(
        None,
        "--status",
        "-s",
        help="Filter by connection status",
    ),
    workspace_id: str | None = typer.Option(
        None, "--workspace-id", "-w", help="Filter by workspace ID"
    ),
    source_id: str | None = typer.Option(
        None, "--source-id", help="Filter by source ID"
    ),
    destination_id: str | None = typer.Option(
        None, "--destination-id", help="Filter by destination ID"
    ),
    all_columns: bool = typer.Option(
        False,
        "--all-columns",
        help="Show all top-level fields in the table",
    ),
    tui: bool = typer.Option(
        False,
        "--tui",
        help="Show output in a Textual UI",
    ),
    tui_export_file: str = typer.Option(
        "connections_filtered.json",
        "--tui-export-file",
        help="File path for TUI export",
    ),
    pager: bool = typer.Option(
        False,
        "--pager",
        help="Show output in an interactive pager",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit connections as JSON on stdout"
    ),
    output_file: str | None = typer.Option(
        None,
        "--output-file",
        help="Also write the connections JSON to this file path",
    ),
    no_active_streams: bool = typer.Option(
        False,
        "--no-active-streams",
        help="Show only active connections with no selected streams",
    ),
):
    """List all Airbyte connections."""
    try:
        client, base_url = build_authenticated_client()
    except (ConfigError, AuthError) as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(code=1)

    if not as_json:
        console.print("[blue]Fetching Airbyte connections...[/blue]\n")

    try:
        connections = []
        for conn in list_connections(client, base_url):
            # Filter by status if specified
            if status_filter and conn.get("status") != status_filter.value:
                continue
            if workspace_id and (
                conn.get("workspaceId") or conn.get("workspace_id")
            ) != workspace_id:
                continue
            if source_id and conn.get("sourceId") != source_id:
                continue
            if destination_id and conn.get("destinationId") != destination_id:
                continue

            if no_active_streams:
                if conn.get("status") != ConnectionStatus.active.value:
                    continue
                conn_id = conn.get("connectionId") or conn.get("connection_id")
                if not conn_id:
                    continue
                try:
                    detail = get_connection(client, base_url, conn_id)
                except Exception as exc:
                    console.print(
                        f"[yellow]Warning: could not inspect {conn_id}: "
                        f"{exc}[/yellow]"
                    )
                    continue
                if connection_has_active_streams(detail):
                    continue

            connections.append(conn)

        if not connections:
            console.print("[yellow]No connections found[/yellow]")
            return

        if output_file:
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(connections, f, indent=2)
            console.print(f"[green]Saved connections to {output_file}[/green]")

        if as_json:
            typer.echo(json.dumps(connections, indent=2, ensure_ascii=False))
            return

        # Build columns and rows
        def format_cell(value: object) -> str:
            if value is None:
                return "N/A"
            if isinstance(value, (dict, list)):
                return json.dumps(value, ensure_ascii=False)
            return str(value)

        if all_columns:
            columns = sorted({k for conn in connections for k in conn})
            rows = [
                [format_cell(conn.get(key)) for key in columns] for conn in connections
            ]
        else:
            columns = ["Name", "Connection ID", "Schedule", "Status"]
            rows = []
            for conn in connections:
                name = conn.get("name", "N/A")
                conn_id = conn.get("connectionId") or conn.get("connection_id", "N/A")
                status = conn.get("status", "N/A")
                schedule = conn.get("schedule") or {}
                schedule_type = schedule.get("scheduleType")
                cron_expr = schedule.get("cronExpression")
                cron_tz = schedule.get("cronTimeZone")
                basic_timing = schedule.get("basicTiming")
                if schedule_type and cron_expr:
                    schedule_display = f"{schedule_type}:{cron_expr}"
                    if cron_tz:
                        schedule_display = f"{schedule_display} {cron_tz}"
                elif schedule_type == "basic" and basic_timing:
                    schedule_display = f"basic:{basic_timing}"
                elif schedule_type:
                    schedule_display = str(schedule_type)
                else:
                    schedule_display = "N/A"
                rows.append([name, conn_id, schedule_display, status])

        if tui:
            if not TEXTUAL_AVAILABLE:
                console.print(
                    "[red]Textual is not installed. Install with: pip install textual[/red]"
                )
                raise typer.Exit(code=1)

            ConnectionsApp(columns, rows, tui_export_file).run()
        else:
            table = Table(
                show_header=True,
                header_style="bold cyan",
                box=box.SIMPLE_HEAVY,
                show_lines=False,
                expand=True,
            )
            term_width = console.size.width
            if all_columns:
                # Distribute width across columns, keep a sensible minimum
                per_col = max(12, (term_width - 4) // max(1, len(columns)))
                for col in columns:
                    table.add_column(col, overflow="ellipsis", max_width=per_col)
            else:
                # Fit key columns to terminal width
                status_w = 10
                id_w = 36
                schedule_w = max(20, min(44, (term_width - 6) // 3))
                name_w = max(20, term_width - (status_w + id_w + schedule_w + 6))

                table.add_column(
                    "Name",
                    style="cyan",
                    overflow="ellipsis",
                    max_width=name_w,
                )
                table.add_column(
                    "Connection ID",
                    style="dim",
                    overflow="ellipsis",
                    max_width=id_w,
                    no_wrap=True,
                )
                table.add_column(
                    "Schedule",
                    style="green",
                    overflow="ellipsis",
                    max_width=schedule_w,
                )
                table.add_column(
                    "Status",
                    style="magenta",
                    overflow="ellipsis",
                    max_width=status_w,
                    no_wrap=True,
                )
            for row in rows:
                table.add_row(*[str(v) for v in row])

            if pager:
                with console.pager():
                    console.print(table)
                    console.print(
                        f"\n[dim]Total: {len(connections)} connection(s)[/dim]"
                    )
            else:
                console.print(table)
                console.print(f"\n[dim]Total: {len(connections)} connection(s)[/dim]")

    except Exception as e:
        console.print(f"[red]Error fetching connections: {e}[/red]")
        raise typer.Exit(code=1)
    finally:
        client.close()


@app.command()
def update(
    connection_id: str | None = typer.Option(
        None,
        "--connection-id",
        "-c",
        help="Specific connection ID to update (if not provided, updates all active connections)",
    ),
    source_id: str | None = typer.Option(
        None,
        "--source-id",
        help="Only update connections with this source ID",
    ),
    destination_id: str | None = typer.Option(
        None,
        "--destination-id",
        help="Only update connections with this destination ID",
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        help="Connection name",
    ),
    schedule_type: ScheduleType | None = typer.Option(
        None,
        "--schedule-type",
        help="Schedule type",
    ),
    cron: str | None = typer.Option(
        None,
        "--cron",
        help="Quartz cron expression (required if schedule-type=cron). Optional timezone suffix is allowed (e.g. '0 0 0,12 ? * * Europe/Berlin')",
    ),
    cron_time_zone: str | None = typer.Option(
        None,
        "--cron-timezone",
        help="Timezone for cron schedule (e.g. Europe/Berlin)",
    ),
    data_residency: DataResidency | None = typer.Option(
        None,
        "--data-residency",
        help="Data residency location",
    ),
    namespace_definition: NamespaceDefinition | None = typer.Option(
        None,
        "--namespace-definition",
        help="Namespace definition",
    ),
    namespace_format: str | None = typer.Option(
        None,
        "--namespace-format",
        help="Namespace format template (used with custom_format)",
    ),
    prefix: str | None = typer.Option(
        None,
        "--prefix",
        help="Prefix to add to table names",
    ),
    non_breaking_schema_updates_behavior: SchemaUpdatesBehavior | None = typer.Option(
        None,
        "--non-breaking-schema-updates-behavior",
        "-b",
        help="Schema update behavior",
    ),
    status: ConnectionStatus | None = typer.Option(
        None,
        "--status",
        "-s",
        help="Connection status",
    ),
    tags: list[str] | None = typer.Option(
        None,
        "--tag",
        help="Add tag by name (repeatable). Tags are applied via web_backend endpoint.",
    ),
    tag_from_cron: bool = typer.Option(
        False,
        "--tag-from-cron",
        help="Add a tag containing the cron string when updating cron",
    ),
    use_web_backend: bool = typer.Option(
        False,
        "--use-web-backend",
        help="Use /api/v1/web_backend/connections/update endpoint",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview changes without applying them",
    ),
    sleep: float = typer.Option(
        0.0,
        "--sleep",
        help="Sleep duration (seconds) between updates (only for bulk updates)",
    ),
    yes: bool = YesOption,
):
    """Update Airbyte connection(s) with specified parameters."""
    # Auto-infer schedule_type from cron if not explicitly provided
    if cron and not schedule_type:
        schedule_type = ScheduleType.cron

    # Validate inputs
    if schedule_type == ScheduleType.cron and not cron:
        console.print("[red]Error: --cron required when --schedule-type=cron[/red]")
        raise typer.Exit(code=2)

    if cron and not validate_cron_expression(cron):
        console.print(f"[red]Error: Invalid cron expression: {cron}[/red]")
        raise typer.Exit(code=2)

    if cron_time_zone:
        try:
            ZoneInfo(cron_time_zone)
        except Exception:
            console.print(f"[red]Error: Invalid cron timezone: {cron_time_zone}[/red]")
            raise typer.Exit(code=2)

    cron_tz = None
    if cron:
        _, cron_tz = split_cron_timezone(cron)
    if (cron_time_zone or cron_tz) and not use_web_backend:
        console.print(
            "[red]Error: Cron timezone updates require --use-web-backend[/red]"
        )
        raise typer.Exit(code=2)

    # Build updates dict
    updates: dict = {}

    if name:
        updates["name"] = name

    if schedule_type:
        updates["schedule"] = {"scheduleType": schedule_type.value}
        if schedule_type == ScheduleType.cron and cron:
            cron_expr, cron_tz = split_cron_timezone(cron)
            if cron_tz and cron_time_zone:
                console.print(
                    "[yellow]Ignoring timezone in --cron because --cron-timezone was provided[/yellow]"
                )
            # Always send timezone separately (never inline in cronExpression)
            updates["schedule"]["cronExpression"] = cron_expr
            if cron_time_zone:
                updates["schedule"]["cronTimeZone"] = cron_time_zone
            elif cron_tz:
                updates["schedule"]["cronTimeZone"] = cron_tz

    if data_residency:
        updates["dataResidency"] = data_residency.value

    if namespace_definition:
        updates["namespaceDefinition"] = namespace_definition.value

    if namespace_format is not None:
        updates["namespaceFormat"] = namespace_format

    if prefix is not None:  # Allow empty string to clear prefix
        updates["prefix"] = prefix

    if non_breaking_schema_updates_behavior:
        updates["nonBreakingSchemaUpdatesBehavior"] = (
            non_breaking_schema_updates_behavior.value
        )

    if status:
        updates["status"] = status.value

    tag_names = [t.strip() for t in (tags or []) if t and t.strip()]
    if tag_from_cron:
        if not cron:
            console.print("[red]Error: --tag-from-cron requires --cron[/red]")
            raise typer.Exit(code=2)
        cron_expr, cron_tz = split_cron_timezone(cron)
        tz = cron_time_zone or cron_tz
        cron_tag = f"{cron_expr} {tz}" if tz else cron_expr
        tag_names.append(cron_tag)

    if not updates and not tag_names:
        console.print("[red]Error: No update parameters provided[/red]")
        raise typer.Exit(code=2)

    if tag_names and not use_web_backend:
        console.print(
            "[red]Error: Tags can only be updated with --use-web-backend[/red]"
        )
        raise typer.Exit(code=2)

    web_backend_updates = build_web_backend_updates(updates)
    if use_web_backend and not (web_backend_updates or tag_names):
        console.print(
            "[red]Error: No compatible update parameters for web_backend endpoint[/red]"
        )
        raise typer.Exit(code=2)

    try:
        client, base_url = build_authenticated_client()
    except (ConfigError, AuthError) as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(code=1)

    # Update connection(s)
    updated = 0

    try:
        tags_resolver = TagResolver(client, base_url)

        def _tags_update_for(conn: dict) -> dict:
            """Merge requested tag names into a connection's web-backend update."""
            workspace = conn.get("workspaceId") or conn.get("workspace_id")
            new_tags = [tags_resolver.ensure(name, workspace) for name in tag_names]
            existing_tags = conn.get("tags") or []
            return {
                **web_backend_updates,
                "tags": merge_tags(existing_tags, new_tags),
            }

        if connection_id:
            # Update single connection
            console.print(f"[blue]Updating connection {connection_id}...[/blue]\n")

            try:
                conn = get_connection(client, base_url, connection_id)
                conn_name = conn.get("name", "")
                if source_id and conn.get("sourceId") != source_id:
                    console.print(
                        f"[yellow]Skipping: sourceId {conn.get('sourceId')} does not match {source_id}[/yellow]"
                    )
                    raise typer.Exit(code=0)
                if destination_id and conn.get("destinationId") != destination_id:
                    console.print(
                        f"[yellow]Skipping: destinationId {conn.get('destinationId')} does not match {destination_id}[/yellow]"
                    )
                    raise typer.Exit(code=0)
                console.print(f"- {conn_name} ({connection_id})")
                web_backend_updates_for_conn = web_backend_updates
                if use_web_backend and tag_names:
                    web_backend_updates_for_conn = _tags_update_for(conn)

                console.print(
                    f"  Updates: {web_backend_updates_for_conn if use_web_backend else updates}"
                )

                if not dry_run:
                    if use_web_backend:
                        update_connection_web_backend(
                            client,
                            base_url,
                            connection_id,
                            web_backend_updates_for_conn,
                        )
                    else:
                        patch_connection(client, base_url, connection_id, updates)
                    updated = 1
                    console.print("  [green]✓ Updated[/green]")
                else:
                    console.print("  [dim](dry run - no changes made)[/dim]")

            except typer.Exit:
                # Clean skips/exits must not be re-reported as errors.
                raise
            except Exception as e:
                console.print(f"[red]Error updating connection: {e}[/red]")
                raise typer.Exit(code=1)

        else:
            # Update all active connections
            targets = [
                c
                for c in list_connections(client, base_url)
                if _matches_filters(c, source_id, destination_id)
            ]
            if not targets:
                console.print("[yellow]No matching active connections.[/yellow]")
                raise typer.Exit(code=0)
            if not dry_run:
                _confirm_bulk(
                    f"About to update {len(targets)} active connection(s).", yes
                )
            console.print("[blue]Updating all active connections...[/blue]\n")

            for connection in targets:
                conn_id = connection.get("connectionId") or connection.get(
                    "connection_id"
                )
                conn_name = connection.get("name", "")
                console.print(f"- {conn_name} ({conn_id})")

                updates_for_conn = updates
                web_backend_updates_for_conn = web_backend_updates
                if use_web_backend and tag_names:
                    web_backend_updates_for_conn = _tags_update_for(connection)

                if dry_run:
                    console.print(
                        f"  Updates: {web_backend_updates_for_conn if use_web_backend else updates_for_conn}"
                    )
                    console.print("  [dim](dry run - no changes made)[/dim]")
                    continue

                try:
                    if use_web_backend:
                        update_connection_web_backend(
                            client, base_url, conn_id, web_backend_updates_for_conn
                        )
                    else:
                        patch_connection(client, base_url, conn_id, updates_for_conn)
                    updated += 1
                    if sleep:
                        time.sleep(sleep)
                except Exception as e:
                    console.print(f"  [yellow]Warning: Failed to update: {e}[/yellow]")

        console.print(f"\n[green]Updated: {updated} (dry-run={dry_run})[/green]")

    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Error updating connections: {e}[/red]")
        raise typer.Exit(code=1)
    finally:
        client.close()


@app.command("set-cursor")
def set_cursor(
    to: str | None = typer.Option(
        None,
        "--to",
        help="Target date/timestamp for date cursors, e.g. 2024-01-01 "
        "(only ISO date-string cursors are rewritten; numeric cursors are skipped).",
    ),
    xmin: int | None = typer.Option(
        None,
        "--xmin",
        help="Set xmin cursors to this exact transaction id (e.g. 0 to re-read all).",
    ),
    xmin_factor: float | None = typer.Option(
        None,
        "--xmin-factor",
        help="Scale xmin cursors: new xid = round(old * factor), e.g. 0.1 rewinds to 10%.",
    ),
    connection_id: str | None = typer.Option(
        None, "--connection-id", "-c", help="Operate on a single connection."
    ),
    source_id: str | None = typer.Option(
        None, "--source-id", help="Only connections with this source ID."
    ),
    destination_id: str | None = typer.Option(
        None, "--destination-id", help="Only connections with this destination ID."
    ),
    force: bool = typer.Option(
        False, "--force", help="Write even if the connection has a running job."
    ),
    only_rewind: bool = typer.Option(
        False,
        "--only-rewind",
        help="Never move a cursor forward: skip date cursors currently earlier "
        "than --to and xmin rewrites that would raise the xid (a forward move "
        "makes the next sync skip records).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview the plan without writing."
    ),
    sync: bool = typer.Option(
        False,
        "--sync",
        help="Trigger a sync on each connection whose state was rewritten.",
    ),
    backup: bool = typer.Option(
        False,
        "--backup",
        help="Before writing, save each connection's current state to "
        "<backup-dir>/<connection-id>.json.",
    ),
    backup_dir: str = typer.Option(
        ".",
        "--backup-dir",
        help="Directory for --backup state files (created if missing).",
    ),
    sleep: float = typer.Option(
        0.0, "--sleep", help="Sleep (seconds) between connections in bulk mode."
    ),
    yes: bool = YesOption,
):
    """Rewrite cursor state values across connection(s).

    Date-shaped cursors are rewritten to --to (ISO date/datetime string, format
    preserved). xmin cursors (Postgres transaction-id state) are set to --xmin
    (absolute) or scaled by --xmin-factor (new xid = round(old * factor)); a
    calendar date can't drive them. Numeric / epoch / CDC ("opaque") cursors are
    never touched. Pass --to together with an xmin flag to fix both kinds in one
    run. At least one of --to / --xmin / --xmin-factor is required.

    With --only-rewind, cursors are never moved forward in time: streams whose
    date cursor is already earlier than --to (or whose xmin would increase) are
    skipped, since advancing a cursor makes the next sync silently skip records.

    With --sync, a sync job is triggered on each connection whose state was
    actually rewritten (connections left unchanged are not synced). With --backup,
    each connection's pre-change state is saved to <backup-dir>/<connection-id>.json
    before it is written.
    """
    if to is None and xmin is None and xmin_factor is None:
        console.print(
            "[red]Error: provide at least one of --to, --xmin or --xmin-factor.[/red]"
        )
        raise typer.Exit(code=2)
    if xmin is not None and xmin_factor is not None:
        console.print(
            "[red]Error: --xmin and --xmin-factor are mutually exclusive.[/red]"
        )
        raise typer.Exit(code=2)
    if xmin is not None and xmin < 0:
        console.print("[red]Error: --xmin must be >= 0.[/red]")
        raise typer.Exit(code=2)
    if xmin_factor is not None and xmin_factor < 0:
        console.print("[red]Error: --xmin-factor must be >= 0.[/red]")
        raise typer.Exit(code=2)

    target = None
    if to is not None:
        target = parse_target_date(to)
        if target is None:
            console.print(
                f"[red]Error: --to must be an ISO date/timestamp (got {to!r})[/red]"
            )
            raise typer.Exit(code=2)

    with airbyte_client() as (client, base_url):
        # Resolve target connections.
        if connection_id:
            conn = get_connection(client, base_url, connection_id)
            if source_id and conn.get("sourceId") != source_id:
                console.print(
                    f"[yellow]Skipping: sourceId {conn.get('sourceId')} != {source_id}[/yellow]"
                )
                raise typer.Exit(code=0)
            if destination_id and conn.get("destinationId") != destination_id:
                console.print(
                    f"[yellow]Skipping: destinationId {conn.get('destinationId')} != {destination_id}[/yellow]"
                )
                raise typer.Exit(code=0)
            targets = [conn]
        else:
            targets = [
                c
                for c in list_connections(client, base_url)
                if _matches_filters(c, source_id, destination_id)
            ]

        if not targets:
            console.print("[yellow]No matching active connections.[/yellow]")
            raise typer.Exit(code=0)

        ops = []
        if target is not None:
            ops.append(f"date cursors -> {target.isoformat()}")
        if xmin is not None:
            ops.append(f"xmin -> {xmin}")
        if xmin_factor is not None:
            ops.append(f"xmin *= {xmin_factor}")
        if only_rewind:
            ops.append("only-rewind")
        op_desc = "; ".join(ops)

        if not dry_run:
            _confirm_bulk(
                f"About to rewrite cursors ({op_desc}) on up to {len(targets)} "
                f"connection(s). Editing state on a running connection is unsafe; "
                f"busy connections are skipped unless --force.",
                yes,
            )

        rewritten = 0
        touched_connections = 0
        synced = 0
        for conn in targets:
            conn_id = conn.get("connectionId") or conn.get("connection_id")
            if not conn_id:
                continue
            conn_id = str(conn_id)
            conn_label = f"{conn.get('name', '')} ({conn_id})"
            try:
                if not force and _connection_is_busy(client, base_url, conn_id):
                    console.print(f"- {conn_label}")
                    console.print("  [yellow]skip: busy (running job)[/yellow]")
                    continue

                state = get_connection_state(client, base_url, conn_id)
                new_state, actions = plan_cursor_rewrites(
                    state, target, xmin_value=xmin, xmin_factor=xmin_factor,
                    only_rewind=only_rewind,
                )
                _print_cursor_plan(conn_label, actions)

                n_rewrite = sum(1 for a in actions if a["action"].startswith("rewrite"))
                if dry_run:
                    console.print("  [dim](dry run - no changes made)[/dim]")
                    if backup and n_rewrite:
                        console.print(
                            f"  [dim](dry run - would back up state to "
                            f"{_backup_path(backup_dir, conn_id)})[/dim]"
                        )
                    if sync and n_rewrite:
                        console.print("  [dim](dry run - would trigger sync)[/dim]")
                    rewritten += n_rewrite
                    continue
                if n_rewrite == 0:
                    continue

                if backup:
                    path = _backup_path(backup_dir, conn_id)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(
                        json.dumps(state, indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    console.print(f"  [green]✓ Backed up state to {path}[/green]")

                update_connection_state(client, base_url, conn_id, new_state)
                console.print(f"  [green]✓ Wrote {n_rewrite} cursor(s)[/green]")
                rewritten += n_rewrite
                touched_connections += 1

                if sync:
                    job = trigger_sync_job(client, base_url, conn_id)
                    job_id = job.get("id") or job.get("jobId") or job.get("job_id")
                    console.print(
                        "  [green]✓ Sync triggered[/green]"
                        + (f" (jobId={job_id})" if job_id else "")
                    )
                    synced += 1
                if sleep:
                    time.sleep(sleep)
            except typer.Exit:
                raise
            except Exception as exc:  # per-connection isolation
                console.print(f"  [yellow]Warning: failed on {conn_label}: {exc}[/yellow]")

        summary = (
            f"\n[green]Rewrote {rewritten} cursor(s) across "
            f"{touched_connections} connection(s)"
        )
        if sync:
            summary += f", synced {synced}"
        summary += f" (dry-run={dry_run})[/green]"
        console.print(summary)


@app.command()
def sync(
    connection_id: str | None = typer.Option(
        None, "--connection-id", "-c", help="Connection ID to sync"
    ),
    source_id: str | None = typer.Option(
        None,
        "--source-id",
        help="Only sync connections with this source ID",
    ),
    destination_id: str | None = typer.Option(
        None,
        "--destination-id",
        help="Only sync connections with this destination ID",
    ),
    wait: bool = typer.Option(False, "--wait", help="Wait for sync job to finish"),
    poll_interval: float = typer.Option(
        5.0, "--poll-interval", help="Polling interval in seconds"
    ),
    timeout: int | None = typer.Option(
        None, "--timeout", help="Timeout in seconds when waiting"
    ),
    sleep: float = typer.Option(
        1.0,
        "--sleep",
        help="Sleep duration (seconds) between syncs (only for bulk sync)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="List the connections that would be synced without triggering anything",
    ),
    yes: bool = YesOption,
):
    """Trigger a sync for a connection."""
    try:
        client, base_url = build_authenticated_client()
    except (ConfigError, AuthError) as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(code=1)

    if wait and not connection_id:
        console.print("[red]--wait is only supported with --connection-id[/red]")
        raise typer.Exit(code=2)

    try:
        if connection_id and dry_run:
            console.print(f"[dim](dry run) Would sync {connection_id}[/dim]")
            raise typer.Exit(code=0)
        if connection_id:
            console.print(f"[blue]Triggering sync for {connection_id}...[/blue]")
            job = trigger_sync_job(client, base_url, connection_id)
            job_id = job.get("id") or job.get("jobId") or job.get("job_id")
            if job_id:
                console.print(f"[green]✓ Sync job started[/green] (jobId={job_id})")
            else:
                console.print("[green]✓ Sync job started[/green]")

            if wait:
                start = time.time()
                while True:
                    if timeout and (time.time() - start) > timeout:
                        console.print("[red]Timeout waiting for job to finish[/red]")
                        raise typer.Exit(code=1)

                    if not job_id:
                        console.print("[red]Cannot wait: job id not returned[/red]")
                        raise typer.Exit(code=1)

                    details = get_job(client, base_url, str(job_id))
                    status = details.get("status") or details.get("jobStatus")
                    if status:
                        console.print(f"[dim]Status: {status}[/dim]")

                    if status in {"succeeded", "failed", "cancelled", "canceled"}:
                        console.print(
                            f"[green]Job finished with status: {status}[/green]"
                        )
                        break
                    time.sleep(poll_interval)
        else:
            targets = [
                c
                for c in list_connections(client, base_url)
                if _matches_filters(c, source_id, destination_id)
            ]
            if not targets:
                console.print("[yellow]No matching active connections.[/yellow]")
                raise typer.Exit(code=0)
            if dry_run:
                console.print(
                    f"[dim](dry run) Would sync {len(targets)} connection(s):[/dim]"
                )
                for connection in targets:
                    conn_id = connection.get("connectionId") or connection.get(
                        "connection_id"
                    )
                    console.print(f"- {connection.get('name', '')} ({conn_id})")
                raise typer.Exit(code=0)

            _confirm_bulk(
                f"About to trigger a sync on {len(targets)} active connection(s).",
                yes,
            )
            console.print("[blue]Triggering sync for active connections...[/blue]")
            for connection in targets:
                conn_id = connection.get("connectionId") or connection.get(
                    "connection_id"
                )
                conn_name = connection.get("name", "")
                console.print(f"- {conn_name} ({conn_id})")
                try:
                    trigger_sync_job(client, base_url, conn_id)
                    if sleep:
                        time.sleep(sleep)
                except Exception as e:
                    console.print(f"  [yellow]Warning: Failed to sync: {e}[/yellow]")
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Error triggering sync: {e}[/red]")
        raise typer.Exit(code=1)
    finally:
        client.close()


@app.command("get")
def get_connection_cmd(
    connection_id: str = typer.Option(..., "--connection-id", "-c", help="Connection ID"),
):
    """Get connection details."""
    try:
        client, base_url = build_authenticated_client()
    except (ConfigError, AuthError) as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(code=1)

    try:
        conn = get_connection(client, base_url, connection_id)
        console.print(json.dumps(conn, indent=2, ensure_ascii=False))
    except Exception as exc:
        console.print(f"[red]Error getting connection: {exc}[/red]")
        raise typer.Exit(code=1)
    finally:
        client.close()


@app.command("create")
def create_connection_cmd(
    source_id: str = typer.Option(..., "--source-id", help="Source ID"),
    destination_id: str = typer.Option(..., "--destination-id", help="Destination ID"),
    name: str | None = typer.Option(None, "--name", "-n", help="Connection name"),
    schedule_type: ScheduleType | None = typer.Option(
        None, "--schedule-type", help="Schedule type"
    ),
    cron: str | None = typer.Option(
        None,
        "--cron",
        help="Quartz cron expression (required if schedule-type=cron). Optional timezone suffix allowed.",
    ),
    namespace_definition: NamespaceDefinition | None = typer.Option(
        None, "--namespace-definition", help="Namespace definition"
    ),
    status: ConnectionStatus | None = typer.Option(
        None, "--status", "-s", help="Connection status"
    ),
):
    """Create a new Airbyte connection."""
    # Auto-infer schedule_type from cron if not explicitly provided
    if cron and not schedule_type:
        schedule_type = ScheduleType.cron

    if schedule_type == ScheduleType.cron and not cron:
        console.print("[red]Error: --cron required when --schedule-type=cron[/red]")
        raise typer.Exit(code=2)

    if cron and not validate_cron_expression(cron):
        console.print(f"[red]Error: Invalid cron expression: {cron}[/red]")
        raise typer.Exit(code=2)

    schedule: dict | None = None
    if schedule_type:
        schedule = {"scheduleType": schedule_type.value}
        if schedule_type == ScheduleType.cron and cron:
            cron_expr, cron_tz = split_cron_timezone(cron)
            schedule["cronExpression"] = cron_expr
            if cron_tz:
                schedule["cronTimeZone"] = cron_tz

    try:
        client, base_url = build_authenticated_client()
    except (ConfigError, AuthError) as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(code=1)

    try:
        conn = create_connection(
            client,
            base_url,
            source_id=source_id,
            destination_id=destination_id,
            name=name,
            schedule=schedule,
            namespace_definition=namespace_definition.value if namespace_definition else None,
            status=status.value if status else None,
        )
        console.print(json.dumps(conn, indent=2, ensure_ascii=False))
    except Exception as exc:
        console.print(f"[red]Error creating connection: {exc}[/red]")
        raise typer.Exit(code=1)
    finally:
        client.close()


@app.command("delete")
def delete_connection_cmd(
    connection_id: str = typer.Option(..., "--connection-id", "-c", help="Connection ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
):
    """Delete an Airbyte connection."""
    if not yes:
        typer.confirm(f"Delete connection {connection_id}?", abort=True)

    try:
        client, base_url = build_authenticated_client()
    except (ConfigError, AuthError) as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(code=1)

    try:
        delete_connection(client, base_url, connection_id)
        console.print(f"[green]Connection {connection_id} deleted[/green]")
    except Exception as exc:
        console.print(f"[red]Error deleting connection: {exc}[/red]")
        raise typer.Exit(code=1)
    finally:
        client.close()


@app.command("refresh")
def refresh(
    connection_id: str | None = typer.Option(
        None, "--connection-id", "-c", help="Connection ID to refresh."
    ),
    source_id: str | None = typer.Option(
        None, "--source-id", help="Only refresh connections with this source ID."
    ),
    destination_id: str | None = typer.Option(
        None, "--destination-id", help="Only refresh connections with this destination ID."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="List the connections that would be refreshed."
    ),
    sleep: float = typer.Option(
        1.0, "--sleep", help="Sleep (seconds) between refreshes (bulk mode)."
    ),
    yes: bool = YesOption,
):
    """Trigger an Airbyte 'refresh' job (re-pull history) on matched connection(s)."""
    from dataplat.services.airbyte.jobs import trigger_job

    with airbyte_client() as (client, base_url):
        if connection_id:
            if dry_run:
                console.print(f"[dim](dry run) Would refresh {connection_id}[/dim]")
                raise typer.Exit(code=0)
            console.print(f"[blue]Triggering refresh for {connection_id}...[/blue]")
            job = trigger_job(client, base_url, connection_id, "refresh")
            job_id = job.get("jobId") or job.get("id")
            console.print(
                "[green]✓ Refresh job started[/green]"
                + (f" (jobId={job_id})" if job_id else "")
            )
            raise typer.Exit(code=0)

        targets = [
            c
            for c in list_connections(client, base_url)
            if _matches_filters(c, source_id, destination_id)
        ]
        if not targets:
            console.print("[yellow]No matching active connections.[/yellow]")
            raise typer.Exit(code=0)

        if dry_run:
            console.print(f"[dim](dry run) Would refresh {len(targets)} connection(s):[/dim]")
            for conn in targets:
                conn_id = conn.get("connectionId") or conn.get("connection_id")
                console.print(f"- {conn.get('name', '')} ({conn_id})")
            raise typer.Exit(code=0)

        _confirm_bulk(
            f"About to trigger a refresh on {len(targets)} active connection(s).",
            yes,
        )
        console.print("[blue]Triggering refresh for active connections...[/blue]")
        for conn in targets:
            conn_id = conn.get("connectionId") or conn.get("connection_id")
            console.print(f"- {conn.get('name', '')} ({conn_id})")
            try:
                trigger_job(client, base_url, conn_id, "refresh")
                if sleep:
                    time.sleep(sleep)
            except Exception as exc:
                console.print(f"  [yellow]Warning: Failed to refresh: {exc}[/yellow]")


@app.command("reset")
def reset_connection_cmd(
    connection_id: str = typer.Option(
        ..., "--connection-id", "-c", help="Connection ID to reset"
    ),
    clear: bool = typer.Option(
        False,
        "--clear",
        help="Trigger a 'clear' job (drop data without re-syncing) instead of a reset.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
):
    """Reset a connection's data (drops and re-syncs all streams)."""
    from dataplat.cli.ingest.airbyte._common import airbyte_client
    from dataplat.services.airbyte.jobs import trigger_job

    job_type = "clear" if clear else "reset"
    if not yes:
        typer.confirm(
            f"Trigger a {job_type} of ALL streams on connection {connection_id}? "
            "This drops destination data for the connection.",
            abort=True,
        )

    with airbyte_client() as (client, base_url):
        job = trigger_job(client, base_url, connection_id, job_type)
        job_id = job.get("jobId") or job.get("id")
        console.print(
            f"[green]✓ {job_type.capitalize()} job started[/green]"
            + (f" (jobId={job_id})" if job_id else "")
        )
        console.print(
            f"[dim]Watch it with: dp ingest airbyte jobs get {job_id}[/dim]"
            if job_id
            else ""
        )
