from __future__ import annotations

from typing import Any
from unittest.mock import patch

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
    resolve_params_or_exit,
)
from dataplat.core import trace
from dataplat.core.errors import ExitCode
from dataplat.services.db.connection import SqlEngine


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
