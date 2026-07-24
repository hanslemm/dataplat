"""``dp db role drop`` — drop one or more roles cross-database.

Per role, in each target database: ``REASSIGN OWNED BY <role> TO <owner>``
(unless ``--no-reassign``) then ``DROP OWNED BY <role>``. Once every DB
is processed, ``DROP ROLE <role>`` runs once on the connection-DB.

The reassign-to owner defaults to the target's ``<PREFIX>_REASSIGN_OWNER``
env var; ``--reassign-to`` overrides for all DBs.

On Redshift, ownership of the dropped user's schemas/relations is
transferred and group memberships are dropped instead of REASSIGN/DROP
OWNED. Pass ``--reassign-to`` explicitly when using ``--engine`` without
``--target``.
"""

from __future__ import annotations

from typing import Any

import psycopg
import typer
from rich.console import Console
from rich.table import Table

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
    resolve_params_or_exit,
)
from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.role_admin import (
    DropPlan,
    MissingReassignOwnerError,
    SqlOp,
    build_drop_plan,
    list_databases,
    parse_csv_flag,
)
from dataplat.services.db.role_dialects import OwnedForDrop, dialect_for
from dataplat.services.db.targets import default_target_name, resolve_target


def _resolve_databases(
    *,
    cursor: Any,
    databases_flag: tuple[str, ...],
    all_databases: bool,
    fallback_db: str,
    engine: SqlEngine = SqlEngine.postgresql,
    warnings: list[str] | None = None,
) -> list[str]:
    if all_databases and databases_flag:
        raise typer.BadParameter(
            "--all-databases and --databases are mutually exclusive"
        )
    if engine == SqlEngine.redshift and (all_databases or databases_flag):
        if warnings is not None:
            warnings.append(
                "Redshift operates on the connected database only; "
                "ignoring --all-databases/--databases"
            )
        return [fallback_db]
    if all_databases:
        return list_databases(cursor)
    if databases_flag:
        return list(databases_flag)
    # For drop, single-DB default is dangerous (privileges in other DBs would
    # block DROP ROLE), so make the user pass it explicitly.
    return [fallback_db]


def _render_plan(
    console: Console, plans: list[DropPlan], conn_ctx: Any
) -> None:
    for plan in plans:
        console.print(f"\n[bold cyan]Role:[/bold cyan] {plan.role}")
        if plan.pre_cluster_ops:
            console.print("  [dim]Pre-cluster (membership setup):[/dim]")
            for op in plan.pre_cluster_ops:
                _print_op(console, op, conn_ctx, indent=4)
        for db, ops in plan.per_database_ops.items():
            console.print(f"  [dim]Database:[/dim] {db}")
            for op in ops:
                _print_op(console, op, conn_ctx, indent=4)
        console.print("  [dim]Cluster:[/dim]")
        for op in plan.cluster_ops:
            _print_op(console, op, conn_ctx, indent=4)


def _print_op(console: Console, op: SqlOp, conn_ctx: Any, *, indent: int) -> None:
    pad = " " * indent
    rendered = op.statement.as_string(conn_ctx)
    console.print(f"{pad}{rendered};")


def _execute_plan(
    *,
    plan: DropPlan,
    conn_params_kwargs: dict[str, Any],
    console: Console,
) -> None:
    """Run pre-cluster, per-database, then cluster ops in order."""
    if plan.pre_cluster_ops:
        with psycopg.connect(**conn_params_kwargs) as conn:
            with conn.cursor() as cursor:
                for op in plan.pre_cluster_ops:
                    cursor.execute(op.statement)
            conn.commit()
        console.print(f"  [green]✓[/green] {plan.role} — membership granted")

    for db, ops in plan.per_database_ops.items():
        if not ops:
            continue
        db_kwargs = {**conn_params_kwargs, "dbname": db}
        with psycopg.connect(**db_kwargs) as conn:
            with conn.cursor() as cursor:
                for op in ops:
                    cursor.execute(op.statement)
            conn.commit()
        console.print(f"  [green]✓[/green] {plan.role} — {db}")

    with psycopg.connect(**conn_params_kwargs) as conn:
        with conn.cursor() as cursor:
            for op in plan.cluster_ops:
                cursor.execute(op.statement)
        conn.commit()
    console.print(f"  [green]✓[/green] {plan.role} — DROP ROLE")


def drop_command(
    names: list[str] = typer.Argument(
        ..., help="One or more role names to drop."
    ),
    reassign_to: str | None = typer.Option(
        None, "--reassign-to",
        help="Role to receive ownership transfer in every database. "
             "Defaults vary per DB; pass to override.",
    ),
    no_reassign: bool = typer.Option(
        False, "--no-reassign",
        help="Skip REASSIGN OWNED. DROP OWNED alone will revoke privileges "
             "but error if the role still owns objects.",
    ),
    no_grant_membership: bool = typer.Option(
        False, "--no-grant-membership",
        help="Skip the automatic GRANT <role> TO <connection-user> that lets "
             "non-superusers run REASSIGN/DROP OWNED. Use when running as "
             "superuser or already a member.",
    ),
    databases_flag: list[str] | None = typer.Option(
        None, "--databases",
        help="Comma-separated databases to clean up. Repeatable.",
    ),
    all_databases: bool = typer.Option(
        False, "--all-databases",
        help="Iterate every non-template database on the cluster.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Print the SQL plan and exit without connecting.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Skip the confirmation prompt.",
    ),
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
    """Entry point for ``dp db role drop``."""
    console = Console()

    conn_params = resolve_params_or_exit(
        ConnCliParams(
            target=target, engine=engine, user=user, password=password,
            database=database, host=host, port=port, sslmode=sslmode,
            env_prefix=env_prefix,
        )
    )

    conn_kwargs = conn_params.as_psycopg_kwargs()
    dialect = dialect_for(conn_params.engine)
    warnings: list[str] = []

    try:
        with psycopg.connect(**conn_kwargs) as conn:  # type: ignore[arg-type]
            with conn.cursor() as cursor:
                target_dbs = _resolve_databases(
                    cursor=cursor,
                    databases_flag=parse_csv_flag(databases_flag),
                    all_databases=all_databases,
                    fallback_db=conn_params.dbname,
                    engine=conn_params.engine,
                    warnings=warnings,
                )
                missing = [n for n in names if not dialect.role_exists(cursor, n)]
                owned_by: dict[str, OwnedForDrop] = {}
                groups_by: dict[str, list[str]] = {}
                if conn_params.engine == SqlEngine.redshift:
                    try:
                        for n in names:
                            owned_by[n] = dialect.enumerate_owned(cursor, n)
                            groups_by[n] = dialect.groups_of(cursor, n)
                    except ValueError as exc:
                        console.print(f"[red]Error: {exc}[/red]")
                        raise typer.Exit(code=1)
            if missing:
                console.print(
                    f"[red]Error: role(s) not found: {', '.join(missing)}[/red]"
                )
                raise typer.Exit(code=1)
            grant_membership_to = (
                None if no_grant_membership else conn_params.user
            )
            effective_reassign_to = reassign_to
            if reassign_to is None and not no_reassign:
                # The reassign owner is a property of the target, not the
                # db name: <PREFIX>_REASSIGN_OWNER.
                target_name = target or default_target_name()
                if target_name is not None:
                    tgt = resolve_target(target_name)
                    if tgt.reassign_owner:
                        effective_reassign_to = tgt.reassign_owner
            try:
                plans = [
                    build_drop_plan(
                        n, target_dbs, dialect,
                        reassign_to=effective_reassign_to, no_reassign=no_reassign,
                        grant_membership_to=grant_membership_to,
                        owned=owned_by.get(n), groups=groups_by.get(n),
                    )
                    for n in names
                ]
            except (ValueError, MissingReassignOwnerError) as exc:
                console.print(f"[red]Error: {exc}[/red]")
                raise typer.Exit(code=1)
            console.print(
                f"[bold red]Plan:[/bold red] DROP {len(plans)} role(s) "
                f"across {len(target_dbs)} database(s) "
                f"({', '.join(target_dbs)})"
            )
            for w in warnings:
                console.print(f"[yellow]Warning: {w}[/yellow]")
            _render_plan(console, plans, conn)
    except psycopg.Error as exc:
        console.print(f"[red]Database error: {exc}[/red]")
        raise typer.Exit(code=1)

    if dry_run:
        console.print("\n[yellow]Dry-run; no SQL executed.[/yellow]")
        return

    if not yes:
        confirmed = typer.confirm(
            "\n[!] This is destructive. Proceed?", default=False,
        )
        if not confirmed:
            console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(code=1)

    console.print()
    for plan in plans:
        try:
            _execute_plan(
                plan=plan, conn_params_kwargs=conn_kwargs, console=console,
            )
        except psycopg.Error as exc:
            console.print(f"[red]✗ {plan.role}: {exc}[/red]")
            raise typer.Exit(code=1)

    table = Table(title="Dropped", show_header=True, header_style="bold")
    table.add_column("Role")
    table.add_column("Databases")
    for plan in plans:
        table.add_row(plan.role, ", ".join(plan.per_database_ops.keys()))
    console.print(table)
