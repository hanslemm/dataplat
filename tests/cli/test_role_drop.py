"""``dp db role drop`` — the confirmation gate and the SQL preview.

Drop is irreversible, so every path in and out of the gate is covered here:
dry-run, accepted, declined, non-interactive, and ``--yes``. The preview is
covered with hostile identifiers because it is the last thing a user reads
before the DROP runs, and Rich would otherwise eat or choke on ``[...]``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from dataplat.cli import _prompt
from dataplat.cli.db import role as role_mod
from dataplat.cli.db import role_drop as rd
from dataplat.services.db.connection import SqlEngine

runner = CliRunner()

# A role name that both crashes Rich (unbalanced close tag) and would be
# silently swallowed by it (a real style name).
HOSTILE = "svc[/x][bold]"


class _Cursor:
    def __init__(self, exists: bool = True) -> None:
        self._exists = exists
        self.executed: list[Any] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, statement: Any, params: Any = None) -> None:
        self.executed.append(statement)

    def fetchone(self) -> tuple[int] | None:
        return (1,) if self._exists else None


class _Conn:
    # psycopg renders a Composed against ``context.connection``; None makes
    # the real sql.Composed.as_string() fall back to utf-8.
    connection = None

    def __init__(self, exists: bool = True) -> None:
        self.cursors: list[_Cursor] = []
        self.commits = 0
        self._exists = exists

    def __enter__(self) -> _Conn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        cur = _Cursor(self._exists)
        self.cursors.append(cur)
        return cur

    def commit(self) -> None:
        self.commits += 1


class _Stdin:
    """Stand-in for ``sys.stdin``: the gate only asks whether it is a TTY."""

    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.fixture
def tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the gate take its interactive branch.

    CliRunner replaces ``sys.stdin`` with a non-TTY pipe *inside* ``invoke``,
    so swapping the module-global ``sys`` is the only way to reach the prompt.
    """
    monkeypatch.setattr(_prompt, "sys", SimpleNamespace(stdin=_Stdin(True)))


@pytest.fixture
def executed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every plan the command would execute, without touching a DB."""
    calls: list[str] = []

    def _record(*, plan: Any, conn_params_kwargs: Any, console: Any) -> None:
        calls.append(plan.role)

    monkeypatch.setattr(rd, "_execute_plan", _record)
    return calls


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real plan building and rendering; only the connection is faked."""
    monkeypatch.setattr(
        rd,
        "resolve_params_or_exit",
        lambda p: SimpleNamespace(
            as_psycopg_kwargs=lambda: {"dbname": "analytics"},
            dbname="analytics",
            engine=SqlEngine.postgresql,
            user="admin",
        ),
    )
    monkeypatch.setattr(rd.psycopg, "connect", lambda **kw: _Conn())


def _invoke(args: list[str], **kwargs: Any) -> Any:
    return runner.invoke(role_mod.app, ["drop", *args], **kwargs)


BASE = ["--reassign-to", "owner", "--databases", "analytics"]


def test_dry_run_previews_the_sql_and_executes_nothing(
    wired: None, executed: list[str]
) -> None:
    result = _invoke([*BASE, "--dry-run", HOSTILE])
    assert result.exit_code == 0, result.output
    assert "DROP ROLE" in result.output
    assert "Dry-run; no SQL executed." in result.output
    assert executed == []


def test_dry_run_needs_no_confirmation(wired: None, executed: list[str]) -> None:
    """A preview is not destructive, so the gate must not fire at all."""
    result = _invoke([*BASE, "--dry-run", "svc"])
    assert result.exit_code == 0, result.output
    assert "--yes" not in result.output


def test_confirmation_accepted_executes(
    wired: None, executed: list[str], tty: None
) -> None:
    result = _invoke([*BASE, "svc"], input="y\n")
    assert result.exit_code == 0, result.output
    assert executed == ["svc"]
    assert "Dropped" in result.output


def test_confirmation_declined_exits_one_and_executes_nothing(
    wired: None, executed: list[str], tty: None
) -> None:
    result = _invoke([*BASE, "svc"], input="n\n")
    assert result.exit_code == 1
    assert executed == []
    assert "Aborted." in result.output


def test_confirmation_keeps_its_destructive_framing(
    wired: None, executed: list[str], tty: None
) -> None:
    result = _invoke([*BASE, "svc"], input="n\n")
    assert "This is destructive." in result.output


def test_non_interactive_without_yes_refuses_with_the_flag_hint(
    wired: None, executed: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No TTY used to mean click's bare Abort; now it names the escape hatch."""
    monkeypatch.setattr(_prompt, "sys", SimpleNamespace(stdin=_Stdin(False)))
    result = _invoke([*BASE, "svc"])
    assert result.exit_code == 1
    assert executed == []
    assert "--yes" in result.output


def test_yes_proceeds_without_prompting(
    wired: None, executed: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*args: object, **kwargs: object) -> bool:
        raise AssertionError("--yes must not prompt")

    monkeypatch.setattr(rd.typer, "confirm", _boom)
    result = _invoke([*BASE, "--yes", "svc"])
    assert result.exit_code == 0, result.output
    assert executed == ["svc"]


# --- markup safety -------------------------------------------------------


def test_sql_preview_survives_hostile_identifiers(
    wired: None, executed: list[str]
) -> None:
    """The regression: ``[/x]`` used to raise MarkupError mid-render."""
    result = _invoke([*BASE, "--dry-run", HOSTILE])
    assert result.exit_code == 0, result.output
    assert f'DROP ROLE "{HOSTILE}"' in result.output
    assert "[bold]" in result.output  # not consumed as a style


def test_dropped_table_shows_hostile_role_verbatim(
    wired: None, executed: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _invoke([*BASE, "--yes", HOSTILE])
    assert result.exit_code == 0, result.output
    assert HOSTILE in result.output


def test_missing_role_error_escapes_markup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        rd,
        "resolve_params_or_exit",
        lambda p: SimpleNamespace(
            as_psycopg_kwargs=lambda: {"dbname": "analytics"},
            dbname="analytics",
            engine=SqlEngine.postgresql,
            user="admin",
        ),
    )
    monkeypatch.setattr(rd.psycopg, "connect", lambda **kw: _Conn(exists=False))
    result = _invoke([*BASE, "--dry-run", HOSTILE])
    assert result.exit_code == 1
    assert f"role(s) not found: {HOSTILE}" in result.output


def test_database_error_escapes_markup(monkeypatch: pytest.MonkeyPatch) -> None:
    def _explode(**kw: object) -> _Conn:
        raise psycopg.Error("relation [/x] does not exist")

    monkeypatch.setattr(
        rd,
        "resolve_params_or_exit",
        lambda p: SimpleNamespace(
            as_psycopg_kwargs=lambda: {"dbname": "analytics"},
            dbname="analytics",
            engine=SqlEngine.postgresql,
            user="admin",
        ),
    )
    monkeypatch.setattr(rd.psycopg, "connect", _explode)
    result = _invoke([*BASE, "--dry-run", "svc"])
    assert result.exit_code == 1
    assert "relation [/x] does not exist" in result.output


def test_execution_failure_escapes_markup(
    wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail(*, plan: Any, conn_params_kwargs: Any, console: Any) -> None:
        raise psycopg.Error("owner [/x] missing")

    monkeypatch.setattr(rd, "_execute_plan", _fail)
    result = _invoke([*BASE, "--yes", HOSTILE])
    assert result.exit_code == 1
    assert HOSTILE in result.output
    assert "owner [/x] missing" in result.output


# --- execution order ----------------------------------------------------


def test_execute_plan_runs_pre_cluster_then_per_db_then_cluster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DROP ROLE must come last: the per-DB cleanup is its precondition."""
    from rich.console import Console

    from dataplat.services.db.role_admin import build_drop_plan
    from dataplat.services.db.role_dialects import dialect_for

    plan = build_drop_plan(
        "svc",
        ["analytics"],
        dialect_for(SqlEngine.postgresql),
        reassign_to="owner",
        grant_membership_to="admin",
    )
    conns: list[tuple[str, _Conn]] = []

    def _connect(**kw: Any) -> _Conn:
        conn = _Conn()
        conns.append((kw["dbname"], conn))
        return conn

    monkeypatch.setattr(rd.psycopg, "connect", _connect)
    rd._execute_plan(
        plan=plan,
        conn_params_kwargs={"dbname": "postgres"},
        console=Console(),
    )
    assert [db for db, _ in conns] == ["postgres", "analytics", "postgres"]
    assert all(conn.commits == 1 for _, conn in conns)
    last = conns[-1][1].cursors[0].executed
    assert "DROP ROLE" in last[0].as_string(None)


# --- a DuckDB target: there are no roles to drop ---------------------------
#
# Against a real DuckDB database, not a stub. `role drop` is irreversible, so
# the refusal has to land before the plan, before the confirmation gate and
# before any connection — both drivers are booby-trapped to prove the last one.


def _flat(text: str) -> str:
    """One long line: Rich wraps at the terminal width, assertions are wording."""
    return " ".join(text.split())


def _duckdb_target(monkeypatch, tmp_path: Path) -> Path:
    import duckdb

    path = tmp_path / "warehouse.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute("CREATE TABLE orders(id INTEGER)")
    connection.close()
    monkeypatch.setenv("DP_TARGETS", "ddb")
    monkeypatch.setenv("DDB_ENGINE", "duckdb")
    monkeypatch.setenv("DDB_PATH", str(path))
    monkeypatch.delenv("DP_DEFAULT_TARGET", raising=False)
    return path


def _forbid_connections(monkeypatch) -> None:
    import duckdb

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("a refused command opened a connection")

    monkeypatch.setattr(psycopg, "connect", _forbidden)
    monkeypatch.setattr(duckdb, "connect", _forbidden)


def test_drop_refuses_a_duckdb_target_before_the_gate(
    monkeypatch, tmp_path: Path
) -> None:
    """No ``--yes``: the user must never be asked to approve this at all."""
    from dataplat.cli.db import app as db_app
    from dataplat.core.errors import ExitCode

    path = _duckdb_target(monkeypatch, tmp_path)
    before = path.read_bytes()
    _forbid_connections(monkeypatch)

    result = runner.invoke(db_app, ["role", "drop", "svc", "-t", "ddb"])

    out = _flat(result.output)
    assert result.exit_code == ExitCode.INVALID_INPUT, result.output
    assert "dp db role drop cannot run against DuckDB" in out
    assert "it has no users or roles at all" in out
    assert "That is what DuckDB is, not a missing dataplat feature" in out
    for wording in ("not supported", "not implemented"):
        assert wording not in out.lower()
    assert "This is destructive" not in out
    assert "Plan:" not in out
    # The database is untouched, byte for byte.
    assert path.read_bytes() == before
