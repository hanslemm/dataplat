from __future__ import annotations

from typer.testing import CliRunner

import dataplat.main as main_module
from dataplat.cli.db import _classify_sql

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

    result = runner.invoke(
        main_module.app, ["db", "query", "DELETE FROM t"]
    )

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
