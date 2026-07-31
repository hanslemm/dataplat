"""``dp db schema drop`` — drop one or more schemas.

Destructive, so the pre-flight is the point: one ``list_schemas`` call supplies
owner and object counts for every named schema, and those are shown *before* the
confirmation, so ``--cascade``'s blast radius is visible rather than implied.

``RESTRICT`` is the default and is emitted explicitly. The difference between
"fails if anything is in it" and "destroys everything in it" is the whole question
this command turns on, and it should never be inferred from a server default.

``--like`` selects by pattern instead of by name. Protected schemas are re-checked
after matching, not only when named: ``list_schemas``'s own predicate hides
``pg_*`` and ``information_schema`` but not ``public``, ``main`` or
``catalog_history``, so without the second check ``--like`` could reach a schema
that an explicit name would be refused for.
"""

from __future__ import annotations

import typer
from rich import box as _box
from rich.console import Console
from rich.table import Table

from dataplat.cli._exit import fail
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
    db_session,
    resolve_any_params_or_exit,
)
from dataplat.cli.db._plan import execute_ops, print_ops
from dataplat.cli.db._schema_opts import SchemaLikeOption, is_protected_schema
from dataplat.core.errors import ValidationError
from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.role_admin import parse_csv_flag
from dataplat.services.db.schema_admin import (
    SchemaSummary,
    build_drop_plan,
    translate_like_pattern,
)
from dataplat.services.db.schema_dialects import schema_dialect_for


def _render_preflight(console: Console, rows: list[SchemaSummary]) -> None:
    """What each schema holds, before anything is destroyed."""
    table = Table(
        box=_box.HORIZONTALS,
        show_header=True,
        header_style="bold",
        padding=(0, 2),
        pad_edge=False,
        show_edge=False,
    )
    table.add_column("Schema")
    table.add_column("Owner", style="dim")
    table.add_column("Tables", justify="right")
    table.add_column("Views", justify="right")
    table.add_column("Other", justify="right")
    for row in rows:
        table.add_row(
            cell(row.name),
            cell(row.owner),
            str(row.tables),
            str(row.views),
            str(row.other),
        )
    console.print(table)


def drop_command(
    names: list[str] | None = typer.Argument(
        None, help="One or more schema names to drop. Comma-separated accepted."
    ),
    cascade: bool = typer.Option(
        False,
        "--cascade",
        help="Drop contained objects too. Without it, RESTRICT fails on a "
        "non-empty schema.",
    ),
    like: str | None = SchemaLikeOption,
    if_exists: bool = typer.Option(
        False, "--if-exists", help="Do not fail when the schema is not there."
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
    """Entry point for ``dp db schema drop``."""
    console = Console()
    named = parse_csv_flag(names)
    if named and like:
        raise typer.BadParameter("pass schema names or --like, not both")
    if not named and not like:
        raise typer.BadParameter("name at least one schema, or pass --like")

    conn_params = resolve_any_params_or_exit(
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
    dialect = schema_dialect_for(conn_params.engine)

    with db_session(conn_params) as conn, conn.cursor() as cursor:
        if like:
            matched = [
                row
                for row in dialect.list_schemas(
                    cursor, like=translate_like_pattern(like)
                )
                # Re-applied after matching: see the module docstring.
                if not is_protected_schema(row.name)
            ]
            if not matched:
                fail(ValidationError(f'no schemas matched "{like}".'), console=console)
            targets = [row.name for row in matched]
            rows = matched
        else:
            protected = [n for n in named if is_protected_schema(n)]
            if protected:
                fail(
                    ValidationError(
                        "refusing to drop protected schema(s): "
                        f"{', '.join(protected)}. These are catalog or default "
                        "schemas that the database itself depends on."
                    ),
                    console=console,
                )
            targets = list(named)
            found = {row.name: row for row in dialect.list_schemas(cursor)}
            missing = [n for n in targets if n not in found]
            if missing and not if_exists:
                fail(
                    ValidationError(
                        f"schema(s) not found: {', '.join(missing)}. "
                        "Pass --if-exists to ignore."
                    ),
                    console=console,
                )
            rows = [found[n] for n in targets if n in found]

        try:
            plan = build_drop_plan(targets, cascade=cascade, if_exists=if_exists)
        except ValidationError as exc:
            fail(exc, console=console)

        console.print(f"[bold]Plan:[/bold] drop {len(plan.ops)} schema(s)")
        if rows:
            _render_preflight(console, rows)
        contents = sum(row.object_count for row in rows)
        if contents and not cascade:
            # RESTRICT will refuse; say so now rather than after a confirmation.
            console.print(
                f"[yellow]Warning: {contents} object(s) in scope and --cascade "
                "was not passed. RESTRICT will refuse to drop a non-empty "
                "schema.[/yellow]"
            )
        elif contents:
            console.print(f"[red]This will destroy {contents} object(s).[/red]")
        print_ops(console, plan.ops, conn)

        if dry_run:
            console.print("\n[yellow]Dry-run; no SQL executed.[/yellow]")
            return
        confirm_or_exit(yes=yes, prompt="\nProceed?", console=console)
        execute_ops(cursor, plan.ops)

    console.print(f"\n[green]Dropped:[/green] {', '.join(esc(n) for n in targets)}")
