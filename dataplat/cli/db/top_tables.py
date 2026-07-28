"""`dp db top-tables` — list the largest tables in schemas with a given prefix."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager

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
from dataplat.cli.db._common import DuckDbCursor
from dataplat.cli.db._report import fmt_rows, fmt_size
from dataplat.core.errors import ConfigError, DataplatError, ValidationError
from dataplat.services.db.connection import (
    ConnectionParams,
    DuckDbConnectionParams,
    SqlEngine,
    ensure_duckdb_database_exists,
    load_duckdb,
    resolve_engine_params,
)
from dataplat.services.db.targets import DbTarget, resolve_targets
from dataplat.services.db.top_tables import (
    SIZE_BASIS,
    TopTablesResult,
    drop_statement,
    fetch_top_tables,
)

_ENGINE_LABEL: dict[SqlEngine, str] = {
    SqlEngine.postgresql: "Postgres",
    SqlEngine.redshift: "Redshift",
    SqlEngine.duckdb: "DuckDB",
}


class _DriverError(DataplatError):
    """A driver-level failure from one target, so the run can reach the next.

    This command reports one line per target and exits 1 at the end rather than
    stopping at the first failure, which is why it opens its own connections
    instead of going through ``db_session`` (that funnel exits on a driver
    error, by design, for the single-target commands). psycopg and duckdb share
    no exception base class, and ``duckdb.Error`` cannot even be named until the
    optional driver is imported — so the DuckDB branch translates into this,
    and the loop catches one type per driver family.
    """


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


def _sizes_unknown(engine: SqlEngine) -> bool:
    """Whether this engine reports no per-table byte size.

    True only for DuckDB, and it changes what the report *shows*, not just the
    numbers in it: a Size column of "—" on every row and a "0 B (0.0% of disk)"
    footer would read as a dataplat defect rather than as the engine's answer.
    So the two byte-valued columns are dropped and :data:`SIZE_BASIS` says why,
    in the section a reader is already looking at.
    """
    return engine is SqlEngine.duckdb


def _render_section(
    console: Console,
    target: DbTarget,
    result: TopTablesResult,
    prefixes: list[str],
) -> None:
    engine = target.engine
    label = _ENGINE_LABEL[engine]
    sizeless = _sizes_unknown(engine)
    # --schema-prefix is user input, so the hint is external everywhere it is
    # interpolated below.
    prefix_hint = esc(", ".join(f"{p}*" for p in prefixes))
    ranked_by = "estimated rows" if sizeless else "size"
    console.print(
        f"\n[bold cyan]{label}[/bold cyan] [dim]— schemas: {prefix_hint}"
        f" — ranked by {ranked_by}[/dim]"
    )
    if sizeless:
        # Printed for the engine whose numbers would otherwise be misread. The
        # basis for the other two is what a reader of `dp db top-tables` has
        # always been shown (a Size column of real bytes), so repeating it on
        # every section would be noise; --json carries it for all three.
        console.print(f"[dim italic]  {SIZE_BASIS[engine]}.[/dim italic]")

    if not result.rows:
        console.print("[yellow]  (no matching tables)[/yellow]")
        return

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
    table.add_column("#", style="dim", justify="right", width=4)
    table.add_column("Schema")
    table.add_column("Table")
    if engine is SqlEngine.postgresql:
        table.add_column("Owner", style="dim")
    table.add_column("Rows (est.)" if sizeless else "Rows", justify="right")
    if not sizeless:
        table.add_column("Size", justify="right", style="green")
        table.add_column("% of disk", justify="right", style="magenta")

    for i, row in enumerate(result.rows, start=1):
        cells: list[str | Text] = [str(i), cell(row.schema), cell(row.name)]
        if engine is SqlEngine.postgresql:
            cells.append(cell(row.owner or "—"))
        cells.append(_fmt_rows(row.row_estimate))
        if not sizeless:
            cells.extend(
                [
                    _fmt_size(row.size_bytes),
                    _pct(row.size_bytes, result.disk_bytes),
                ]
            )
        table.add_row(*cells)

    console.print(table)

    remaining = max(0, result.matched_count - len(result.rows))
    if sizeless:
        # No shares, and the file total is named for what it is: it covers every
        # schema in the database, so it is not a denominator for the rows above.
        console.print(
            f"[dim]  Database file: [/dim]"
            f"[green]{_fmt_size(result.disk_bytes)}[/green]"
            f"  [dim]| matched {prefix_hint} "
            f"({result.matched_count:,} tables), sizes unknown"
            f"  | top {len(result.rows)} shown"
            f"  ({remaining:,} more not shown)[/dim]"
        )
        return

    shown_bytes = sum(r.size_bytes or 0 for r in result.rows)
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
    engine = target.engine
    label = _ENGINE_LABEL[engine]
    sizeless = _sizes_unknown(engine)
    prefix_hint = ", ".join(f"{p}*" for p in prefixes)
    lines = [
        f"-- {label} — schemas matching {prefix_hint}",
        f"-- Run against {target.env_prefix}_* (e.g. `dp db query -t {target.name} -`)",
    ]
    if sizeless:
        lines += [
            f"-- Database file: {_fmt_size(result.disk_bytes)}; matched "
            f"{result.matched_count} tables, sizes unknown.",
            f"-- {SIZE_BASIS[engine]}.",
            f"-- Emitting {len(result.rows)} DROP statements "
            "(largest estimated row count first).",
            # Probed on duckdb 1.5.5, and the opposite of what the libpq note
            # below promises: enable_view_dependencies defaults to false, so a
            # DROP TABLE succeeds while a view still selects from the table and
            # leaves that view in the catalog, broken. A foreign-key child does
            # block the drop. There is no CASCADE either way.
            "-- Review before running. DuckDB does NOT block a drop on a "
            "dependent view (it",
            "-- leaves the view broken); a foreign-key child does block it. "
            "No CASCADE.",
        ]
    else:
        lines += [
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
            if sizeless:
                lines.append(
                    f"{drop_statement(row)}  -- ~{_fmt_rows(row.row_estimate)} rows"
                )
                continue
            size = _fmt_size(row.size_bytes)
            pct = _pct(row.size_bytes, result.disk_bytes)
            lines.append(f"{drop_statement(row)}  -- {size} ({pct} of disk)")
        lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


def _connection_params(target: DbTarget) -> ConnectionParams:
    """Resolve the target's settings, in whichever shape its engine needs.

    ``resolve_engine_params``, not ``resolve_connection_params``: the libpq-only
    resolver raises a ValidationError for a DuckDB target, and this command
    catches ConfigError and driver errors only — so `-t all` over a config that
    includes one DuckDB target used to abort the whole run with a traceback.
    """
    return resolve_engine_params(
        engine=target.engine,
        env_prefix=target.env_prefix,
        user=None,
        password=None,
        host=None,
        port=None,
        database=None,
        sslmode=None,
    )


@contextmanager
def _duckdb_cursor(params: DuckDbConnectionParams) -> Iterator[DuckDbCursor]:
    """Open the DuckDB database and yield a cursor over it.

    ``DuckDbCursor`` rather than the raw connection, even though the raw
    connection has the same three methods this file calls: it is the seam that
    writes every statement to the ``--verbose`` tracer, and a new code path that
    skipped it would be building the gap in deliberately.

    ``load_duckdb`` and ``ensure_duckdb_database_exists`` raise ConfigError,
    which the caller already reports per target — a missing driver package or a
    path that is not there is exactly as much this target's problem as a missing
    ``<PREFIX>_HOST`` is for a server. Everything the driver raises becomes a
    :class:`_DriverError` here, in one place, so neither caller has to name
    ``duckdb.Error`` (which cannot be named before the import).
    """
    duckdb = load_duckdb()
    ensure_duckdb_database_exists(params)
    try:
        connection = duckdb.connect(database=params.path, read_only=params.read_only)
    except duckdb.Error as exc:
        raise _DriverError(str(exc)) from exc
    try:
        yield DuckDbCursor(connection)
    except duckdb.Error as exc:
        raise _DriverError(str(exc)) from exc
    finally:
        connection.close()


def _collect(target: DbTarget, prefixes: list[str], limit: int) -> TopTablesResult:
    params = _connection_params(target)
    if isinstance(params, DuckDbConnectionParams):
        with _duckdb_cursor(params) as ddb_cursor:
            return fetch_top_tables(ddb_cursor, target.engine, prefixes, limit)
    # The ignore is psycopg's connect() signature, not a doubt about the values:
    # as_psycopg_kwargs is dict[str, str | int | None] and connect() types each
    # keyword separately, so **-expansion cannot be matched. Same ignore, same
    # reason, as the one in cli/db/_common.py's psycopg session.
    with (
        psycopg.connect(**params.as_psycopg_kwargs()) as conn,  # type: ignore[arg-type]
        conn.cursor() as cursor,
    ):
        return fetch_top_tables(cursor, target.engine, prefixes, limit)


def _execute_drops(console: Console, target: DbTarget, result: TopTablesResult) -> int:
    """Run the DROP statements for ``result.rows`` in a single transaction."""
    params = _connection_params(target)
    if isinstance(params, DuckDbConnectionParams):
        return _execute_duckdb_drops(console, params, result)
    with psycopg.connect(**params.as_psycopg_kwargs()) as conn:  # type: ignore[arg-type]
        with conn.cursor() as cursor:
            for row in result.rows:
                cursor.execute(drop_statement(row))
                console.print(
                    f"  [green]✓[/green] dropped {esc(row.schema)}.{esc(row.name)}"
                )
        conn.commit()
    return len(result.rows)


def _execute_duckdb_drops(
    console: Console, params: DuckDbConnectionParams, result: TopTablesResult
) -> int:
    """The DuckDB half of :func:`_execute_drops`, one transaction like psycopg's.

    ``BEGIN TRANSACTION``/``COMMIT`` in SQL rather than the driver's
    ``begin()``/``commit()``, so the statements that run are the ones
    ``--drop-sql`` printed for review. DuckDB rolls DDL back (probed on 1.5.5),
    so a drop that fails part-way through — a foreign-key child blocks one, or
    the target is ``<PREFIX>_READ_ONLY`` — leaves the database untouched.
    """
    with _duckdb_cursor(params) as cursor:
        cursor.execute("BEGIN TRANSACTION")
        for row in result.rows:
            cursor.execute(drop_statement(row))
            console.print(
                f"  [green]✓[/green] dropped {esc(row.schema)}.{esc(row.name)}"
            )
        cursor.execute("COMMIT")
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
    descending — except on DuckDB, which reports no per-table size, where it is
    sorted by estimated row count and every size reads "unknown". The section
    header and ``--json``'s ``size_basis`` say which you are looking at.
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
        except (psycopg.Error, _DriverError) as exc:
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
                    # What the numbers are, per engine, so a consumer merging
                    # two databases can see that a null size_bytes is DuckDB's
                    # answer rather than a bug, and that the disk_bytes of two
                    # engines are not the same measurement.
                    "ranked_by": (
                        "row_estimate" if _sizes_unknown(tgt.engine) else "size_bytes"
                    ),
                    "size_basis": SIZE_BASIS[tgt.engine],
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
                except (psycopg.Error, _DriverError) as exc:
                    errors[tgt.name] = f"database error during drop: {exc}"
                    console.print(f"  [red]✗ {esc(exc)}[/red]")

    if errors:
        raise typer.Exit(code=1)
