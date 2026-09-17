"""``dp bi superset dashboards usage``: who actually opens these dashboards.

Superset's own API cannot answer this. ``/api/v1/log/`` times out on any
instance with real history, and the dashboard endpoints carry no view counts at
all. What every instance does have is the ``logs`` table its metadata database
writes to -- so this command reads that, through an ordinary ``dp`` database
target, and joins the titles back on from the API.

That makes it the first command in the ``bi`` area to open a database
connection, which is the one genuinely unusual thing here. The seam is narrow
on purpose: :mod:`dataplat.services.superset.usage` builds SQL and shapes rows
and knows nothing about Typer or Rich, and this module does the rest.
"""

from __future__ import annotations

import json
from datetime import datetime

import typer
from rich import box
from rich.console import Console
from rich.table import Table

from dataplat.cli._exit import fail
from dataplat.cli._options import JsonOption
from dataplat.cli._render import cell
from dataplat.cli.bi._superset_auth import load_auth_context
from dataplat.cli.db._common import ConnCliParams, db_session
from dataplat.core.errors import (
    AuthError,
    ConfigError,
    ServiceError,
    ValidationError,
)
from dataplat.services.superset.client import build_client
from dataplat.services.superset.client import login as _login
from dataplat.services.superset.dashboards import iter_dashboards
from dataplat.services.superset.usage import (
    DEFAULT_VIEW_ACTION,
    ActionCount,
    DashboardUsage,
    action_rows,
    actions_query,
    load_usage_config,
    usage_query,
    usage_rows,
)

__all__ = ["usage_command"]

console = Console()


def resolve_usage_params(target: str):
    """Connection parameters for the target holding Superset's log tables.

    Named rather than inlined so the tests have one seam to replace, and so the
    error a bad ``DP_SUPERSET_USAGE_TARGET`` produces is raised from one place.
    """
    return ConnCliParams(target=target).resolve_any()


def _fmt_when(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d") if value is not None else "—"


def _titles(base_url: str, username: str, password: str) -> dict[int, dict]:
    """Every dashboard the API knows, by id.

    The warehouse stores ids; a report of bare ids is not one anybody can act
    on. This is also what makes ``--unused`` possible at all -- a dashboard
    nobody opened has no row in the logs, so it can only be found by starting
    from the list of dashboards that exist.
    """
    with build_client() as client:
        token = _login(client, base_url, username, password)
        return {
            int(d["id"]): d
            for d in iter_dashboards(client, base_url, token)
            if d.get("id") is not None
        }


def _render_actions(rows: list[ActionCount], *, days: int) -> None:
    table = Table(
        show_header=True, header_style="bold cyan", box=box.SIMPLE_HEAVY, expand=True
    )
    table.add_column("Action", style="cyan")
    table.add_column("Events", justify="right")
    table.add_column("With dashboard", justify="right")
    table.add_column("Last seen", style="dim")
    for row in rows:
        table.add_row(
            cell(row.action),
            cell(f"{row.events:,}"),
            cell(f"{row.with_dashboard:,}"),
            cell(_fmt_when(row.last_seen)),
        )
    console.print(table)
    console.print(
        f"\n[dim]Actions logged in the last {days} day(s). "
        f"Pass the one your instance uses for a dashboard open to "
        f"--view-action (default: {DEFAULT_VIEW_ACTION}).[/dim]"
    )


def _render_usage(rows: list[DashboardUsage], *, days: int) -> None:
    table = Table(
        show_header=True, header_style="bold cyan", box=box.SIMPLE_HEAVY, expand=True
    )
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Title", style="cyan")
    table.add_column("Viewers", justify="right")
    table.add_column("Views", justify="right")
    table.add_column("Last viewed", style="dim")
    for row in rows:
        table.add_row(
            cell(row.dashboard_id),
            cell(row.title or "[unknown to the API]"),
            cell(f"{row.viewers:,}"),
            cell(f"{row.views:,}"),
            cell(_fmt_when(row.last_viewed)),
        )
    console.print(table)
    console.print(f"\n[dim]{len(rows)} dashboard(s), last {days} day(s)[/dim]")


def _render_unused(rows: list[dict], *, days: int, listed: int, unlisted: int) -> None:
    table = Table(
        show_header=True, header_style="bold cyan", box=box.SIMPLE_HEAVY, expand=True
    )
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Title", style="cyan")
    table.add_column("Status")
    for row in rows:
        table.add_row(
            cell(row["dashboard_id"]), cell(row["title"]), cell(row["status"])
        )
    console.print(table)
    console.print(
        f"\n[dim]{len(rows)} of {listed} dashboard(s) with no recorded view in "
        f"{days} day(s)[/dim]"
    )
    if unlisted:
        # Superset filters its dashboard list by what the account may see, so a
        # non-admin gets a short list and this report would quietly mean "of the
        # ones I can see". The logs naming dashboards the listing does not is
        # proof of exactly that, and saying nothing would invite someone to
        # delete on the strength of a number that never covered everything.
        console.print(
            f"[yellow]! {unlisted} dashboard(s) with views are not in this "
            f"account's dashboard list.[/yellow]"
        )
        console.print(
            "  [dim]They were deleted, or this account cannot see them. "
            "Run as an admin for the complete picture.[/dim]"
        )


def usage_command(
    days: int = typer.Option(
        90, "--days", help="How far back to count. Default: 90 days."
    ),
    limit: int = typer.Option(
        25, "--limit", "-n", help="Show at most this many dashboards."
    ),
    view_action: list[str] = typer.Option(
        [],
        "--view-action",
        help="Log action that counts as a dashboard open. Repeatable. "
        f"Default: {DEFAULT_VIEW_ACTION}. Run --actions to see yours.",
    ),
    actions: bool = typer.Option(
        False,
        "--actions",
        help="List the log actions this instance records, with counts, and "
        "stop. Use it to find what your Superset version calls a dashboard "
        "open, and to see the cache and thumbnail traffic being excluded.",
    ),
    unused: bool = typer.Option(
        False,
        "--unused",
        help="Invert: list dashboards nobody opened in the window. Never capped "
        "by --limit, since a capped scan would call unseen dashboards unused.",
    ),
    as_json: bool = JsonOption,
) -> None:
    """Rank dashboards by how many people actually open them."""
    if actions and unused:
        fail(
            ValidationError(
                "--actions reports the log vocabulary and --unused reports "
                "dashboards; ask for one at a time."
            ),
            console=console,
        )

    try:
        config = load_usage_config()
        params = resolve_usage_params(config.target)

        if actions:
            sql, query_params = actions_query(config, days=days, limit=limit)
        else:
            sql, query_params = usage_query(
                config,
                days=days,
                view_actions=tuple(view_action) or (DEFAULT_VIEW_ACTION,),
                # --unused subtracts these from the dashboards that exist, so it
                # has to see all of them, not the top --limit.
                limit=None if unused else limit,
            )

        with db_session(params) as conn, conn.cursor() as cursor:
            cursor.execute(sql, query_params)
            raw = cursor.fetchall()
    except (AuthError, ConfigError, ServiceError, ValidationError) as exc:
        fail(exc, console=console)

    if actions:
        counts = action_rows(raw)
        if as_json:
            typer.echo(
                json.dumps(
                    [
                        {
                            "action": c.action,
                            "events": c.events,
                            "with_dashboard": c.with_dashboard,
                            "last_seen": _fmt_when(c.last_seen),
                        }
                        for c in counts
                    ],
                    indent=2,
                )
            )
            return
        if not counts:
            console.print("[yellow]No log rows in that window[/yellow]")
            return
        _render_actions(counts, days=days)
        return

    measured = usage_rows(raw)

    try:
        base_url, username, password = load_auth_context(console)
        known = _titles(base_url, username, password)
    except (AuthError, ConfigError, ServiceError) as exc:
        fail(exc, console=console)

    if unused:
        viewed = {row.dashboard_id for row in measured}
        unlisted = len(viewed - set(known))
        rows = sorted(
            (
                {
                    "dashboard_id": did,
                    "title": str(d.get("dashboard_title", "")),
                    "status": str(d.get("status", "")),
                }
                for did, d in known.items()
                if did not in viewed
            ),
            key=lambda r: str(r["title"]),
        )
        if as_json:
            typer.echo(json.dumps(rows, indent=2, ensure_ascii=False))
            return
        if not rows:
            console.print(
                f"[green]Every dashboard was opened at least once in "
                f"{days} day(s)[/green]"
            )
            return
        _render_unused(rows, days=days, listed=len(known), unlisted=unlisted)
        return

    named = [
        DashboardUsage(
            dashboard_id=row.dashboard_id,
            views=row.views,
            viewers=row.viewers,
            last_viewed=row.last_viewed,
            title=str(known.get(row.dashboard_id, {}).get("dashboard_title", "")),
        )
        for row in measured
    ]

    if as_json:
        typer.echo(
            json.dumps(
                [
                    {
                        "dashboard_id": r.dashboard_id,
                        "title": r.title,
                        "viewers": r.viewers,
                        "views": r.views,
                        "last_viewed": _fmt_when(r.last_viewed),
                    }
                    for r in named
                ],
                indent=2,
                ensure_ascii=False,
            )
        )
        return

    if not named:
        console.print(
            f"[yellow]No dashboard was opened in the last {days} day(s)[/yellow]"
        )
        return

    _render_usage(named, days=days)
