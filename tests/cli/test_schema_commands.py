"""``dp db schema create/drop/grant/revoke/alter`` at the CLI seam.

The plan builders are tested in ``tests/services/db/test_schema_plans.py`` and
the SQL runs for real in the integration tiers. What is asserted here is what the
CLI adds: the protected-schema refusals, the two ways of naming grantees, the
per-engine gates, and that a dry run touches nothing.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from dataplat.cli.db import app as db_app
from dataplat.cli.db import schema_alter, schema_create, schema_drop, schema_grant
from dataplat.cli.db._schema_opts import is_protected_schema
from dataplat.core.errors import ExitCode


class _Cursor:
    """Records executed statements; answers the catalog reads the CLI makes."""

    def __init__(
        self,
        schemas: list[tuple] | None = None,
        kinds: dict[str, bool] | None = None,
        held: list[tuple] | None = None,
    ) -> None:
        self._schemas = schemas if schemas is not None else []
        self._kinds = kinds or {}
        self._held = held or []
        self._result: list[tuple] = []
        self.statements: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def execute(self, query, params=None) -> None:
        if hasattr(query, "as_string"):  # a Composed: a statement, not a read
            self.statements.append(query.as_string(None))
            self._result = []
            return
        text = str(query)
        if text.startswith(("SAVEPOINT", "ROLLBACK", "RELEASE")):
            self._result = []
        elif "pg_namespace" in text and "nspacl" not in text:
            self._result = list(self._schemas)
        elif "rolcanlogin" in text:
            name = params[0]
            self._result = [(self._kinds[name],)] if name in self._kinds else []
        elif "aclexplode" in text:
            self._result = list(self._held)
        else:
            self._result = []

    def fetchall(self) -> list[tuple]:
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None


class _Conn:
    """Stands in for a psycopg connection, including the attribute as_string wants."""

    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def cursor(self):
        return self._cursor

    @property
    def connection(self):
        return None  # what psycopg's as_string reads; None renders plainly


def _patch(monkeypatch, module, cursor: _Cursor) -> None:
    @contextlib.contextmanager
    def _session(params):
        yield _Conn(cursor)

    monkeypatch.setattr(module, "db_session", _session)


_CONN = {
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


# ---------------------------------------------------------------------------
# The protected-schema rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "public",
        "PUBLIC",
        "Public",  # casefold, not lower
        "main",  # DuckDB's default schema
        "information_schema",
        "catalog_history",  # Redshift's
        "pg_catalog",
        "pg_toast",
        "PG_CATALOG",
    ],
)
def test_protected_names_are_recognised(name: str) -> None:
    assert is_protected_schema(name) is True


@pytest.mark.parametrize("name", ["analytics", "dev_x", "publicity", "mainly", "pgx"])
def test_ordinary_names_are_not_protected(name: str) -> None:
    """`publicity` and `mainly` start with a protected name; `pgx` looks like pg_."""
    assert is_protected_schema(name) is False


def test_drop_refuses_a_protected_schema(monkeypatch, capsys) -> None:
    cursor = _Cursor()
    _patch(monkeypatch, schema_drop, cursor)

    with pytest.raises(typer.Exit) as exc:
        schema_drop.drop_command(
            names=["public"],
            cascade=False,
            like=None,
            if_exists=False,
            dry_run=False,
            yes=True,
            **_CONN,
        )

    assert exc.value.exit_code == ExitCode.INVALID_INPUT
    assert cursor.statements == []
    assert "protected" in capsys.readouterr().out


def test_like_cannot_reach_a_protected_schema(monkeypatch, capsys) -> None:
    """list_schemas hides pg_* but not public, so --like needs its own check."""
    cursor = _Cursor(
        schemas=[("public", "postgres", 0, 0, 0), ("pubx", "postgres", 0, 0, 0)]
    )
    _patch(monkeypatch, schema_drop, cursor)

    schema_drop.drop_command(
        names=None,
        cascade=False,
        like="pu*",
        if_exists=False,
        dry_run=True,
        yes=True,
        **_CONN,
    )

    out = capsys.readouterr().out
    assert "pubx" in out
    assert '"public"' not in out


def test_alter_refuses_a_protected_schema(monkeypatch, capsys) -> None:
    cursor = _Cursor()
    _patch(monkeypatch, schema_alter, cursor)

    with pytest.raises(typer.Exit) as exc:
        schema_alter.alter_command(
            names=["public"],
            owner=None,
            quota=None,
            rename_to="p2",
            dry_run=False,
            yes=True,
            **_CONN,
        )

    assert exc.value.exit_code == ExitCode.INVALID_INPUT
    assert cursor.statements == []


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_executes_the_plan(monkeypatch, capsys) -> None:
    cursor = _Cursor()
    _patch(monkeypatch, schema_create, cursor)

    schema_create.create_command(
        names=["a,b"],
        owner=None,
        quota=None,
        if_not_exists=False,
        dry_run=False,
        yes=True,
        **_CONN,
    )

    assert cursor.statements == ['CREATE SCHEMA "a"', 'CREATE SCHEMA "b"']
    assert "Created:" in capsys.readouterr().out


def test_create_dry_run_executes_nothing(monkeypatch, capsys) -> None:
    cursor = _Cursor()
    _patch(monkeypatch, schema_create, cursor)

    schema_create.create_command(
        names=["a"],
        owner=None,
        quota=None,
        if_not_exists=False,
        dry_run=True,
        yes=True,
        **_CONN,
    )

    assert cursor.statements == []
    out = capsys.readouterr().out
    assert "Dry-run" in out
    assert 'CREATE SCHEMA "a"' in out  # the SQL is still shown


# ---------------------------------------------------------------------------
# drop
# ---------------------------------------------------------------------------


def test_drop_shows_the_blast_radius_before_confirming(monkeypatch, capsys) -> None:
    cursor = _Cursor(schemas=[("dev_x", "svc", 2, 1, 3)])
    _patch(monkeypatch, schema_drop, cursor)

    schema_drop.drop_command(
        names=["dev_x"],
        cascade=True,
        like=None,
        if_exists=False,
        dry_run=True,
        yes=True,
        **_CONN,
    )

    out = capsys.readouterr().out
    # 2 tables + 1 view + 3 other, counted from the same row `list` renders.
    assert "destroy 6 object(s)" in " ".join(out.split())


def test_drop_warns_that_restrict_will_refuse(monkeypatch, capsys) -> None:
    """Said before the confirmation, not discovered after it."""
    cursor = _Cursor(schemas=[("dev_x", "svc", 1, 0, 0)])
    _patch(monkeypatch, schema_drop, cursor)

    schema_drop.drop_command(
        names=["dev_x"],
        cascade=False,
        like=None,
        if_exists=False,
        dry_run=True,
        yes=True,
        **_CONN,
    )

    assert "RESTRICT will refuse" in " ".join(capsys.readouterr().out.split())


def test_drop_of_a_missing_schema_names_the_flag(monkeypatch, capsys) -> None:
    cursor = _Cursor(schemas=[])
    _patch(monkeypatch, schema_drop, cursor)

    with pytest.raises(typer.Exit) as exc:
        schema_drop.drop_command(
            names=["ghost"],
            cascade=False,
            like=None,
            if_exists=False,
            dry_run=False,
            yes=True,
            **_CONN,
        )

    assert exc.value.exit_code == ExitCode.INVALID_INPUT
    assert "--if-exists" in capsys.readouterr().out


def test_names_and_like_together_are_rejected(monkeypatch) -> None:
    _patch(monkeypatch, schema_drop, _Cursor())

    with pytest.raises(typer.BadParameter):
        schema_drop.drop_command(
            names=["a"],
            cascade=False,
            like="a*",
            if_exists=False,
            dry_run=True,
            yes=True,
            **_CONN,
        )


def test_neither_names_nor_like_is_rejected(monkeypatch) -> None:
    _patch(monkeypatch, schema_drop, _Cursor())

    with pytest.raises(typer.BadParameter):
        schema_drop.drop_command(
            names=None,
            cascade=False,
            like=None,
            if_exists=False,
            dry_run=True,
            yes=True,
            **_CONN,
        )


# ---------------------------------------------------------------------------
# grant / revoke
# ---------------------------------------------------------------------------


def _grant(monkeypatch, cursor: _Cursor, **overrides):
    _patch(monkeypatch, schema_grant, cursor)
    kwargs = {
        "schemas": ["dev_x"],
        "like": None,
        "to": ["readers"],
        "privileges": ["usage"],
        "grant_pairs": None,
        "to_kind": None,
        "default_for": None,
        "dry_run": False,
        "yes": True,
        **_CONN,
    }
    kwargs.update(overrides)
    return schema_grant.grant_command(**kwargs)


def test_grant_executes_the_plan(monkeypatch, capsys) -> None:
    cursor = _Cursor(schemas=[("dev_x", "svc", 0, 0, 0)], kinds={"readers": False})

    _grant(monkeypatch, cursor)

    assert cursor.statements == ['GRANT USAGE ON SCHEMA "dev_x" TO "readers"']


def test_the_grantee_pair_form_gives_each_its_own_privileges(
    monkeypatch, capsys
) -> None:
    cursor = _Cursor(
        schemas=[("dev_x", "svc", 0, 0, 0)],
        kinds={"readers": False, "etl": False},
    )

    _grant(
        monkeypatch,
        cursor,
        to=None,
        privileges=None,
        grant_pairs=["readers:usage", "etl:create"],
    )

    assert cursor.statements == [
        'GRANT USAGE ON SCHEMA "dev_x" TO "readers"',
        'GRANT CREATE ON SCHEMA "dev_x" TO "etl"',
    ]


def test_a_grantee_in_both_forms_accumulates(monkeypatch) -> None:
    """The operator asked for both; dropping either would be a silent surprise."""
    cursor = _Cursor(schemas=[("dev_x", "svc", 0, 0, 0)], kinds={"readers": False})

    _grant(
        monkeypatch,
        cursor,
        to=["readers"],
        privileges=["usage"],
        grant_pairs=["readers:create"],
    )

    assert cursor.statements == [
        'GRANT USAGE ON SCHEMA "dev_x" TO "readers"',
        'GRANT CREATE ON SCHEMA "dev_x" TO "readers"',
    ]


def test_to_without_privileges_is_refused(monkeypatch, capsys) -> None:
    cursor = _Cursor(schemas=[("dev_x", "svc", 0, 0, 0)], kinds={"readers": False})

    with pytest.raises(typer.Exit) as exc:
        _grant(monkeypatch, cursor, privileges=None)

    assert exc.value.exit_code == ExitCode.INVALID_INPUT
    assert "--privileges" in capsys.readouterr().out


def test_the_schema_owner_is_the_default_grantor(monkeypatch) -> None:
    """dbt and migrations run as the owner, so the owner creates the tables."""
    cursor = _Cursor(schemas=[("dev_x", "svc_etl", 0, 0, 0)], kinds={"readers": False})

    _grant(monkeypatch, cursor, privileges=["default-select"])

    assert any('FOR ROLE "svc_etl"' in s for s in cursor.statements)


def test_default_for_overrides_the_owner(monkeypatch) -> None:
    cursor = _Cursor(schemas=[("dev_x", "svc_etl", 0, 0, 0)], kinds={"readers": False})

    _grant(monkeypatch, cursor, privileges=["default-select"], default_for="other")

    assert any('FOR ROLE "other"' in s for s in cursor.statements)


def test_public_is_accepted_as_a_grantee(monkeypatch) -> None:
    """Legal for object privileges — and refused by `role grant`, which is a
    different question. No catalog lookup is made for it."""
    cursor = _Cursor(schemas=[("dev_x", "svc", 0, 0, 0)])

    _grant(monkeypatch, cursor, to=["PUBLIC"])

    assert cursor.statements == ['GRANT USAGE ON SCHEMA "dev_x" TO PUBLIC']


def test_an_absent_grantee_is_refused(monkeypatch, capsys) -> None:
    cursor = _Cursor(schemas=[("dev_x", "svc", 0, 0, 0)], kinds={})

    with pytest.raises(typer.Exit) as exc:
        _grant(monkeypatch, cursor)

    assert exc.value.exit_code == ExitCode.INVALID_INPUT
    assert "not found" in capsys.readouterr().out


def test_a_missing_schema_is_refused(monkeypatch, capsys) -> None:
    cursor = _Cursor(schemas=[], kinds={"readers": False})

    with pytest.raises(typer.Exit) as exc:
        _grant(monkeypatch, cursor)

    assert exc.value.exit_code == ExitCode.INVALID_INPUT
    assert "not found" in capsys.readouterr().out


def test_held_privileges_are_reported_and_skipped(monkeypatch, capsys) -> None:
    cursor = _Cursor(
        schemas=[("dev_x", "svc", 0, 0, 0)],
        kinds={"readers": False},
        held=[("dev_x", "readers", "USAGE")],
    )

    _grant(monkeypatch, cursor, privileges=["usage", "create"])

    assert cursor.statements == ['GRANT CREATE ON SCHEMA "dev_x" TO "readers"']
    assert "Already held (1)" in capsys.readouterr().out


def test_a_fully_held_grant_does_nothing(monkeypatch, capsys) -> None:
    cursor = _Cursor(
        schemas=[("dev_x", "svc", 0, 0, 0)],
        kinds={"readers": False},
        held=[("dev_x", "readers", "USAGE")],
    )

    _grant(monkeypatch, cursor, yes=False)  # would block if it prompted

    assert cursor.statements == []
    assert "Nothing to do." in capsys.readouterr().out


def test_revoke_does_not_consult_held(monkeypatch) -> None:
    """A held grant is why you are revoking, not a reason to skip."""
    cursor = _Cursor(
        schemas=[("dev_x", "svc", 0, 0, 0)],
        kinds={"readers": False},
        held=[("dev_x", "readers", "USAGE")],
    )
    _patch(monkeypatch, schema_grant, cursor)

    schema_grant.revoke_command(
        schemas=["dev_x"],
        like=None,
        from_=["readers"],
        privileges=["usage"],
        grant_pairs=None,
        to_kind=None,
        default_for=None,
        cascade=True,
        dry_run=False,
        yes=True,
        **_CONN,
    )

    assert cursor.statements == [
        'REVOKE USAGE ON SCHEMA "dev_x" FROM "readers" CASCADE'
    ]


# ---------------------------------------------------------------------------
# Per-engine gates
# ---------------------------------------------------------------------------


def _duckdb_target(monkeypatch, tmp_path: Path) -> None:
    import duckdb

    path = tmp_path / "warehouse.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute("CREATE SCHEMA analytics")
    connection.close()
    monkeypatch.setenv("DP_TARGETS", "ddb")
    monkeypatch.setenv("DDB_ENGINE", "duckdb")
    monkeypatch.setenv("DDB_PATH", str(path))
    monkeypatch.delenv("DP_DEFAULT_TARGET", raising=False)


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            [
                "schema",
                "grant",
                "--schemas",
                "analytics",
                "--to",
                "bob",
                "--privileges",
                "read",
            ],
            "no GRANT statement",
        ),
        (
            [
                "schema",
                "revoke",
                "--schemas",
                "analytics",
                "--from",
                "bob",
                "--privileges",
                "read",
            ],
            "no GRANT statement",
        ),
        (["schema", "alter", "analytics", "--rename-to", "a2"], "ALTER SCHEMA"),
        (["schema", "create", "x", "--owner", "bob"], "no users or roles"),
    ],
)
def test_duckdb_refuses_what_it_cannot_do(
    monkeypatch, tmp_path, argv: list[str], expected: str
) -> None:
    """Each refusal quotes the engine's own reason, and exits 2."""
    _duckdb_target(monkeypatch, tmp_path)

    result = CliRunner().invoke(db_app, [*argv, "-t", "ddb", "--yes"])

    out = " ".join(result.output.split())
    assert result.exit_code == ExitCode.INVALID_INPUT, result.output
    assert expected in out
    assert "cannot run against DuckDB" in out
    for wording in ("not supported", "not implemented"):
        assert wording not in out.lower()


@pytest.mark.parametrize(
    "argv",
    [
        ["schema", "list"],  # read-only, so no --yes to give it
        ["schema", "create", "x", "--yes"],
        ["schema", "create", "x", "--yes", "--if-not-exists"],
        ["schema", "drop", "x", "--yes", "--if-exists"],
    ],
)
def test_duckdb_allows_what_it_can_do(monkeypatch, tmp_path, argv: list[str]) -> None:
    """create, drop and list are not gated: DuckDB has schemas and can make them.

    The counterpart to the refusals above — a capability gate that refused too
    much would be just as wrong, and nothing else would catch it.
    """
    _duckdb_target(monkeypatch, tmp_path)

    result = CliRunner().invoke(db_app, [*argv, "-t", "ddb"])

    assert result.exit_code == ExitCode.SUCCESS, result.output
