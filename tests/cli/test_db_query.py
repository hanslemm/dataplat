from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from rich.console import Console
from typer.testing import CliRunner

import dataplat.cli.db as db_cli
import dataplat.main as main_module
from dataplat.cli.db import _classify_sql, _render_rows, _write_preview

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
