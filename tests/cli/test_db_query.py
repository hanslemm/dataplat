from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import duckdb
from rich.console import Console
from typer.testing import CliRunner

import dataplat.cli.db as db_cli
import dataplat.main as main_module
from dataplat.cli.db import _classify_sql, _render_rows, _write_preview
from dataplat.core import trace
from dataplat.core.errors import ExitCode

runner = CliRunner()


def _disable_envrc(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "load_envrc", lambda: None)


class TestClassifySql:
    def test_select_is_read(self) -> None:
        assert _classify_sql("SELECT 1") == "read"

    def test_leading_line_comment_select_is_read(self) -> None:
        assert _classify_sql("-- top\nSELECT 1") == "read"

    def test_leading_block_comment_select_is_read(self) -> None:
        assert _classify_sql("/* hi */ SELECT 1") == "read"

    def test_plain_with_is_read(self) -> None:
        assert _classify_sql("WITH x AS (SELECT 1) SELECT * FROM x") == "read"

    def test_data_modifying_cte_is_write(self) -> None:
        sql = "WITH moved AS (DELETE FROM a RETURNING *) SELECT * FROM moved"
        assert _classify_sql(sql) == "write"

    def test_insert_is_write(self) -> None:
        assert _classify_sql("INSERT INTO t VALUES (1)") == "write"

    def test_update_is_write(self) -> None:
        assert _classify_sql("UPDATE t SET a = 1") == "write"

    def test_explain_is_read(self) -> None:
        assert _classify_sql("EXPLAIN SELECT 1") == "read"

    def test_show_is_read(self) -> None:
        assert _classify_sql("SHOW search_path") == "read"

    def test_ddl_is_write(self) -> None:
        assert _classify_sql("DROP TABLE t") == "write"


class TestClassifyDuckDbStatements:
    """The heads DuckDB has and PostgreSQL does not.

    Each expectation below was checked against duckdb 1.5.5 rather than read in
    a table of keywords — the read ones because they must not be stopped by a
    write gate, the write ones because the gate is the only thing between them
    and a file on disk.
    """

    def test_from_first_query_is_read(self) -> None:
        # `FROM t` is DuckDB's spelling of `SELECT * FROM t`.
        assert _classify_sql("FROM dev.events") == "read"
        assert _classify_sql("from dev.events select id") == "read"

    def test_from_first_query_mentioning_a_write_keyword_is_write(self) -> None:
        """Conservative, exactly as the WITH branch is.

        ``FROM t DELETE WHERE …`` parses DELETE as a table alias on 1.5.5 and
        deletes nothing, so this is a false positive today — the trade is a
        prompt on a query with 'delete' in a literal, against an unprompted
        write if a FROM-first DELETE ever lands.
        """
        assert _classify_sql("FROM t WHERE note = 'delete me'") == "write"

    def test_describe_and_summarize_are_reads(self) -> None:
        assert _classify_sql("DESCRIBE dev.events") == "read"
        assert _classify_sql("SUMMARIZE dev.events") == "read"

    def test_pivot_statements_are_reads(self) -> None:
        assert _classify_sql("PIVOT sales ON year USING sum(amount)") == "read"
        assert _classify_sql("UNPIVOT sales ON q1, q2") == "read"

    def test_side_effecting_duckdb_statements_are_writes(self) -> None:
        # COPY writes a file or a table; EXPORT/IMPORT DATABASE a directory of
        # them; ATTACH creates the database file when it is missing; INSTALL and
        # LOAD fetch and execute an extension binary. None is a read, and the
        # classifier's default is what covers them — so this test is really
        # asserting that nobody added them to the read list.
        for statement in (
            "COPY events TO 'out.parquet'",
            "COPY events FROM 'in.csv'",
            "EXPORT DATABASE 'dump'",
            "IMPORT DATABASE 'dump'",
            "ATTACH 'other.duckdb' AS other",
            "INSTALL httpfs",
            "LOAD httpfs",
        ):
            assert _classify_sql(statement) == "write", statement


def test_write_statement_requires_write_flag_when_not_tty(monkeypatch) -> None:
    _disable_envrc(monkeypatch)

    result = runner.invoke(main_module.app, ["db", "query", "DELETE FROM t"])

    assert result.exit_code == 1
    assert "--write" in result.stdout


def test_write_statement_guard_precedes_connection(monkeypatch) -> None:
    """The guard must fire before any connection attempt."""
    _disable_envrc(monkeypatch)
    # No DEMO_PG_*/PG* env: if the guard ran after connection resolution,
    # we would see a missing-settings error instead of the write hint.
    for var in ("DEMO_PG_HOST", "PGHOST", "DB_HOST"):
        monkeypatch.delenv(var, raising=False)

    result = runner.invoke(main_module.app, ["db", "query", "DROP TABLE x"])

    assert result.exit_code == 1
    assert "--write" in result.stdout


def test_write_guard_previews_statement_with_markup(monkeypatch) -> None:
    """A statement carrying markup must be previewed, not crash the gate."""
    _disable_envrc(monkeypatch)

    result = runner.invoke(
        main_module.app, ["db", "query", "DELETE FROM t WHERE s = '[/x] [bold]'"]
    )

    assert result.exit_code == 1
    assert "This statement can modify data" in result.stdout
    assert "[/x] [bold]" in result.stdout
    assert "--write" in result.stdout


class TestWritePreview:
    def test_collapses_whitespace(self) -> None:
        assert _write_preview("DELETE\n  FROM   t") == "DELETE FROM t"

    def test_caps_at_120_chars(self) -> None:
        preview = _write_preview("DELETE FROM t WHERE x = '" + "a" * 200 + "'")
        assert len(preview) == 120
        assert preview.endswith("…")


class TestRenderRowsMarkupSafety:
    """Regression: warehouse text is data, never Rich markup."""

    def _render(self, monkeypatch, columns: list[str], rows: list[tuple]) -> str:
        recorder = Console(record=True, width=200)
        monkeypatch.setattr(db_cli, "console", recorder)
        _render_rows(columns, rows)
        return recorder.export_text()

    def test_closing_tag_value_does_not_crash(self, monkeypatch) -> None:
        # `dp db query "select 'closes [/issue] 42'"` used to raise MarkupError.
        out = self._render(monkeypatch, ["note"], [("closes [/issue] 42",)])
        assert "closes [/issue] 42" in out

    def test_style_name_value_is_not_swallowed(self, monkeypatch) -> None:
        out = self._render(monkeypatch, ["note"], [("[bold]not bold",)])
        assert "[bold]not bold" in out

    def test_column_alias_markup_is_literal(self, monkeypatch) -> None:
        out = self._render(monkeypatch, ["[/x]", "[bold]"], [("a", "b")])
        assert "[/x]" in out
        assert "[bold]" in out

    def test_none_renders_as_empty_cell(self, monkeypatch) -> None:
        out = self._render(monkeypatch, ["note"], [(None,)])
        assert "None" not in out


def _fake_connect(columns: list[str], rows: list[tuple]) -> tuple:
    """A psycopg.connect stand-in plus the cursor it hands out."""

    class _Cursor:
        description = [SimpleNamespace(name=c) for c in columns]
        rowcount = len(rows)

        def __init__(self) -> None:
            self.executed: list[str] = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def execute(self, sql, params=None) -> None:
            self.executed.append(str(sql))

        def fetchall(self) -> list[tuple]:
            return rows

    cursor = _Cursor()

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def cursor(self):
            return cursor

    return (lambda **kwargs: _Conn()), cursor


def _pg_env(monkeypatch) -> None:
    monkeypatch.setenv("DEMO_PG_HOST", "localhost")
    monkeypatch.setenv("DEMO_PG_USER", "dbadmin")
    monkeypatch.setenv("DEMO_PG_PASSWORD", "x")
    monkeypatch.setenv("DEMO_PG_DATABASE", "analytics")


def test_query_end_to_end_renders_markup_value(monkeypatch) -> None:
    """The reproduced crash, through the real CLI render path."""
    _disable_envrc(monkeypatch)
    _pg_env(monkeypatch)
    connect, cursor = _fake_connect(["msg"], [("closes [/issue] 42",)])

    with patch("dataplat.cli.db._common.psycopg.connect", connect):
        result = runner.invoke(
            main_module.app, ["db", "query", "select 'closes [/issue] 42'"]
        )

    assert result.exit_code == 0, result.output
    assert "closes [/issue] 42" in result.stdout
    assert cursor.executed


def test_pagination_wrapper_alias_is_dataplat_owned(monkeypatch) -> None:
    """The alias shows up in server errors, so it must name this tool."""
    _disable_envrc(monkeypatch)
    _pg_env(monkeypatch)
    connect, cursor = _fake_connect(["n"], [(1,)])

    with patch("dataplat.cli.db._common.psycopg.connect", connect):
        result = runner.invoke(main_module.app, ["db", "query", "select 1"])

    assert result.exit_code == 0, result.output
    assert "AS dp_query" in cursor.executed[0]
    assert "dna_sql" not in cursor.executed[0]


def test_progress_spinner_never_captures_the_verbose_trace(monkeypatch) -> None:
    """rich's Live redirects ``sys.stderr``; the SQL trace must escape it.

    ``Progress`` paints into this module's console, which writes to *stdout*,
    and rich's default ``redirect_stderr=True`` swaps ``sys.stderr`` for a proxy
    onto that console for as long as the query runs. Every ``[dp:sql]`` line
    therefore came out on stdout — for TTY users only, since the spinner is
    skipped when output is piped — which is the one thing
    :mod:`dataplat.core.trace` promises can never happen.
    """
    import io

    from dataplat.core.trace import trace_sql

    _disable_envrc(monkeypatch)
    _pg_env(monkeypatch)

    class _Cursor:
        description = None
        rowcount = 0

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def execute(self, sql, params=None) -> None:
            # Stands in for _TracingCursor: the point under test is where the
            # line lands while the spinner owns the screen, not who wrote it.
            trace_sql(str(sql), params=params)

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def cursor(self):
            return _Cursor()

    # force_terminal, because rich only redirects when it believes it is on a
    # terminal — which is exactly the case that was broken.
    spinner_screen = io.StringIO()
    monkeypatch.setattr(
        db_cli, "console", Console(file=spinner_screen, force_terminal=True)
    )
    monkeypatch.setattr(db_cli, "_supports_live_query_progress", lambda _console: True)

    with (
        trace.verbose(),
        patch("dataplat.cli.db._common.psycopg.connect", lambda **kwargs: _Conn()),
    ):
        result = runner.invoke(
            main_module.app, ["db", "query", "delete from t", "--write"]
        )

    assert result.exit_code == 0, result.output
    assert "[dp:sql] delete from t | no params" in result.stderr
    assert "[dp:sql]" not in spinner_screen.getvalue()


def test_spinner_follows_the_output_format_to_stderr(monkeypatch) -> None:
    """The spinner must paint where the notices go, not always on stdout.

    While Rich only claimed a terminal for real TTYs this was invisible: the
    frames are erased. But FORCE_COLOR makes is_terminal true for a pipe as
    well — plenty of people export it — and then
    `dp db query --format json > file` collected the escape sequences and the
    file stopped parsing. Verified by hand before the fix: 167 bytes of ANSI and
    a JSONDecodeError.
    """
    consoles: list[Console] = []

    class _SpyProgress:
        def __init__(self, *columns, console=None, **kwargs):
            consoles.append(console)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def add_task(self, *a, **k):
            return None

    class _Cursor:
        description = None
        rowcount = 1

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def execute(self, sql, params=None) -> None:
            return None

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def cursor(self):
            return _Cursor()

    _disable_envrc(monkeypatch)
    _pg_env(monkeypatch)
    monkeypatch.setattr(db_cli, "Progress", _SpyProgress)
    monkeypatch.setattr(db_cli, "_supports_live_query_progress", lambda _console: True)

    for fmt, expect_stderr in (("json", True), ("csv", True), ("table", False)):
        consoles.clear()
        with patch("dataplat.cli.db._common.psycopg.connect", lambda **kwargs: _Conn()):
            result = runner.invoke(
                main_module.app,
                ["db", "query", "select 1", "--format", fmt],
            )
        assert result.exit_code == 0, result.output
        assert consoles, f"no Progress built for --format {fmt}"
        assert consoles[0].stderr is expect_stderr, (
            f"--format {fmt}: spinner painted to "
            f"{'stderr' if consoles[0].stderr else 'stdout'}"
        )


# =========================================================================
# DuckDB, end to end, against a real database.
#
# `dp db query` sends the user's SQL unchanged, so the only statement under
# test that dataplat wrote is the LIMIT/OFFSET wrapper — and whether DuckDB
# accepts the subquery-with-alias form it builds is not something a fake
# cursor can answer. DuckDB is in-process, so these open a real file.
# =========================================================================


def _duckdb_target(monkeypatch, path: str | Path, *, read_only: bool = False) -> None:
    """Declare a third target, `demo_ddb`, alongside the suite's two."""
    monkeypatch.setenv("DP_TARGETS", "demo_pg,demo_rs,demo_ddb")
    monkeypatch.setenv("DEMO_DDB_ENGINE", "duckdb")
    monkeypatch.setenv("DEMO_DDB_PATH", str(path))
    if read_only:
        monkeypatch.setenv("DEMO_DDB_READ_ONLY", "1")


def _make_warehouse(path: Path, rows: int = 5) -> Path:
    conn = duckdb.connect(database=str(path))
    conn.execute("CREATE TABLE events(id BIGINT, note VARCHAR)")
    conn.execute("INSERT INTO events SELECT i, 'note ' || i FROM range(?) t(i)", [rows])
    conn.close()
    return path


def _ddb(monkeypatch, tmp_path, *, rows: int = 5, read_only: bool = False) -> Path:
    _disable_envrc(monkeypatch)
    database = _make_warehouse(tmp_path / "w.duckdb", rows=rows)
    _duckdb_target(monkeypatch, database, read_only=read_only)
    return database


def _query(*args: str):
    return runner.invoke(main_module.app, ["db", "query", *args, "-t", "demo_ddb"])


def test_query_duckdb_accepts_the_pagination_wrapper(monkeypatch, tmp_path) -> None:
    """The wrapper is the one statement dataplat composes. DuckDB must take it."""
    _ddb(monkeypatch, tmp_path)

    first = _query("select id from events order by id", "--limit", "2")

    assert first.exit_code == 0, first.output
    assert "More rows available" in first.stdout
    assert "Rows: 2" in first.stdout

    second = _query("select id from events order by id", "--limit", "2", "--page", "2")

    assert second.exit_code == 0, second.output
    # Row numbering continues from the offset, so page 2 starts at 3.
    for expected in ("3", "4"):
        assert expected in second.stdout


def test_query_duckdb_paginates_a_from_first_query(monkeypatch, tmp_path) -> None:
    """`FROM t` is a read and gets the row cap, or it streams the whole table."""
    _ddb(monkeypatch, tmp_path)

    result = _query("from events", "--limit", "2")

    assert result.exit_code == 0, result.output
    assert "This statement can modify data" not in result.output
    assert "More rows available" in result.stdout
    assert "Rows: 2" in result.stdout


def test_query_duckdb_wrapper_survives_a_trailing_comment(
    monkeypatch, tmp_path
) -> None:
    _ddb(monkeypatch, tmp_path)

    result = _query("select id from events order by id -- mine", "--limit", "2")

    assert result.exit_code == 0, result.output
    assert "Rows: 2" in result.stdout


def test_query_duckdb_limit_zero_sends_the_statement_unwrapped(
    monkeypatch, tmp_path
) -> None:
    _ddb(monkeypatch, tmp_path)

    with trace.verbose():
        result = _query("select id from events order by id", "--limit", "0")

    assert result.exit_code == 0, result.output
    assert "dp_query" not in result.stderr
    assert "[dp:sql] select id from events order by id" in result.stderr
    assert "Rows: 5" in result.stdout


def test_query_duckdb_column_names_come_from_the_cursor(monkeypatch, tmp_path) -> None:
    """cursor.description on DuckDB is a plain tuple; .name has to work anyway."""
    _ddb(monkeypatch, tmp_path)

    result = _query("""select id as event_id, note as "[/x]" from events limit 1""")

    assert result.exit_code == 0, result.output
    assert "event_id" in result.stdout
    # A markup-shaped alias must render literally rather than raise MarkupError.
    assert "[/x]" in result.stdout


def test_query_duckdb_csv_and_json_carry_duckdb_values(monkeypatch, tmp_path) -> None:
    _disable_envrc(monkeypatch)
    database = tmp_path / "w.duckdb"
    conn = duckdb.connect(database=str(database))
    # A row of types psycopg never returns: DuckDB hands back date, Decimal,
    # list and dict objects, and both formats have to survive them.
    conn.execute(
        "CREATE TABLE t AS SELECT 1 AS n, DATE '2024-01-02' AS d, "
        "3.14::DECIMAL(10,4) AS dec, [1, 2] AS tags, {'k': 'v'} AS obj"
    )
    conn.close()
    _duckdb_target(monkeypatch, database)

    as_csv = _query("select * from t", "--format", "csv")
    assert as_csv.exit_code == 0, as_csv.output
    assert as_csv.stdout.splitlines()[0] == "n,d,dec,tags,obj"
    assert "2024-01-02" in as_csv.stdout
    # Decoration goes to stderr, so stdout is the CSV and nothing else.
    assert "Execution time" not in as_csv.stdout

    as_json = _query("select * from t", "--format", "json")
    assert as_json.exit_code == 0, as_json.output
    payload = json.loads(as_json.stdout)
    assert payload == [
        {"n": 1, "d": "2024-01-02", "dec": "3.1400", "tags": [1, 2], "obj": {"k": "v"}}
    ]


def test_query_duckdb_write_needs_the_flag_then_runs(monkeypatch, tmp_path) -> None:
    database = _ddb(monkeypatch, tmp_path)

    refused = _query("delete from events where id = 0")

    assert refused.exit_code == 1
    assert "--write" in refused.stdout

    allowed = _query("delete from events where id = 0", "--write")

    assert allowed.exit_code == 0, allowed.output
    conn = duckdb.connect(database=str(database), read_only=True)
    remaining = conn.execute("SELECT count(*) FROM events").fetchone()
    conn.close()
    assert remaining == (4,)


def test_query_duckdb_ddl_reports_duckdbs_own_answer(monkeypatch, tmp_path) -> None:
    """No invented row count.

    DuckDB's rowcount is -1 for every statement, and it answers DDL with a
    one-column result set of its own ('Count' or 'Success'). So the "N rows
    affected" line never fires here — which is the honest outcome, because
    "-1 rows affected" is what inventing one would print.
    """
    _ddb(monkeypatch, tmp_path)

    result = _query("create table staged(a integer)", "--write")

    assert result.exit_code == 0, result.output
    assert "rows affected" not in result.stdout


def test_query_duckdb_read_only_target_refuses_a_write(monkeypatch, tmp_path) -> None:
    _ddb(monkeypatch, tmp_path, read_only=True)

    result = _query("delete from events", "--write")

    # A write against a read-only database is the statement's own fault, so it
    # stays at FAILURE rather than claiming a retryable service problem.
    assert result.exit_code == ExitCode.FAILURE
    assert "read-only" in result.output


def test_query_duckdb_bad_sql_is_a_database_error(monkeypatch, tmp_path) -> None:
    _ddb(monkeypatch, tmp_path)

    result = _query("select * from no_such_table")

    assert result.exit_code == ExitCode.FAILURE
    assert "Database error" in result.output


def test_query_duckdb_trace_never_reaches_a_machine_readable_stdout(
    monkeypatch, tmp_path
) -> None:
    _ddb(monkeypatch, tmp_path, rows=2)

    with trace.verbose():
        result = _query("select id from events order by id", "--format", "json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [{"id": 0}, {"id": 1}]
    assert "[dp:sql]" in result.stderr
    assert "engine=duckdb" in result.stderr


def test_query_reads_stdin_when_the_sql_is_a_bare_dash(monkeypatch, tmp_path) -> None:
    """`dp db query -t <target> -` is what --drop-sql tells people to run.

    Before the dash was recognized, that invocation sent the database a
    statement consisting of one hyphen — so the script `dp db top-tables
    --drop-sql` emits could not be run the way its own header said to run it.
    """
    _ddb(monkeypatch, tmp_path)

    result = runner.invoke(
        main_module.app,
        ["db", "query", "-", "-t", "demo_ddb"],
        input="select count(*) as n from events\n",
    )

    assert result.exit_code == 0, result.output
    assert "Rows: 1" in result.stdout


def test_query_runs_the_whole_drop_script_from_stdin(monkeypatch, tmp_path) -> None:
    """The generated script is multi-statement, and DuckDB takes it in one go."""
    database = _ddb(monkeypatch, tmp_path)

    script = (
        "-- header\nBEGIN;\nDROP TABLE IF EXISTS main.events;  -- ~5 rows\nCOMMIT;\n"
    )
    result = runner.invoke(
        main_module.app,
        ["db", "query", "-", "-t", "demo_ddb", "--write"],
        input=script,
    )

    assert result.exit_code == 0, result.output
    conn = duckdb.connect(database=str(database), read_only=True)
    left = conn.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE table_name = 'events'"
    ).fetchone()
    conn.close()
    assert left == (0,)


def test_query_duckdb_wraps_a_cte_query_too(monkeypatch, tmp_path) -> None:
    """All three paginated heads go inside the wrapper, so all three are checked."""
    _ddb(monkeypatch, tmp_path)

    result = _query(
        "with recent as (select id from events) select * from recent order by id",
        "--limit",
        "2",
    )

    assert result.exit_code == 0, result.output
    assert "More rows available" in result.stdout
