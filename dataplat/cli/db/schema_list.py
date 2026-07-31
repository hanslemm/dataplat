"""``dp db schema list`` — schemas with owner and object counts.

Works on all three engines, which is why the dialect answers rather than this
module: PostgreSQL resolves the owner through ``pg_roles``, Redshift through
``pg_user`` and adds quotas, and DuckDB has no ``pg_roles`` at all.

The quota columns appear only when the target reports them. Redshift is the only
engine with schema quotas, and even there the view is version-dependent — so an
unknown quota renders as ``?``, never as ``0``, because "nobody could tell" and
"no limit" are different answers.
"""

from __future__ import annotations

import dataclasses
import json
import sys

import typer
from rich import box as _box
from rich.console import Console
from rich.table import Table
from rich.text import Text

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
    resolve_any_params_or_exit,
)
from dataplat.services.db._like import glob_to_like
from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.schema_admin import SchemaSummary
from dataplat.services.db.schema_dialects import schema_dialect_for


def _mb(value: int | None) -> str:
    """Render a megabyte count, distinguishing unknown from zero."""
    if value is None:
        return "?"
    if value >= 1024:
        return f"{value / 1024:.1f} GB"
    return f"{value} MB"


def _render(console: Console, rows: list[SchemaSummary]) -> None:
    # Quota columns are Redshift-only, and on a cluster whose quota view is
    # unavailable every value is None. Showing two columns of "?" to every
    # Postgres user would be noise, so they appear only when something is known.
    has_quota = any(r.quota_mb is not None or r.used_mb is not None for r in rows)

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
    if has_quota:
        table.add_column("Used", justify="right")
        table.add_column("Quota", justify="right")

    for r in rows:
        # cell() for catalog-sourced names: a schema or owner containing `[` is
        # data, and Rich would read it as markup. The counts are our own digits.
        cells: list[str | Text] = [
            cell(r.name),
            cell(r.owner),
            str(r.tables),
            str(r.views),
        ]
        if has_quota:
            cells += [_mb(r.used_mb), _mb(r.quota_mb)]
        table.add_row(*cells)

    console.print(table)
    console.print(f"[dim]Total: {len(rows)} schema(s)[/dim]")


def list_command(
    like: str | None = typer.Option(
        None,
        "--like",
        help="Match schema names against a pattern. Glob `*` works as SQL `%`, "
        "so `dev_*` and `dev_%` are equivalent.",
    ),
    include_system: bool = typer.Option(
        False,
        "--include-system",
        help="Include catalog schemas (pg_catalog, information_schema, …).",
    ),
    as_json: bool = json_option("Emit schemas as a JSON array on stdout."),
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
    """Entry point for ``dp db schema list``."""
    console = Console()
    # resolve_any_: this command supports DuckDB, so the params may be either
    # shape. No require_capability call — every engine here has schemas, which is
    # exactly why this subcommand is not gated the way `role list` is.
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
    pattern = glob_to_like(like) if like is not None else None
    with db_session(conn_params) as conn, conn.cursor() as cursor:
        rows = dialect.list_schemas(cursor, include_system=include_system, like=pattern)

    if as_json:
        payload = [dataclasses.asdict(r) for r in rows]
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        return

    if not rows:
        console.print("[yellow]No schemas match.[/yellow]")
        return
    _render(console, rows)
