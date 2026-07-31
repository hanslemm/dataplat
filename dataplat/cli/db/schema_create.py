"""``dp db schema create`` — create one or more schemas.

Works on all three engines, with two flags that do not: ``--owner`` needs a
principal to own the schema, and ``--quota`` exists only on Redshift. Both are
refused up front on an engine that cannot honour them, rather than accepted and
quietly dropped — a schema created without the owner you asked for is a schema
whose future tables belong to the wrong role.
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
    resolve_any_params_or_exit,
)
from dataplat.cli.db._plan import execute_ops, print_ops
from dataplat.core.errors import ValidationError
from dataplat.services.db.capabilities import Capability, require_capability
from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.role_admin import parse_csv_flag
from dataplat.services.db.schema_admin import CreateSchemaSpec, build_create_plan
from dataplat.services.db.schema_dialects import schema_dialect_for


def create_command(
    names: list[str] = typer.Argument(
        ..., help="One or more schema names to create. Comma-separated accepted."
    ),
    owner: str | None = typer.Option(
        None,
        "--owner",
        help="Role that will own the schema (CREATE SCHEMA ... AUTHORIZATION).",
    ),
    quota: str | None = typer.Option(
        None,
        "--quota",
        help="Redshift schema quota: <int>MB|GB|TB, or UNLIMITED (e.g. 50GB).",
    ),
    if_not_exists: bool = typer.Option(
        False,
        "--if-not-exists",
        help="Do not fail when the schema is already there.",
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
    """Entry point for ``dp db schema create``."""
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
    resolved_engine = engine_or_exit(conn_cli)
    # --owner is the only part of this command an engine can refuse: DuckDB's
    # parser rejects AUTHORIZATION outright, because it has no principals. Gated
    # here so the user reads the engine's reason instead of a parser error, and
    # only when the flag is actually used — plain `create` works everywhere.
    if owner is not None:
        try:
            require_capability(
                resolved_engine,
                Capability.roles,
                command="dp db schema create --owner",
                detail="A schema needs an existing role to own it.",
            )
        except ValidationError as exc:
            fail(exc, console=console)

    conn_params = resolve_any_params_or_exit(conn_cli)
    dialect = schema_dialect_for(conn_params.engine)

    try:
        specs = [
            CreateSchemaSpec(
                name=name, owner=owner, quota=quota, if_not_exists=if_not_exists
            )
            for name in parse_csv_flag(names)
        ]
        plan = build_create_plan(specs, dialect)
    except ValidationError as exc:
        fail(exc, console=console)

    console.print(f"[bold]Plan:[/bold] create {len(plan.ops)} schema(s)")
    for warning in plan.warnings:
        console.print(f"[yellow]Warning: {warning}[/yellow]")

    with db_session(conn_params) as conn, conn.cursor() as cursor:
        print_ops(console, plan.ops, conn)
        if dry_run:
            console.print("\n[yellow]Dry-run; no SQL executed.[/yellow]")
            return
        confirm_or_exit(yes=yes, prompt="\nProceed?", console=console)
        execute_ops(cursor, plan.ops)

    created = ", ".join(esc(s.name) for s in specs)
    console.print(f"\n[green]Created:[/green] {created}")
