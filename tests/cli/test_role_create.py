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
        # target=None, like the CLI passes when --target is omitted: the command
        # now asks the capability matrix which engine it is talking to before it
        # resolves anything, and that reads the target. Leaving the parameter at
        # its Typer default hands `resolve_target` an OptionInfo object.
        target=None,
        engine=None,
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
        target=None,  # see _invoke_create
        engine=None,
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


# --- a DuckDB target: there are no roles to create -------------------------
#
# Against a real DuckDB database, not a stub, with both drivers booby-trapped:
# `role create` writes, and a refusal that arrives after a connection has been
# opened has already taken DuckDB's single-writer lock. It also generates
# passwords and a credentials file, so "before any work" is not a nicety here.


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


def test_create_refuses_a_duckdb_target(monkeypatch, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from dataplat.cli.db import app as db_app
    from dataplat.core.errors import ExitCode

    _duckdb_target(monkeypatch, tmp_path)
    _forbid_connections(monkeypatch)
    creds = tmp_path / "creds.csv"

    result = CliRunner().invoke(
        db_app,
        [
            "role",
            "create",
            "svc",
            "-t",
            "ddb",
            "--yes",
            "--credentials-out",
            str(creds),
        ],
    )

    out = _flat(result.output)
    assert result.exit_code == ExitCode.INVALID_INPUT, result.output
    assert "dp db role create cannot run against DuckDB" in out
    assert "it has no users or roles at all" in out
    assert "That is what DuckDB is, not a missing dataplat feature" in out
    for wording in ("not supported", "not implemented"):
        assert wording not in out.lower()
    # No plan was built, and above all no password was generated and written to
    # disk for a role that could never exist.
    assert not creds.exists()
    assert "Plan:" not in out


def test_credentials_default_path_is_not_the_working_directory(
    monkeypatch, tmp_path
) -> None:
    """A generated password must not land in whatever directory you ran from.

    The old default was ``./dp-credentials-<stamp>.csv``. A data engineer's cwd is
    usually a checkout, nothing gitignores that name, and the file holds a real
    password — so the default was one `git add -A` away from committing a
    credential.
    """
    import stat

    from dataplat.cli.db import role_create

    monkeypatch.setattr(role_create, "CREDENTIALS_DIR", tmp_path / "credentials")
    monkeypatch.chdir(tmp_path)

    path = role_create._credentials_default_path()

    assert path.parent == tmp_path / "credentials"
    assert path.parent != Path.cwd()
    assert path.name.startswith("dp-credentials-")
    # The directory listing alone leaks which roles were created and when.
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_credentials_file_is_created_unreadable_to_others(
    monkeypatch, tmp_path
) -> None:
    """0600, and created that way atomically rather than chmod-ed after."""
    import stat

    from dataplat.cli.db import role_create

    target = tmp_path / "creds.csv"
    handle, is_new = role_create._open_credentials_file(target)
    handle.close()

    assert is_new is True
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert role_create._file_mode_secure(target) is True
