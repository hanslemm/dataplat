from __future__ import annotations

from pathlib import Path

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
            can_login=True,
            superuser=False,
            create_db=False,
            create_role=False,
            inherit=True,
            replication=False,
            bypass_rls=False,
            connection_limit=-1,
            password_set=True,
            valid_until=None,
        ),
        memberships_out=[
            MembershipEdge(role="readers", inherit=True, depth=1, via="alice"),
        ],
        memberships_in=[],
        owned=OwnedObjectsSummary(
            schemas=["scratch"],
            relations_by_schema={"scratch": {"table": 2}},
            total_relations=2,
        ),
        closure={"alice", "readers", "public"},
        direct_only=False,
        effective_privileges=[
            EffectivePrivilege(
                scope="schema",
                qualified_name="public",
                kind="schema",
                privilege="USAGE",
                grantor="dbadmin",
                via="public",
                grantable=False,
            ),
            EffectivePrivilege(
                scope="relation",
                qualified_name="public.users",
                kind="table",
                privilege="SELECT",
                grantor="dbadmin",
                via="readers",
                grantable=False,
            ),
        ],
        default_privileges=[
            DefaultPrivilege(
                owner="dbadmin",
                schema="public",
                object_type="table",
                privilege="SELECT",
                via="readers",
                grantable=False,
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
        # pg_authid readable? password_set is only knowable to a superuser, so
        # fetch_attributes probes for the privilege before choosing its query.
        (True,),
        # attributes
        (True, False, True, True, True, False, False, -1, False, None),
    ]
    cursor.fetchall.side_effect = [
        [("readers", True, 1, "alice")],  # memberships_out
        [],  # memberships_in
        [],  # owned schemas
        [],  # owned relations
        [],  # schemas
        [],  # relations
        [],  # sequences
        [],  # functions
        [],  # defaults
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
            scope="relation",
            qualified_name=f"public.t_{i}",
            kind="table",
            privilege="SELECT",
            grantor="dbadmin",
            via="alice",
            grantable=False,
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
            scope="relation",
            qualified_name=f"public.t_{i}",
            kind="table",
            privilege="SELECT",
            grantor="dbadmin",
            via="alice",
            grantable=False,
        )
        for i in range(25)
    ]
    desc = replace(_blank_role_description(), effective_privileges=many)
    console = Console(record=True, width=120)
    render_role_description(console, desc, SqlEngine.postgresql, max_rows=0)
    out = console.export_text()
    assert "public.t_24" in out
    assert (
        "more" not in out.split("Effective")[1].split("Default")[0].lower()
        or "more (" not in out
    )  # no "… and N more" footer


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
        # pg_authid privilege probe (see the smoke test above).
        (True,),
        (True, False, True, True, True, False, False, -1, False, None),
    ]
    cursor.fetchall.side_effect = [
        [("readers", True, 1, "alice")],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
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
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def execute(self, *a, **k):
            return None

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def cursor(self):
            return _Cur()

    monkeypatch.setattr(
        rl,
        "resolve_params_or_exit",
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
            list_roles=lambda cur: [RoleSummary("svc", True, False, False, False, 0, 0)]
        )

    monkeypatch.setattr(rl, "dialect_for", _fake_dialect)

    rl.list_command(
        filter_substring=None,
        users_only=False,
        groups_only=False,
        as_json=True,
        target="demo_rs",
        engine=None,
        user="u",
        password=None,
        database="dev",
        host="h",
        port=5439,
        sslmode=None,
        env_prefix="DEMO_RS",
    )
    assert captured["engine"] == SqlEngine.redshift


def test_drop_redshift_group_target_errors(monkeypatch) -> None:
    from types import SimpleNamespace

    from dataplat.cli.db import role_drop as rd
    from dataplat.services.db.connection import SqlEngine

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def execute(self, *a, **k):
            return None

        def fetchone(self):
            return (1,)  # role_exists -> True

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def cursor(self):
            return _Cur()

    monkeypatch.setattr(
        rd,
        "resolve_params_or_exit",
        lambda p: SimpleNamespace(
            as_psycopg_kwargs=lambda: {"dbname": "dev"},
            dbname="dev",
            engine=SqlEngine.redshift,
            user="admin",
        ),
    )
    monkeypatch.setattr(rd.psycopg, "connect", lambda **kw: _Conn())

    def _enumerate_raises(cur, name):
        raise ValueError(f'"{name}" is not a Redshift user')

    monkeypatch.setattr(
        rd,
        "dialect_for",
        lambda engine: SimpleNamespace(
            role_exists=lambda cur, n: True,
            enumerate_owned=_enumerate_raises,
            groups_of=lambda cur, n: [],
        ),
    )
    with pytest.raises(typer.Exit) as excinfo:
        rd.drop_command(
            names=["reporting"],
            reassign_to=None,
            no_reassign=False,
            no_grant_membership=False,
            databases_flag=None,
            all_databases=False,
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


def test_drop_redshift_defaults_reassign_owner_from_target(monkeypatch) -> None:
    from types import SimpleNamespace

    from dataplat.cli.db import role_drop as rd
    from dataplat.services.db.connection import SqlEngine
    from dataplat.services.db.role_admin import DropPlan

    captured = {}

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def execute(self, *a, **k):
            return None

        def fetchone(self):
            return (1,)

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def cursor(self):
            return _Cur()

    monkeypatch.setattr(
        rd,
        "resolve_params_or_exit",
        lambda p: SimpleNamespace(
            as_psycopg_kwargs=lambda: {"dbname": "dev"},
            dbname="dev",
            engine=SqlEngine.redshift,
            user="admin",
        ),
    )
    monkeypatch.setattr(rd.psycopg, "connect", lambda **kw: _Conn())
    monkeypatch.setattr(
        rd,
        "dialect_for",
        lambda engine: SimpleNamespace(
            role_exists=lambda cur, n: True,
            enumerate_owned=lambda cur, n: rd.OwnedForDrop(),
            groups_of=lambda cur, n: [],
        ),
    )

    def _capture(name, dbs, dialect, **kw):
        captured.update(kw)
        return DropPlan(
            role=name, pre_cluster_ops=[], per_database_ops={dbs[0]: []}, cluster_ops=[]
        )

    monkeypatch.setattr(rd, "build_drop_plan", _capture)
    rd.drop_command(
        names=["svc"],
        reassign_to=None,
        no_reassign=False,
        no_grant_membership=False,
        databases_flag=None,
        all_databases=False,
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
    assert captured["reassign_to"] == "admin"


# =========================================================================
# Markup safety: every value below reaches Rich straight from the warehouse.
# ``[/x]`` used to raise MarkupError mid-render and kill the command;
# ``[bold]`` used to be swallowed, misrepresenting the data.
# =========================================================================

HOSTILE_NAME = "svc[/x][bold]"


def _hostile_role_description() -> RoleDescription:
    from dataclasses import replace

    base = _blank_role_description()
    return replace(
        base,
        ref=RoleRef(oid=16385, name=HOSTILE_NAME, kind=RoleKind.user),
        attributes=replace(base.attributes, valid_until="2030-01-01 [/x]"),
        memberships_out=[
            MembershipEdge(
                role="read[/x]ers", inherit=True, depth=1, via="ali[bold]ce"
            ),
        ],
        memberships_in=[
            MembershipEdge(role="chi[/x]ld", inherit=False, depth=1, via="sv[bold]c"),
        ],
        owned=OwnedObjectsSummary(
            schemas=["scr[/x]atch"],
            relations_by_schema={"scr[/x]atch": {"ta[bold]ble": 2}},
            total_relations=2,
        ),
        effective_privileges=[
            EffectivePrivilege(
                scope="relation",
                qualified_name="pub[/x].use[bold]rs",
                kind="ta[/x]ble",
                privilege="SEL[/x]ECT",
                grantor="dba[bold]",
                via="read[/x]ers",
                grantable=True,
            ),
        ],
        default_privileges=[
            DefaultPrivilege(
                owner="dba[/x]",
                schema="pub[bold]",
                object_type="ta[/x]ble",
                privilege="SEL[bold]ECT",
                via="read[/x]ers",
                grantable=True,
            ),
        ],
    )


def test_render_role_survives_hostile_warehouse_data() -> None:
    console = Console(record=True, width=200)
    render_role_description(console, _hostile_role_description(), SqlEngine.postgresql)
    out = console.export_text()
    for value in (
        HOSTILE_NAME,
        "2030-01-01 [/x]",
        "read[/x]ers",
        "ali[bold]ce",
        "chi[/x]ld",
        "scr[/x]atch",
        "ta[bold]ble",
        "pub[/x].use[bold]rs",
        "SEL[/x]ECT",
        "dba[bold]",
        "dba[/x]",
        "pub[bold]",
        "SEL[bold]ECT",
    ):
        assert value in out, value


def test_render_role_json_is_untouched_by_escaping() -> None:
    """--json must stay byte-identical: no markup escaping in the payload."""
    import json as _json

    payload = _json.loads(role_description_to_json(_hostile_role_description()))
    assert payload["ref"]["name"] == HOSTILE_NAME
    assert payload["effective_privileges"][0]["privilege"] == "SEL[/x]ECT"


def test_render_role_list_survives_hostile_role_name() -> None:
    from dataplat.cli.db.role_list import _render
    from dataplat.services.db.role_admin import RoleSummary

    console = Console(record=True, width=200)
    _render(console, [RoleSummary(HOSTILE_NAME, True, False, False, False, 1, 2)])
    out = console.export_text()
    assert HOSTILE_NAME in out


def test_show_command_error_escapes_markup(monkeypatch, capsys) -> None:
    import contextlib
    from types import SimpleNamespace

    from dataplat.cli.db import role as role_mod
    from dataplat.services.db.role import RoleNotFoundError

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

    class _Conn:
        def cursor(self):
            return _Cur()

    @contextlib.contextmanager
    def _sess(params):
        yield _Conn()

    monkeypatch.setattr(
        role_mod,
        "resolve_params_or_exit",
        lambda p: SimpleNamespace(engine=SqlEngine.postgresql),
    )
    monkeypatch.setattr(role_mod, "db_session", _sess)

    def _missing(cursor, name, *, engine, direct_only):
        raise RoleNotFoundError(f'role "{name}" does not exist')

    monkeypatch.setattr(role_mod, "describe_role", _missing)

    with pytest.raises(typer.Exit) as excinfo:
        role_mod.show_command(
            name=HOSTILE_NAME,
            target=None,
            engine=None,
            user="u",
            password=None,
            database="d",
            host="h",
            port=5432,
            sslmode=None,
            env_prefix="DEMO_PG",
            direct_only=False,
            max_rows=10,
            as_json=False,
        )
    assert excinfo.value.exit_code == 1
    assert HOSTILE_NAME in capsys.readouterr().out


# --- a DuckDB target: there are no roles to describe -----------------------
#
# Against a real DuckDB database, not a stub: the engine is in-process and
# file-backed, so there is nothing to fake, and both drivers are booby-trapped
# so that a refusal arriving *after* a connection fails the test. Opening first
# would already have taken DuckDB's single-writer lock on a file someone may be
# running dbt against.


def _duckdb_target(monkeypatch, tmp_path) -> Path:
    """Create a real DuckDB database and declare it as the only target."""
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
    import psycopg

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("a refused command opened a connection")

    monkeypatch.setattr(psycopg, "connect", _forbidden)
    monkeypatch.setattr(duckdb, "connect", _forbidden)


def _flat(text: str) -> str:
    """One long line, because Rich wraps at the terminal width.

    Every assertion below is about wording, and a clause that happens to fit
    today would silently start straddling a line break the next time the
    sentence grows or COLUMNS changes.
    """
    return " ".join(text.split())


def _assert_no_roles_refusal(result, command: str) -> None:
    """The one shape every role refusal takes, asserted once."""
    from dataplat.core.errors import ExitCode

    out = _flat(result.output)
    assert result.exit_code == ExitCode.INVALID_INPUT, result.output
    assert f"{command} cannot run against DuckDB" in out
    # The reason, not merely the fact: this is the only place a user learns it.
    assert "it has no users or roles at all" in out
    assert "That is what DuckDB is, not a missing dataplat feature" in out
    for wording in ("not supported", "not implemented"):
        assert wording not in out.lower()


def test_role_show_refuses_a_duckdb_target(monkeypatch, tmp_path) -> None:
    from typer.testing import CliRunner

    from dataplat.cli.db import app as db_app

    _duckdb_target(monkeypatch, tmp_path)
    _forbid_connections(monkeypatch)

    result = CliRunner().invoke(db_app, ["role", "show", "alice", "-t", "ddb"])

    _assert_no_roles_refusal(result, "dp db role show")


def test_role_list_refuses_a_duckdb_target(monkeypatch, tmp_path) -> None:
    from typer.testing import CliRunner

    from dataplat.cli.db import app as db_app

    _duckdb_target(monkeypatch, tmp_path)
    _forbid_connections(monkeypatch)

    result = CliRunner().invoke(db_app, ["role", "list", "-t", "ddb"])

    _assert_no_roles_refusal(result, "dp db role list")


def test_role_show_refuses_duckdb_named_by_flag_without_a_target(
    monkeypatch, tmp_path
) -> None:
    """`--engine duckdb` is the other way in, and it must refuse identically.

    A target is not required to reach a DuckDB database: ``-e duckdb -d <path>``
    is enough. The capability question is asked about the *resolved* engine, so
    the flag route cannot slip past the check that the target route hits.
    """
    from typer.testing import CliRunner

    from dataplat.cli.db import app as db_app

    path = _duckdb_target(monkeypatch, tmp_path)
    monkeypatch.delenv("DP_TARGETS", raising=False)
    _forbid_connections(monkeypatch)

    result = CliRunner().invoke(
        db_app, ["role", "show", "alice", "-e", "duckdb", "-d", str(path)]
    )

    _assert_no_roles_refusal(result, "dp db role show")


def test_role_commands_still_serve_a_postgres_target(monkeypatch, tmp_path) -> None:
    """The refusal is the engine's, not a new gate in front of every target.

    Cheap to state and worth stating: a capability check placed one line too
    early is exactly how a working command starts refusing everything.
    """
    import contextlib
    from types import SimpleNamespace

    from dataplat.cli.db import role_list as rl
    from dataplat.services.db.role_admin import RoleSummary

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

    class _Conn:
        def cursor(self):
            return _Cur()

    @contextlib.contextmanager
    def _sess(params):
        yield _Conn()

    monkeypatch.setattr(
        rl,
        "resolve_params_or_exit",
        lambda p: SimpleNamespace(engine=SqlEngine.postgresql),
    )
    monkeypatch.setattr(rl, "db_session", _sess)
    monkeypatch.setattr(
        rl,
        "dialect_for",
        lambda engine: SimpleNamespace(
            list_roles=lambda cur: [RoleSummary("svc", True, False, False, False, 0, 0)]
        ),
    )

    rl.list_command(
        filter_substring=None,
        users_only=False,
        groups_only=False,
        as_json=False,
        target="demo_pg",
        engine=None,
        user="u",
        password=None,
        database="analytics",
        host="h",
        port=5432,
        sslmode=None,
        env_prefix="DEMO_PG",
    )
