from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import duckdb
import psycopg
import pytest
import typer
from psycopg import sql

from dataplat.cli import _options
from dataplat.cli.db import _common
from dataplat.cli.db._common import (
    ConnCliParams,
    JsonOption,
    YesOption,
    db_session,
    engine_or_exit,
    resolve_any_params_or_exit,
    resolve_params_or_exit,
)
from dataplat.core import trace
from dataplat.core.errors import ExitCode, ValidationError
from dataplat.services.db.capabilities import Capability, require_capability
from dataplat.services.db.connection import DuckDbConnectionParams, SqlEngine


def _set_target_env(monkeypatch, prefix: str) -> None:
    monkeypatch.setenv(f"{prefix}_HOST", "db.example.com")
    monkeypatch.setenv(f"{prefix}_USER", "alice")
    monkeypatch.setenv(f"{prefix}_DATABASE", "warehouse")


def test_target_sets_prefix_and_engine(monkeypatch) -> None:
    _set_target_env(monkeypatch, "DEMO_RS")

    params = ConnCliParams(target="demo_rs").resolve()

    assert params.engine == SqlEngine.redshift
    assert params.host == "db.example.com"
    assert params.port == 5439


def test_flags_override_target(monkeypatch) -> None:
    _set_target_env(monkeypatch, "DEMO_RS")

    params = ConnCliParams(
        target="demo_rs", engine=SqlEngine.postgresql, host="other"
    ).resolve()

    assert params.engine == SqlEngine.postgresql
    assert params.host == "other"


def test_default_prefix_is_demo_pg(monkeypatch) -> None:
    _set_target_env(monkeypatch, "DEMO_PG")

    params = ConnCliParams().resolve()

    assert params.host == "db.example.com"
    assert params.engine == SqlEngine.postgresql


def test_explicit_env_prefix_wins_over_target(monkeypatch) -> None:
    _set_target_env(monkeypatch, "CUSTOM")

    params = ConnCliParams(target="demo_rs", env_prefix="CUSTOM").resolve()

    assert params.host == "db.example.com"
    # engine still comes from the target when not otherwise specified
    assert params.engine == SqlEngine.redshift


def test_resolve_params_or_exit_unknown_target(monkeypatch, capsys) -> None:
    with pytest.raises(typer.Exit):
        resolve_params_or_exit(ConnCliParams(target="nope"))

    assert "Unknown target" in capsys.readouterr().out


def test_resolve_params_or_exit_missing_settings(monkeypatch, capsys) -> None:
    for var in ("DEMO_PG_HOST", "DEMO_PG_USER", "DEMO_PG_DATABASE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("PGHOST", raising=False)
    monkeypatch.delenv("PGUSER", raising=False)
    monkeypatch.delenv("PGDATABASE", raising=False)
    monkeypatch.delenv("DB_HOST", raising=False)
    monkeypatch.delenv("DB_USER", raising=False)
    monkeypatch.delenv("DB_NAME", raising=False)

    with pytest.raises(typer.Exit):
        resolve_params_or_exit(ConnCliParams())

    assert "Missing required connection settings" in capsys.readouterr().out


def test_options_are_the_shared_ones() -> None:
    """db commands must not re-declare --json/--yes."""
    assert JsonOption is _options.JsonOption
    assert YesOption is _options.YesOption


def test_resolve_params_or_exit_escapes_target_name(capsys) -> None:
    """A closing tag in the target name used to raise MarkupError."""
    with pytest.raises(typer.Exit):
        resolve_params_or_exit(ConnCliParams(target="no[/x]pe"))

    out = capsys.readouterr().out
    assert "no[/x]pe" in out


def test_resolve_params_or_exit_keeps_style_name_visible(capsys) -> None:
    with pytest.raises(typer.Exit):
        resolve_params_or_exit(ConnCliParams(target="[bold]"))

    assert "[bold]" in capsys.readouterr().out


def test_db_session_escapes_driver_message(monkeypatch, capsys) -> None:
    """psycopg quotes the failing SQL verbatim, brackets included."""

    def _boom(**kwargs):
        raise psycopg.OperationalError("FATAL: closes [/issue] 42 [bold]")

    params = ConnCliParams(
        target="demo_pg", host="localhost", user="u", database="d"
    ).resolve()
    with (
        patch.object(_common.psycopg, "connect", _boom),
        pytest.raises(typer.Exit),
        db_session(params),
    ):
        pass  # pragma: no cover - connect raises first

    out = capsys.readouterr().out
    assert "closes [/issue] 42" in out
    assert "[bold]" in out


# =========================================================================
# The exit-code contract.
#
# resolve_params_or_exit is the funnel every db command's connection passes
# through, so it is where a caller first learns *which* kind of thing went
# wrong. The two conditions below used to be indistinguishable at 1.
# =========================================================================


def test_unknown_target_exits_invalid_input() -> None:
    """An unknown --target is a ValidationError: 2, like any rejected argument."""
    with pytest.raises(typer.Exit) as excinfo:
        resolve_params_or_exit(ConnCliParams(target="nope"))

    assert excinfo.value.exit_code == ExitCode.INVALID_INPUT


def test_missing_connection_settings_exits_config(monkeypatch) -> None:
    """Missing <PREFIX>_HOST is a ConfigError: 3, so a script can tell them apart."""
    for var in ("DEMO_PG_HOST", "DEMO_PG_USER", "DEMO_PG_DATABASE"):
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(typer.Exit) as excinfo:
        resolve_params_or_exit(ConnCliParams())

    assert excinfo.value.exit_code == ExitCode.CONFIG


def test_driver_failure_still_exits_failure() -> None:
    """An unclassified driver error stays at 1; only OperationalError is promoted.

    ``psycopg.Error`` carries no declared code, so the CLI chooses one — but only
    for the subclass whose meaning is unambiguous. ``OperationalError`` is the
    environment failing and exits SERVICE (see the two tests below); a bare
    ``psycopg.Error`` could be anything, and "you do not know what happened" is
    what 1 means.
    """

    def _boom(**kwargs: object) -> None:
        raise psycopg.Error("something the driver could not classify")

    params = ConnCliParams(
        target="demo_pg", host="localhost", user="u", database="d"
    ).resolve()
    with (
        patch.object(_common.psycopg, "connect", _boom),
        pytest.raises(typer.Exit) as excinfo,
        db_session(params),
    ):
        pass  # pragma: no cover - connect raises first

    assert excinfo.value.exit_code == ExitCode.FAILURE


# =========================================================================
# --verbose SQL tracing.
#
# The contract (dataplat.core.trace): stderr only, so --json and --format csv
# stay machine-clean; statement text and whether params were bound, never the
# values; and nothing at all when it is off.
# =========================================================================


class _RecordingConnect:
    """Stand-in for ``psycopg.connect`` that captures the kwargs it was given."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def __call__(self, **kwargs: Any) -> _RecordingConnect:
        self.kwargs = kwargs
        return self

    def __enter__(self) -> _RecordingConnect:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


def _params() -> Any:
    return ConnCliParams(
        target="demo_pg",
        host="db.example.com",
        user="alice",
        database="warehouse",
        password="s3cr3t",
    ).resolve()


def test_session_is_untraced_by_default(capsys) -> None:
    """Off means off: psycopg's own cursor, and not one byte on either stream."""
    connect = _RecordingConnect()
    with patch.object(_common.psycopg, "connect", connect), db_session(_params()):
        pass

    assert connect.kwargs["cursor_factory"] is None
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_session_traces_the_connection_to_stderr_without_the_password(capsys) -> None:
    connect = _RecordingConnect()
    with (
        trace.verbose(),
        patch.object(_common.psycopg, "connect", connect),
        db_session(_params()),
    ):
        pass

    captured = capsys.readouterr()
    # stdout is what --json and --format csv write to; the tracer may never
    # touch it, or every machine-readable caller breaks under --verbose.
    assert captured.out == ""
    assert "[dp:sql] connect alice@db.example.com:5432/warehouse" in captured.err
    assert "s3cr3t" not in captured.err


def test_session_installs_the_tracing_cursor_when_verbose() -> None:
    """The factory is the mechanism: no call site opts in, so none can forget."""
    connect = _RecordingConnect()
    with (
        trace.verbose(),
        patch.object(_common.psycopg, "connect", connect),
        db_session(_params()),
    ):
        pass

    assert connect.kwargs["cursor_factory"] is _common._TracingCursor


def _detached_cursor() -> Any:
    """A ``_TracingCursor`` with no server behind it.

    ``psycopg.Cursor`` can only be constructed from a live connection, so the
    two attributes rendering a statement actually reads are supplied by hand:
    the connection (``None`` is psycopg's own "assume UTF-8" case) and the
    adapters map (the global one, which is what a real cursor inherits).
    Nothing else is faked — tests pair this with a patched
    ``psycopg.Cursor.execute`` so the delegation stays observable.
    """
    cursor = object.__new__(_common._TracingCursor)
    cursor._conn = None
    cursor._adapters = psycopg.adapters
    return cursor


def test_tracing_cursor_reports_the_count_and_never_the_values(
    monkeypatch, capsys
) -> None:
    sent: list[tuple[Any, Any]] = []

    def _record(self: Any, query: Any, params: Any = None, **kwargs: Any) -> Any:
        sent.append((query, params))
        return self

    monkeypatch.setattr(psycopg.Cursor, "execute", _record)

    with trace.verbose():
        _detached_cursor().execute(
            "SELECT * FROM users WHERE email = %s", ("ada@example.com",)
        )

    err = capsys.readouterr().err
    assert "[dp:sql] SELECT * FROM users WHERE email = %s | 1 params bound" in err
    # The values are warehouse data, and psycopg keeps them out of the SQL for
    # a reason. Saying how many were bound is the whole promise.
    assert "ada@example.com" not in err
    # ...and the statement still reached the driver, unchanged.
    assert sent == [("SELECT * FROM users WHERE email = %s", ("ada@example.com",))]


def test_tracing_cursor_traces_before_the_statement_runs(monkeypatch, capsys) -> None:
    """A statement that never returns is the case --verbose exists for.

    Tracing after the call would print nothing for the query the user gives up
    on, which is the one they most need to see.
    """

    def _hang(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(psycopg.Cursor, "execute", _hang)

    with trace.verbose(), pytest.raises(KeyboardInterrupt):
        _detached_cursor().execute("SELECT pg_sleep(600)")

    assert "[dp:sql] SELECT pg_sleep(600) | no params" in capsys.readouterr().err


def test_composed_statement_is_rendered_not_repred() -> None:
    """``str()`` on a Composed hides the SQL and defeats redaction.

    ``repr`` is a Python data structure (``Literal('s3cr3t')``), which is
    neither what the server receives nor a shape ``redact`` can recognize — so
    a password inside one would have survived the redactor untouched.
    """
    statement = sql.SQL("CREATE ROLE {role} LOGIN PASSWORD {pw}").format(
        role=sql.Identifier("alice"), pw=sql.Literal("s3cr3t")
    )

    text = _common._statement_text(statement, None)

    assert text == "CREATE ROLE \"alice\" LOGIN PASSWORD 's3cr3t'"
    assert "Literal(" not in text
    # And once rendered, the redactor can see it.
    assert "s3cr3t" not in trace.redact(text)


def test_composed_password_never_reaches_the_trace(monkeypatch, capsys) -> None:
    """The end of that argument: the traced line has no credential in it."""
    monkeypatch.setattr(psycopg.Cursor, "execute", lambda self, *a, **kw: self)
    statement = sql.SQL("ALTER ROLE {role} PASSWORD {pw}").format(
        role=sql.Identifier("alice"), pw=sql.Literal("s3cr3t")
    )

    with trace.verbose():
        _detached_cursor().execute(statement)

    err = capsys.readouterr().err
    assert "ALTER ROLE" in err
    assert "s3cr3t" not in err


def test_unreachable_server_exits_service_not_unclassified(monkeypatch) -> None:
    """An unreachable warehouse is the retryable case, and exit 5 says so.

    README documents 5 as "a warehouse that refused the operation … Yes, retry".
    Leaving every psycopg.Error at 1 made that promise false for the commands
    most likely to hit it.
    """
    import psycopg

    def _refuse(**kwargs: object) -> object:
        raise psycopg.OperationalError("connection failed: Connection refused")

    monkeypatch.setattr(psycopg, "connect", _refuse)

    params = ConnCliParams(
        engine=SqlEngine.postgresql,
        host="127.0.0.1",
        port=1,
        database="d",
        user="u",
    ).resolve()

    with pytest.raises(typer.Exit) as excinfo, db_session(params):
        pass

    assert excinfo.value.exit_code == ExitCode.SERVICE


def test_bad_sql_stays_unclassified(monkeypatch) -> None:
    """A syntax error must NOT be reported as retryable.

    Mapping every psycopg.Error to SERVICE would tell a wrapper script to keep
    retrying a statement that will fail identically forever.
    """
    import psycopg

    def _reject(**kwargs: object) -> object:
        raise psycopg.ProgrammingError('column "nope" does not exist')

    monkeypatch.setattr(psycopg, "connect", _reject)

    params = ConnCliParams(
        engine=SqlEngine.postgresql,
        host="127.0.0.1",
        port=1,
        database="d",
        user="u",
    ).resolve()

    with pytest.raises(typer.Exit) as excinfo, db_session(params):
        pass

    assert excinfo.value.exit_code == ExitCode.FAILURE


# =========================================================================
# DuckDB: the same funnel, a second driver.
#
# Every test below runs against a real DuckDB database, because DuckDB is
# in-process and file-backed — there is no reason to fake one, and a fake would
# not have told us that duckdb.cursor() cannot see an open transaction, which
# is the single fact that shaped DuckDbSession.
# =========================================================================


def _duckdb_target(monkeypatch, path: str, *, read_only: bool = False) -> None:
    """Declare a third target, `demo_ddb`, alongside the suite's two."""
    monkeypatch.setenv("DP_TARGETS", "demo_pg,demo_rs,demo_ddb")
    monkeypatch.setenv("DEMO_DDB_ENGINE", "duckdb")
    monkeypatch.setenv("DEMO_DDB_PATH", path)
    if read_only:
        monkeypatch.setenv("DEMO_DDB_READ_ONLY", "1")


def _duckdb_params(path: str, *, read_only: bool = False) -> DuckDbConnectionParams:
    return DuckDbConnectionParams(path=path, read_only=read_only)


def _make_database(path: Path) -> None:
    """Create a small DuckDB database at ``path`` and close it again."""
    conn = duckdb.connect(database=str(path))
    conn.execute("CREATE TABLE t(id INTEGER, name TEXT)")
    conn.execute("INSERT INTO t VALUES (1, 'ada'), (2, 'grace')")
    conn.close()


def test_resolve_any_returns_the_duckdb_shape(monkeypatch, tmp_path: Path) -> None:
    _duckdb_target(monkeypatch, str(tmp_path / "w.duckdb"))

    # Through a named target, so the engine comes from DEMO_DDB_ENGINE the way
    # a user's configuration supplies it.
    params = ConnCliParams(target="demo_ddb").resolve_any()

    assert isinstance(params, DuckDbConnectionParams)
    assert params.engine == SqlEngine.duckdb
    assert params.path == str(tmp_path / "w.duckdb")


def test_resolve_refuses_a_duckdb_target_at_invalid_input(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """The commands DuckDB cannot serve keep calling resolve(), and exit 2."""
    _duckdb_target(monkeypatch, str(tmp_path / "w.duckdb"))

    with pytest.raises(typer.Exit) as excinfo:
        resolve_params_or_exit(ConnCliParams(env_prefix="DEMO_DDB"))

    assert excinfo.value.exit_code == ExitCode.INVALID_INPUT
    out = capsys.readouterr().out
    assert "duckdb" in out
    assert "not implemented" not in out.lower()


def test_resolve_any_params_or_exit_reports_a_bad_duckdb_config(
    monkeypatch, capsys
) -> None:
    """Same funnel, same exit-code contract: a missing path is a ConfigError."""
    monkeypatch.setenv("DEMO_DDB_ENGINE", "duckdb")
    monkeypatch.delenv("DEMO_DDB_PATH", raising=False)
    monkeypatch.delenv("DEMO_DDB_DATABASE", raising=False)

    with pytest.raises(typer.Exit) as excinfo:
        resolve_any_params_or_exit(ConnCliParams(env_prefix="DEMO_DDB"))

    assert excinfo.value.exit_code == ExitCode.CONFIG
    assert "DEMO_DDB_PATH" in capsys.readouterr().out


def test_duckdb_session_runs_a_query_through_the_shared_shape(tmp_path: Path) -> None:
    """`with db_session(...) as conn, conn.cursor() as cur` — unchanged."""
    database = tmp_path / "w.duckdb"
    _make_database(database)

    with (
        db_session(_duckdb_params(str(database))) as conn,
        conn.cursor() as cursor,
    ):
        cursor.execute("SELECT id, name FROM t ORDER BY id")
        rows = cursor.fetchall()
        columns = [desc.name for desc in cursor.description]

    assert rows == [(1, "ada"), (2, "grace")]
    # desc.name is what `dp db query` reads; DuckDB's own description is a
    # plain tuple with no attributes at all.
    assert columns == ["id", "name"]


def test_duckdb_session_binds_question_mark_params(tmp_path: Path) -> None:
    database = tmp_path / "w.duckdb"
    _make_database(database)

    with db_session(_duckdb_params(str(database))) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT id FROM t WHERE name = ?", ("grace",))

        assert cursor.fetchall() == [(2,)]


def test_duckdb_session_supports_memory(tmp_path: Path) -> None:
    with db_session(_duckdb_params(":memory:")) as conn, conn.cursor() as cursor:
        cursor.execute("CREATE TABLE t(x INTEGER)")
        cursor.execute("INSERT INTO t VALUES (7)")
        cursor.execute("SELECT x FROM t")

        assert cursor.fetchall() == [(7,)]


def test_duckdb_cursor_sees_the_sessions_open_transaction(tmp_path: Path) -> None:
    """Why DuckDbSession.cursor() never calls duckdb's own cursor().

    DuckDB's cursor() opens a *second* connection, which cannot see uncommitted
    work — so a harness that wraps each test in BEGIN/ROLLBACK (the way the
    PostgreSQL suite does) would hand commands an empty database.
    """
    database = tmp_path / "w.duckdb"
    _make_database(database)

    with db_session(_duckdb_params(str(database))) as conn:
        conn.execute("BEGIN")
        conn.execute("CREATE TABLE staged(x INTEGER)")
        with conn.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM staged")
            assert cursor.fetchall() == [(0,)]
        conn.execute("ROLLBACK")
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM duckdb_tables() WHERE table_name = 'staged'"
            )
            assert cursor.fetchall() == [(0,)]


def test_duckdb_cursor_close_does_not_end_the_session(tmp_path: Path) -> None:
    """Cursors share the connection, so closing one must not close it."""
    with db_session(_duckdb_params(":memory:")) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1")
        second = conn.cursor()
        second.execute("SELECT 2")

        assert second.fetchall() == [(2,)]


def test_duckdb_session_closes_the_connection_on_exit(tmp_path: Path) -> None:
    database = tmp_path / "w.duckdb"
    _make_database(database)

    with db_session(_duckdb_params(str(database))) as conn:
        raw = conn.raw

    with pytest.raises(duckdb.Error):
        raw.execute("SELECT 1")


def test_duckdb_cursor_fetch_variants_and_rowcount(tmp_path: Path) -> None:
    database = tmp_path / "w.duckdb"
    _make_database(database)

    with db_session(_duckdb_params(str(database))) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT id FROM t ORDER BY id")
        assert cursor.fetchone() == (1,)
        cursor.execute("SELECT id FROM t ORDER BY id")
        assert cursor.fetchmany(1) == [(1,)]
        # -1 is DuckDB's answer for every statement: the DB-API's "unknown".
        assert cursor.rowcount == -1


def test_duckdb_cursor_executemany(tmp_path: Path) -> None:
    with db_session(_duckdb_params(":memory:")) as conn, conn.cursor() as cursor:
        cursor.execute("CREATE TABLE t(x INTEGER)")
        cursor.executemany("INSERT INTO t VALUES (?)", [(1,), (2,), (3,)])
        cursor.execute("SELECT count(*) FROM t")

        assert cursor.fetchall() == [(3,)]


def test_duckdb_cursor_renders_a_composed_statement(tmp_path: Path) -> None:
    """psycopg's identifier quoting produces SQL DuckDB accepts, so it is used.

    Rendering rather than rejecting keeps a shared helper that quotes an
    identifier working on both engines. Placeholders are still not translated.
    """
    with db_session(_duckdb_params(":memory:")) as conn, conn.cursor() as cursor:
        cursor.execute(
            sql.SQL("CREATE TABLE {name}(x INTEGER)").format(
                name=sql.Identifier("Mixed Case")
            )
        )
        cursor.execute(
            "SELECT count(*) FROM duckdb_tables() WHERE table_name = 'Mixed Case'"
        )

        assert cursor.fetchall() == [(1,)]


# --- the exit-code contract, on the DuckDB side ------------------------------


def test_duckdb_bad_statement_stays_unclassified(tmp_path: Path) -> None:
    """A CatalogException is duckdb.ProgrammingError: the statement's own fault."""
    database = tmp_path / "w.duckdb"
    _make_database(database)

    with (
        pytest.raises(typer.Exit) as excinfo,
        db_session(_duckdb_params(str(database))) as conn,
        conn.cursor() as cur,
    ):
        cur.execute("SELECT * FROM does_not_exist")

    assert excinfo.value.exit_code == ExitCode.FAILURE


def test_duckdb_read_only_write_is_not_reported_as_retryable(tmp_path: Path) -> None:
    """Refused write → InvalidInputException → ProgrammingError → 1, not 5."""
    database = tmp_path / "w.duckdb"
    _make_database(database)

    with (
        pytest.raises(typer.Exit) as excinfo,
        db_session(_duckdb_params(str(database), read_only=True)) as conn,
        conn.cursor() as cur,
    ):
        cur.execute("CREATE TABLE nope(x INTEGER)")

    assert excinfo.value.exit_code == ExitCode.FAILURE


def test_duckdb_read_only_still_reads(tmp_path: Path) -> None:
    database = tmp_path / "w.duckdb"
    _make_database(database)

    with (
        db_session(_duckdb_params(str(database), read_only=True)) as conn,
        conn.cursor() as cursor,
    ):
        cursor.execute("SELECT count(*) FROM t")

        assert cursor.fetchall() == [(2,)]


def test_duckdb_locked_database_exits_service(tmp_path: Path) -> None:
    """Another process holding the file is the retryable case: exit 5.

    A real second process, because that is the only thing that takes DuckDB's
    file lock — two connections inside one process share the instance instead.
    """
    database = tmp_path / "w.duckdb"
    _make_database(database)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            # The write is what takes the lock: an idle reader does not.
            "import duckdb, sys, time;"
            "c = duckdb.connect(database=sys.argv[1]);"
            "c.execute('CREATE TABLE holder(x INTEGER)');"
            "print('held', flush=True);"
            "time.sleep(60)",
            str(database),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"

        with (
            pytest.raises(typer.Exit) as excinfo,
            db_session(_duckdb_params(str(database))),
        ):
            pass  # pragma: no cover - the connect raises first

        assert excinfo.value.exit_code == ExitCode.SERVICE
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_duckdb_conflicting_configuration_exits_service(tmp_path: Path) -> None:
    """ConnectionException is duckdb.OperationalError, so it is retryable too."""
    database = tmp_path / "w.duckdb"
    _make_database(database)
    writer = duckdb.connect(database=str(database))
    try:
        with (
            pytest.raises(typer.Exit) as excinfo,
            db_session(_duckdb_params(str(database), read_only=True)),
        ):
            pass  # pragma: no cover - the connect raises first

        assert excinfo.value.exit_code == ExitCode.SERVICE
    finally:
        writer.close()


def test_duckdb_missing_file_is_a_config_error_not_a_new_database(
    tmp_path: Path, capsys
) -> None:
    """duckdb.connect() would have created it; a wrong path must say so."""
    absent = tmp_path / "absent.duckdb"

    with pytest.raises(typer.Exit) as excinfo, db_session(_duckdb_params(str(absent))):
        pass  # pragma: no cover - the check raises first

    assert excinfo.value.exit_code == ExitCode.CONFIG
    assert "absent.duckdb" in capsys.readouterr().out
    assert not absent.exists()


def test_duckdb_missing_package_names_the_extra(monkeypatch, capsys) -> None:
    """A psycopg-only user who points a target at DuckDB gets a command to run."""
    monkeypatch.setitem(sys.modules, "duckdb", None)

    with pytest.raises(typer.Exit) as excinfo, db_session(_duckdb_params(":memory:")):
        pass  # pragma: no cover - the import raises first

    assert excinfo.value.exit_code == ExitCode.CONFIG
    assert "dataplat[duckdb]" in capsys.readouterr().out


# --- --verbose, on the DuckDB side ------------------------------------------


def test_duckdb_session_is_untraced_by_default(tmp_path: Path, capsys) -> None:
    with db_session(_duckdb_params(":memory:")) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT 1")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_duckdb_session_traces_the_connection_to_stderr(tmp_path: Path, capsys) -> None:
    database = tmp_path / "w.duckdb"
    _make_database(database)

    with (
        trace.verbose(),
        db_session(_duckdb_params(str(database), read_only=True)) as conn,
        conn.cursor() as cursor,
    ):
        cursor.execute("SELECT id FROM t WHERE name = ?", ("ada",))
        cursor.fetchall()

    captured = capsys.readouterr()
    # stdout stays machine-clean under --json/--format csv, on every engine.
    assert captured.out == ""
    assert f"[dp:sql] connect {database} engine=duckdb read-only" in captured.err
    assert "[dp:sql] SELECT id FROM t WHERE name = ? | 1 params bound" in captured.err
    # The value is warehouse data: the count is the whole promise.
    assert "ada" not in captured.err


def test_duckdb_tracing_needs_no_call_site_opt_in(tmp_path: Path, capsys) -> None:
    """The cursor is the mechanism, exactly as cursor_factory is for psycopg."""
    with (
        trace.verbose(),
        db_session(_duckdb_params(":memory:")) as conn,
    ):
        conn.execute("SELECT 1")
        conn.cursor().executemany("SELECT ?", [(1,), (2,)])

    err = capsys.readouterr().err
    assert "[dp:sql] SELECT 1 | no params" in err
    assert "[dp:sql] SELECT ? | no params" in err


# --- the seam a refusing command uses --------------------------------------


def test_engine_or_exit_answers_before_anything_is_resolved(
    monkeypatch, tmp_path: Path
) -> None:
    """No connection setting is read, so it works for a target with none."""
    monkeypatch.setenv("DP_TARGETS", "demo_pg,demo_rs,demo_ddb")
    monkeypatch.setenv("DEMO_DDB_ENGINE", "duckdb")
    monkeypatch.delenv("DEMO_DDB_PATH", raising=False)
    monkeypatch.delenv("DEMO_DDB_DATABASE", raising=False)

    assert engine_or_exit(ConnCliParams(target="demo_ddb")) == SqlEngine.duckdb
    assert engine_or_exit(ConnCliParams(target="demo_rs")) == SqlEngine.redshift
    assert engine_or_exit(ConnCliParams()) == SqlEngine.postgresql


def test_engine_or_exit_prefers_the_flag(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DP_TARGETS", "demo_pg,demo_rs,demo_ddb")
    monkeypatch.setenv("DEMO_DDB_ENGINE", "duckdb")

    engine = engine_or_exit(
        ConnCliParams(target="demo_ddb", engine=SqlEngine.postgresql)
    )

    assert engine == SqlEngine.postgresql


def test_engine_or_exit_reports_a_bad_engine_value(monkeypatch, capsys) -> None:
    monkeypatch.setenv("DEMO_PG_ENGINE", "sqlite")

    with pytest.raises(typer.Exit) as excinfo:
        engine_or_exit(ConnCliParams(env_prefix="DEMO_PG"))

    assert excinfo.value.exit_code == ExitCode.CONFIG
    assert "must be one of" in capsys.readouterr().out


def test_the_refusal_a_command_gets_names_the_engines_own_reason(
    monkeypatch, capsys
) -> None:
    """The pattern in engine_or_exit's docstring, end to end.

    Asking the matrix first is what makes `dp db role list -t <duckdb>` say
    "it has no users or roles at all" rather than "this command speaks libpq".
    """
    monkeypatch.setenv("DP_TARGETS", "demo_pg,demo_rs,demo_ddb")
    monkeypatch.setenv("DEMO_DDB_ENGINE", "duckdb")
    conn_cli = ConnCliParams(target="demo_ddb")

    engine = engine_or_exit(conn_cli)
    with pytest.raises(ValidationError) as excinfo:
        require_capability(engine, Capability.roles, command="dp db role list")

    assert "no users or roles" in str(excinfo.value)
    assert excinfo.value.exit_code == ExitCode.INVALID_INPUT
