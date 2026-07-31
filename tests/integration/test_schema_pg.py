"""Schema listing SQL executed against a live PostgreSQL server.

The unit tests assert the *shape* of these statements — which catalog each engine
reads, which placeholder it binds. Only a server can say the statements parse,
that the columns exist, and that the counts match what the catalog actually
holds. Two of the things pinned here were wrong or unprovable until run for real:
the `ESCAPE '#'` on the system predicate, and whether a LEFT JOIN with no
relations yields NULL or 0.

``psycopg`` is imported inside function bodies, matching the rest of this tier:
it ships in the optional ``db`` extra, and a module-level import would turn its
absence into a collection error instead of a skip.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from dataplat.services.db.role_dialects import ParentKind
from dataplat.services.db.schema_admin import (
    CreateSchemaSpec,
    GranteeSpec,
    SchemaPrivilege,
    build_alter_plan,
    build_create_plan,
    build_drop_plan,
    build_grant_plan,
    translate_like_pattern,
)
from dataplat.services.db.schema_dialects import PostgresSchemaDialect
from tests.integration.conftest import TempRoleFactory

if TYPE_CHECKING:
    from psycopg import Cursor
    from psycopg.rows import TupleRow

pytestmark = pytest.mark.integration


@pytest.fixture
def schemas(pg_cursor: Cursor[TupleRow]) -> str:
    """Three schemas with known contents, rolled back with the test.

    ``dp_list_a`` holds two tables, one view, one sequence — one of each bucket
    the count query distinguishes, so a miscounted relkind shows up as a wrong
    number rather than as nothing.
    """
    pg_cursor.execute("CREATE SCHEMA dp_list_a")
    pg_cursor.execute("CREATE TABLE dp_list_a.t1(i int)")
    pg_cursor.execute("CREATE TABLE dp_list_a.t2(i int)")
    pg_cursor.execute("CREATE VIEW dp_list_a.v1 AS SELECT 1")
    pg_cursor.execute("CREATE SEQUENCE dp_list_a.s1")
    pg_cursor.execute("CREATE SCHEMA dp_list_b")
    pg_cursor.execute("CREATE SCHEMA dpx")
    return "dp_list_a"


def _by_name(rows) -> dict:
    return {r.name: r for r in rows}


def test_counts_match_the_catalog(pg_cursor: Cursor[TupleRow], schemas: str) -> None:
    rows = _by_name(PostgresSchemaDialect().list_schemas(pg_cursor))

    row = rows[schemas]
    assert (row.tables, row.views) == (2, 1)
    # The sequence lands in `other` rather than being silently uncounted, which
    # is what a drop pre-flight depends on.
    assert row.other == 1


def test_an_empty_schema_counts_zero_not_null(
    pg_cursor: Cursor[TupleRow], schemas: str
) -> None:
    """The LEFT JOIN yields NULL for a schema with no relations at all."""
    row = _by_name(PostgresSchemaDialect().list_schemas(pg_cursor))["dp_list_b"]

    assert (row.tables, row.views, row.other) == (0, 0, 0)


def test_the_owner_comes_back_from_pg_roles(
    pg_cursor: Cursor[TupleRow], schemas: str
) -> None:
    row = _by_name(PostgresSchemaDialect().list_schemas(pg_cursor))[schemas]

    pg_cursor.execute("SELECT current_user")
    (expected,) = pg_cursor.fetchone() or ("",)
    assert row.owner == expected


def test_quota_columns_stay_none_on_postgres(
    pg_cursor: Cursor[TupleRow], schemas: str
) -> None:
    """Postgres has no schema quotas, and must not invent zeros for them."""
    row = _by_name(PostgresSchemaDialect().list_schemas(pg_cursor))[schemas]

    assert row.quota_mb is None
    assert row.used_mb is None


def test_system_schemas_are_hidden_and_can_be_asked_for(
    pg_cursor: Cursor[TupleRow], schemas: str
) -> None:
    dialect = PostgresSchemaDialect()

    hidden = {r.name for r in dialect.list_schemas(pg_cursor)}
    shown = {r.name for r in dialect.list_schemas(pg_cursor, include_system=True)}

    assert "pg_catalog" not in hidden
    assert "information_schema" not in hidden
    assert {"pg_catalog", "information_schema"} <= shown
    # pg_toast is the one that motivates a pattern rather than a name list.
    assert "pg_toast" in shown


def test_the_escape_keeps_a_schema_that_merely_starts_with_pg(
    pg_cursor: Cursor[TupleRow], schemas: str
) -> None:
    """`pg#_%` ESCAPE '#' matches `pg_`, not `pg` + any character.

    Without the escape, `_` is a single-character wildcard and `dpx`-style names
    like `pgx` would be hidden as system schemas. PostgreSQL reserves the literal
    `pg_` prefix for itself, so `pgx` is the closest a user schema can get — and
    it must survive.
    """
    pg_cursor.execute("CREATE SCHEMA pgx")

    names = {r.name for r in PostgresSchemaDialect().list_schemas(pg_cursor)}

    assert "pgx" in names


def test_postgres_reserves_the_pg_underscore_prefix(
    pg_cursor: Cursor[TupleRow],
) -> None:
    """Why the predicate above is safe: the server will not let one exist.

    Asserted rather than assumed, because the hiding predicate is only correct
    if no legitimate schema can carry that prefix.
    """
    import psycopg

    pg_cursor.execute("SAVEPOINT before_reserved")
    with pytest.raises(psycopg.errors.ReservedName):
        pg_cursor.execute("CREATE SCHEMA pg_mine")
    pg_cursor.execute("ROLLBACK TO SAVEPOINT before_reserved")


def test_a_like_pattern_filters_server_side(
    pg_cursor: Cursor[TupleRow], schemas: str
) -> None:
    rows = PostgresSchemaDialect().list_schemas(pg_cursor, like="dp#_list#_%")

    # No ESCAPE clause on the user pattern, so `#` is literal and matches nothing.
    assert rows == []

    rows = PostgresSchemaDialect().list_schemas(pg_cursor, like="dp_list_%")
    assert {r.name for r in rows} == {"dp_list_a", "dp_list_b"}


def test_a_glob_pattern_reaches_the_same_rows(
    pg_cursor: Cursor[TupleRow], schemas: str
) -> None:
    """`dp_list_*` is the spelling an operator reaches for first."""
    rows = PostgresSchemaDialect().list_schemas(
        pg_cursor, like=translate_like_pattern("dp_list_*")
    )

    assert {r.name for r in rows} == {"dp_list_a", "dp_list_b"}


def test_a_pattern_matching_nothing_returns_no_rows(
    pg_cursor: Cursor[TupleRow], schemas: str
) -> None:
    assert PostgresSchemaDialect().list_schemas(pg_cursor, like="nope_%") == []


def test_results_are_ordered_by_name(pg_cursor: Cursor[TupleRow], schemas: str) -> None:
    """ORDER BY in SQL, so paging and diffing a listing are stable."""
    names = [r.name for r in PostgresSchemaDialect().list_schemas(pg_cursor)]

    assert names == sorted(names)


def test_schema_exists_agrees_with_the_catalog(
    pg_cursor: Cursor[TupleRow], schemas: str
) -> None:
    dialect = PostgresSchemaDialect()

    assert dialect.schema_exists(pg_cursor, schemas) is True
    assert dialect.schema_exists(pg_cursor, "dp_absent_schema") is False
    # A system schema exists too: the predicate that hides it from a listing is
    # not an existence claim.
    assert dialect.schema_exists(pg_cursor, "pg_catalog") is True


def test_a_matview_counts_as_a_view(pg_cursor: Cursor[TupleRow], schemas: str) -> None:
    """relkind 'm', which the first version of this query missed."""
    pg_cursor.execute("CREATE MATERIALIZED VIEW dp_list_a.mv1 AS SELECT 1 AS one")

    row = _by_name(PostgresSchemaDialect().list_schemas(pg_cursor))[schemas]

    assert row.views == 2


# ---------------------------------------------------------------------------
# create / drop / alter / grant executed against the server
# ---------------------------------------------------------------------------


def _run(cursor, plan) -> None:
    for op in plan.ops:
        cursor.execute(op.statement)


def test_create_and_read_back(pg_cursor: Cursor[TupleRow]) -> None:
    plan = build_create_plan([CreateSchemaSpec("dp_new")], PostgresSchemaDialect())
    _run(pg_cursor, plan)

    assert PostgresSchemaDialect().schema_exists(pg_cursor, "dp_new") is True


def test_create_with_an_owner_sets_the_owner(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory
) -> None:
    """AUTHORIZATION is not decoration: the owner is who can create in it."""
    owner = temp_role("schema_owner", login=False)
    _run(
        pg_cursor,
        build_create_plan(
            [CreateSchemaSpec("dp_owned", owner=owner)], PostgresSchemaDialect()
        ),
    )

    rows = {r.name: r for r in PostgresSchemaDialect().list_schemas(pg_cursor)}
    assert rows["dp_owned"].owner == owner


def test_if_not_exists_is_idempotent(pg_cursor: Cursor[TupleRow]) -> None:
    dialect = PostgresSchemaDialect()
    plan = build_create_plan(
        [CreateSchemaSpec("dp_twice", if_not_exists=True)], dialect
    )
    _run(pg_cursor, plan)
    _run(pg_cursor, plan)  # would raise DuplicateSchema without IF NOT EXISTS

    assert dialect.schema_exists(pg_cursor, "dp_twice") is True


def test_create_without_if_not_exists_refuses_a_duplicate(
    pg_cursor: Cursor[TupleRow],
) -> None:
    import psycopg

    plan = build_create_plan([CreateSchemaSpec("dp_dup")], PostgresSchemaDialect())
    _run(pg_cursor, plan)

    pg_cursor.execute("SAVEPOINT before_dup")
    with pytest.raises(psycopg.errors.DuplicateSchema):
        _run(pg_cursor, plan)
    pg_cursor.execute("ROLLBACK TO SAVEPOINT before_dup")


def test_restrict_refuses_a_non_empty_schema(pg_cursor: Cursor[TupleRow]) -> None:
    """The behaviour the drop pre-flight warns about, confirmed by the server."""
    import psycopg

    pg_cursor.execute("CREATE SCHEMA dp_full")
    pg_cursor.execute("CREATE TABLE dp_full.t(i int)")

    pg_cursor.execute("SAVEPOINT before_restrict")
    with pytest.raises(psycopg.errors.DependentObjectsStillExist):
        _run(pg_cursor, build_drop_plan(["dp_full"]))
    pg_cursor.execute("ROLLBACK TO SAVEPOINT before_restrict")


def test_cascade_destroys_the_contents(pg_cursor: Cursor[TupleRow]) -> None:
    dialect = PostgresSchemaDialect()
    pg_cursor.execute("CREATE SCHEMA dp_cascade")
    pg_cursor.execute("CREATE TABLE dp_cascade.t(i int)")
    pg_cursor.execute("CREATE VIEW dp_cascade.v AS SELECT 1")

    _run(pg_cursor, build_drop_plan(["dp_cascade"], cascade=True))

    assert dialect.schema_exists(pg_cursor, "dp_cascade") is False


def test_if_exists_tolerates_a_missing_schema(pg_cursor: Cursor[TupleRow]) -> None:
    _run(pg_cursor, build_drop_plan(["dp_never_existed"], if_exists=True))


def test_alter_owner_and_rename_take_effect(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory
) -> None:
    dialect = PostgresSchemaDialect()
    new_owner = temp_role("new_owner", login=False)
    pg_cursor.execute("CREATE SCHEMA dp_alter")

    _run(pg_cursor, build_alter_plan(["dp_alter"], dialect, owner=new_owner))
    _run(pg_cursor, build_alter_plan(["dp_alter"], dialect, rename_to="dp_altered"))

    rows = {r.name: r for r in dialect.list_schemas(pg_cursor)}
    assert "dp_alter" not in rows
    assert rows["dp_altered"].owner == new_owner


def test_every_privilege_in_the_vocabulary_executes(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory
) -> None:
    """The assertion fakes cannot make: that all twelve statements parse.

    Each one is built by the dialect and handed straight to PostgreSQL. A keyword
    in the wrong place, a missing ``ON``, an ``ALL`` where the engine wants a
    privilege name — none of that survives here.
    """
    from psycopg import sql

    dialect = PostgresSchemaDialect()
    owner = temp_role("priv_owner", login=False)
    grantee = temp_role("priv_grantee", login=False)
    pg_cursor.execute(
        sql.SQL("CREATE SCHEMA dp_privs AUTHORIZATION {o}").format(
            o=sql.Identifier(owner)
        )
    )

    every = tuple(SchemaPrivilege)
    plan = build_grant_plan(
        ["dp_privs"],
        [GranteeSpec(grantee, ParentKind.role, every)],
        dialect,
        grantors={"dp_privs": owner},
    )
    # Nothing skipped on Postgres: it has sequences, functions and TO ROLE.
    assert plan.warnings == []
    assert len(plan.ops) == len(every)
    _run(pg_cursor, plan)

    revoked = build_grant_plan(
        ["dp_privs"],
        [GranteeSpec(grantee, ParentKind.role, every)],
        dialect,
        grantors={"dp_privs": owner},
        revoke=True,
    )
    _run(pg_cursor, revoked)


def test_held_detection_reads_back_a_grant_it_issued(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory
) -> None:
    """Round trip through aclexplode, which is what makes the skip trustworthy."""
    dialect = PostgresSchemaDialect()
    grantee = temp_role("held_grantee", login=False)
    other = temp_role("held_other", login=False)
    pg_cursor.execute("CREATE SCHEMA dp_held")

    assert dialect.held_schema_privileges(pg_cursor, ["dp_held"], [grantee]) == set()

    _run(
        pg_cursor,
        build_grant_plan(
            ["dp_held"],
            [
                GranteeSpec(
                    grantee,
                    ParentKind.role,
                    (SchemaPrivilege.usage, SchemaPrivilege.create),
                )
            ],
            dialect,
        ),
    )

    held = dialect.held_schema_privileges(pg_cursor, ["dp_held"], [grantee, other])
    assert held == {("dp_held", grantee, "usage"), ("dp_held", grantee, "create")}
    # Only what was asked about, and only for the grantee that holds it.
    assert not any(name == other for _, name, _ in held)


def test_a_second_grant_of_the_same_privileges_has_nothing_to_do(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory
) -> None:
    """Re-running converges, which is what held-detection is for."""
    dialect = PostgresSchemaDialect()
    grantee = temp_role("idem_grantee", login=False)
    pg_cursor.execute("CREATE SCHEMA dp_idem")
    privileges = (SchemaPrivilege.usage, SchemaPrivilege.create)

    first = build_grant_plan(
        ["dp_idem"], [GranteeSpec(grantee, ParentKind.role, privileges)], dialect
    )
    _run(pg_cursor, first)

    second = build_grant_plan(
        ["dp_idem"],
        [GranteeSpec(grantee, ParentKind.role, privileges)],
        dialect,
        held=dialect.held_schema_privileges(pg_cursor, ["dp_idem"], [grantee]),
    )

    assert second.ops == []
    assert len(second.already_held) == 2


def test_granting_to_public_is_accepted_here_unlike_role_membership(
    pg_cursor: Cursor[TupleRow],
) -> None:
    """The asymmetry, asserted from the side where PUBLIC *is* legal.

    ``GRANT <role> TO PUBLIC`` fails with 'role "public" does not exist' — pinned
    in test_roles_pg.py. An object privilege to PUBLIC is ordinary SQL, which is
    why resolve_grantee_kinds refuses it by default and the schema path opts in.
    """
    pg_cursor.execute("CREATE SCHEMA dp_public")

    _run(
        pg_cursor,
        build_grant_plan(
            ["dp_public"],
            [GranteeSpec("PUBLIC", ParentKind.role, (SchemaPrivilege.usage,))],
            PostgresSchemaDialect(),
        ),
    )


def test_default_privileges_apply_to_a_table_created_later(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory
) -> None:
    """The point of ALTER DEFAULT PRIVILEGES, and of insisting on a grantor.

    The grantor is the role that will create the tables. This creates one *as*
    that role and asserts the grantee can select from it — which is the outcome
    the whole feature exists for, and which silently does not happen when the
    grantor clause is omitted.
    """
    from psycopg import sql

    dialect = PostgresSchemaDialect()
    owner = temp_role("dflt_owner", login=False)
    grantee = temp_role("dflt_grantee", login=False)
    pg_cursor.execute(
        sql.SQL("CREATE SCHEMA dp_dflt AUTHORIZATION {o}").format(
            o=sql.Identifier(owner)
        )
    )
    _run(
        pg_cursor,
        build_grant_plan(
            ["dp_dflt"],
            [GranteeSpec(grantee, ParentKind.role, (SchemaPrivilege.default_select,))],
            dialect,
            grantors={"dp_dflt": owner},
        ),
    )

    # Create the table as the grantor, so the default privileges bind to it.
    pg_cursor.execute(sql.SQL("SET ROLE {r}").format(r=sql.Identifier(owner)))
    pg_cursor.execute("CREATE TABLE dp_dflt.later(i int)")
    pg_cursor.execute("RESET ROLE")

    pg_cursor.execute(
        "SELECT has_table_privilege(%s, 'dp_dflt.later', 'SELECT')", (grantee,)
    )
    row = pg_cursor.fetchone()
    assert row is not None and row[0] is True
