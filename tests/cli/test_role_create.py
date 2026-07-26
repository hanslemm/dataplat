from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
import typer

from dataplat.cli.db import role_create as rc
from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.role_dialects import ParentKind


class _FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def execute(self, *args, **kwargs):
        return None

    def fetchone(self):
        return None  # role_exists -> False


class _FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def cursor(self):
        return _FakeCursor()

    def commit(self):
        return None


def _invoke_create(creds_path: Path) -> None:
    rc.create_command(
        names=["alice"],
        schema_usage=None,
        schema_create=None,
        table_select=["reporting"],
        table_all=None,
        sequence_usage=None,
        default_table_select=None,
        default_table_all=None,
        member_of=None,
        grant_to=None,
        no_login=False,
        databases_flag=["d1"],
        all_databases=False,
        credentials_out=creds_path,
        password_length=32,
        dry_run=False,
        yes=True,
        user="u",
        password=None,
        database="d0",
        host="h",
        port=5432,
        sslmode=None,
        env_prefix="DEMO_PG",
    )


def test_password_recorded_even_when_grants_fail(monkeypatch, tmp_path) -> None:
    """A per-DB grant failure must not lose the generated password."""
    creds_path = tmp_path / "creds.csv"

    monkeypatch.setattr(
        rc,
        "resolve_params_or_exit",
        lambda params: SimpleNamespace(
            as_psycopg_kwargs=lambda: {"dbname": "d0"},
            dbname="d0",
            engine=SqlEngine.postgresql,
        ),
    )
    monkeypatch.setattr(rc.psycopg, "connect", lambda **kw: _FakeConn())
    monkeypatch.setattr(rc, "_render_plan", lambda *a, **kw: None)
    monkeypatch.setattr(rc, "_execute_cluster_ops", lambda **kw: None)

    def _grants_fail(**kw):
        raise psycopg.Error("grant failed")

    monkeypatch.setattr(rc, "_execute_per_db_ops", _grants_fail)

    with pytest.raises(typer.Exit) as excinfo:
        _invoke_create(creds_path)

    assert excinfo.value.exit_code == 1
    content = creds_path.read_text()
    assert "alice" in content  # credentials row written before grants ran


def test_no_row_written_when_create_role_fails(monkeypatch, tmp_path) -> None:
    creds_path = tmp_path / "creds.csv"

    monkeypatch.setattr(
        rc,
        "resolve_params_or_exit",
        lambda params: SimpleNamespace(
            as_psycopg_kwargs=lambda: {"dbname": "d0"},
            dbname="d0",
            engine=SqlEngine.postgresql,
        ),
    )
    monkeypatch.setattr(rc.psycopg, "connect", lambda **kw: _FakeConn())
    monkeypatch.setattr(rc, "_render_plan", lambda *a, **kw: None)

    def _create_fails(**kw):
        raise psycopg.Error("create failed")

    monkeypatch.setattr(rc, "_execute_cluster_ops", _create_fails)

    with pytest.raises(typer.Exit):
        _invoke_create(creds_path)

    assert "alice" not in creds_path.read_text()


def test_no_login_writes_no_credentials_file(monkeypatch, tmp_path) -> None:
    """--no-login must not create or touch a credentials CSV."""
    creds_path = tmp_path / "creds.csv"

    monkeypatch.setattr(
        rc,
        "resolve_params_or_exit",
        lambda params: SimpleNamespace(
            as_psycopg_kwargs=lambda: {"dbname": "d0"},
            dbname="d0",
            engine=SqlEngine.postgresql,
        ),
    )
    monkeypatch.setattr(rc.psycopg, "connect", lambda **kw: _FakeConn())
    monkeypatch.setattr(rc, "_render_plan", lambda *a, **kw: None)
    monkeypatch.setattr(rc, "_execute_cluster_ops", lambda **kw: None)
    monkeypatch.setattr(rc, "_execute_per_db_ops", lambda **kw: None)

    rc.create_command(
        names=["readers"],
        schema_usage=None,
        schema_create=None,
        table_select=["reporting"],
        table_all=None,
        sequence_usage=None,
        default_table_select=None,
        default_table_all=None,
        member_of=None,
        grant_to=["alice"],
        no_login=True,
        databases_flag=["d1"],
        all_databases=False,
        credentials_out=creds_path,
        password_length=32,
        dry_run=False,
        yes=True,
        user="u",
        password=None,
        database="d0",
        host="h",
        port=5432,
        sslmode=None,
        env_prefix="DEMO_PG",
    )

    assert not creds_path.exists()


class _RedshiftCursor(_FakeCursor):
    def fetchone(self):
        return None  # role_exists -> not found (no conflicts)


def _params(engine, monkeypatch):
    monkeypatch.setattr(
        rc,
        "resolve_params_or_exit",
        lambda params: SimpleNamespace(
            as_psycopg_kwargs=lambda: {"dbname": "dev"},
            dbname="dev",
            engine=engine,
            user="admin",
        ),
    )


def test_create_redshift_absent_parent_hard_errors(monkeypatch, tmp_path) -> None:
    _params(SqlEngine.redshift, monkeypatch)
    monkeypatch.setattr(rc.psycopg, "connect", lambda **kw: _FakeConn())
    monkeypatch.setattr(
        rc,
        "dialect_for",
        lambda engine: SimpleNamespace(
            role_exists=lambda cur, n: False,
            resolve_parent_kind=lambda cur, p: ParentKind.absent,
        ),
    )
    with pytest.raises(typer.Exit) as excinfo:
        rc.create_command(
            names=["svc"],
            schema_usage=None,
            schema_create=None,
            table_select=None,
            table_all=None,
            sequence_usage=None,
            default_table_select=None,
            default_table_all=None,
            member_of=["ghost"],
            grant_to=None,
            no_login=False,
            databases_flag=None,
            all_databases=False,
            credentials_out=tmp_path / "c.csv",
            password_length=32,
            dry_run=True,
            yes=True,
            target="demo_rs",
            engine=None,
            user="admin",
            password=None,
            database="dev",
            host="h",
            port=5439,
            sslmode=None,
            env_prefix="DEMO_RS",
        )
    assert excinfo.value.exit_code == 1


# =========================================================================
# The confirmation gate and markup safety.
#
# The SQL preview is the highest-risk renderer in this module: statements are
# full of brackets, and a role name containing [/x] used to raise MarkupError
# mid-render — after the plan had already been built, but before the user
# could read it.
# =========================================================================

HOSTILE = "svc[/x][bold]"


class _RenderConn(_FakeConn):
    # psycopg renders a Composed against ``context.connection``; None makes
    # the real sql.Composed.as_string() fall back to utf-8.
    connection = None


class _Stdin:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _wire(monkeypatch, conn_cls=_RenderConn) -> None:
    monkeypatch.setattr(
        rc,
        "resolve_params_or_exit",
        lambda params: SimpleNamespace(
            as_psycopg_kwargs=lambda: {"dbname": "d0"},
            dbname="d0",
            engine=SqlEngine.postgresql,
            user="admin",
        ),
    )
    monkeypatch.setattr(rc.psycopg, "connect", lambda **kw: conn_cls())


def _create(**overrides):
    kwargs = dict(
        names=[HOSTILE],
        schema_usage=None,
        schema_create=None,
        table_select=["rep[/x]orting"],
        table_all=None,
        sequence_usage=None,
        default_table_select=None,
        default_table_all=None,
        member_of=None,
        grant_to=None,
        no_login=True,
        databases_flag=["d[bold]1"],
        all_databases=False,
        credentials_out=None,
        password_length=32,
        dry_run=True,
        yes=True,
        target=None,
        engine=None,
        user="admin",
        password=None,
        database="d0",
        host="h",
        port=5432,
        sslmode=None,
        env_prefix="DEMO_PG",
    )
    kwargs.update(overrides)
    return rc.create_command(**kwargs)


def test_sql_preview_survives_hostile_identifiers(monkeypatch, capsys) -> None:
    """The regression: ``[/x]`` in a role name used to kill the preview."""
    _wire(monkeypatch)
    _create()
    out = capsys.readouterr().out
    assert f'CREATE ROLE "{HOSTILE}"' in out
    assert '"rep[/x]orting"' in out
    assert "[bold]" in out  # not consumed as a style
    assert "Dry-run; no SQL executed." in out


def test_plan_header_shows_hostile_database_verbatim(monkeypatch, capsys) -> None:
    _wire(monkeypatch)
    _create()
    assert "d[bold]1" in capsys.readouterr().out


class _ExistingCursor(_FakeCursor):
    def fetchone(self):
        return (1,)  # role_exists -> True


class _ExistingConn(_RenderConn):
    def cursor(self):
        return _ExistingCursor()


def test_conflicting_role_error_escapes_markup(monkeypatch, capsys) -> None:
    _wire(monkeypatch, conn_cls=_ExistingConn)
    with pytest.raises(typer.Exit) as excinfo:
        _create()
    assert excinfo.value.exit_code == 1
    assert f"already exist: {HOSTILE}" in capsys.readouterr().out


def test_absent_parent_error_escapes_markup(monkeypatch, capsys) -> None:
    _wire(monkeypatch)
    monkeypatch.setattr(
        rc,
        "dialect_for",
        lambda engine: SimpleNamespace(
            role_exists=lambda cur, n: False,
            resolve_parent_kind=lambda cur, p: ParentKind.absent,
            resolve_grantee_kind=lambda cur, t: ParentKind.role,
        ),
    )
    with pytest.raises(typer.Exit):
        _create(member_of=["gho[/x]st"])
    assert "not found: gho[/x]st" in capsys.readouterr().out


def test_database_error_escapes_markup(monkeypatch, capsys) -> None:
    def _explode(**kw):
        raise psycopg.Error("could not connect to [/x]")

    monkeypatch.setattr(
        rc,
        "resolve_params_or_exit",
        lambda params: SimpleNamespace(
            as_psycopg_kwargs=lambda: {"dbname": "d0"},
            dbname="d0",
            engine=SqlEngine.postgresql,
            user="admin",
        ),
    )
    monkeypatch.setattr(rc.psycopg, "connect", _explode)
    with pytest.raises(typer.Exit):
        _create()
    assert "could not connect to [/x]" in capsys.readouterr().out


def test_credentials_path_with_markup_is_shown_verbatim(
    monkeypatch, capsys, tmp_path
) -> None:
    creds_path = tmp_path / "creds[bold].csv"
    _wire(monkeypatch)
    monkeypatch.setattr(rc, "_execute_cluster_ops", lambda **kw: None)
    monkeypatch.setattr(rc, "_execute_per_db_ops", lambda **kw: None)
    _create(no_login=False, dry_run=False, credentials_out=creds_path)
    out = capsys.readouterr().out
    assert "creds[bold].csv" in out
    # csv.writer output must stay untouched by any escaping.
    assert HOSTILE in creds_path.read_text().splitlines()[1]


# --- the gate ------------------------------------------------------------


def _gate(monkeypatch, *, tty: bool, answer: bool = False) -> list[str]:
    from dataplat.cli import _prompt

    executed: list[str] = []
    monkeypatch.setattr(_prompt, "sys", SimpleNamespace(stdin=_Stdin(tty)))
    monkeypatch.setattr(typer, "confirm", lambda *a, **k: answer)
    monkeypatch.setattr(
        rc,
        "_execute_cluster_ops",
        lambda *, plan, conn_params_kwargs: executed.append(plan.role),
    )
    monkeypatch.setattr(rc, "_execute_per_db_ops", lambda **kw: None)
    return executed


def test_confirmation_accepted_executes(monkeypatch) -> None:
    _wire(monkeypatch)
    executed = _gate(monkeypatch, tty=True, answer=True)
    _create(dry_run=False, yes=False)
    assert executed == [HOSTILE]


def test_confirmation_declined_exits_one_without_executing(monkeypatch, capsys) -> None:
    _wire(monkeypatch)
    executed = _gate(monkeypatch, tty=True, answer=False)
    with pytest.raises(typer.Exit) as excinfo:
        _create(dry_run=False, yes=False)
    assert excinfo.value.exit_code == 1
    assert executed == []
    assert "Aborted." in capsys.readouterr().out


def test_non_interactive_without_yes_names_the_flag(monkeypatch, capsys) -> None:
    """No TTY used to mean click's bare Abort; now it names the escape hatch."""
    _wire(monkeypatch)
    executed = _gate(monkeypatch, tty=False)
    with pytest.raises(typer.Exit) as excinfo:
        _create(dry_run=False, yes=False)
    assert excinfo.value.exit_code == 1
    assert executed == []
    assert "--yes" in capsys.readouterr().out


def test_yes_skips_the_prompt(monkeypatch) -> None:
    _wire(monkeypatch)
    executed = _gate(monkeypatch, tty=False)

    def _boom(*args, **kwargs):
        raise AssertionError("--yes must not prompt")

    monkeypatch.setattr(typer, "confirm", _boom)
    _create(dry_run=False, yes=True)
    assert executed == [HOSTILE]


def test_dry_run_never_prompts(monkeypatch) -> None:
    _wire(monkeypatch)
    executed = _gate(monkeypatch, tty=False)
    _create(dry_run=True, yes=False)
    assert executed == []
