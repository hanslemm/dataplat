"""Role service SQL executed against a live PostgreSQL server.

``dataplat/services/db/role*.py`` is the highest-consequence SQL in the repo --
it decides who can read a warehouse and what gets dropped -- and every other
test for it drives a fake cursor, which proves only that ``execute`` was
called. This module hands the same functions a real psycopg cursor and lets the
server judge: statements must parse, columns must exist, and the *result* must
match what the catalog says.

Two things this suite deliberately does NOT do:

* Re-assert row-to-dataclass mapping. ``tests/services/db/test_role.py``
  already covers that with fakes, and repeating it here buys nothing.
* Accept a green result from a service function that is wrong. Where a function
  disagrees with PostgreSQL itself, the test asserts the *correct* answer and
  carries ``xfail(strict=True)``, so the assertion stays honest and the marker
  turns into a failure the moment the bug is fixed.

``psycopg`` is imported inside function bodies, never at module level: it ships
in the optional ``db`` extra, and a module-level import would turn its absence
into a collection error instead of the skip the harness promises.
"""

from __future__ import annotations

import itertools
import os
from typing import TYPE_CHECKING, Any, LiteralString

import pytest

from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.role import (
    EffectivePrivilege,
    RoleNotFoundError,
    build_closure,
    describe_role,
    fetch_attributes,
    fetch_default_privileges,
    fetch_effective_privileges,
    fetch_memberships_in,
    fetch_memberships_out,
    fetch_owned_objects,
    resolve_role,
)
from dataplat.services.db.role_admin import (
    CreatePlan,
    CreateRoleSpec,
    DropPlan,
    build_create_plan,
    build_drop_plan,
    generate_password,
    list_databases,
    list_roles,
    role_exists,
)
from dataplat.services.db.role_dialects import PostgresDialect, SqlOp
from tests.integration.conftest import TempRoleFactory, _slug

if TYPE_CHECKING:
    from collections.abc import Sequence

    from psycopg import Cursor
    from psycopg.rows import TupleRow

pytestmark = pytest.mark.integration

PG = SqlEngine.postgresql


# --- helpers ---------------------------------------------------------------


def _scalar(cursor: Cursor[TupleRow], query: str, *params: Any) -> Any:
    """Run a single-value query and return that value (None when no row)."""
    cursor.execute(query, params or None)
    row = cursor.fetchone()
    return None if row is None else row[0]


def _exec(cursor: Cursor[TupleRow], template: LiteralString, **idents: str) -> None:
    """Execute fixture DDL, composing every identifier through psycopg.

    All of this module's own DDL goes through here so the *tests* are as
    quote-safe as the code under test -- otherwise the hostile-identifier test
    could not create its subjects in the first place.
    """
    from psycopg import sql

    cursor.execute(
        sql.SQL(template).format(
            **{key: sql.Identifier(value) for key, value in idents.items()}
        )
    )


def _run_ops(cursor: Cursor[TupleRow], ops: Sequence[SqlOp]) -> None:
    """Execute each op's composed statement, exactly as the CLI layer does."""
    for op in ops:
        cursor.execute(op.statement)


def _run_create_plan(cursor: Cursor[TupleRow], plan: CreatePlan, database: str) -> None:
    """Run a create plan in the documented order: cluster ops, then per-DB."""
    _run_ops(cursor, plan.cluster_ops)
    _run_ops(cursor, plan.per_database_ops[database])


def _run_drop_plan(cursor: Cursor[TupleRow], plan: DropPlan, database: str) -> None:
    """Run a drop plan in the documented order: pre-cluster, per-DB, cluster."""
    _run_ops(cursor, plan.pre_cluster_ops)
    _run_ops(cursor, plan.per_database_ops[database])
    _run_ops(cursor, plan.cluster_ops)


def _relation_oid(cursor: Cursor[TupleRow], schema: str, relation: str) -> int:
    """OID of a relation, looked up by name rather than cast from text.

    ``has_table_privilege(role, 'schema.table', ...)`` re-parses its text
    argument as a qualified identifier and rejects anything with a quote in it,
    so every privilege probe here takes an OID instead.
    """
    oid = _scalar(
        cursor,
        """
        SELECT c.oid FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relname = %s
        """,
        schema,
        relation,
    )
    assert oid is not None, f"no relation {schema}.{relation}"
    return int(oid)


def _function_oid(cursor: Cursor[TupleRow], schema: str, name: str) -> int:
    oid = _scalar(
        cursor,
        """
        SELECT p.oid FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = %s AND p.proname = %s
        """,
        schema,
        name,
    )
    assert oid is not None, f"no function {schema}.{name}"
    return int(oid)


def _in_schema(
    privileges: Sequence[EffectivePrivilege], schema: str
) -> dict[tuple[str, str, str], EffectivePrivilege]:
    """Index the privileges touching ``schema`` by (scope, qualified name, priv).

    Necessary because ``fetch_effective_privileges`` scans the whole database:
    PUBLIC's grants on ``information_schema`` are always in the result and would
    drown any assertion made on the raw list.
    """
    return {
        (row.scope, row.qualified_name, row.privilege): row
        for row in privileges
        if row.qualified_name == schema or row.qualified_name.startswith(f"{schema}.")
    }


_PLAN_ROLE_COUNTER = itertools.count()


def _plan_role_name(request: pytest.FixtureRequest, suffix: str = "role") -> str:
    """Name a role that a *plan* will create, not ``temp_role``.

    Roles the drop path removes cannot come from ``temp_role``: its teardown
    runs ``DROP OWNED BY <name>``, which errors on a role that is already gone
    and would turn a passing test into a teardown ERROR. Cleanup for these is
    the transaction rollback. The name keeps the harness's shape -- uniqueness
    in front, ``dp_it_`` prefix intact -- so the leak sweep in
    ``test_harness.py`` still catches one if a rollback is ever skipped.
    """
    raw = (
        f"dp_it_p{next(_PLAN_ROLE_COUNTER)}_{os.getpid()}_"
        f"{_slug(f'{request.node.name}_{suffix}')}"
    )
    return raw[:63].rstrip("_")


@pytest.fixture
def database(pg_cursor: Cursor[TupleRow]) -> str:
    """Name of the connected database, for the plan builders' ``databases``."""
    return str(_scalar(pg_cursor, "SELECT current_database()"))


# --- resolve_role ----------------------------------------------------------


def test_resolve_role_finds_a_login_user(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory
) -> None:
    name = temp_role("login", login=True, password="pw-for-resolve")
    ref = resolve_role(pg_cursor, PG, name)
    assert ref.name == name
    assert ref.kind.value == "user"
    # The oid is what every downstream membership/ownership query keys on, so
    # it has to be the real pg_roles oid, not a positional accident.
    assert ref.oid == _scalar(
        pg_cursor, "SELECT oid FROM pg_roles WHERE rolname = %s", name
    )


def test_resolve_role_finds_a_nologin_group_role(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory
) -> None:
    name = temp_role("group")
    ref = resolve_role(pg_cursor, PG, name)
    assert ref.kind.value == "group"
    assert ref.oid > 0


def test_resolve_role_raises_for_a_role_the_cluster_does_not_have(
    pg_cursor: Cursor[TupleRow],
) -> None:
    missing = f"dp_it_absent_{os.getpid()}"
    with pytest.raises(RoleNotFoundError, match=f'"{missing}" not found'):
        resolve_role(pg_cursor, PG, missing)


# --- attributes ------------------------------------------------------------


def test_fetch_attributes_reads_every_postgres_flag(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory
) -> None:
    name = temp_role(
        "flags",
        login=True,
        password="pw-for-flags",
        options=["createdb", "createrole", "replication", "bypassrls", "noinherit"],
    )
    # CONNECTION LIMIT and VALID UNTIL take values, which temp_role's
    # bare-keyword allowlist rightly refuses, so set them here.
    _exec(
        pg_cursor,
        "ALTER ROLE {r} CONNECTION LIMIT 7 VALID UNTIL '2030-01-02 03:04:05+00'",
        r=name,
    )

    attrs = fetch_attributes(pg_cursor, name, PG)
    assert attrs.can_login is True
    assert attrs.superuser is False
    assert attrs.create_db is True
    assert attrs.create_role is True
    assert attrs.inherit is False
    assert attrs.replication is True
    assert attrs.bypass_rls is True
    assert attrs.connection_limit == 7
    assert attrs.valid_until is not None
    assert attrs.valid_until.startswith("2030-01-02")


def test_fetch_attributes_reports_a_superuser(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory
) -> None:
    name = temp_role("super", options=["superuser"])
    assert fetch_attributes(pg_cursor, name, PG).superuser is True


def test_fetch_attributes_of_a_plain_group_role(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory
) -> None:
    attrs = fetch_attributes(pg_cursor, temp_role("plain"), PG)
    assert (attrs.can_login, attrs.superuser, attrs.create_db) == (False, False, False)
    assert (attrs.create_role, attrs.replication, attrs.bypass_rls) == (
        False,
        False,
        False,
    )
    assert attrs.inherit is True  # INHERIT is the CREATE ROLE default
    assert attrs.connection_limit == -1  # -1 is "unlimited"
    assert attrs.valid_until is None


def test_fetch_attributes_raises_for_a_missing_role(
    pg_cursor: Cursor[TupleRow],
) -> None:
    with pytest.raises(RoleNotFoundError):
        fetch_attributes(pg_cursor, f"dp_it_absent_{os.getpid()}", PG)


def test_fetch_attributes_password_set_distinguishes_a_passwordless_role(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory
) -> None:
    with_password = temp_role("haspw", login=True, password="pw-is-set")
    without_password = temp_role("nopw")

    # pg_authid is the only place the truth lives; the connecting user is
    # superuser here, so both the test and the service can read it.
    pg_cursor.execute(
        "SELECT rolname, rolpassword IS NOT NULL FROM pg_authid "
        "WHERE rolname = ANY(%s)",
        ([with_password, without_password],),
    )
    truth: dict[str, bool] = dict(pg_cursor.fetchall())
    assert truth == {with_password: True, without_password: False}

    assert fetch_attributes(pg_cursor, with_password, PG).password_set is True
    assert fetch_attributes(pg_cursor, without_password, PG).password_set is False


def test_fetch_attributes_password_set_is_none_without_pg_authid_access(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory
) -> None:
    """A session that may not read pg_authid gets None, not a fabricated bool.

    ``SET LOCAL ROLE`` to an ordinary role is how the probe is made to answer
    "no" for real -- revoking superuser from the connecting user would break
    every later test on this session. It also proves the promise that matters
    most: reading the attributes of a role must not poison the transaction, and
    a permission error on pg_authid would do exactly that.
    """
    from psycopg.pq import TransactionStatus

    from dataplat.services.db.role import _pg_authid_readable

    subject = temp_role("subject", login=True, password="pw-is-set")
    unprivileged = temp_role("unprivileged")
    assert _pg_authid_readable(pg_cursor) is True

    _exec(pg_cursor, "SET LOCAL ROLE {r}", r=unprivileged)
    try:
        assert _pg_authid_readable(pg_cursor) is False
        attrs = fetch_attributes(pg_cursor, subject, PG)
    finally:
        # Before the assertions below: temp_role's teardown needs superuser
        # back, and a failure here must not cascade into a teardown ERROR.
        pg_cursor.execute("RESET ROLE")

    # The password really is set; the point is that the service refuses to
    # claim either answer when it could not look.
    assert attrs.password_set is None
    # Everything pg_roles can answer is still answered.
    assert attrs.can_login is True
    assert attrs.connection_limit == -1
    assert pg_cursor.connection.info.transaction_status is TransactionStatus.INTRANS, (
        "reading attributes without pg_authid access aborted the transaction"
    )


# --- membership walks and the privilege closure ----------------------------


def test_memberships_out_walks_direct_and_nested_ancestors(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory
) -> None:
    user = temp_role("member", login=True, password="pw")
    middle = temp_role("middle")
    top = temp_role("top")
    _exec(pg_cursor, "GRANT {parent} TO {child}", parent=middle, child=user)
    _exec(pg_cursor, "GRANT {parent} TO {child}", parent=top, child=middle)

    ref = resolve_role(pg_cursor, PG, user)
    edges = {edge.role: edge for edge in fetch_memberships_out(pg_cursor, ref.oid, PG)}
    assert set(edges) == {middle, top}
    assert (edges[middle].depth, edges[middle].inherit) == (1, True)
    assert edges[middle].via == user
    # The nested grant is the whole point of the recursive CTE: `top` is never
    # granted to `user` directly and only a transitive walk can find it.
    assert (edges[top].depth, edges[top].inherit) == (2, True)
    assert edges[top].via == middle


def test_memberships_in_walks_direct_and_nested_descendants(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory
) -> None:
    user = temp_role("leaf", login=True, password="pw")
    middle = temp_role("middle")
    top = temp_role("top")
    _exec(pg_cursor, "GRANT {parent} TO {child}", parent=middle, child=user)
    _exec(pg_cursor, "GRANT {parent} TO {child}", parent=top, child=middle)

    ref = resolve_role(pg_cursor, PG, top)
    edges = {edge.role: edge for edge in fetch_memberships_in(pg_cursor, ref.oid, PG)}
    assert set(edges) == {middle, user}
    assert edges[middle].depth == 1
    assert edges[middle].via == top
    assert edges[user].depth == 2
    assert edges[user].via == middle


def test_memberships_out_marks_a_noinherit_edge_and_the_closure_drops_it(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory
) -> None:
    user = temp_role("member", login=True, password="pw")
    inheriting = temp_role("inheriting")
    isolated = temp_role("isolated")
    _exec(pg_cursor, "GRANT {parent} TO {child}", parent=inheriting, child=user)
    _exec(
        pg_cursor,
        "GRANT {parent} TO {child} WITH INHERIT FALSE",
        parent=isolated,
        child=user,
    )

    ref = resolve_role(pg_cursor, PG, user)
    ancestors = fetch_memberships_out(pg_cursor, ref.oid, PG)
    flags = {edge.role: edge.inherit for edge in ancestors}
    assert flags == {inheriting: True, isolated: False}
    # Cross-check against the catalog column the CTE reads, so a hard-coded
    # `true` in the SQL could not fake this.
    pg_cursor.execute(
        """
        SELECT r.rolname, am.inherit_option
        FROM pg_auth_members am
        JOIN pg_roles r ON r.oid = am.roleid
        WHERE am.member = %s
        """,
        (ref.oid,),
    )
    assert dict(pg_cursor.fetchall()) == flags

    closure = build_closure(self_name=user, ancestors=ancestors)
    assert closure == {user, inheriting, "public"}


def test_memberships_out_ors_inherit_across_paths_to_one_ancestor(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory
) -> None:
    """A diamond: the shared ancestor is reachable twice, once via NOINHERIT.

    The NOINHERIT grant is created FIRST on purpose. The old query kept
    whichever equal-depth row ``DISTINCT ON`` happened to sort first, which is
    pg_auth_members' physical row order, so this ordering used to report
    ``inherit=False`` for an ancestor the role really does inherit.
    """
    user = temp_role("member", login=True, password="pw")
    inheriting = temp_role("inheriting")
    isolated = temp_role("isolated")
    shared = temp_role("shared")
    _exec(pg_cursor, "GRANT {parent} TO {child}", parent=shared, child=inheriting)
    _exec(pg_cursor, "GRANT {parent} TO {child}", parent=shared, child=isolated)
    _exec(
        pg_cursor,
        "GRANT {parent} TO {child} WITH INHERIT FALSE",
        parent=isolated,
        child=user,
    )
    _exec(pg_cursor, "GRANT {parent} TO {child}", parent=inheriting, child=user)

    ref = resolve_role(pg_cursor, PG, user)
    ancestors = fetch_memberships_out(pg_cursor, ref.oid, PG)
    edges = {edge.role: edge for edge in ancestors}
    assert set(edges) == {inheriting, isolated, shared}
    assert edges[shared].depth == 2
    assert edges[shared].inherit is True
    assert build_closure(self_name=user, ancestors=ancestors) == {
        user,
        inheriting,
        shared,
        "public",
    }


def test_closure_agrees_with_postgres_about_a_diamond_grant(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory, sample_schema: str
) -> None:
    """The closure must not under-report privileges the server actually grants.

    ``has_table_privilege`` is the ground truth: whatever it says about the
    role, ``fetch_effective_privileges`` over the closure has to agree.
    """
    user = temp_role("member", login=True, password="pw")
    inheriting = temp_role("inheriting")
    isolated = temp_role("isolated")
    shared = temp_role("shared")
    _exec(
        pg_cursor,
        "GRANT SELECT ON {s}.{t} TO {r}",
        s=sample_schema,
        t="customers",
        r=shared,
    )
    _exec(pg_cursor, "GRANT {parent} TO {child}", parent=shared, child=inheriting)
    _exec(pg_cursor, "GRANT {parent} TO {child}", parent=shared, child=isolated)
    _exec(
        pg_cursor,
        "GRANT {parent} TO {child} WITH INHERIT FALSE",
        parent=isolated,
        child=user,
    )
    _exec(pg_cursor, "GRANT {parent} TO {child}", parent=inheriting, child=user)

    customers = _relation_oid(pg_cursor, sample_schema, "customers")
    assert (
        _scalar(
            pg_cursor, "SELECT has_table_privilege(%s, %s, 'SELECT')", user, customers
        )
        is True
    )

    described = describe_role(pg_cursor, user, engine=PG)
    assert shared in described.closure
    key = ("relation", f"{sample_schema}.customers", "SELECT")
    found = _in_schema(described.effective_privileges, sample_schema)
    assert key in found, "the SELECT PostgreSQL grants was not reported"
    assert found[key].via == shared


# --- effective privileges --------------------------------------------------


def test_effective_privileges_cover_schema_relation_sequence_and_function(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory, sample_schema: str
) -> None:
    role = temp_role("reader")
    _exec(
        pg_cursor, "GRANT USAGE, CREATE ON SCHEMA {s} TO {r}", s=sample_schema, r=role
    )
    _exec(
        pg_cursor,
        "GRANT SELECT ON {s}.{t} TO {r}",
        s=sample_schema,
        t="customers",
        r=role,
    )
    _exec(
        pg_cursor,
        "GRANT SELECT ON {s}.{t} TO {r}",
        s=sample_schema,
        t="active_customers",
        r=role,
    )
    _exec(
        pg_cursor,
        "GRANT SELECT ON {s}.{t} TO {r}",
        s=sample_schema,
        t="customers_per_org",
        r=role,
    )
    _exec(
        pg_cursor,
        "GRANT USAGE ON SEQUENCE {s}.{q} TO {r}",
        s=sample_schema,
        q="invoice_number_seq",
        r=role,
    )
    _exec(
        pg_cursor,
        "GRANT EXECUTE ON FUNCTION {s}.{f}() TO {r}",
        s=sample_schema,
        f="touch_updated_at",
        r=role,
    )

    # The server's own verdict on the grants, so the assertions below are
    # checked against reality rather than against the same ACL query.
    assert _scalar(
        pg_cursor,
        "SELECT has_function_privilege(%s, %s, 'EXECUTE')",
        role,
        _function_oid(pg_cursor, sample_schema, "touch_updated_at"),
    )

    closure = {role, "public"}
    privileges = fetch_effective_privileges(pg_cursor, closure=closure, engine=PG)
    found = _in_schema(privileges, sample_schema)

    schema_usage = found[("schema", sample_schema, "USAGE")]
    assert schema_usage.kind == "schema"
    assert schema_usage.via == role
    assert schema_usage.grantor == _scalar(pg_cursor, "SELECT current_user")
    assert schema_usage.grantable is False
    assert ("schema", sample_schema, "CREATE") in found

    # relkind is translated to a label, and each kind takes a different CASE
    # branch, so all three shapes need a row.
    assert found[("relation", f"{sample_schema}.customers", "SELECT")].kind == "table"
    assert found[("relation", f"{sample_schema}.active_customers", "SELECT")].kind == (
        "view"
    )
    assert found[("relation", f"{sample_schema}.customers_per_org", "SELECT")].kind == (
        "matview"
    )
    # Sequences are a separate query, deliberately excluded from "relation".
    sequence = found[("sequence", f"{sample_schema}.invoice_number_seq", "USAGE")]
    assert sequence.kind == "sequence"
    assert ("relation", f"{sample_schema}.invoice_number_seq", "USAGE") not in found

    # The function name is assembled with pg_get_function_identity_arguments,
    # which renders "()" for a zero-argument trigger function.
    function = found[("function", f"{sample_schema}.touch_updated_at()", "EXECUTE")]
    assert function.kind == "function"
    # Read from the raw list, not the index: the explicit GRANT materializes
    # proacl, so PUBLIC's implicit EXECUTE becomes a second row with the same
    # (scope, name, privilege) key and the index keeps only one of them.
    assert {
        row.via
        for row in privileges
        if row.scope == "function" and row.qualified_name.startswith(sample_schema)
    } == {role, "public"}


def test_effective_privileges_report_a_grant_received_through_a_group(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory, sample_schema: str
) -> None:
    """Nothing is granted to the user; everything arrives via the group."""
    user = temp_role("member", login=True, password="pw")
    group = temp_role("analysts")
    _exec(pg_cursor, "GRANT {parent} TO {child}", parent=group, child=user)
    _exec(pg_cursor, "GRANT USAGE ON SCHEMA {s} TO {r}", s=sample_schema, r=group)
    _exec(
        pg_cursor,
        "GRANT SELECT ON {s}.{t} TO {r}",
        s=sample_schema,
        t="orgs",
        r=group,
    )

    described = describe_role(pg_cursor, user, engine=PG)
    found = _in_schema(described.effective_privileges, sample_schema)
    assert found[("schema", sample_schema, "USAGE")].via == group
    assert found[("relation", f"{sample_schema}.orgs", "SELECT")].via == group
    # No direct grant exists, so a query that only looked at the role itself
    # would return nothing here.
    assert not [row for row in found.values() if row.via == user]


def test_effective_privileges_omit_a_group_reached_through_noinherit(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory, sample_schema: str
) -> None:
    user = temp_role("member", login=True, password="pw")
    group = temp_role("noinherit_group")
    _exec(
        pg_cursor,
        "GRANT {parent} TO {child} WITH INHERIT FALSE",
        parent=group,
        child=user,
    )
    _exec(
        pg_cursor,
        "GRANT SELECT ON {s}.{t} TO {r}",
        s=sample_schema,
        t="orgs",
        r=group,
    )

    orgs = _relation_oid(pg_cursor, sample_schema, "orgs")
    # PostgreSQL agrees: a NOINHERIT member must SET ROLE to use the grant.
    assert (
        _scalar(pg_cursor, "SELECT has_table_privilege(%s, %s, 'SELECT')", user, orgs)
        is False
    )
    described = describe_role(pg_cursor, user, engine=PG)
    assert group not in described.closure
    assert not _in_schema(described.effective_privileges, sample_schema)


def test_effective_privileges_flag_a_grant_made_with_grant_option(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory, sample_schema: str
) -> None:
    role = temp_role("delegator")
    _exec(
        pg_cursor,
        "GRANT SELECT ON {s}.{t} TO {r} WITH GRANT OPTION",
        s=sample_schema,
        t="orgs",
        r=role,
    )
    found = _in_schema(
        fetch_effective_privileges(pg_cursor, closure={role}, engine=PG),
        sample_schema,
    )
    assert found[("relation", f"{sample_schema}.orgs", "SELECT")].grantable is True


# --- default privileges ----------------------------------------------------


def test_default_privileges_read_the_fixtures_public_entry(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    rows = [
        row
        for row in fetch_default_privileges(pg_cursor, closure={"public"}, engine=PG)
        if row.schema == sample_schema
    ]
    assert len(rows) == 1
    entry = rows[0]
    assert entry.object_type == "table"
    assert entry.privilege == "SELECT"
    assert entry.via == "public"
    assert entry.grantable is False
    # defaclrole is whoever ran ALTER DEFAULT PRIVILEGES, i.e. the fixture.
    assert entry.owner == _scalar(pg_cursor, "SELECT current_user")


def test_default_privileges_label_each_object_type(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory, sample_schema: str
) -> None:
    role = temp_role("future_reader")
    _exec(
        pg_cursor,
        "ALTER DEFAULT PRIVILEGES IN SCHEMA {s} GRANT USAGE ON SEQUENCES TO {r}",
        s=sample_schema,
        r=role,
    )
    _exec(
        pg_cursor,
        "ALTER DEFAULT PRIVILEGES IN SCHEMA {s} GRANT EXECUTE ON FUNCTIONS TO {r}",
        s=sample_schema,
        r=role,
    )
    rows = [
        row
        for row in fetch_default_privileges(pg_cursor, closure={role}, engine=PG)
        if row.schema == sample_schema
    ]
    # defaclobjtype is a one-byte "char"; the mapping only works if psycopg
    # hands it back as a str.
    assert {(row.object_type, row.privilege) for row in rows} == {
        ("sequence", "USAGE"),
        ("function", "EXECUTE"),
    }
    assert {row.via for row in rows} == {role}


# --- owned objects ---------------------------------------------------------


def test_owned_objects_lists_schemas_and_relation_kinds(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory, sample_schema: str
) -> None:
    owner = temp_role("owner")
    _exec(pg_cursor, "ALTER SCHEMA {s} OWNER TO {r}", s=sample_schema, r=owner)
    _exec(
        pg_cursor,
        "ALTER TABLE {s}.{t} OWNER TO {r}",
        s=sample_schema,
        t="orgs",
        r=owner,
    )
    _exec(
        pg_cursor,
        "ALTER VIEW {s}.{t} OWNER TO {r}",
        s=sample_schema,
        t="active_customers",
        r=owner,
    )
    _exec(
        pg_cursor,
        "ALTER MATERIALIZED VIEW {s}.{t} OWNER TO {r}",
        s=sample_schema,
        t="customers_per_org",
        r=owner,
    )
    _exec(
        pg_cursor,
        "ALTER SEQUENCE {s}.{q} OWNER TO {r}",
        s=sample_schema,
        q="invoice_number_seq",
        r=owner,
    )

    ref = resolve_role(pg_cursor, PG, owner)
    summary = fetch_owned_objects(pg_cursor, ref.oid, PG)
    assert summary.schemas == [sample_schema]
    assert summary.relations_by_schema[sample_schema] == {
        "table": 1,
        "view": 1,
        "matview": 1,
        # Two: the standalone sequence, plus orgs' identity sequence, which
        # PostgreSQL hands over with its owning table. A count of one would
        # mean the query is missing sequences the role really controls.
        "sequence": 2,
    }
    assert summary.total_relations == 5


def test_owned_objects_counts_partitioned_tables_in_the_table_total(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory, sample_schema: str
) -> None:
    owner = temp_role("owner")
    for relation in ("orgs", "customers", "events"):
        _exec(
            pg_cursor,
            "ALTER TABLE {s}.{t} OWNER TO {r}",
            s=sample_schema,
            t=relation,
            r=owner,
        )
    ref = resolve_role(pg_cursor, PG, owner)
    # 'events' is relkind 'p'; ALTER TABLE ... OWNER TO does not recurse into
    # its partitions, so exactly three relations changed hands.
    assert (
        _scalar(
            pg_cursor,
            "SELECT count(*) FROM pg_class"
            " WHERE relowner = %s AND relkind IN ('r','p')",
            ref.oid,
        )
        == 3
    )

    summary = fetch_owned_objects(pg_cursor, ref.oid, PG)
    breakdown = summary.relations_by_schema[sample_schema]
    # relkind 'r' and 'p' are two GROUP BY rows sharing the "table" label, so
    # both have to land in one tally: orgs + customers + the partitioned events.
    # Accumulating is the whole fix; assigning reported 2 here.
    assert breakdown["table"] == 3
    # Three identity sequences changed hands with their owning tables (as the
    # sibling ownership test spells out), so they belong in the total too.
    assert breakdown == {"table": 3, "sequence": 3}
    # The invariant the CLI depends on: the per-schema breakdown it prints must
    # add up to the total printed underneath it.
    assert sum(breakdown.values()) == summary.total_relations == 6


# --- describe_role ---------------------------------------------------------


def test_describe_role_assembles_every_section_in_one_transaction(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory, sample_schema: str
) -> None:
    from psycopg.pq import TransactionStatus

    user = temp_role("subject", login=True, password="pw")
    group = temp_role("group")
    child = temp_role("child", login=True, password="pw")
    _exec(pg_cursor, "GRANT {parent} TO {child}", parent=group, child=user)
    _exec(pg_cursor, "GRANT {parent} TO {child}", parent=user, child=child)
    _exec(pg_cursor, "GRANT USAGE ON SCHEMA {s} TO {r}", s=sample_schema, r=group)
    _exec(
        pg_cursor, "ALTER TABLE {s}.{t} OWNER TO {r}", s=sample_schema, t="orgs", r=user
    )

    described = describe_role(pg_cursor, user, engine=PG)
    assert described.ref.name == user
    assert described.attributes.can_login is True
    assert [edge.role for edge in described.memberships_out] == [group]
    assert [edge.role for edge in described.memberships_in] == [child]
    # orgs plus the identity sequence that follows its owner.
    assert described.owned.relations_by_schema == {
        sample_schema: {"table": 1, "sequence": 1}
    }
    assert described.closure == {user, group, "public"}
    assert described.direct_only is False
    assert ("schema", sample_schema, "USAGE") in _in_schema(
        described.effective_privileges, sample_schema
    )
    assert any(row.schema == sample_schema for row in described.default_privileges)
    # None, not False: the Redshift RBAC probe must never run on Postgres. If
    # it had, its SAVEPOINT would also show up in the transaction state below.
    assert described.redshift_rbac is None
    assert pg_cursor.connection.info.transaction_status is TransactionStatus.INTRANS, (
        "describe_role left the transaction unusable"
    )


def test_describe_role_direct_only_ignores_inherited_ancestors(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory, sample_schema: str
) -> None:
    user = temp_role("subject", login=True, password="pw")
    group = temp_role("group")
    _exec(pg_cursor, "GRANT {parent} TO {child}", parent=group, child=user)
    _exec(pg_cursor, "GRANT USAGE ON SCHEMA {s} TO {r}", s=sample_schema, r=group)

    described = describe_role(pg_cursor, user, engine=PG, direct_only=True)
    assert described.closure == {user, "public"}
    # The ancestor is still listed; only the privilege closure ignores it.
    assert [edge.role for edge in described.memberships_out] == [group]
    assert not _in_schema(described.effective_privileges, sample_schema)


# --- role_admin catalog helpers -------------------------------------------


def test_list_roles_counts_memberships_and_hides_system_roles(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory
) -> None:
    parent = temp_role("parent")
    middle = temp_role("middle", login=True, password="pw", options=["createdb"])
    leaf = temp_role("leaf", login=True, password="pw")
    _exec(pg_cursor, "GRANT {parent} TO {child}", parent=parent, child=middle)
    _exec(pg_cursor, "GRANT {parent} TO {child}", parent=middle, child=leaf)

    summaries = {row.name: row for row in list_roles(pg_cursor)}
    assert not [name for name in summaries if name.startswith("pg_")]

    assert summaries[middle].can_login is True
    assert summaries[middle].create_db is True
    assert summaries[middle].superuser is False
    # One parent (`parent`) and one member (`leaf`) -- the two correlated
    # subqueries must not be transposed.
    assert summaries[middle].member_of_count == 1
    assert summaries[middle].members_count == 1
    assert (summaries[parent].member_of_count, summaries[parent].members_count) == (
        0,
        1,
    )
    assert (summaries[leaf].member_of_count, summaries[leaf].members_count) == (1, 0)


def test_list_databases_excludes_templates_and_the_postgres_database(
    pg_cursor: Cursor[TupleRow], database: str
) -> None:
    databases = list_databases(pg_cursor)
    assert database in databases
    assert "postgres" not in databases
    assert not [
        name
        for name in databases
        if _scalar(
            pg_cursor, "SELECT datistemplate FROM pg_database WHERE datname = %s", name
        )
    ]


def test_role_exists_matches_the_catalog(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory
) -> None:
    name = temp_role("present")
    assert role_exists(pg_cursor, name) is True
    assert role_exists(pg_cursor, f"dp_it_absent_{os.getpid()}") is False
    # PostgresDialect delegates to the same SQL; prove the seam is wired up.
    assert PostgresDialect().role_exists(pg_cursor, name) is True


# --- create path, end to end ----------------------------------------------


def test_create_plan_for_a_login_role_lands_every_grant(
    pg_cursor: Cursor[TupleRow],
    temp_role: TempRoleFactory,
    sample_schema: str,
    database: str,
    request: pytest.FixtureRequest,
) -> None:
    parent = temp_role("parent")
    existing_user = temp_role("existing", login=True, password="pw")
    name = _plan_role_name(request, "new")
    password = generate_password()

    spec = CreateRoleSpec(
        name=name,
        password=password,
        member_of=(parent,),
        grant_to=(existing_user,),
        schema_usage=(sample_schema,),
        schema_create=(sample_schema,),
        table_select=(sample_schema,),
        sequence_usage=(sample_schema,),
        default_table_select=(sample_schema,),
    )
    plan = build_create_plan(spec, [database], PostgresDialect())
    _run_create_plan(pg_cursor, plan, database)

    # The password must have reached pg_authid as a real verifier, not as the
    # eight-asterisk placeholder pg_roles reports.
    verifier = _scalar(
        pg_cursor, "SELECT rolpassword FROM pg_authid WHERE rolname = %s", name
    )
    assert verifier is not None
    assert verifier.startswith("SCRAM-SHA-256$")
    assert password not in verifier
    assert _scalar(
        pg_cursor, "SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", name
    )

    # Membership in both directions: the new role joined `parent`, and
    # `existing_user` joined the new role.
    pg_cursor.execute(
        """
        SELECT r.rolname, m.rolname
        FROM pg_auth_members am
        JOIN pg_roles r ON r.oid = am.roleid
        JOIN pg_roles m ON m.oid = am.member
        WHERE r.rolname = ANY(%s) OR m.rolname = ANY(%s)
        """,
        ([name, parent], [name, existing_user]),
    )
    assert set(pg_cursor.fetchall()) == {(parent, name), (name, existing_user)}

    assert _scalar(
        pg_cursor, "SELECT has_schema_privilege(%s, %s, 'USAGE')", name, sample_schema
    )
    assert _scalar(
        pg_cursor, "SELECT has_schema_privilege(%s, %s, 'CREATE')", name, sample_schema
    )
    # "ALL TABLES" has to have reached the view and the matview too, not just
    # ordinary tables.
    for relation in ("customers", "active_customers", "customers_per_org", "events"):
        oid = _relation_oid(pg_cursor, sample_schema, relation)
        assert _scalar(
            pg_cursor, "SELECT has_table_privilege(%s, %s, 'SELECT')", name, oid
        ), relation
    sequence = _relation_oid(pg_cursor, sample_schema, "invoice_number_seq")
    assert _scalar(
        pg_cursor, "SELECT has_sequence_privilege(%s, %s, 'USAGE')", name, sequence
    )

    # The ALTER DEFAULT PRIVILEGES op is only proven by a *future* table
    # inheriting the grant.
    _exec(
        pg_cursor, "CREATE TABLE {s}.{t} (id int)", s=sample_schema, t="created_later"
    )
    later = _relation_oid(pg_cursor, sample_schema, "created_later")
    assert _scalar(
        pg_cursor, "SELECT has_table_privilege(%s, %s, 'SELECT')", name, later
    )
    assert not _scalar(
        pg_cursor, "SELECT has_table_privilege(%s, %s, 'INSERT')", name, later
    )


def test_create_plan_for_a_nologin_group_makes_a_passwordless_role(
    pg_cursor: Cursor[TupleRow],
    sample_schema: str,
    database: str,
    request: pytest.FixtureRequest,
) -> None:
    name = _plan_role_name(request, "group")
    spec = CreateRoleSpec(name=name, password=None, table_all=(sample_schema,))
    plan = build_create_plan(spec, [database], PostgresDialect())
    _run_create_plan(pg_cursor, plan, database)

    pg_cursor.execute(
        "SELECT rolcanlogin, rolpassword IS NOT NULL FROM pg_authid WHERE rolname = %s",
        (name,),
    )
    assert pg_cursor.fetchone() == (False, False)
    assert resolve_role(pg_cursor, PG, name).kind.value == "group"
    # table_all implies schema USAGE, which is what makes the grant usable.
    assert _scalar(
        pg_cursor, "SELECT has_schema_privilege(%s, %s, 'USAGE')", name, sample_schema
    )
    customers = _relation_oid(pg_cursor, sample_schema, "customers")
    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
        assert _scalar(
            pg_cursor,
            "SELECT has_table_privilege(%s, %s, %s)",
            name,
            customers,
            privilege,
        ), privilege


def test_create_plan_treats_the_password_as_a_literal_not_a_placeholder(
    pg_cursor: Cursor[TupleRow], database: str, request: pytest.FixtureRequest
) -> None:
    """A password full of SQL punctuation must survive sql.Literal intact.

    ``%s`` in particular: psycopg only interpolates when parameters are passed,
    and these statements carry none, so a regression to %-formatting or to
    naive quoting would show up here as a syntax error or a wrong password.
    """
    name = _plan_role_name(request, "punct")
    password = "a'b\"c%s%%d\\e;--f"
    # SCRAM salts randomly, so its verifier cannot be re-derived and compared.
    # md5 hashes password||rolname with no salt, which makes the stored
    # verifier an exact, reproducible function of the bytes the server
    # received. SET LOCAL keeps the choice inside this transaction.
    pg_cursor.execute("SET LOCAL password_encryption = 'md5'")

    plan = build_create_plan(
        CreateRoleSpec(name=name, password=password), [database], PostgresDialect()
    )
    _run_create_plan(pg_cursor, plan, database)

    stored = _scalar(
        pg_cursor, "SELECT rolpassword FROM pg_authid WHERE rolname = %s", name
    )
    expected = _scalar(pg_cursor, "SELECT 'md5' || md5(%s || %s)", password, name)
    assert stored == expected, "the password the server stored is not the one given"


# --- drop path, end to end ------------------------------------------------


def test_drop_plan_reassigns_ownership_then_removes_the_role(
    pg_cursor: Cursor[TupleRow],
    temp_role: TempRoleFactory,
    sample_schema: str,
    database: str,
    request: pytest.FixtureRequest,
) -> None:
    doomed = _plan_role_name(request, "doomed")
    successor = temp_role("successor")
    executor = str(_scalar(pg_cursor, "SELECT current_user"))

    _run_create_plan(
        pg_cursor,
        build_create_plan(
            CreateRoleSpec(name=doomed, password=generate_password()),
            [database],
            PostgresDialect(),
        ),
        database,
    )
    # Give the role something to lose: a schema, a table, and a privilege that
    # DROP ROLE would otherwise refuse to leave behind.
    _exec(pg_cursor, "ALTER SCHEMA {s} OWNER TO {r}", s=sample_schema, r=doomed)
    _exec(
        pg_cursor,
        "ALTER TABLE {s}.{t} OWNER TO {r}",
        s=sample_schema,
        t="orgs",
        r=doomed,
    )
    _exec(
        pg_cursor,
        "GRANT SELECT ON {s}.{t} TO {r}",
        s=sample_schema,
        t="customers",
        r=doomed,
    )
    customers = _relation_oid(pg_cursor, sample_schema, "customers")

    plan = build_drop_plan(
        doomed,
        [database],
        PostgresDialect(),
        reassign_to=successor,
        grant_membership_to=executor,
    )
    _run_drop_plan(pg_cursor, plan, database)

    # Ownership actually moved, per the catalog rather than per the plan text.
    assert (
        _scalar(
            pg_cursor,
            "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = %s",
            sample_schema,
        )
        == successor
    )
    assert (
        _scalar(
            pg_cursor,
            "SELECT pg_get_userbyid(relowner) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = 'orgs'",
            sample_schema,
        )
        == successor
    )
    # The reassigned table is still there -- REASSIGN, not DROP.
    assert _scalar(pg_cursor, "SELECT count(*) FROM pg_class WHERE oid = %s", customers)

    assert role_exists(pg_cursor, doomed) is False
    with pytest.raises(RoleNotFoundError):
        resolve_role(pg_cursor, PG, doomed)
    # DROP OWNED BY had to revoke the leftover SELECT; otherwise DROP ROLE
    # fails with "role cannot be dropped because some objects depend on it".
    acl = _scalar(
        pg_cursor, "SELECT relacl::text FROM pg_class WHERE oid = %s", customers
    )
    assert doomed not in (acl or "")
    assert (
        _scalar(
            pg_cursor,
            "SELECT count(*) FROM pg_auth_members am"
            " JOIN pg_roles r ON r.oid = am.member WHERE r.rolname = %s",
            executor,
        )
        == 0
    ), "the membership granted for REASSIGN/DROP OWNED outlived the role"


def test_drop_plan_with_no_reassign_drops_the_owned_objects(
    pg_cursor: Cursor[TupleRow],
    sample_schema: str,
    database: str,
    request: pytest.FixtureRequest,
) -> None:
    doomed = _plan_role_name(request, "doomed")
    _run_create_plan(
        pg_cursor,
        build_create_plan(
            CreateRoleSpec(name=doomed, password=None), [database], PostgresDialect()
        ),
        database,
    )
    _exec(pg_cursor, "CREATE TABLE {s}.{t} (id int)", s=sample_schema, t="disposable")
    _exec(
        pg_cursor,
        "ALTER TABLE {s}.{t} OWNER TO {r}",
        s=sample_schema,
        t="disposable",
        r=doomed,
    )

    plan = build_drop_plan(
        doomed,
        [database],
        PostgresDialect(),
        no_reassign=True,
        grant_membership_to=str(_scalar(pg_cursor, "SELECT current_user")),
    )
    assert [op.description for op in plan.per_database_ops[database]] == [
        f"DROP OWNED BY {doomed}"
    ]
    _run_drop_plan(pg_cursor, plan, database)

    assert role_exists(pg_cursor, doomed) is False
    assert (
        _scalar(
            pg_cursor,
            "SELECT count(*) FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = %s AND c.relname = 'disposable'",
            sample_schema,
        )
        == 0
    ), "DROP OWNED BY should have taken the role's table with it"


# --- hostile identifiers ---------------------------------------------------

# A double quote terminates a quoted identifier, a semicolon ends a statement,
# and `--` comments out whatever escaping was supposed to follow. Every name
# below also spells out a destructive statement, so a composition bug does not
# merely produce a syntax error -- it drops the canary.
_CANARY = "dp_it_canary"


def _hostile(kind: str, payload: str) -> str:
    name = f'dp_it_h{os.getpid()}_{kind}";{payload};--'
    assert len(name.encode()) <= 63, "PostgreSQL truncates identifiers at 63 bytes"
    return name


def test_hostile_role_and_schema_names_survive_create_and_drop(
    pg_cursor: Cursor[TupleRow],
    temp_role: TempRoleFactory,
    database: str,
) -> None:
    schema = _hostile("s", f"DROP TABLE {_CANARY}")
    role = _hostile("r", "DROP ROLE postgres")
    table = 'ta"ble;x'
    successor = temp_role("successor")

    _exec(pg_cursor, "CREATE TABLE {t} (id int)", t=_CANARY)
    _exec(pg_cursor, "CREATE SCHEMA {s}", s=schema)
    _exec(pg_cursor, "CREATE TABLE {s}.{t} (id int)", s=schema, t=table)

    spec = CreateRoleSpec(
        name=role,
        password="p'a\"s;s--word",
        schema_usage=(schema,),
        schema_create=(schema,),
        table_select=(schema,),
        default_table_select=(schema,),
    )
    _run_create_plan(
        pg_cursor, build_create_plan(spec, [database], PostgresDialect()), database
    )

    # The role exists under the hostile name, byte for byte.
    assert role_exists(pg_cursor, role) is True
    assert resolve_role(pg_cursor, PG, role).name == role
    assert _scalar(
        pg_cursor, "SELECT has_schema_privilege(%s, %s, 'USAGE')", role, schema
    )
    assert _scalar(
        pg_cursor,
        "SELECT has_table_privilege(%s, %s, 'SELECT')",
        role,
        _relation_oid(pg_cursor, schema, table),
    )

    # Reading it back through the service round-trips the quotes untouched.
    described = describe_role(pg_cursor, role, engine=PG)
    found = _in_schema(described.effective_privileges, schema)
    assert found[("schema", schema, "USAGE")].via == role
    assert found[("relation", f"{schema}.{table}", "SELECT")].via == role
    assert [
        row.via for row in described.default_privileges if row.schema == schema
    ] == [role]

    _exec(pg_cursor, "ALTER TABLE {s}.{t} OWNER TO {r}", s=schema, t=table, r=role)
    _run_drop_plan(
        pg_cursor,
        build_drop_plan(
            role,
            [database],
            PostgresDialect(),
            reassign_to=successor,
            grant_membership_to=str(_scalar(pg_cursor, "SELECT current_user")),
        ),
        database,
    )
    assert role_exists(pg_cursor, role) is False
    assert (
        _scalar(
            pg_cursor,
            "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = %s",
            _relation_oid(pg_cursor, schema, table),
        )
        == successor
    )


def test_hostile_names_are_never_executed_as_sql(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory, database: str
) -> None:
    schema = _hostile("s", f"DROP TABLE {_CANARY}")
    role = _hostile("r", "DROP ROLE postgres")
    successor = temp_role("successor")

    _exec(pg_cursor, "CREATE TABLE {t} (id int)", t=_CANARY)
    _exec(pg_cursor, "INSERT INTO {t} VALUES (1)", t=_CANARY)
    _exec(pg_cursor, "CREATE SCHEMA {s}", s=schema)

    spec = CreateRoleSpec(name=role, password=None, schema_usage=(schema,))
    _run_create_plan(
        pg_cursor, build_create_plan(spec, [database], PostgresDialect()), database
    )
    _run_drop_plan(
        pg_cursor,
        build_drop_plan(
            role,
            [database],
            PostgresDialect(),
            reassign_to=successor,
            grant_membership_to=str(_scalar(pg_cursor, "SELECT current_user")),
        ),
        database,
    )

    # The payloads: the canary table and its row are intact, and the superuser
    # the role name told the server to drop is still there.
    assert _scalar(pg_cursor, f"SELECT count(*) FROM {_CANARY}") == 1
    assert _scalar(
        pg_cursor, "SELECT count(*) FROM pg_roles WHERE rolname = 'postgres'"
    )

    # Exactly the objects asked for, and nothing named after a fragment of the
    # payload -- an injected CREATE would have left a second object behind.
    assert (
        _scalar(
            pg_cursor, "SELECT count(*) FROM pg_namespace WHERE nspname = %s", schema
        )
        == 1
    )
    assert (
        _scalar(
            pg_cursor,
            r"SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'dp\_it\_h%'",
        )
        == 1
    )
    assert (
        _scalar(
            pg_cursor,
            r"SELECT count(*) FROM pg_roles WHERE rolname LIKE 'dp\_it\_h%'",
        )
        == 0
    ), "the hostile role should be gone and nothing else created in its name"
