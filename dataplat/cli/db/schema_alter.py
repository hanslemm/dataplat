"""``dp db schema alter`` — change a schema's owner, quota, or name.

Refused entirely on DuckDB, which does not implement ``ALTER SCHEMA`` — the engine
answers "Altering schemas is not yet supported" — and has neither owners nor
quotas to alter.

Protected schemas are refused here as well as in ``drop``. Renaming ``public`` or
reowning ``information_schema`` is not destructive in the way a drop is, but it
breaks every unqualified reference in the database, which is close enough.
"""

from __future__ import annotations

import typer
from rich.console import Console

from dataplat.cli._exit import fail
from dataplat.cli._options import YesOption
from dataplat.cli._prompt import confirm_or_exit
from dataplat.cli._render import esc
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
    engine_or_exit,
    resolve_params_or_exit,
)
from dataplat.cli.db._plan import execute_ops, print_ops
from dataplat.cli.db._schema_opts import is_protected_schema
from dataplat.core.errors import ValidationError
from dataplat.services.db.capabilities import Capability, require_capability
from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.role_admin import parse_csv_flag
from dataplat.services.db.schema_admin import build_alter_plan
from dataplat.services.db.schema_dialects import schema_dialect_for


def alter_command(
    names: list[str] = typer.Argument(
        ..., help="Schema(s) to alter. Comma-separated accepted."
    ),
    owner: str | None = typer.Option(
        None, "--owner", help="New owner (ALTER SCHEMA ... OWNER TO)."
    ),
    quota: str | None = typer.Option(
        None,
        "--quota",
        help="New Redshift quota: <int>MB|GB|TB, or UNLIMITED (e.g. 50GB).",
    ),
    rename_to: str | None = typer.Option(
        None,
        "--rename-to",
        help="New name. Takes exactly one schema.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the SQL and exit without executing it."
    ),
    yes: bool = YesOption,
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
    """Entry point for ``dp db schema alter``."""
    console = Console()
    conn_cli = ConnCliParams(
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
    # Before resolving or connecting, so the refusal names the engine's reason.
    try:
        require_capability(
            engine_or_exit(conn_cli),
            Capability.schema_alter,
            command="dp db schema alter",
        )
    except ValidationError as exc:
        fail(exc, console=console)
    conn_params = resolve_params_or_exit(conn_cli)
    dialect = schema_dialect_for(conn_params.engine)

    selected = parse_csv_flag(names)
    protected = [n for n in selected if is_protected_schema(n)]
    if protected:
        fail(
            ValidationError(
                f"refusing to alter protected schema(s): {', '.join(protected)}. "
                "These are catalog or default schemas that the database itself "
                "depends on."
            ),
            console=console,
        )

    try:
        plan = build_alter_plan(
            selected, dialect, owner=owner, quota=quota, rename_to=rename_to
        )
    except ValidationError as exc:
        fail(exc, console=console)

    console.print(f"[bold]Plan:[/bold] {len(plan.ops)} change(s)")
    for warning in plan.warnings:
        console.print(f"[yellow]Warning: {warning}[/yellow]")

    with db_session(conn_params) as conn, conn.cursor() as cursor:
        print_ops(console, plan.ops, conn)
        if dry_run:
            console.print("\n[yellow]Dry-run; no SQL executed.[/yellow]")
            return
        confirm_or_exit(yes=yes, prompt="\nProceed?", console=console)
        execute_ops(cursor, plan.ops)

    console.print(f"\n[green]Altered:[/green] {', '.join(esc(n) for n in selected)}")
