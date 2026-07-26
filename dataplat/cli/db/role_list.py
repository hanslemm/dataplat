"""``dp db role list`` — list roles on the cluster.

``pg_roles`` is shared across all databases on the cluster, so a single
connection is enough to enumerate every login user / group regardless of
which database the cursor is connected to.
"""

from __future__ import annotations

import dataclasses
import json
import sys

import typer
from rich import box as _box
from rich.console import Console
from rich.table import Table

from dataplat.cli._options import json_option
from dataplat.cli._render import cell
from dataplat.cli.db._common import (
    ConnCliParams,
    DatabaseOption,
    EngineOption,
    EnvPrefixOption,
    HostOption,
    PasswordOption,
    PortOption,
    SslmodeOption,
    TargetOption,
    UserOption,
    db_session,
    resolve_params_or_exit,
)
from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.role_admin import RoleSummary
from dataplat.services.db.role_dialects import dialect_for


def _filter_rows(
    rows: list[RoleSummary],
    *,
    substring: str | None,
    users_only: bool,
    groups_only: bool,
) -> list[RoleSummary]:
    if users_only and groups_only:
        raise typer.BadParameter(
            "--users-only and --groups-only are mutually exclusive"
        )
    out = rows
    if substring:
        needle = substring.lower()
        out = [r for r in out if needle in r.name.lower()]
    if users_only:
        out = [r for r in out if r.can_login]
    if groups_only:
        out = [r for r in out if not r.can_login]
    return out


def _render(console: Console, rows: list[RoleSummary]) -> None:
    table = Table(
        box=_box.HORIZONTALS,
        show_header=True,
        header_style="bold",
        padding=(0, 2),
        pad_edge=False,
        show_edge=False,
    )
    table.add_column("Role")
    table.add_column("Type", style="dim")
    table.add_column("Login")
    table.add_column("Super")
    table.add_column("CreateDB")
    table.add_column("CreateRole")
    table.add_column("Member of", justify="right")
    table.add_column("Members", justify="right")

    for r in rows:
        kind = "user" if r.can_login else "group"
        table.add_row(
            cell(r.name),
            kind,
            "yes" if r.can_login else "no",
            "yes" if r.superuser else "",
            "yes" if r.create_db else "",
            "yes" if r.create_role else "",
            str(r.member_of_count),
            str(r.members_count),
        )
    console.print(table)
    console.print(f"[dim]Total: {len(rows)} role(s)[/dim]")


def list_command(
    filter_substring: str | None = typer.Option(
        None,
        "--filter",
        "-f",
        help="Case-insensitive substring match on role name.",
    ),
    users_only: bool = typer.Option(
        False,
        "--users-only",
        help="Only show roles that can log in.",
    ),
    groups_only: bool = typer.Option(
        False,
        "--groups-only",
        help="Only show roles that cannot log in.",
    ),
    as_json: bool = json_option("Emit roles as a JSON array on stdout."),
    target: str | None = TargetOption,
    engine: SqlEngine | None = EngineOption,
    user: str | None = UserOption,
    password: str | None = PasswordOption,
    database: str | None = DatabaseOption,
    host: str | None = HostOption,
    port: int | None = PortOption,
    sslmode: str | None = SslmodeOption,
    env_prefix: str | None = EnvPrefixOption,
) -> None:
    """Entry point for ``dp db role list``."""
    console = Console()
    conn_params = resolve_params_or_exit(
        ConnCliParams(
            target=target,
            engine=engine,
            user=user,
            password=password,
            database=database,
            host=host,
            port=port,
            sslmode=sslmode,
            env_prefix=env_prefix,
        )
    )

    dialect = dialect_for(conn_params.engine)
    with db_session(conn_params) as conn, conn.cursor() as cursor:
        rows = dialect.list_roles(cursor)

    rows = _filter_rows(
        rows,
        substring=filter_substring,
        users_only=users_only,
        groups_only=groups_only,
    )

    if as_json:
        payload = [dataclasses.asdict(r) for r in rows]
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        return

    if not rows:
        console.print("[yellow]No roles match the filter.[/yellow]")
        return
    _render(console, rows)
