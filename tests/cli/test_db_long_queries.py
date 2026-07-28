from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from dataplat.cli.db import app as db_app
from dataplat.core.errors import ExitCode
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


def _markup_row() -> LongQueryRow:
    return LongQueryRow(
        query_id="[bold]42",
        user_name="svc[/x]",
        db_name="wh[bold]",
        status="[/issue]",
        start_time=datetime(2026, 5, 21, 8, 31, 8, tzinfo=UTC),
        elapsed_s=120,
        query_text="select 'closes [/issue] 42'",
        session_id="7[/x]7",
    )


def test_long_queries_renders_markup_like_data_literally() -> None:
    """Regression: a query text or identifier carrying markup must survive."""
    with patch(_FETCH, return_value=[_markup_row()]):
        result = runner.invoke(db_app, ["long-queries", "-t", "demo_pg"])

    assert result.exit_code == 0, result.output
    out = result.output
    assert "select 'closes [/issue] 42'" in out
    assert "[bold]42" in out
    assert "svc[/x]" in out
    assert "7[/x]7" in out
    # The status cell was inline-styled markup; it must still be styled *and*
    # show the raw value.
    assert "[/issue]" in out


def test_long_queries_history_renders_markup_like_sql() -> None:
    from dataplat.services.db.long_queries import QueryHistoryRow

    row = QueryHistoryRow(
        calls=3,
        total_s=9.0,
        mean_s=3.0,
        max_s=4.0,
        query_text="select 'closes [/issue] 42' /* [bold] */",
    )
    with patch("dataplat.cli.db.long_queries._fetch_history", return_value=[row]):
        result = runner.invoke(db_app, ["long-queries", "-t", "demo_pg", "--history"])

    assert result.exit_code == 0, result.output
    assert "closes [/issue] 42" in result.output
    assert "[bold]" in result.output


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
    """A bad --target is invalid input, not a generic failure."""
    result = runner.invoke(db_app, ["long-queries", "-t", "nope"])

    assert result.exit_code == ExitCode.INVALID_INPUT
    assert "Unknown target" in result.output


def test_kill_unknown_target_exits_invalid_input() -> None:
    result = runner.invoke(db_app, ["kill", "123", "-t", "nope"])

    assert result.exit_code == ExitCode.INVALID_INPUT
    assert "Unknown target" in result.output


def test_kill_requires_confirmation_non_interactive() -> None:
    """A refusal is not a failure of the warehouse: it stays 1.

    Deliberate, and pinned: routing the confirmation gate through the error
    codes would make "the user said no" indistinguishable from "the tool could
    not do it", which is the one thing a wrapper script must be able to tell.
    """
    result = runner.invoke(db_app, ["kill", "123"])

    assert result.exit_code == ExitCode.FAILURE
    assert "--yes" in result.output


def test_kill_summary_wording_survives_shared_gate() -> None:
    result = runner.invoke(db_app, ["kill", "123", "456", "-t", "demo_rs"])

    assert result.exit_code == 1
    assert "Cancel 2 session(s) on demo_rs: 123, 456" in result.output


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


# --- a DuckDB target: both commands rest on other sessions existing ---------
#
# Every test below runs against a real DuckDB database, not a stub. DuckDB is
# in-process and file-backed, so there is nothing to fake, and a refusal that
# only works against an imagined target proves nothing about the one a user has.


def _flat(text: str) -> str:
    """One long line, because Rich wraps at the terminal width.

    Every assertion below is about wording, and a clause that happens to fit
    today would silently start straddling a line break the next time the
    sentence grows or COLUMNS changes.
    """
    return " ".join(text.split())


def _duckdb_target(monkeypatch, tmp_path: Path, name: str = "ddb") -> Path:
    """Create a real DuckDB database and declare it as the only target."""
    import duckdb

    path = tmp_path / "warehouse.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute("CREATE TABLE orders(id INTEGER)")
    connection.close()
    monkeypatch.setenv("DP_TARGETS", name)
    monkeypatch.setenv(f"{name.upper()}_ENGINE", "duckdb")
    monkeypatch.setenv(f"{name.upper()}_PATH", str(path))
    monkeypatch.delenv("DP_DEFAULT_TARGET", raising=False)
    return path


def _forbid_connections(monkeypatch) -> None:
    """Make opening any database fail the test.

    A refusal that arrives *after* a connection is a different thing: it has
    already taken DuckDB's single-writer lock on a file someone may be running
    dbt against, or spent a round trip on a server. Patching both drivers at the
    module they are called through is what pins "before".
    """
    import duckdb
    import psycopg

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("a refused command opened a connection")

    monkeypatch.setattr(psycopg, "connect", _forbidden)
    monkeypatch.setattr(duckdb, "connect", _forbidden)


def test_long_queries_refuses_a_duckdb_target(monkeypatch, tmp_path: Path) -> None:
    """Exit 2 with the engine's own reason, and nothing opened."""
    _duckdb_target(monkeypatch, tmp_path)
    _forbid_connections(monkeypatch)

    result = runner.invoke(db_app, ["long-queries", "-t", "ddb"])

    out = _flat(result.output)
    assert result.exit_code == ExitCode.INVALID_INPUT, result.output
    assert "dp db long-queries cannot run against DuckDB" in out
    assert "no other sessions to inspect or cancel" in out
    # The distinction the message exists to draw: this is what the engine is.
    assert "That is what DuckDB is, not a missing dataplat feature" in out
    for wording in ("not supported", "not implemented"):
        assert wording not in out.lower()


def test_long_queries_refuses_duckdb_before_history_is_considered(
    monkeypatch, tmp_path: Path
) -> None:
    """--history's own error would send the reader after the wrong fact.

    "--history uses pg_stat_statements and needs a Postgres target" is true and
    useless here: no flag on this command can work against an engine with no
    other sessions, and only the engine's reason says so.
    """
    _duckdb_target(monkeypatch, tmp_path)
    _forbid_connections(monkeypatch)

    result = runner.invoke(db_app, ["long-queries", "-t", "ddb", "--history"])

    assert result.exit_code == ExitCode.INVALID_INPUT, result.output
    assert "no other sessions to inspect or cancel" in _flat(result.output)
    assert "pg_stat_statements" not in result.output


def test_long_queries_all_still_serves_the_targets_that_have_sessions(
    monkeypatch, tmp_path: Path
) -> None:
    """`-t all` is the default, so one DuckDB target must not end the report."""
    _duckdb_target(monkeypatch, tmp_path)
    monkeypatch.setenv("DP_TARGETS", "ddb,demo_pg")

    with patch(_FETCH, return_value=[_row("42", "running", 120)]) as fetch:
        result = runner.invoke(db_app, ["long-queries", "-t", "all"])

    # Nothing failed: the question does not apply to one target, which is not
    # the same as that target being broken.
    assert result.exit_code == 0, result.output
    assert [call.args[0].name for call in fetch.call_args_list] == ["demo_pg"]
    assert "Postgres" in result.stdout
    # Named, with its reason, rather than silently absent from a report someone
    # is reading to conclude nothing is wrong.
    assert "[ddb]" in result.stderr
    assert "no other sessions to inspect or cancel" in _flat(result.stderr)


def test_long_queries_json_stays_parseable_with_a_duckdb_target(
    monkeypatch, tmp_path: Path
) -> None:
    """The note is a notice, so it goes to stderr — `--json` is a document."""
    _duckdb_target(monkeypatch, tmp_path)
    monkeypatch.setenv("DP_TARGETS", "ddb,demo_pg")

    with patch(_FETCH, return_value=[]):
        result = runner.invoke(db_app, ["long-queries", "-t", "all", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"demo_pg": []}
    assert "DuckDB" in result.stderr


def test_kill_refuses_a_duckdb_target_before_asking(
    monkeypatch, tmp_path: Path
) -> None:
    """There is nothing to confirm terminating: those PIDs cannot exist.

    ``--yes`` is deliberately absent. The refusal has to precede the gate, or a
    user would be asked to approve killing sessions on an engine that has none.
    """
    _duckdb_target(monkeypatch, tmp_path)
    _forbid_connections(monkeypatch)

    result = runner.invoke(db_app, ["kill", "123", "-t", "ddb"])

    out = _flat(result.output)
    assert result.exit_code == ExitCode.INVALID_INPUT, result.output
    assert "dp db kill cannot run against DuckDB" in out
    assert "no other sessions to inspect or cancel" in out
    assert "Terminate 1 session(s)" not in out
    assert "--yes" not in out
