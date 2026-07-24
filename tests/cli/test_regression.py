from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from dataplat.cli import db as db_cli
from dataplat.cli.cloud.aws import secrets as secrets_cli
from dataplat.cli.db._common import ConnCliParams


class _FakeCursor:
    def __init__(self) -> None:
        self.description = [SimpleNamespace(name="value")]
        self.rowcount = 1
        self.executed_sql: str | None = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def execute(self, sql: str):
        self.executed_sql = sql

    def fetchall(self):
        return [(1,)]


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def cursor(self):
        return self._cursor


def _patch_connection(monkeypatch, cursor: _FakeCursor) -> None:
    monkeypatch.setattr(db_cli, "resolve_params_or_exit", lambda params: object())

    @contextmanager
    def fake_session(params):
        yield _FakeConnection(cursor)

    monkeypatch.setattr(db_cli, "db_session", fake_session)


def test_db_query_wraps_select_with_limit_and_offset(monkeypatch) -> None:
    cursor = _FakeCursor()
    _patch_connection(monkeypatch, cursor)

    db_cli._execute_query(
        sql="select 1",
        conn_cli=ConnCliParams(),
        limit=10,
        page=2,
        write=False,
        fmt=db_cli.OutputFormat.table,
    )

    assert cursor.executed_sql is not None
    assert "LIMIT 11 OFFSET 10" in cursor.executed_sql


def test_db_query_wraps_select_with_leading_comment(monkeypatch) -> None:
    cursor = _FakeCursor()
    _patch_connection(monkeypatch, cursor)

    db_cli._execute_query(
        sql="-- comment\nselect 1",
        conn_cli=ConnCliParams(),
        limit=10,
        page=1,
        write=False,
        fmt=db_cli.OutputFormat.table,
    )

    assert cursor.executed_sql is not None
    assert "LIMIT 11 OFFSET 0" in cursor.executed_sql


def test_db_query_limit_zero_disables_wrapping(monkeypatch) -> None:
    cursor = _FakeCursor()
    _patch_connection(monkeypatch, cursor)

    db_cli._execute_query(
        sql="select 1",
        conn_cli=ConnCliParams(),
        limit=0,
        page=1,
        write=False,
        fmt=db_cli.OutputFormat.table,
    )

    assert cursor.executed_sql == "select 1"


def test_db_query_prints_execution_time(monkeypatch) -> None:
    cursor = _FakeCursor()
    cursor.description = None
    cursor.rowcount = 3
    printed: list[str] = []

    _patch_connection(monkeypatch, cursor)
    monkeypatch.setattr(
        db_cli,
        "console",
        SimpleNamespace(
            print=lambda *args, **kwargs: printed.append(" ".join(str(arg) for arg in args))
        ),
    )

    db_cli._execute_query(
        sql="update some_table set value = 1",
        conn_cli=ConnCliParams(),
        limit=10,
        page=1,
        write=True,
        fmt=db_cli.OutputFormat.table,
    )

    assert any("Execution time:" in line for line in printed)


def test_secrets_resolve_profiles_expands_all_and_deduplicates(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "DP_AWS_PROFILE_ALIASES", "prod=Admin-Prod, qa=Admin-QA"
    )

    profiles = secrets_cli._resolve_profiles(["prod", "all", "qa", "prod"])

    assert profiles == ["Admin-Prod", "Admin-QA"]
