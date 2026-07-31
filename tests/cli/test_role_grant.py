"""``dp db role grant`` at the CLI seam.

The plan builder is tested in ``tests/services/db/test_role_grant_plan.py``;
these are about what the command does around it — which statements reach the
cursor, whether a secret ever reaches disk, and what happens on the paths where
something fails partway.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from dataplat.cli.db import role_grant as rg
from dataplat.core.errors import ExitCode


class _Cursor:
    """Fake cursor answering the two grant-side reads, recording writes.

    Reads are answered from ``kinds`` and ``held``; anything else is treated as
    a statement to record. Recording the rendered SQL rather than the Composed
    object is deliberate — the assertions are about the text an engine receives.
    """

    def __init__(
        self,
        kinds: dict[str, bool] | None = None,
        held: list[tuple[str, str]] | None = None,
    ) -> None:
        # name -> can_login (absent from the dict means the name does not exist)
        self._kinds = kinds or {}
        self._held = held or []
        self._result: list[tuple] = []
        self.statements: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def execute(self, query, params=None) -> None:
        if hasattr(query, "as_string"):  # a psycopg Composed: this is a write
            self.statements.append(query.as_string(None))
            self._result = []
            return
        text = str(query)
        if "rolcanlogin" in text:
            name = params[0]
            self._result = [(self._kinds[name],)] if name in self._kinds else []
        elif "pg_auth_members" in text:
            self._result = list(self._held)
        else:  # pragma: no cover - defensive
            self._result = []

    def fetchall(self) -> list[tuple]:
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None


class _Conn:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def cursor(self):
        return self._cursor

    def commit(self) -> None:
        return None


def _patch_session(monkeypatch, cursor: _Cursor) -> None:
    """Swap db_session for one that yields ``cursor`` and never connects."""
    import contextlib

    @contextlib.contextmanager
    def _session(params):
        yield _Conn(cursor)

    monkeypatch.setattr(rg, "db_session", _session)


def _invoke(**overrides):
    """Call grant_command with every Typer parameter supplied explicitly."""
    kwargs: dict[str, object] = {
        "roles": ["analyst"],
        "to": ["ana"],
        "create_missing_users": False,
        "kind": None,
        "to_kind": None,
        "credentials_out": None,
        "dry_run": False,
        "yes": True,
        # target=None mirrors the CLI when --target is omitted; the command asks
        # the capability matrix which engine it is talking to before resolving.
        "target": None,
        "engine": None,
        "user": "u",
        "password": None,
        "database": "d0",
        "host": "h",
        "port": 5432,
        "sslmode": None,
        "env_prefix": "DEMO_PG",
    }
    kwargs.update(overrides)
    return rg.grant_command(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The happy paths
# ---------------------------------------------------------------------------


def test_grant_issues_one_statement_per_pair(monkeypatch, capsys) -> None:
    cursor = _Cursor(kinds={"analyst": False, "reader": False, "ana": True, "bo": True})
    _patch_session(monkeypatch, cursor)

    _invoke(roles=["analyst,reader"], to=["ana", "bo"])

    assert cursor.statements == [
        'GRANT "analyst" TO "ana"',
        'GRANT "analyst" TO "bo"',
        'GRANT "reader" TO "ana"',
        'GRANT "reader" TO "bo"',
    ]
    out = capsys.readouterr().out
    assert "4 grant(s)" in out


def test_dry_run_executes_nothing_and_writes_nothing(
    monkeypatch, tmp_path, capsys
) -> None:
    cursor = _Cursor(kinds={"analyst": False, "ana": True})
    _patch_session(monkeypatch, cursor)
    # A dry run must not even create the credentials directory.
    creds_dir = tmp_path / "credentials"
    monkeypatch.setattr(rg, "credentials_default_path", lambda: creds_dir / "c.csv")

    _invoke(dry_run=True)

    assert cursor.statements == []
    assert not creds_dir.exists()
    out = capsys.readouterr().out
    assert "Dry-run" in out
    assert "analyst" in out


def test_a_grant_already_held_is_reported_and_skipped(monkeypatch, capsys) -> None:
    cursor = _Cursor(
        kinds={"analyst": False, "ana": True, "bo": True},
        held=[("analyst", "ana")],
    )
    _patch_session(monkeypatch, cursor)

    _invoke(to=["ana", "bo"])

    assert cursor.statements == ['GRANT "analyst" TO "bo"']
    out = capsys.readouterr().out
    assert "Already held (1)" in out
    assert "1 already held" in out


def test_nothing_to_do_when_every_grant_is_already_held(monkeypatch, capsys) -> None:
    """No confirmation prompt and no statements when the plan is empty."""
    cursor = _Cursor(kinds={"analyst": False, "ana": True}, held=[("analyst", "ana")])
    _patch_session(monkeypatch, cursor)

    _invoke(yes=False)  # would block on a prompt if the command asked for one

    assert cursor.statements == []
    assert "Nothing to do." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# --create-missing-users
# ---------------------------------------------------------------------------


def test_missing_target_is_refused_without_the_flag(monkeypatch, capsys) -> None:
    cursor = _Cursor(kinds={"analyst": False})
    _patch_session(monkeypatch, cursor)

    with pytest.raises(typer.Exit) as exc:
        _invoke(to=["newhire"])

    assert exc.value.exit_code == ExitCode.INVALID_INPUT
    assert cursor.statements == []
    assert "--create-missing-users" in capsys.readouterr().out


def test_create_missing_users_creates_then_grants(
    monkeypatch, tmp_path, capsys
) -> None:
    cursor = _Cursor(kinds={"analyst": False})
    _patch_session(monkeypatch, cursor)
    creds = tmp_path / "creds.csv"

    _invoke(to=["newhire"], create_missing_users=True, credentials_out=creds)

    # The user must exist before it can be granted anything.
    assert len(cursor.statements) == 2
    assert cursor.statements[0].startswith('CREATE ROLE "newhire" LOGIN PASSWORD')
    assert cursor.statements[1] == 'GRANT "analyst" TO "newhire"'
    out = capsys.readouterr().out
    assert "1 user(s) created" in out
    # The generated password is never printed.
    secret = creds.read_text().splitlines()[1].split(",")[1]
    assert secret not in out


def test_generated_credentials_are_written_0600_with_a_header(
    monkeypatch, tmp_path
) -> None:
    import stat

    cursor = _Cursor(kinds={"analyst": False})
    _patch_session(monkeypatch, cursor)
    creds = tmp_path / "creds.csv"

    _invoke(to=["newhire"], create_missing_users=True, credentials_out=creds)

    lines = creds.read_text().splitlines()
    assert lines[0] == "username,password,created_at,databases"
    username, secret, created_at, databases = lines[1].split(",")
    assert username == "newhire"
    assert len(secret) >= 32
    assert created_at  # an ISO timestamp
    # No database column: this command grants cluster-wide membership only.
    assert databases == ""
    assert stat.S_IMODE(creds.stat().st_mode) == 0o600


def test_no_credentials_file_when_nothing_is_created(monkeypatch, tmp_path) -> None:
    """Granting to existing users must not leave an empty secrets file behind."""
    cursor = _Cursor(kinds={"analyst": False, "ana": True})
    _patch_session(monkeypatch, cursor)
    creds = tmp_path / "creds.csv"

    _invoke(credentials_out=creds, create_missing_users=True)

    assert not creds.exists()


def test_a_failed_grant_writes_no_credentials(monkeypatch, tmp_path) -> None:
    """The transaction rolls back the CREATE, so no password may be recorded.

    A row here would name a user that does not exist — and, worse, imply a
    password that would work.
    """
    import psycopg

    class _FailingCursor(_Cursor):
        def execute(self, query, params=None) -> None:
            if hasattr(query, "as_string") and "GRANT" in query.as_string(None):
                raise psycopg.ProgrammingError("permission denied")
            super().execute(query, params)

    cursor = _FailingCursor(kinds={"analyst": False})
    _patch_session(monkeypatch, cursor)
    creds = tmp_path / "creds.csv"

    with pytest.raises(psycopg.ProgrammingError):
        _invoke(to=["newhire"], create_missing_users=True, credentials_out=creds)

    # Opened before the DDL (to fail fast on a bad path) but never written to.
    assert creds.read_text() == ""


def test_a_bad_credentials_path_fails_before_any_ddl(monkeypatch, tmp_path) -> None:
    """Fail on the path first, not after a user exists with a lost password."""
    cursor = _Cursor(kinds={"analyst": False})
    _patch_session(monkeypatch, cursor)

    with pytest.raises(OSError):
        _invoke(
            to=["newhire"],
            create_missing_users=True,
            credentials_out=tmp_path / "no" / "such" / "dir" / "creds.csv",
        )

    assert cursor.statements == []


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_public_is_refused_before_anything_runs(monkeypatch, capsys) -> None:
    cursor = _Cursor(kinds={"analyst": False})
    _patch_session(monkeypatch, cursor)

    with pytest.raises(typer.Exit) as exc:
        _invoke(to=["PUBLIC"])

    assert exc.value.exit_code == ExitCode.INVALID_INPUT
    assert cursor.statements == []
    assert "PUBLIC" in capsys.readouterr().out


def test_declining_the_confirmation_executes_nothing(monkeypatch) -> None:
    cursor = _Cursor(kinds={"analyst": False, "ana": True})
    _patch_session(monkeypatch, cursor)
    monkeypatch.setattr(rg.typer, "confirm", lambda *a, **k: False)

    with pytest.raises(typer.Exit) as exc:
        _invoke(yes=False)

    # A refusal is not a service failure.
    assert exc.value.exit_code == ExitCode.FAILURE
    assert cursor.statements == []


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


def test_grant_refuses_a_duckdb_target(monkeypatch, tmp_path) -> None:
    """DuckDB has no users at all, so the refusal must name the reason."""
    import duckdb
    import psycopg
    from typer.testing import CliRunner

    from dataplat.cli.db import app as db_app

    _duckdb_target(monkeypatch, tmp_path)

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("a refused command opened a connection")

    monkeypatch.setattr(psycopg, "connect", _forbidden)
    monkeypatch.setattr(duckdb, "connect", _forbidden)

    result = CliRunner().invoke(
        db_app, ["role", "grant", "--roles", "analyst", "--to", "ana", "-t", "ddb"]
    )

    out = " ".join(result.output.split())
    assert result.exit_code == ExitCode.INVALID_INPUT, result.output
    assert "dp db role grant cannot run against DuckDB" in out
    assert "it has no users or roles at all" in out
    for wording in ("not supported", "not implemented"):
        assert wording not in out.lower()
