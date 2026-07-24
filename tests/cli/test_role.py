from __future__ import annotations

import pytest
import typer
from rich.console import Console

from dataplat.cli.db.role import (
    render_role_description,
    role_description_to_json,
)
from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.role import (
    DefaultPrivilege,
    EffectivePrivilege,
    MembershipEdge,
    OwnedObjectsSummary,
    RoleAttributes,
    RoleDescription,
    RoleKind,
    RoleRef,
)


def _blank_role_description() -> RoleDescription:
    return RoleDescription(
        ref=RoleRef(oid=16384, name="alice", kind=RoleKind.user),
        attributes=RoleAttributes(
            can_login=True, superuser=False, create_db=False,
            create_role=False, inherit=True, replication=False,
            bypass_rls=False, connection_limit=-1,
            password_set=True, valid_until=None,
        ),
        memberships_out=[
            MembershipEdge(role="readers", inherit=True, depth=1, via="alice"),
        ],
        memberships_in=[],
        owned=OwnedObjectsSummary(schemas=["scratch"],
                                  relations_by_schema={"scratch": {"table": 2}},
                                  total_relations=2),
        closure={"alice", "readers", "public"},
        direct_only=False,
        effective_privileges=[
            EffectivePrivilege(
                scope="schema", qualified_name="public", kind="schema",
                privilege="USAGE", grantor="dbadmin", via="public",
                grantable=False,
            ),
            EffectivePrivilege(
                scope="relation", qualified_name="public.users", kind="table",
                privilege="SELECT", grantor="dbadmin", via="readers",
                grantable=False,
            ),
        ],
        default_privileges=[
            DefaultPrivilege(
                owner="dbadmin", schema="public", object_type="table",
                privilege="SELECT", via="readers", grantable=False,
            ),
        ],
    )


def test_render_role_basic() -> None:
    console = Console(record=True, width=120)
    render_role_description(console, _blank_role_description(), SqlEngine.postgresql)
    out = console.export_text()
    assert "alice" in out
    assert "User" in out or "user" in out
    assert "Attributes" in out
    assert "Memberships" in out
    assert "Ownership" in out or "Owned" in out
    assert "Privileges" in out
    assert "public.users" in out
    assert "SELECT" in out
    assert "Default" in out


def test_render_role_includes_via_column() -> None:
    console = Console(record=True, width=120)
    render_role_description(console, _blank_role_description(), SqlEngine.postgresql)
    out = console.export_text()
    assert "readers" in out


def test_render_role_redshift_shows_probing_note_when_rbac_off() -> None:
    from dataclasses import replace
    desc = replace(
        _blank_role_description(),
        default_privileges=[],
        redshift_rbac=False,
    )
    console = Console(record=True, width=120)
    render_role_description(console, desc, SqlEngine.redshift)
    out = console.export_text()
    assert "RBAC not available" in out
    assert "probing has_*_privilege" in out


def test_render_role_redshift_rbac_note() -> None:
    from dataclasses import replace
    desc = replace(
        _blank_role_description(),
        default_privileges=[],
        redshift_rbac=True,
    )
    console = Console(record=True, width=120)
    render_role_description(console, desc, SqlEngine.redshift)
    out = console.export_text()
    assert "svv_*_privileges" in out


def test_db_role_cli_smoke() -> None:
    from unittest.mock import MagicMock, patch

    from typer.testing import CliRunner

    from dataplat.cli.db import app as db_app

    cursor = MagicMock()
    cursor.fetchone.side_effect = [
        # resolve_role
        (16384, True, False),
        # attributes
        (True, False, True, True, True, False, False, -1, False, None),
    ]
    cursor.fetchall.side_effect = [
        [("readers", True, 1, "alice")],   # memberships_out
        [],                                  # memberships_in
        [],                                  # owned schemas
        [],                                  # owned relations
        [],                                  # schemas
        [],                                  # relations
        [],                                  # sequences
        [],                                  # functions
        [],                                  # defaults
    ]

    fake_conn = MagicMock()
    fake_conn.__enter__.return_value = fake_conn
    fake_conn.cursor.return_value.__enter__.return_value = cursor

    env = {
        "DEMO_PG_USER": "svc",
        "DEMO_PG_HOST": "localhost",
        "DEMO_PG_DATABASE": "analytics",
    }
    runner = CliRunner()
    with patch("dataplat.cli.db._common.psycopg.connect", return_value=fake_conn):
        result = runner.invoke(db_app, ["role", "show", "alice"], env=env)
    assert result.exit_code == 0, result.output
    assert "alice" in result.output
    assert "Attributes" in result.output


def test_render_role_truncates_sections() -> None:
    from dataclasses import replace
    many = [
        EffectivePrivilege(
            scope="relation", qualified_name=f"public.t_{i}", kind="table",
            privilege="SELECT", grantor="dbadmin", via="alice", grantable=False,
        )
        for i in range(25)
    ]
    desc = replace(_blank_role_description(), effective_privileges=many)
    console = Console(record=True, width=120)
    render_role_description(console, desc, SqlEngine.postgresql, max_rows=10)
    out = console.export_text()
    assert "public.t_0" in out
    assert "public.t_9" in out
    assert "public.t_10" not in out  # truncated
    assert "15 more" in out


def test_render_role_max_rows_zero_shows_all() -> None:
    from dataclasses import replace
    many = [
        EffectivePrivilege(
            scope="relation", qualified_name=f"public.t_{i}", kind="table",
            privilege="SELECT", grantor="dbadmin", via="alice", grantable=False,
        )
        for i in range(25)
    ]
    desc = replace(_blank_role_description(), effective_privileges=many)
    console = Console(record=True, width=120)
    render_role_description(console, desc, SqlEngine.postgresql, max_rows=0)
    out = console.export_text()
    assert "public.t_24" in out
    assert "more" not in out.split("Effective")[1].split("Default")[0].lower() \
        or "more (" not in out  # no "… and N more" footer


def test_role_description_to_json_roundtrips() -> None:
    import json as _json
    desc = _blank_role_description()
    payload = _json.loads(role_description_to_json(desc))
    assert payload["ref"]["name"] == "alice"
    assert payload["ref"]["kind"] == "user"
    assert payload["attributes"]["can_login"] is True
    assert payload["closure"] == ["alice", "public", "readers"]
    assert payload["memberships_out"][0]["role"] == "readers"
    assert payload["effective_privileges"][1]["qualified_name"] == "public.users"
    assert payload["default_privileges"][0]["object_type"] == "table"


def test_db_role_cli_json_flag() -> None:
    import json as _json
    from unittest.mock import MagicMock, patch

    from typer.testing import CliRunner

    from dataplat.cli.db import app as db_app

    cursor = MagicMock()
    cursor.fetchone.side_effect = [
        (16384, True, False),
        (True, False, True, True, True, False, False, -1, False, None),
    ]
    cursor.fetchall.side_effect = [
        [("readers", True, 1, "alice")],
        [], [], [], [], [], [], [], [],
    ]
    fake_conn = MagicMock()
    fake_conn.__enter__.return_value = fake_conn
    fake_conn.cursor.return_value.__enter__.return_value = cursor

    env = {
        "DEMO_PG_USER": "svc",
        "DEMO_PG_HOST": "localhost",
        "DEMO_PG_DATABASE": "analytics",
    }
    runner = CliRunner()
    with patch("dataplat.cli.db._common.psycopg.connect", return_value=fake_conn):
        result = runner.invoke(db_app, ["role", "show", "alice", "--json"], env=env)
    assert result.exit_code == 0, result.output
    # Payload is the first JSON object in stdout.
    start = result.output.index("{")
    end = result.output.rindex("}") + 1
    payload = _json.loads(result.output[start:end])
    assert payload["ref"]["name"] == "alice"
    assert payload["ref"]["kind"] == "user"
    assert "Attributes" not in result.output  # Rich report suppressed


def test_list_command_redshift_uses_dialect(monkeypatch) -> None:
    from types import SimpleNamespace

    from dataplat.cli.db import role_list as rl
    from dataplat.services.db.connection import SqlEngine
    from dataplat.services.db.role_admin import RoleSummary

    captured = {}

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, *a, **k): return None

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def cursor(self): return _Cur()

    monkeypatch.setattr(
        rl, "resolve_params_or_exit",
        lambda p: SimpleNamespace(engine=SqlEngine.redshift),
    )
    import contextlib

    @contextlib.contextmanager
    def _sess(params):
        yield _Conn()

    monkeypatch.setattr(rl, "db_session", _sess)

    def _fake_dialect(engine):
        captured["engine"] = engine
        return SimpleNamespace(
            list_roles=lambda cur: [
                RoleSummary("svc", True, False, False, False, 0, 0)
            ]
        )

    monkeypatch.setattr(rl, "dialect_for", _fake_dialect)

    rl.list_command(
        filter_substring=None, users_only=False, groups_only=False,
        as_json=True, target="demo_rs", engine=None, user="u",
        password=None, database="dev", host="h", port=5439, sslmode=None,
        env_prefix="DEMO_RS",
    )
    assert captured["engine"] == SqlEngine.redshift


def test_drop_redshift_group_target_errors(monkeypatch) -> None:
    from types import SimpleNamespace

    from dataplat.cli.db import role_drop as rd
    from dataplat.services.db.connection import SqlEngine

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, *a, **k): return None
        def fetchone(self): return (1,)  # role_exists -> True

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def cursor(self): return _Cur()

    monkeypatch.setattr(
        rd, "resolve_params_or_exit",
        lambda p: SimpleNamespace(
            as_psycopg_kwargs=lambda: {"dbname": "dev"},
            dbname="dev", engine=SqlEngine.redshift, user="admin",
        ),
    )
    monkeypatch.setattr(rd.psycopg, "connect", lambda **kw: _Conn())

    def _enumerate_raises(cur, name):
        raise ValueError(f'"{name}" is not a Redshift user')

    monkeypatch.setattr(
        rd, "dialect_for",
        lambda engine: SimpleNamespace(
            role_exists=lambda cur, n: True,
            enumerate_owned=_enumerate_raises,
            groups_of=lambda cur, n: [],
        ),
    )
    with pytest.raises(typer.Exit) as excinfo:
        rd.drop_command(
            names=["reporting"], reassign_to=None, no_reassign=False,
            no_grant_membership=False, databases_flag=None, all_databases=False,
            dry_run=True, yes=True, target="demo_rs", engine=None,
            user="admin", password=None, database="dev", host="h",
            port=5439, sslmode=None, env_prefix="DEMO_RS",
        )
    assert excinfo.value.exit_code == 1


def test_drop_redshift_defaults_reassign_owner_from_target(monkeypatch) -> None:
    from types import SimpleNamespace

    from dataplat.cli.db import role_drop as rd
    from dataplat.services.db.connection import SqlEngine
    from dataplat.services.db.role_admin import DropPlan

    captured = {}

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, *a, **k): return None
        def fetchone(self): return (1,)

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def cursor(self): return _Cur()

    monkeypatch.setattr(
        rd, "resolve_params_or_exit",
        lambda p: SimpleNamespace(
            as_psycopg_kwargs=lambda: {"dbname": "dev"},
            dbname="dev", engine=SqlEngine.redshift, user="admin",
        ),
    )
    monkeypatch.setattr(rd.psycopg, "connect", lambda **kw: _Conn())
    monkeypatch.setattr(rd, "dialect_for", lambda engine: SimpleNamespace(
        role_exists=lambda cur, n: True,
        enumerate_owned=lambda cur, n: rd.OwnedForDrop(),
        groups_of=lambda cur, n: [],
    ))

    def _capture(name, dbs, dialect, **kw):
        captured.update(kw)
        return DropPlan(role=name, pre_cluster_ops=[],
                        per_database_ops={dbs[0]: []}, cluster_ops=[])

    monkeypatch.setattr(rd, "build_drop_plan", _capture)
    rd.drop_command(
        names=["svc"], reassign_to=None, no_reassign=False,
        no_grant_membership=False, databases_flag=None, all_databases=False,
        dry_run=True, yes=True, target="demo_rs", engine=None,
        user="admin", password=None, database="dev", host="h", port=5439,
        sslmode=None, env_prefix="DEMO_RS",
    )
    assert captured["reassign_to"] == "admin"
