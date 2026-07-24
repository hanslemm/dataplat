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
        rc, "resolve_params_or_exit",
        lambda params: SimpleNamespace(
            as_psycopg_kwargs=lambda: {"dbname": "dev"},
            dbname="dev", engine=engine, user="admin",
        ),
    )


def test_create_redshift_absent_parent_hard_errors(monkeypatch, tmp_path) -> None:
    _params(SqlEngine.redshift, monkeypatch)
    monkeypatch.setattr(rc.psycopg, "connect", lambda **kw: _FakeConn())
    monkeypatch.setattr(
        rc, "dialect_for",
        lambda engine: SimpleNamespace(
            role_exists=lambda cur, n: False,
            resolve_parent_kind=lambda cur, p: ParentKind.absent,
        ),
    )
    with pytest.raises(typer.Exit) as excinfo:
        rc.create_command(
            names=["svc"], schema_usage=None, schema_create=None,
            table_select=None, table_all=None, sequence_usage=None,
            default_table_select=None, default_table_all=None,
            member_of=["ghost"], grant_to=None, no_login=False,
            databases_flag=None, all_databases=False,
            credentials_out=tmp_path / "c.csv", password_length=32,
            dry_run=True, yes=True, target="demo_rs", engine=None,
            user="admin", password=None, database="dev", host="h",
            port=5439, sslmode=None, env_prefix="DEMO_RS",
        )
    assert excinfo.value.exit_code == 1
