"""``dp db long-queries`` and ``dp db kill`` — triage and act on queries.

Redshift targets get the sys_query_history triage view (running + recent
slow + recent failures); Postgres targets get a live pg_stat_activity
snapshot (Postgres keeps no finished-query history).
"""

from __future__ import annotations

import dataclasses
import json
import sys
from datetime import UTC, datetime, timedelta

import typer
from rich import box
from rich.console import Console
from rich.table import Table

from dataplat.cli.db._common import (
    ConnCliParams,
    JsonOption,
    YesOption,
    db_session,
    limit_option,
    resolve_params_or_exit,
)
from dataplat.core.errors import DataplatError, ValidationError
from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.long_queries import (
    LongQueryRow,
    QueryHistoryRow,
    cancel_query_postgres,
    cancel_query_redshift,
    fetch_long_queries,
    fetch_long_queries_postgres,
    fetch_query_history_postgres,
)
from dataplat.services.db.targets import (
    DbTarget,
    default_target_name,
    resolve_target,
    resolve_targets,
)

console = Console()

_LABELS: dict[SqlEngine, str] = {
    SqlEngine.postgresql: "Postgres",
    SqlEngine.redshift: "Redshift",
}


def _fetch_for_target(
    target: DbTarget,
    *,
    min_seconds: int,
    hours: float,
    running_only: bool,
    limit: int,
) -> list[LongQueryRow]:
    params = ConnCliParams(target=target.name).resolve()
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    with db_session(params) as conn, conn.cursor() as cursor:
        if target.engine == SqlEngine.redshift:
            return fetch_long_queries(
                cursor,
                min_seconds=min_seconds,
                limit=limit,
                cutoff=cutoff,
                running_only=running_only,
            )
        return fetch_long_queries_postgres(
            cursor, min_seconds=min_seconds, limit=limit
        )


def _render_rows(target: DbTarget, rows: list[LongQueryRow]) -> None:
    console.print(f"\n[bold cyan]{_LABELS[target.engine]}[/bold cyan]")
    if not rows:
        console.print("[green]  No matching queries.[/green]")
        return
    table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
    table.add_column("PID", justify="right")
    table.add_column("Query ID", justify="right", style="dim")
    table.add_column("User")
    table.add_column("DB", style="dim")
    table.add_column("Status")
    table.add_column("Elapsed", justify="right")
    table.add_column("Query", overflow="fold", max_width=70)
    for row in rows:
        status_style = (
            "red"
            if row.status.lower() in {"failed", "aborted", "canceled", "cancelled"}
            else "yellow"
            if row.status.lower() in {"running", "queued", "active"}
            else "dim"
        )
        table.add_row(
            row.session_id or "—",
            row.query_id,
            row.user_name,
            row.db_name,
            f"[{status_style}]{row.status}[/{status_style}]",
            f"{row.elapsed_s}s",
            row.query_text,
        )
    console.print(table)
    console.print(
        f"[dim]  {len(rows)} row(s). "
        f"Kill with: dp db kill <PID> -t {target.name}[/dim]"
    )


def _fetch_history(target: DbTarget, *, min_seconds: int, limit: int):
    params = ConnCliParams(target=target.name).resolve()
    with db_session(params) as conn, conn.cursor() as cursor:
        return fetch_query_history_postgres(
            cursor, min_seconds=min_seconds, limit=limit
        )


def _render_history(target: DbTarget, rows: list[QueryHistoryRow]) -> None:
    console.print(
        f"\n[bold cyan]{_LABELS[target.engine]}[/bold cyan] "
        f"[dim]— pg_stat_statements history[/dim]"
    )
    if not rows:
        console.print("[green]  No matching statements.[/green]")
        return
    table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
    table.add_column("Calls", justify="right")
    table.add_column("Total (s)", justify="right")
    table.add_column("Mean (s)", justify="right")
    table.add_column("Max (s)", justify="right")
    table.add_column("Query", overflow="fold", max_width=80)
    for row in rows:
        table.add_row(
            str(row.calls),
            f"{row.total_s:.2f}",
            f"{row.mean_s:.2f}",
            f"{row.max_s:.2f}",
            row.query_text or "—",
        )
    console.print(table)


def long_queries_command(
    target: str = typer.Option(
        "all",
        "--target",
        "-t",
        help="Named DB target from DP_TARGETS, or all (default).",
    ),
    min_seconds: int = typer.Option(
        60, "--min-seconds", "-m", min=1, help="Minimum elapsed seconds."
    ),
    hours: float = typer.Option(
        6.0,
        "--hours",
        min=0.1,
        help="Look-back window for finished/failed queries (Redshift only).",
    ),
    running_only: bool = typer.Option(
        False,
        "--running-only",
        help="Only live queries (Redshift; Postgres is always a live snapshot).",
    ),
    history: bool = typer.Option(
        False,
        "--history",
        help="Postgres only: aggregate slow statements from pg_stat_statements "
        "instead of the live snapshot.",
    ),
    limit: int = limit_option(20, "Max rows per target."),
    as_json: bool = JsonOption,
) -> None:
    """Show long-running (and recently failed) queries per target."""
    try:
        targets = resolve_targets(target)
    except ValidationError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(code=1)

    if history:
        targets = [t for t in targets if t.engine == SqlEngine.postgresql]
        if not targets:
            console.print(
                "[red]--history uses pg_stat_statements and needs a "
                "Postgres target.[/red]"
            )
            raise typer.Exit(code=1)

    failures = 0
    payload: dict[str, object] = {}
    for tgt in targets:
        try:
            rows: list = (
                _fetch_history(tgt, min_seconds=min_seconds, limit=limit)
                if history
                else _fetch_for_target(
                    tgt,
                    min_seconds=min_seconds,
                    hours=hours,
                    running_only=running_only,
                    limit=limit,
                )
            )
        except ValidationError as exc:
            console.print(f"[red][{tgt.name}] {exc}[/red]")
            failures += 1
            continue
        except typer.Exit:
            # db_session already printed the error; keep going on "all".
            failures += 1
            if len(targets) == 1:
                raise
            continue
        if as_json:
            payload[tgt.name] = [dataclasses.asdict(r) for r in rows]
        elif history:
            _render_history(tgt, rows)
        else:
            _render_rows(tgt, rows)

    if as_json:
        typer.echo(json.dumps(payload, indent=2, default=str))
    if failures:
        raise typer.Exit(code=1)


def kill_command(
    pids: list[int] = typer.Argument(
        ..., help="Backend PID(s) / Redshift session id(s) to kill."
    ),
    target: str | None = typer.Option(
        None,
        "--target",
        "-t",
        help="Named DB target the PIDs belong to (default: DP_DEFAULT_TARGET).",
    ),
    cancel: bool = typer.Option(
        False,
        "--cancel",
        help="Postgres: cancel the running statement instead of terminating "
        "the backend.",
    ),
    yes: bool = YesOption,
) -> None:
    """Cancel or terminate running queries by PID."""
    try:
        name = target or default_target_name()
        if not name:
            raise ValidationError(
                "No target given and none configured. Pass --target or set "
                "DP_TARGETS."
            )
        tgt = resolve_target(name)
    except DataplatError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(code=1)

    action = "Cancel" if cancel or tgt.engine == SqlEngine.redshift else "Terminate"
    if not yes:
        summary = (
            f"{action} {len(pids)} session(s) on "
            f"[cyan]{tgt.name}[/cyan]: {', '.join(str(p) for p in pids)}"
        )
        console.print(summary)
        if not sys.stdin.isatty():
            console.print(
                "[red]Error: confirmation required. Pass --yes/-y in "
                "non-interactive contexts.[/red]"
            )
            raise typer.Exit(code=1)
        if not typer.confirm("Proceed?", default=False):
            console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(code=1)

    params = resolve_params_or_exit(ConnCliParams(target=tgt.name))
    failed = 0
    with db_session(params) as conn:
        with conn.cursor() as cursor:
            for pid in pids:
                if tgt.engine == SqlEngine.redshift:
                    cancel_query_redshift(cursor, pid)
                    console.print(f"[green]✓ CANCEL {pid} issued[/green]")
                    continue
                ok = cancel_query_postgres(cursor, pid, terminate=not cancel)
                if ok:
                    console.print(f"[green]✓ {action.lower()}d backend {pid}[/green]")
                else:
                    console.print(
                        f"[yellow]! backend {pid} not found (already gone?)[/yellow]"
                    )
                    failed += 1
        conn.commit()
    if failed:
        raise typer.Exit(code=1)
