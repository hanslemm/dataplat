from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import patch

from typer.testing import CliRunner

from dataplat.cli.db import app as db_app
from dataplat.services.db.long_queries import LongQueryRow
from dataplat.services.db.targets import resolve_target

runner = CliRunner()

_FETCH = "dataplat.cli.db.long_queries._fetch_for_target"


def _row(query_id: str, status: str, elapsed_s: int) -> LongQueryRow:
    return LongQueryRow(
        query_id=query_id,
        user_name="m_fender",
        db_name="warehouse",
        status=status,
        start_time=datetime(2026, 5, 21, 8, 31, 8, tzinfo=UTC),
        elapsed_s=elapsed_s,
        query_text="SELECT 1",
        session_id="777",
    )


def test_long_queries_renders_both_targets() -> None:
    with patch(_FETCH, return_value=[_row("42", "running", 120)]) as fetch:
        result = runner.invoke(db_app, ["long-queries"])

    assert result.exit_code == 0, result.output
    assert fetch.call_count == 2  # demo_pg + demo_rs
    assert "Postgres" in result.output
    assert "Redshift" in result.output
    assert "777" in result.output  # PID column drives `dp db kill`


def test_long_queries_single_target() -> None:
    with patch(_FETCH, return_value=[]) as fetch:
        result = runner.invoke(db_app, ["long-queries", "-t", "demo_rs"])

    assert result.exit_code == 0, result.output
    assert fetch.call_count == 1
    assert fetch.call_args.args[0] == resolve_target("demo_rs")


def test_long_queries_json_output() -> None:
    with patch(_FETCH, return_value=[_row("42", "failed", 300)]):
        result = runner.invoke(db_app, ["long-queries", "-t", "demo_pg", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["demo_pg"][0]["query_id"] == "42"
    assert payload["demo_pg"][0]["session_id"] == "777"


def test_long_queries_unknown_target() -> None:
    result = runner.invoke(db_app, ["long-queries", "-t", "nope"])

    assert result.exit_code == 1
    assert "Unknown target" in result.output


def test_kill_requires_confirmation_non_interactive() -> None:
    result = runner.invoke(db_app, ["kill", "123"])

    assert result.exit_code == 1
    assert "--yes" in result.output


def test_kill_postgres_terminates(monkeypatch) -> None:
    from dataplat.cli.db import long_queries as lq

    calls: list[tuple] = []

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def execute(self, sql, params=None):
            calls.append((sql, params))

        def fetchone(self):
            return (True,)

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def cursor(self):
            return _Cursor()

        def commit(self):
            return None

    monkeypatch.setattr(lq, "resolve_params_or_exit", lambda p: object())

    from contextlib import contextmanager

    @contextmanager
    def fake_session(params):
        yield _Conn()

    monkeypatch.setattr(lq, "db_session", fake_session)

    result = runner.invoke(db_app, ["kill", "123", "-t", "demo_pg", "--yes"])

    assert result.exit_code == 0, result.output
    assert any("pg_terminate_backend" in sql for sql, _ in calls)


def test_kill_redshift_issues_cancel(monkeypatch) -> None:
    from dataplat.cli.db import long_queries as lq

    calls: list[str] = []

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def execute(self, sql, params=None):
            calls.append(sql)

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def cursor(self):
            return _Cursor()

        def commit(self):
            return None

    monkeypatch.setattr(lq, "resolve_params_or_exit", lambda p: object())

    from contextlib import contextmanager

    @contextmanager
    def fake_session(params):
        yield _Conn()

    monkeypatch.setattr(lq, "db_session", fake_session)

    result = runner.invoke(db_app, ["kill", "456", "-t", "demo_rs", "--yes"])

    assert result.exit_code == 0, result.output
    assert "CANCEL 456" in calls
