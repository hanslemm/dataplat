"""Database query CLI commands."""

from __future__ import annotations

import csv
import json
import re
import sys
from enum import Enum
from shutil import get_terminal_size
from time import perf_counter
from typing import cast

import typer
from psycopg.abc import Query
from rich import box
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from dataplat.cli._prompt import confirm_or_exit
from dataplat.cli._render import cell, esc, shorten
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
from dataplat.cli.db.dbt_orphans import app as dbt_orphans_app
from dataplat.cli.db.describe import app as describe_app
from dataplat.cli.db.long_queries import kill_command, long_queries_command
from dataplat.cli.db.role import app as role_app
from dataplat.cli.db.schema import app as schema_app
from dataplat.cli.db.top_tables import top_tables_command
from dataplat.services.db.connection import SqlEngine

app = typer.Typer(
    name="db",
    help="Database query commands",
    no_args_is_help=True,
)
app.add_typer(dbt_orphans_app, name="dbt-orphans")
app.add_typer(describe_app, name="describe")
app.add_typer(role_app, name="role")
app.add_typer(schema_app, name="schema")
app.command(
    "top-tables",
    help="Rank largest tables in schemas matching a prefix (default: dev_*).",
)(top_tables_command)
app.command(
    "long-queries",
    help="Show long-running (and recently failed) queries per target.",
)(long_queries_command)
app.command(
    "kill",
    help="Cancel or terminate running queries by PID.",
)(kill_command)

console = Console()
err_console = Console(stderr=True)


class OutputFormat(str, Enum):
    """Result rendering for ``dp db query``."""

    table = "table"
    csv = "csv"
    json = "json"


_SQL_COMMENT_RE = re.compile(r"(--[^\n]*)|(/\*.*?\*/)", re.DOTALL)
_WRITE_KEYWORD_RE = re.compile(r"\b(insert|update|delete|merge)\b", re.IGNORECASE)

# Statement heads that cannot modify anything on any supported engine.
#
# The last four are DuckDB's own query forms, probed on 1.5.5: DESCRIBE and
# SUMMARIZE return one row per column, PIVOT/UNPIVOT are reshaping queries.
# Listing them costs the libpq engines nothing — none is valid PostgreSQL or
# Redshift SQL, so those servers reject the statement as a syntax error exactly
# as they did before, which is a wrong statement failing rather than a write
# slipping through.
_READ_FIRST_KEYWORDS = {
    "select",
    "show",
    "explain",
    "table",
    "values",
    "describe",
    "summarize",
    "pivot",
    "unpivot",
}

# Heads whose statement is a read *unless* it carries a data-modifying keyword.
#
# ``with``: a CTE can hide an INSERT/UPDATE/DELETE with RETURNING.
# ``from``: DuckDB's FROM-first syntax, where ``FROM t`` means ``SELECT * FROM
# t`` — the form a DuckDB user types by hand, and the reason a read used to be
# stopped by the write gate. It is scanned rather than trusted outright even
# though no FROM-first write form was found (``FROM t DELETE WHERE …`` parses
# DELETE as a table alias and deletes nothing, probed on 1.5.5): the cost of
# scanning is a confirmation prompt on a query whose *literal* mentions
# "delete", and the cost of not scanning would be an unprompted write if a
# later DuckDB release grows one.
_SCANNED_FIRST_KEYWORDS = {"with", "from"}

# Heads the LIMIT/OFFSET wrapper may be wrapped around. Narrower than the read
# set on purpose: the wrapper is ``SELECT * FROM (<statement>) AS dp_query``, and
# only a statement that is legal as a subquery may go inside one — ``EXPLAIN``
# and ``SHOW`` are not. All three were probed inside the wrapper on duckdb 1.5.5;
# ``from`` matters most there, because an unpaginated ``FROM huge_table`` is
# exactly the runaway result set the wrapper exists to prevent.
_PAGINATED_FIRST_KEYWORDS = {"select", "with", "from"}


def _strip_sql_comments(sql: str) -> str:
    return _SQL_COMMENT_RE.sub(" ", sql)


def _classify_sql(sql: str) -> str:
    """Classify a statement as ``read`` or ``write``.

    Conservative in both directions: a WITH or FROM query containing any
    data-modifying keyword is treated as a write even if the keyword only
    appears in a literal, and any head not listed above is a write.

    That default is what covers the statements DuckDB has and PostgreSQL does
    not, and it is the right answer for every one of them: ``COPY`` writes a
    file or a table, ``EXPORT DATABASE`` writes a directory, ``IMPORT DATABASE``
    replays it, ``ATTACH`` creates the database file when it is missing (probed
    on 1.5.5), and ``INSTALL``/``LOAD`` fetch and execute an extension binary —
    not a data change, but the last thing that should happen without the user
    seeing it. So none of them needs a branch here; what they need is for
    nobody to add them to the read list.
    """
    cleaned = _strip_sql_comments(sql).strip()
    if not cleaned:
        return "read"
    first = cleaned.split(None, 1)[0].lower().rstrip(";")
    if first in _READ_FIRST_KEYWORDS:
        return "read"
    if first in _SCANNED_FIRST_KEYWORDS:
        return "write" if _WRITE_KEYWORD_RE.search(cleaned) else "read"
    return "write"


def _supports_live_query_progress(spinner_console: Console) -> bool:
    return (
        isinstance(spinner_console, Console)
        and spinner_console.is_terminal
        and not spinner_console.is_dumb_terminal
    )


# The Unix spelling of "the statement is on stdin". `dp db top-tables
# --drop-sql` has always printed `dp db query -t <target> -` as the way to run
# the script it emits, and until this was here that invocation sent the server a
# statement consisting of one hyphen: `sql` was non-blank, so the stdin branch
# below was never reached. A statement that is literally "-" has no other
# meaning, so there is nothing to weigh against the convention.
_STDIN_SQL = "-"


def _load_sql(sql: str | None) -> str:
    if sql and sql.strip() and sql.strip() != _STDIN_SQL:
        return sql
    if not sys.stdin.isatty():
        return sys.stdin.read()
    console.print(
        "[red]Error: SQL is required. Provide it as an argument or via stdin.[/red]"
    )
    raise typer.Exit(code=1)


def _rows_fit_terminal(row_count: int) -> bool:
    return row_count <= _max_rows_for_terminal()


def _max_rows_for_terminal() -> int:
    term_lines = get_terminal_size((120, 30)).lines
    max_rows = term_lines - 6
    return max(1, max_rows)


def _default_page_limit() -> int:
    return min(100, _max_rows_for_terminal())


def _render_rows(columns: list[str], rows: list[tuple], start_index: int = 1) -> None:
    display_rows = rows
    if rows and not _rows_fit_terminal(len(rows)):
        max_rows = _max_rows_for_terminal()
        display_rows = rows[:max_rows]
        console.print(
            "[yellow]Result too large for terminal. "
            f"Showing top {len(display_rows)} rows.[/yellow]"
        )

    term_width = get_terminal_size((120, 30)).columns
    available_width = max(40, term_width - 6)
    per_col = max(12, min(40, available_width // max(1, len(columns) + 1)))
    max_cell_length = max(40, per_col * 4)

    table = Table(
        show_header=True,
        header_style="bold cyan",
        box=box.SIMPLE,
        show_lines=True,
    )
    table.add_column("#", style="dim", justify="right", no_wrap=True, width=4)
    for column in columns:
        # Column labels are result-set aliases, i.e. whatever the query asked
        # for — `select 1 as "[/x]"` must not blow up the header.
        table.add_column(cell(column), overflow="fold", max_width=per_col)

    for idx, row in enumerate(display_rows, start=start_index):
        table.add_row(
            str(idx), *[cell(value, max_length=max_cell_length) for value in row]
        )

    console.print(table)
    console.print(f"[dim]Rows: {len(rows)}[/dim]")


def _emit_csv(columns: list[str], rows: list[tuple]) -> None:
    writer = csv.writer(sys.stdout)
    writer.writerow(columns)
    writer.writerows(rows)


def _emit_json(columns: list[str], rows: list[tuple]) -> None:
    payload = [dict(zip(columns, row, strict=False)) for row in rows]
    typer.echo(json.dumps(payload, indent=2, default=str))


def _write_preview(sql_text: str) -> str:
    """One-line, length-capped echo of the statement being confirmed."""
    return shorten(" ".join(sql_text.split()), 120)


def _execute_query(
    *,
    sql: str | None,
    conn_cli: ConnCliParams,
    limit: int | None,
    page: int,
    write: bool,
    fmt: OutputFormat,
) -> None:
    sql_text = _load_sql(sql)
    if _classify_sql(sql_text) == "write":
        confirm_or_exit(
            "[yellow]This statement can modify data:[/yellow] "
            f"{esc(_write_preview(sql_text))}",
            yes=write,
            prompt="Continue?",
            hint="Pass --write to run it non-interactively.",
            console=console,
        )

    cleaned = sql_text.strip().rstrip(";")
    stripped = _strip_sql_comments(cleaned).strip()
    first_keyword = stripped.split(None, 1)[0].lower() if stripped else ""
    is_select = (
        first_keyword in _PAGINATED_FIRST_KEYWORDS and _classify_sql(cleaned) == "read"
    )

    # --limit 0 disables the LIMIT/OFFSET wrapper entirely.
    paginate = is_select and (limit is None or limit > 0)
    safe_limit = 1
    offset = 0
    if paginate:
        effective_limit = limit if limit is not None else _default_page_limit()
        safe_limit = max(1, effective_limit)
        safe_page = max(1, page)
        offset = (safe_page - 1) * safe_limit
        # Newlines guard against a trailing `-- comment` swallowing the paren.
        # The alias leaks into server-side error messages, so it names the tool
        # that added it.
        sql_text = (
            f"SELECT * FROM (\n{cleaned}\n) AS dp_query"
            f" LIMIT {safe_limit + 1} OFFSET {offset}"
        )

    # resolve_any_*, so a DuckDB target resolves to its own param shape instead
    # of being refused by the libpq resolver. This command needs nothing from
    # either shape — db_session takes both, and the SQL is the user's — which is
    # what makes it engine-agnostic where its siblings are not.
    conn_params = resolve_any_params_or_exit(conn_cli)
    # Decorative output goes to stderr for machine-readable formats.
    note = console if fmt == OutputFormat.table else err_console

    with db_session(conn_params) as conn, conn.cursor() as cursor:
        started = perf_counter()
        rows: list[tuple] = []
        columns: list[str] = []
        has_result_set = False

        if _supports_live_query_progress(note):
            with Progress(
                SpinnerColumn(),
                TextColumn("[cyan]Running query...[/cyan]"),
                TimeElapsedColumn(),
                # The spinner follows the same sink as the notices: stdout for a
                # table, stderr for --format json/csv. Painting it on stdout was
                # harmless while Rich only did so for a real terminal, where the
                # frames are erased — but FORCE_COLOR makes is_terminal true for
                # a pipe too, and then `--format json > file` collected the
                # escape sequences and stopped parsing.
                console=note,
                transient=True,
                # Rich's Live replaces both streams with proxies that paint into
                # its own console. Left at the default, the `--verbose` SQL trace
                # surfaced on stdout for the duration of the query — the one
                # thing dataplat.core.trace promises never to do — and anything
                # written meanwhile would land in the spinner's stream rather
                # than the caller's.
                redirect_stderr=False,
                redirect_stdout=False,
            ) as progress:
                progress.add_task("query", total=None)
                cursor.execute(cast(Query, sql_text))
                if cursor.description:
                    has_result_set = True
                    rows = cursor.fetchall()
                    columns = [desc.name for desc in cursor.description]
        else:
            cursor.execute(cast(Query, sql_text))
            if cursor.description:
                has_result_set = True
                rows = cursor.fetchall()
                columns = [desc.name for desc in cursor.description]

        if has_result_set:
            visible = rows
            more = False
            if paginate and len(rows) > safe_limit:
                visible = rows[:safe_limit]
                more = True
            if fmt == OutputFormat.csv:
                _emit_csv(columns, visible)
            elif fmt == OutputFormat.json:
                _emit_json(columns, visible)
            else:
                start_index = offset + 1 if paginate else 1
                _render_rows(columns, visible, start_index=start_index)
            if more:
                note.print(
                    "[yellow]More rows available. "
                    f"Use --page {page + 1} to fetch the next page.[/yellow]"
                )
        else:
            # psycopg only. DuckDB answers every statement with a result set of
            # its own — a 'Count' column for DML and DDL, 'Success' for the rest
            # (probed on 1.5.5) — so this branch cannot be reached there, which
            # is fortunate: its rowcount is -1 for everything, and "-1 rows
            # affected" is what printing it anyway would produce.
            note.print(f"[green]✓ {cursor.rowcount} rows affected[/green]")
        elapsed = perf_counter() - started
        note.print(f"[dim]Execution time: {elapsed:.3f}s[/dim]")


@app.command("query")
def query(
    sql: str | None = typer.Argument(
        None, help="SQL to execute. If omitted, SQL is read from stdin."
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
    limit: int | None = typer.Option(
        None,
        "--limit",
        "-n",
        help=(
            "Max rows per page for SELECT queries "
            "(default: min(100, terminal height); 0 = no limit)"
        ),
    ),
    page: int = typer.Option(1, "--page", help="Page number for SELECT queries"),
    fmt: OutputFormat = typer.Option(
        OutputFormat.table, "--format", help="Output format: table, csv, or json."
    ),
    write: bool = typer.Option(
        False,
        "--write",
        help="Allow statements that modify data without prompting.",
    ),
) -> None:
    """Run ad-hoc SQL against Postgres, Redshift or DuckDB.

    The SQL is yours and is sent as written — no dialect translation — so a
    DuckDB target takes DuckDB SQL and ``?`` placeholders, and a libpq target
    takes its own. The only statement dataplat composes is the LIMIT/OFFSET
    pagination wrapper around a paginated read.
    """
    _execute_query(
        sql=sql,
        conn_cli=ConnCliParams(
            target=target,
            engine=engine,
            user=user,
            password=password,
            database=database,
            host=host,
            port=port,
            sslmode=sslmode,
            env_prefix=env_prefix,
        ),
        limit=limit,
        page=page,
        write=write,
        fmt=fmt,
    )
