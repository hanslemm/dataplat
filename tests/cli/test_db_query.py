from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from rich.console import Console
from typer.testing import CliRunner

import dataplat.cli.db as db_cli
import dataplat.main as main_module
from dataplat.cli.db import _classify_sql, _render_rows, _write_preview
from dataplat.core import trace

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
