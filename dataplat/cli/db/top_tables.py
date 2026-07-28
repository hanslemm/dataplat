"""`dp db top-tables` — list the largest tables in schemas with a given prefix."""

from __future__ import annotations

import json
import sys

import psycopg
import typer
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from dataplat.cli._exit import fail
from dataplat.cli._options import json_option, yes_option
from dataplat.cli._prompt import confirm_or_exit
from dataplat.cli._render import cell, esc
from dataplat.cli.db._report import fmt_rows, fmt_size
from dataplat.core.errors import ConfigError, ValidationError
from dataplat.services.db.connection import SqlEngine, resolve_connection_params
from dataplat.services.db.targets import DbTarget, resolve_targets
from dataplat.services.db.top_tables import (
    TopTablesResult,
    drop_statement,
    fetch_top_tables,
)

_ENGINE_LABEL: dict[SqlEngine, str] = {
    SqlEngine.postgresql: "Postgres",
    SqlEngine.redshift: "Redshift",
}


def _targets_for(name: str, console: Console) -> list[DbTarget]:
    try:
        return resolve_targets(name)
    except ValidationError as exc:
        fail(exc, console=console)


def _split_prefixes(raw: list[str]) -> list[str]:
    """Flatten repeated ``--schema-prefix`` and split comma-separated values."""
    out: list[str] = []
    for item in raw:
        for piece in item.split(","):
            token = piece.strip()
            if token:
                out.append(token)
    seen: set[str] = set()
    unique: list[str] = []
    for p in out:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def _fmt_size(n: int | None) -> str:
    return fmt_size(n, colored=False)


def _fmt_rows(n: int | None) -> str:
    return fmt_rows(n, colored=False)


def _pct(numer: int | None, denom: int) -> str:
    if numer is None or denom <= 0:
        return "—"
    return f"{(numer / denom) * 100:.1f}%"


def _render_section(
    console: Console,
    target: DbTarget,
    result: TopTablesResult,
    prefixes: list[str],
) -> None:
    engine = target.engine
    label = _ENGINE_LABEL[engine]
    # --schema-prefix is user input, so the hint is external everywhere it is
    # interpolated below.
    prefix_hint = esc(", ".join(f"{p}*" for p in prefixes))
    console.print(
        f"\n[bold cyan]{label}[/bold cyan] [dim]— schemas: {prefix_hint}[/dim]"
    )

    if not result.rows:
        console.print("[yellow]  (no matching tables)[/yellow]")
        return

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
    table.add_column("#", style="dim", justify="right", width=4)
    table.add_column("Schema")
    table.add_column("Table")
    if engine is SqlEngine.postgresql:
        table.add_column("Owner", style="dim")
    table.add_column("Rows", justify="right")
    table.add_column("Size", justify="right", style="green")
    table.add_column("% of disk", justify="right", style="magenta")

    for i, row in enumerate(result.rows, start=1):
        cells: list[str | Text] = [str(i), cell(row.schema), cell(row.name)]
        if engine is SqlEngine.postgresql:
            cells.append(cell(row.owner or "—"))
        cells.extend(
            [
                _fmt_rows(row.row_estimate),
                _fmt_size(row.size_bytes),
                _pct(row.size_bytes, result.disk_bytes),
            ]
        )
        table.add_row(*cells)

    console.print(table)

    shown_bytes = sum(r.size_bytes or 0 for r in result.rows)
    remaining = max(0, result.matched_count - len(result.rows))
    console.print(
        f"[dim]  Database disk: [/dim]"
        f"[green]{_fmt_size(result.disk_bytes)}[/green]"
        f"  [dim]| matched {prefix_hint} "
        f"({result.matched_count:,} tables): [/dim]"
        f"[green]{_fmt_size(result.matched_bytes)}[/green] "
        f"[magenta]({_pct(result.matched_bytes, result.disk_bytes)} of disk)[/magenta]"
        f"  [dim]| top {len(result.rows)} shown: [/dim]"
        f"[green]{_fmt_size(shown_bytes)}[/green] "
        f"[magenta]({_pct(shown_bytes, result.disk_bytes)} of disk)[/magenta]"
        f"  [dim]({remaining:,} more not shown)[/dim]"
    )


def _render_drop_sql(
    target: DbTarget, result: TopTablesResult, prefixes: list[str]
) -> str:
    """Emit a review-ready DROP script for the top-N rows. No execution."""
    label = _ENGINE_LABEL[target.engine]
    prefix_hint = ", ".join(f"{p}*" for p in prefixes)
    lines = [
        f"-- {label} — schemas matching {prefix_hint}",
        f"-- Run against {target.env_prefix}_* (e.g. `dp db query -t {target.name} -`)",
        f"-- Database disk: {_fmt_size(result.disk_bytes)}; matched "
        f"{result.matched_count} tables = {_fmt_size(result.matched_bytes)} "
        f"({_pct(result.matched_bytes, result.disk_bytes)} of disk).",
        f"-- Emitting {len(result.rows)} DROP statements (percentages of disk).",
        "-- Review before running. No CASCADE: dependent views/FKs will block.",
    ]
    if not result.rows:
        lines.append("-- (no tables to drop)")
    else:
        lines.append("BEGIN;")
        for row in result.rows:
            size = _fmt_size(row.size_bytes)
            pct = _pct(row.size_bytes, result.disk_bytes)
            lines.append(f"{drop_statement(row)}  -- {size} ({pct} of disk)")
        lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


def _connection_params(target: DbTarget):
    return resolve_connection_params(
        engine=target.engine,
        env_prefix=target.env_prefix,
        user=None,
        password=None,
        host=None,
        port=None,
        database=None,
        sslmode=None,
    )


def _collect(target: DbTarget, prefixes: list[str], limit: int) -> TopTablesResult:
    params = _connection_params(target)
    with (
        psycopg.connect(**params.as_psycopg_kwargs()) as conn,
        conn.cursor() as cursor,
    ):
        return fetch_top_tables(cursor, target.engine, prefixes, limit)


def _execute_drops(console: Console, target: DbTarget, result: TopTablesResult) -> int:
    """Run the DROP statements for ``result.rows`` in a single transaction."""
    params = _connection_params(target)
    with psycopg.connect(**params.as_psycopg_kwargs()) as conn:
        with conn.cursor() as cursor:
            for row in result.rows:
                cursor.execute(drop_statement(row))
                console.print(
                    f"  [green]✓[/green] dropped {esc(row.schema)}.{esc(row.name)}"
                )
        conn.commit()
    return len(result.rows)


def top_tables_command(
    target: str = typer.Option(
        "all",
        "--target",
        "-t",
        help="Named DB target from DP_TARGETS, or all (default).",
    ),
    schema_prefix: list[str] = typer.Option(
        ["dev_"],
        "--schema-prefix",
        help=(
            "Schema name prefix to include (repeatable; comma-separated "
            "values also accepted). Defaults to 'dev_'."
        ),
    ),
    limit: int = typer.Option(
        20, "--limit", "-n", min=1, help="Number of tables per database."
    ),
    as_json: bool = json_option("Emit machine-readable JSON to stdout."),
    drop_sql: bool = typer.Option(
        False,
        "--drop-sql",
        help=(
            "Emit a review-ready DROP script for the top-N rows to stdout. "
            "Does NOT execute — pipe to psql / `dp db query` yourself."
        ),
    ),
    drop: bool = typer.Option(
        False,
        "--drop",
        help="DROP the listed tables after showing them and confirming.",
    ),
    yes: bool = yes_option("Skip the --drop confirmation prompt."),
) -> None:
    """Rank the largest tables in schemas matching a prefix.

    Useful for finding disk-hungry dev/sandbox tables that are candidates
    for cleanup. Runs against every target from ``DP_TARGETS`` (or one via
    ``--target``). Output is grouped by database and sorted by total size
    descending.
    """
    console = Console()
    prefixes = _split_prefixes(schema_prefix)
    if not prefixes:
        console.print("[red]Error: --schema-prefix must not be empty[/red]")
        raise typer.Exit(code=1)

    if sum([as_json, drop_sql, drop]) > 1:
        console.print(
            "[red]Error: --json, --drop-sql, and --drop are mutually exclusive[/red]"
        )
        raise typer.Exit(code=1)

    targets = _targets_for(target, console)
    results: dict[str, TopTablesResult] = {}
    errors: dict[str, str] = {}

    for tgt in targets:
        try:
            results[tgt.name] = _collect(tgt, prefixes, limit)
        except ConfigError as exc:
            errors[tgt.name] = str(exc)
        except psycopg.Error as exc:
            errors[tgt.name] = f"database error: {exc}"

    if as_json:
        payload = {
            "schema_prefixes": prefixes,
            "limit": limit,
            "databases": {
                tgt.name: {
                    "engine": tgt.engine.value,
                    "label": _ENGINE_LABEL[tgt.engine],
                    "error": errors.get(tgt.name),
                    **(
                        results[tgt.name].to_dict()
                        if tgt.name in results
                        else {"rows": [], "total_bytes": 0, "total_count": 0}
                    ),
                }
                for tgt in targets
            },
        }
        sys.stdout.write(json.dumps(payload, indent=2, default=str) + "\n")
        if errors:
            raise typer.Exit(code=1)
        return

    if drop_sql:
        for tgt in targets:
            if tgt.name in errors:
                sys.stdout.write(
                    f"-- {_ENGINE_LABEL[tgt.engine]} — ERROR: {errors[tgt.name]}\n\n"
                )
                continue
            sys.stdout.write(_render_drop_sql(tgt, results[tgt.name], prefixes))
            sys.stdout.write("\n")
        if errors:
            raise typer.Exit(code=1)
        return

    for tgt in targets:
        if tgt.name in errors:
            console.print(
                f"\n[bold cyan]{_ENGINE_LABEL[tgt.engine]}[/bold cyan] "
                f"[red]— {esc(errors[tgt.name])}[/red]"
            )
            continue
        _render_section(console, tgt, results[tgt.name], prefixes)

    if drop:
        total = sum(len(results[t.name].rows) for t in targets if t.name in results)
        if total == 0:
            console.print("\n[yellow]Nothing to drop.[/yellow]")
        else:
            console.print()
            confirm_or_exit(
                yes=yes,
                prompt=(
                    f"DROP the {total} table(s) listed above? This cannot be undone."
                ),
                console=console,
            )
            for tgt in targets:
                if tgt.name not in results or not results[tgt.name].rows:
                    continue
                console.print(f"[bold cyan]{_ENGINE_LABEL[tgt.engine]}[/bold cyan]")
                try:
                    _execute_drops(console, tgt, results[tgt.name])
                except psycopg.Error as exc:
                    errors[tgt.name] = f"database error during drop: {exc}"
                    console.print(f"  [red]✗ {esc(exc)}[/red]")

    if errors:
        raise typer.Exit(code=1)
