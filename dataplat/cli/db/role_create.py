"""``dp db role create`` — create one or more roles cross-database.

The CLI builds a :class:`CreateRoleSpec` per user from the explicit
permission flags, asks the service layer for a :class:`CreatePlan`, then
runs cluster ops once and per-database ops on each target database.

By default roles are login roles with a generated password. ``--no-login``
creates passwordless group-style roles instead (Postgres ``NOLOGIN`` role /
Redshift RBAC role); ``--grant-to`` grants the new role to existing
roles or users (``GRANT <new> TO <target>``).

Generated passwords are written to a CSV at ``--credentials-out`` (default
``./dp-credentials-<timestamp>.csv``) with mode ``0600``. They are never
printed to stdout or logs. With ``--no-login`` no credentials file is
written at all.
"""

from __future__ import annotations

import csv
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import typer
from rich.console import Console
from rich.table import Table

from dataplat.cli._options import YesOption
from dataplat.cli._prompt import confirm_or_exit
from dataplat.cli._render import cell, esc
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
    CreatePlan,
    CreateRoleSpec,
    SqlOp,
    build_create_plan,
    generate_password,
    list_databases,
    parse_csv_flag,
)
from dataplat.services.db.role_dialects import ParentKind, dialect_for


def _credentials_default_path() -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path.cwd() / f"dp-credentials-{stamp}.csv"


def _open_credentials_file(path: Path) -> tuple[Any, bool]:
    """Open the credentials CSV in append mode with secure permissions.

    Returns ``(file, is_new)``. New files are created with mode 0600. If
    the file already exists, we leave its mode alone but warn the caller
    so they can flag insecure permissions in the rendered output.
    """
    is_new = not path.exists()
    if is_new:
        # Create with 0600 atomically via os.open; otherwise there's a window
        # where the file is readable before we chmod it.
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        file = os.fdopen(fd, "a", newline="")
    else:
        file = open(path, "a", newline="")  # noqa: SIM115
    return file, is_new


def _file_mode_secure(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
    except OSError:
        return True  # don't block on a missing file we just created
    return not bool(mode & (stat.S_IRWXG | stat.S_IRWXO))


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
    return [fallback_db]


def _render_plan(console: Console, plans: list[CreatePlan], conn_ctx: Any) -> None:
    """Print a dry-run friendly summary of every plan, masking passwords."""
    for plan in plans:
        console.print(f"\n[bold cyan]Role:[/bold cyan] {esc(plan.role)}")
        console.print("  [dim]Cluster:[/dim]")
        for op in plan.cluster_ops:
            _print_op(console, op, conn_ctx, indent=4)
        for db, ops in plan.per_database_ops.items():
            console.print(f"  [dim]Database:[/dim] {esc(db)}")
            for op in ops:
                _print_op(console, op, conn_ctx, indent=4)


def _print_op(console: Console, op: SqlOp, conn_ctx: Any, *, indent: int) -> None:
    pad = " " * indent
    # SQL is dense in brackets (arrays, quoted identifiers, driver-rendered
    # literals), so an unescaped statement is the likeliest MarkupError here.
    if op.secret:
        console.print(f"{pad}[yellow]{esc(op.description)};[/yellow]")
    else:
        rendered = op.statement.as_string(conn_ctx)
        console.print(f"{pad}{esc(rendered)};")


def _execute_cluster_ops(
    *, plan: CreatePlan, conn_params_kwargs: dict[str, Any]
) -> None:
    """Run the cluster-level ops (CREATE ROLE + password) and commit."""
    with psycopg.connect(**conn_params_kwargs) as conn:
        with conn.cursor() as cursor:
            for op in plan.cluster_ops:
                cursor.execute(op.statement)
        conn.commit()


def _execute_per_db_ops(
    *,
    plan: CreatePlan,
    conn_params_kwargs: dict[str, Any],
    console: Console,
) -> None:
    """Run the per-database grant ops for an already-created role."""
    connection_db = conn_params_kwargs["dbname"]

    for db, ops in plan.per_database_ops.items():
        if not ops:
            continue
        db_kwargs = {**conn_params_kwargs, "dbname": db}
        with psycopg.connect(**db_kwargs) as conn:
            with conn.cursor() as cursor:
                for op in ops:
                    cursor.execute(op.statement)
            conn.commit()
        console.print(f"  [green]✓[/green] {esc(plan.role)} — {esc(db)}")
    # Sanity: warn if connection_db wasn't part of per-database list.
    if connection_db not in plan.per_database_ops:
        console.print(
            f"  [dim](no per-database ops on connection DB {esc(connection_db)})[/dim]"
        )


def create_command(
    names: list[str] = typer.Argument(..., help="One or more role names to create."),
    schema_usage: list[str] | None = typer.Option(
        None,
        "--schema-usage",
        help="Schemas to GRANT USAGE on. Repeatable / comma-separated.",
    ),
    schema_create: list[str] | None = typer.Option(
        None,
        "--schema-create",
        help="Schemas to GRANT CREATE on. Repeatable / comma-separated.",
    ),
    table_select: list[str] | None = typer.Option(
        None,
        "--table-select",
        help="Schemas to GRANT SELECT ON ALL TABLES. Repeatable / comma-separated.",
    ),
    table_all: list[str] | None = typer.Option(
        None,
        "--table-all",
        help="Schemas to GRANT ALL ON ALL TABLES. Repeatable / comma-separated.",
    ),
    sequence_usage: list[str] | None = typer.Option(
        None,
        "--sequence-usage",
        help="Schemas to GRANT USAGE ON ALL SEQUENCES. Repeatable / comma-separated.",
    ),
    default_table_select: list[str] | None = typer.Option(
        None,
        "--default-table-select",
        help="Schemas where future tables get SELECT (ALTER DEFAULT PRIVILEGES).",
    ),
    default_table_all: list[str] | None = typer.Option(
        None,
        "--default-table-all",
        help="Schemas where future tables get ALL (ALTER DEFAULT PRIVILEGES).",
    ),
    member_of: list[str] | None = typer.Option(
        None,
        "--member-of",
        help="Parent roles to GRANT to the new role. Repeatable / comma-separated.",
    ),
    grant_to: list[str] | None = typer.Option(
        None,
        "--grant-to",
        help="Existing roles/users to make members of the new role "
        "(GRANT <new> TO <target>). Repeatable / comma-separated.",
    ),
    no_login: bool = typer.Option(
        False,
        "--no-login",
        help="Create a passwordless group-style role (Postgres NOLOGIN role / "
        "Redshift RBAC role). No credentials are generated.",
    ),
    databases_flag: list[str] | None = typer.Option(
        None,
        "--databases",
        help="Comma-separated databases to apply per-DB grants. Repeatable.",
    ),
    all_databases: bool = typer.Option(
        False,
        "--all-databases",
        help="Apply per-DB grants to every non-template database.",
    ),
    credentials_out: Path | None = typer.Option(
        None,
        "--credentials-out",
        help="CSV file to append generated credentials to. "
        "Default: ./dp-credentials-<timestamp>.csv",
    ),
    password_length: int = typer.Option(
        32,
        "--password-length",
        min=16,
        max=128,
        help="Length of the generated password.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the SQL plan and exit without connecting.",
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
    """Entry point for ``dp db role create``."""
    console = Console()

    spec_kwargs = {
        "schema_usage": parse_csv_flag(schema_usage),
        "schema_create": parse_csv_flag(schema_create),
        "table_select": parse_csv_flag(table_select),
        "table_all": parse_csv_flag(table_all),
        "sequence_usage": parse_csv_flag(sequence_usage),
        "default_table_select": parse_csv_flag(default_table_select),
        "default_table_all": parse_csv_flag(default_table_all),
        "member_of": parse_csv_flag(member_of),
        "grant_to": parse_csv_flag(grant_to),
    }

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

    conn_kwargs = conn_params.as_psycopg_kwargs()
    dialect = dialect_for(conn_params.engine)
    warnings: list[str] = []

    # Resolve the database list and pre-flight existence check on a single
    # connection — we do this even for dry-run because the rendered SQL is
    # the same for every DB and we want to fail early on a typo.
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
                conflicts = [n for n in names if dialect.role_exists(cursor, n)]
                member_parents = parse_csv_flag(member_of)
                parent_kinds: dict[str, ParentKind] = {}
                absent_parents: list[str] = []
                for parent in member_parents:
                    kind = dialect.resolve_parent_kind(cursor, parent)
                    if kind is ParentKind.absent:
                        absent_parents.append(parent)
                    else:
                        parent_kinds[parent] = kind
                grant_targets = parse_csv_flag(grant_to)
                grantee_kinds: dict[str, ParentKind] = {}
                absent_targets: list[str] = []
                for grant_target in grant_targets:
                    kind = dialect.resolve_grantee_kind(cursor, grant_target)
                    if kind is ParentKind.absent:
                        absent_targets.append(grant_target)
                    else:
                        grantee_kinds[grant_target] = kind
            if absent_parents:
                console.print(
                    "[red]Error: --member-of parent(s) not found: "
                    f"{', '.join(esc(p) for p in absent_parents)}[/red]"
                )
                raise typer.Exit(code=1)
            if absent_targets:
                console.print(
                    "[red]Error: --grant-to target(s) not found: "
                    f"{', '.join(esc(t) for t in absent_targets)}[/red]"
                )
                raise typer.Exit(code=1)
            sample_conn_for_render = conn
            specs: list[CreateRoleSpec] = []
            for name in names:
                specs.append(
                    CreateRoleSpec(
                        name=name,
                        password=None
                        if no_login
                        else generate_password(password_length),
                        **spec_kwargs,
                    )
                )
            try:
                plans = [
                    build_create_plan(
                        s,
                        target_dbs,
                        dialect,
                        parent_kinds=parent_kinds,
                        grantee_kinds=grantee_kinds,
                        warnings=warnings,
                    )
                    for s in specs
                ]
            except ValueError as exc:
                console.print(f"[red]Error: {esc(exc)}[/red]")
                raise typer.Exit(code=1)
            if conflicts:
                console.print(
                    "[red]Error: role(s) already exist: "
                    f"{', '.join(esc(c) for c in conflicts)}[/red]"
                )
                raise typer.Exit(code=1)
            console.print(
                f"[bold]Plan:[/bold] create {len(specs)} role(s) "
                f"across {len(target_dbs)} database(s) "
                f"({', '.join(esc(d) for d in target_dbs)})"
            )
            for w in warnings:
                # Warnings are our own fixed strings — nothing to escape.
                console.print(f"[yellow]Warning: {w}[/yellow]")
            _render_plan(console, plans, sample_conn_for_render)
    except psycopg.Error as exc:
        console.print(f"[red]Database error: {esc(exc)}[/red]")
        raise typer.Exit(code=1)

    if dry_run:
        console.print("\n[yellow]Dry-run; no SQL executed.[/yellow]")
        return

    confirm_or_exit(yes=yes, prompt="\nProceed?", console=console)

    # Open credentials file before executing — fail fast on permission issues.
    # --no-login generates no passwords, so no credentials file is touched.
    needs_credentials = any(s.password is not None for s in specs)
    creds_path = credentials_out or _credentials_default_path()
    creds_file = None
    writer = None
    secure = True
    if needs_credentials:
        creds_file, is_new_file = _open_credentials_file(creds_path)
        secure = _file_mode_secure(creds_path)
        writer = csv.writer(creds_file)
        if is_new_file:
            writer.writerow(["username", "password", "created_at", "databases"])
    try:
        created_at = datetime.now(UTC).isoformat(timespec="seconds")

        console.print()
        for plan, spec in zip(plans, specs, strict=True):
            try:
                _execute_cluster_ops(plan=plan, conn_params_kwargs=conn_kwargs)
            except psycopg.Error as exc:
                console.print(f"[red]✗ {esc(plan.role)}: {esc(exc)}[/red]")
                # Role was not created; don't write a row for it.
                raise typer.Exit(code=1)
            # The role (and its password) exists from this point on — record
            # the credentials immediately so a later grant failure can never
            # leave an orphaned role with an unrecoverable password.
            if (
                writer is not None
                and creds_file is not None
                and spec.password is not None
            ):
                writer.writerow(
                    [
                        spec.name,
                        spec.password,
                        created_at,
                        ",".join(plan.per_database_ops.keys()),
                    ]
                )
                creds_file.flush()
            try:
                _execute_per_db_ops(
                    plan=plan,
                    conn_params_kwargs=conn_kwargs,
                    console=console,
                )
            except psycopg.Error as exc:
                console.print(
                    f"[red]✗ {esc(plan.role)}: grants failed: {esc(exc)}[/red]"
                )
                recorded = (
                    f" and its password is recorded in {esc(creds_path)}"
                    if spec.password is not None
                    else ""
                )
                console.print(
                    f"[yellow]The role was created{recorded}. Re-run grants "
                    "manually or drop the role with `dp db role drop`.[/yellow]"
                )
                raise typer.Exit(code=1)
    finally:
        if creds_file is not None:
            creds_file.close()

    if needs_credentials:
        console.print(f"\n[green]Wrote credentials to[/green] {esc(creds_path)}")
        if not secure:
            console.print(
                f"[yellow]Warning: {esc(creds_path)} is readable by group/other. "
                f"Run: chmod 600 {esc(creds_path)}[/yellow]"
            )
    else:
        console.print("\n[dim]No credentials generated (--no-login).[/dim]")

    table = Table(title="Created", show_header=True, header_style="bold")
    table.add_column("Role")
    table.add_column("Databases")
    for plan in plans:
        table.add_row(cell(plan.role), cell(", ".join(plan.per_database_ops.keys())))
    console.print(table)
