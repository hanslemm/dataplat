from __future__ import annotations

import pytest

from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.role import (
    DefaultPrivilege,
    EffectivePrivilege,
    MembershipEdge,
    RoleAttributes,
    RoleDescription,
    RoleKind,
    RoleNotFoundError,
    RoleRef,
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


class FakeCursor:
    def __init__(self, results: list[object] | None = None) -> None:
        self._results: list[object] = list(results or [])
        self.queries: list[tuple[str, tuple]] = []

    def execute(self, query, params=None) -> None:
        self.queries.append((str(query), tuple(params or ())))

    def fetchone(self):
        return self._results.pop(0) if self._results else None

    def fetchall(self):
        if not self._results:
            return []
        head = self._results.pop(0)
        assert isinstance(head, list), "fetchall expected a list"
        return head


def test_resolve_role_postgres_login_user() -> None:
    cursor = FakeCursor([(16384, True, False)])
    ref = resolve_role(cursor, SqlEngine.postgresql, "alice")
    assert ref == RoleRef(oid=16384, name="alice", kind=RoleKind.user)


def test_resolve_role_postgres_nologin_group() -> None:
    cursor = FakeCursor([(16385, False, False)])
    ref = resolve_role(cursor, SqlEngine.postgresql, "readers")
    assert ref == RoleRef(oid=16385, name="readers", kind=RoleKind.group)


def test_resolve_role_postgres_missing() -> None:
    cursor = FakeCursor([None])
    with pytest.raises(RoleNotFoundError, match='"nope" not found'):
        resolve_role(cursor, SqlEngine.postgresql, "nope")


def test_resolve_role_redshift_user() -> None:
    cursor = FakeCursor([(100, True, False)])
    ref = resolve_role(cursor, SqlEngine.redshift, "etl")
    assert ref == RoleRef(oid=100, name="etl", kind=RoleKind.user)


def test_resolve_role_redshift_group() -> None:
    cursor = FakeCursor([None, (200, False, False)])
    ref = resolve_role(cursor, SqlEngine.redshift, "analysts")
    assert ref == RoleRef(oid=200, name="analysts", kind=RoleKind.group)


def test_fetch_attributes_postgres() -> None:
    cursor = FakeCursor(
        [
            (True,),  # has_table_privilege('pg_authid', 'SELECT')
            (
                True,
                False,
                True,
                True,
                True,
                False,
                False,
                -1,
                False,
                "2030-01-01 00:00:00+00",
            ),
        ]
    )
    attrs = fetch_attributes(cursor, "alice", SqlEngine.postgresql)
    assert attrs == RoleAttributes(
        can_login=True,
        superuser=False,
        create_db=True,
        create_role=True,
        inherit=True,
        replication=False,
        bypass_rls=False,
        connection_limit=-1,
        password_set=False,
        valid_until="2030-01-01 00:00:00+00",
    )


def test_fetch_attributes_postgres_dispatches_pg_roles() -> None:
    cursor = FakeCursor(
        [(True,), (False, False, False, False, True, False, False, -1, False, None)]
    )
    fetch_attributes(cursor, "svc", SqlEngine.postgresql)
    # queries[0] is the pg_authid privilege probe; the attributes come next.
    query_text, params = cursor.queries[1]
    assert "pg_roles" in query_text
    assert "rolbypassrls" in query_text
    assert params == ("svc",)


def test_fetch_attributes_postgres_reads_pg_authid_when_permitted() -> None:
    """Probe says yes => join pg_authid, the only place the verifier lives."""
    cursor = FakeCursor(
        [(True,), (True, False, False, False, True, False, False, -1, True, None)]
    )
    assert fetch_attributes(cursor, "alice", SqlEngine.postgresql).password_set is True
    probe, attributes = (query for query, _ in cursor.queries)
    assert "has_table_privilege('pg_authid', 'SELECT')" in probe
    assert "pg_authid" in attributes


def test_fetch_attributes_postgres_password_set_is_none_when_pg_authid_is_denied() -> (
    None
):
    """Probe says no => None ("cannot determine"), and pg_authid is not named.

    Naming pg_authid without the privilege raises, and that error aborts the
    whole transaction -- taking the rest of ``describe_role`` with it. The
    statement actually issued therefore has to be free of it.
    """
    cursor = FakeCursor(
        [(False,), (True, False, False, False, True, False, False, -1, None, None)]
    )
    attrs = fetch_attributes(cursor, "svc", SqlEngine.postgresql)
    assert attrs.password_set is None
    assert attrs.can_login is True  # everything pg_roles knows is still read
    _, attributes = (query for query, _ in cursor.queries)
    assert "pg_authid" not in attributes


def test_fetch_attributes_redshift() -> None:
    cursor = FakeCursor([(True, True, False, "infinity")])
    attrs = fetch_attributes(cursor, "etl", SqlEngine.redshift)
    assert attrs == RoleAttributes(
        can_login=True,
        superuser=True,
        create_db=False,
        create_role=False,
        inherit=True,
        replication=False,
        bypass_rls=False,
        connection_limit=-1,
        # None, not False: Redshift masks pg_user.passwd, so this is unknowable
        # from here rather than known to be absent.
        password_set=None,
        valid_until="infinity",
    )


def test_fetch_memberships_out_postgres_recursive() -> None:
    rows = [
        ("readers", True, 1, "dbadmin"),
        ("analysts", True, 2, "readers"),
    ]
    cursor = FakeCursor([rows])
    edges = fetch_memberships_out(cursor, 16384, SqlEngine.postgresql)
    assert edges == [
        MembershipEdge(role="readers", inherit=True, depth=1, via="dbadmin"),
        MembershipEdge(role="analysts", inherit=True, depth=2, via="readers"),
    ]


def test_fetch_memberships_out_postgres_query_shape() -> None:
    cursor = FakeCursor([[]])
    fetch_memberships_out(cursor, 42, SqlEngine.postgresql)
    query_text, params = cursor.queries[0]
    assert "WITH RECURSIVE" in query_text
    assert "pg_auth_members" in query_text
    assert params == (42,)


def test_fetch_memberships_in_postgres_recursive() -> None:
    rows = [
        ("alice", True, 1, "readers"),
        ("bob", True, 1, "readers"),
    ]
    cursor = FakeCursor([rows])
    edges = fetch_memberships_in(cursor, 16385, SqlEngine.postgresql)
    assert edges == [
        MembershipEdge(role="alice", inherit=True, depth=1, via="readers"),
        MembershipEdge(role="bob", inherit=True, depth=1, via="readers"),
    ]


def test_fetch_memberships_redshift_single_level() -> None:
    rows = [("analysts", True, 1, "")]
    cursor = FakeCursor([rows])
    edges = fetch_memberships_out(cursor, 100, SqlEngine.redshift)
    assert edges == [
        MembershipEdge(role="analysts", inherit=True, depth=1, via=""),
    ]
    query_text, _ = cursor.queries[0]
    assert "pg_group" in query_text
    assert "WITH RECURSIVE" not in query_text


def test_build_closure_includes_self_and_inheriting_ancestors() -> None:
    edges = [
        MembershipEdge("readers", True, 1, "alice"),
        MembershipEdge("analysts", True, 2, "readers"),
        MembershipEdge("admins", False, 1, "alice"),  # NOINHERIT
    ]
    closure = build_closure(self_name="alice", ancestors=edges)
    assert closure == {"alice", "readers", "analysts", "public"}


def test_build_closure_no_ancestors() -> None:
    assert build_closure(self_name="svc", ancestors=[]) == {"svc", "public"}


def test_fetch_owned_objects_postgres() -> None:
    cursor = FakeCursor(
        [
            [("analytics",), ("staging",)],
            [("public", "r", 12), ("public", "v", 3), ("analytics", "r", 47)],
        ]
    )
    summary = fetch_owned_objects(cursor, 16384, SqlEngine.postgresql)
    assert summary.schemas == ["analytics", "staging"]
    assert summary.relations_by_schema == {
        "public": {"table": 12, "view": 3},
        "analytics": {"table": 47},
    }
    assert summary.total_relations == 62


def test_fetch_owned_objects_folds_relkinds_sharing_one_label() -> None:
    """Partitioned ('p') and ordinary ('r') tables both label as "table".

    The query groups by relkind, so they arrive as two rows for one schema.
    Assigning instead of accumulating dropped one of them, leaving a breakdown
    that no longer summed to total_relations.
    """
    cursor = FakeCursor(
        [
            [],
            [("analytics", "p", 2), ("analytics", "r", 5), ("analytics", "v", 1)],
        ]
    )
    summary = fetch_owned_objects(cursor, 16384, SqlEngine.postgresql)
    assert summary.relations_by_schema == {"analytics": {"table": 7, "view": 1}}
    assert sum(summary.relations_by_schema["analytics"].values()) == 8
    assert summary.total_relations == 8


def test_fetch_effective_privileges_postgres_groups_by_scope() -> None:
    cursor = FakeCursor(
        [
            # schemas
            [("analytics", "USAGE", "dbadmin", "alice", False)],
            # relations
            [
                ("public", "users", "table", "SELECT", "dbadmin", "readers", False),
                ("public", "orders", "table", "INSERT", "dbadmin", "alice", False),
            ],
            # sequences
            [("public", "orders_id_seq", "USAGE", "dbadmin", "alice", False)],
            # functions
            [("public", "to_cents(numeric)", "EXECUTE", "dbadmin", "public", False)],
        ]
    )
    closure = {"alice", "readers", "public"}
    rows = fetch_effective_privileges(
        cursor, closure=closure, engine=SqlEngine.postgresql
    )
    assert [r.scope for r in rows] == [
        "schema",
        "relation",
        "relation",
        "sequence",
        "function",
    ]
    assert rows[0] == EffectivePrivilege(
        scope="schema",
        qualified_name="analytics",
        kind="schema",
        privilege="USAGE",
        grantor="dbadmin",
        via="alice",
        grantable=False,
    )
    assert rows[3] == EffectivePrivilege(
        scope="sequence",
        qualified_name="public.orders_id_seq",
        kind="sequence",
        privilege="USAGE",
        grantor="dbadmin",
        via="alice",
        grantable=False,
    )
    assert rows[4] == EffectivePrivilege(
        scope="function",
        qualified_name="public.to_cents(numeric)",
        kind="function",
        privilege="EXECUTE",
        grantor="dbadmin",
        via="public",
        grantable=False,
    )


def test_fetch_effective_privileges_postgres_filters_closure() -> None:
    cursor = FakeCursor([[], [], [], []])
    fetch_effective_privileges(
        cursor, closure={"alice", "public"}, engine=SqlEngine.postgresql
    )
    assert len(cursor.queries) == 4
    for query_text, params in cursor.queries:
        assert "aclexplode" in query_text
        assert params == (["alice", "public"],)


class FakeCursorNoRBAC(FakeCursor):
    """FakeCursor variant that raises on the svv_relation_privileges probe."""

    def execute(self, query, params=None) -> None:
        if "svv_relation_privileges" in str(query) and "LIMIT 0" in str(query):
            raise RuntimeError("svv_relation_privileges not available")
        super().execute(query, params)


def test_redshift_rbac_probe_rolls_back_savepoint_on_failure() -> None:
    """A failed RBAC probe must ROLLBACK TO SAVEPOINT to keep the tx usable."""
    from dataplat.services.db.role import _RBAC_SAVEPOINT, _redshift_rbac_available

    cursor = FakeCursorNoRBAC([])
    assert _redshift_rbac_available(cursor) is False
    issued = [q for q, _ in cursor.queries]
    # SAVEPOINT must be issued first, then ROLLBACK TO SAVEPOINT after the
    # (simulated) probe failure. Without this, psycopg leaves the connection
    # in "current transaction is aborted" state.
    # Named via the constant, not spelled out: the savepoint name has been
    # renamed once already (it carried the upstream tool's prefix), and a test
    # that hardcodes it fails for a reason unrelated to what it checks.
    assert any(f"SAVEPOINT {_RBAC_SAVEPOINT}" in q for q in issued)
    assert any(f"ROLLBACK TO SAVEPOINT {_RBAC_SAVEPOINT}" in q for q in issued)
    # The svv probe itself never reaches our FakeCursor.execute because it
    # raises before super().execute is called — that's by design.
    assert not any("svv_relation_privileges" in q for q in issued)


def test_redshift_rbac_probe_releases_savepoint_on_success() -> None:
    from dataplat.services.db.role import (
        _RBAC_SAVEPOINT,
        _redshift_rbac_available,
    )

    cursor = FakeCursor([[]])  # empty probe result — probe "succeeds"
    assert _redshift_rbac_available(cursor) is True
    issued = [q for q, _ in cursor.queries]
    assert any(f"SAVEPOINT {_RBAC_SAVEPOINT}" in q for q in issued)
    assert any(f"RELEASE SAVEPOINT {_RBAC_SAVEPOINT}" in q for q in issued)


def test_fetch_effective_privileges_redshift_rbac_uses_svv() -> None:
    # FakeCursor returns [] on fetchall after the probe (no rows), so
    # _redshift_rbac_available returns True. Queue rows for the SVV queries.
    cursor = FakeCursor(
        [
            [],  # probe fetchall
            # svv_schema_privileges
            [
                ("analytics", "USAGE", "etl", False),
                ("public", "USAGE", "public", False),
            ],
            # svv_relation_privileges
            [
                ("public", "users", "table", "SELECT", "readers", False),
                ("public", "orders", "table", "INSERT", "etl", False),
            ],
            # svv_function_privileges
            [("public", "to_cents(numeric)", "EXECUTE", "public", False)],
        ]
    )
    rows = fetch_effective_privileges(
        cursor, closure={"etl", "readers", "public"}, engine=SqlEngine.redshift
    )
    scopes = {r.scope for r in rows}
    assert scopes == {"schema", "relation", "function"}
    # `via` carries real identity names, not the "self" placeholder.
    vias = {r.via for r in rows}
    assert vias == {"etl", "public", "readers"}
    # SVV queries were dispatched (three of them after the probe).
    svv_queries = [q for q, _ in cursor.queries if "svv_" in q]
    assert any("svv_schema_privileges" in q for q in svv_queries)
    assert any("svv_relation_privileges" in q for q in svv_queries)
    assert any("svv_function_privileges" in q for q in svv_queries)


def test_fetch_effective_privileges_redshift_info_schema_fallback() -> None:
    """When svv_* views are unavailable, relations + functions resolve via
    information_schema (one query each, no probing). Schemas still probe."""
    cursor = FakeCursorNoRBAC(
        [
            # information_schema.table_privileges result
            [
                ("public", "users", "SELECT", "etl", "NO"),
                ("public", "orders", "SELECT", "etl", "NO"),
                ("public", "orders", "INSERT", "etl", "NO"),
            ],
            # information_schema.routine_privileges result
            [],
            # candidate schemas for the schema-probe tail
            [("analytics",), ("public",)],
            # schema probe union result
            [
                ("etl", "analytics", "USAGE", True),
                ("etl", "analytics", "CREATE", False),
                ("etl", "public", "USAGE", True),
                ("etl", "public", "CREATE", False),
            ],
        ]
    )
    rows = fetch_effective_privileges(
        cursor, closure={"etl", "public"}, engine=SqlEngine.redshift
    )
    # information_schema was queried, not the old relations probing UNION ALL.
    queries = [q for q, _ in cursor.queries]
    assert any("information_schema.table_privileges" in q for q in queries)
    assert any("information_schema.routine_privileges" in q for q in queries)
    assert not any("has_table_privilege" in q for q in queries)
    # Relations via info_schema carry real grantee names (lower-cased).
    relation_vias = {r.via for r in rows if r.scope == "relation"}
    assert relation_vias == {"etl"}
    # Schemas still use the probing path and carry "self".
    schema_vias = {r.via for r in rows if r.scope == "schema"}
    assert schema_vias == {"self"}
    by_name = sorted({(r.qualified_name, r.privilege) for r in rows})
    assert by_name == [
        ("analytics", "USAGE"),
        ("public", "USAGE"),
        ("public.orders", "INSERT"),
        ("public.orders", "SELECT"),
        ("public.users", "SELECT"),
    ]


def test_fetch_default_privileges_postgres() -> None:
    rows = [
        ("dbadmin", "public", "r", "SELECT", "readers", False),
        ("dbadmin", "analytics", "S", "USAGE", "analysts", False),
    ]
    cursor = FakeCursor([rows])
    defs = fetch_default_privileges(
        cursor, closure={"readers", "analysts", "public"}, engine=SqlEngine.postgresql
    )
    assert defs == [
        DefaultPrivilege(
            owner="dbadmin",
            schema="public",
            object_type="table",
            privilege="SELECT",
            via="readers",
            grantable=False,
        ),
        DefaultPrivilege(
            owner="dbadmin",
            schema="analytics",
            object_type="sequence",
            privilege="USAGE",
            via="analysts",
            grantable=False,
        ),
    ]


def test_describe_role_postgres_composes_sections() -> None:
    cursor = FakeCursor(
        [
            # resolve_role: pg_roles
            (16384, True, False),
            # fetch_attributes: pg_authid privilege probe, then the attributes
            (True,),
            (True, False, True, True, True, False, False, -1, False, None),
            # memberships out (recursive)
            [("readers", True, 1, "alice")],
            # memberships in
            [],
            # owned schemas, owned relations
            [("scratch",)],
            [("scratch", "r", 2)],
            # effective privileges: schemas, relations, sequences, functions
            [("public", "USAGE", "dbadmin", "alice", False)],
            [],
            [],
            [],
            # default privileges
            [],
        ]
    )
    desc = describe_role(
        cursor, "alice", engine=SqlEngine.postgresql, direct_only=False
    )
    assert isinstance(desc, RoleDescription)
    assert desc.ref.name == "alice"
    assert desc.attributes.can_login is True
    assert len(desc.memberships_out) == 1
    assert desc.memberships_out[0].role == "readers"
    assert desc.owned.schemas == ["scratch"]
    assert desc.owned.total_relations == 2
    assert desc.closure == {"alice", "readers", "public"}
    assert len(desc.effective_privileges) == 1
    assert desc.effective_privileges[0].qualified_name == "public"
    assert desc.default_privileges == []


def test_describe_role_direct_only_excludes_ancestors() -> None:
    cursor = FakeCursor(
        [
            (16384, True, False),
            (True,),  # pg_authid privilege probe
            (True, False, True, True, True, False, False, -1, False, None),
            [("readers", True, 1, "alice")],
            [],
            [],
            [],
            [],  # schemas
            [],  # relations
            [],  # sequences
            [],  # functions
            [],  # defaults
        ]
    )
    desc = describe_role(cursor, "alice", engine=SqlEngine.postgresql, direct_only=True)
    assert desc.closure == {"alice", "public"}


def test_fetch_effective_privileges_redshift_probe_cap_raises() -> None:
    """Cap still fires on pathological schema counts (no RBAC, no info_schema help)."""
    from dataplat.services.db.role import (
        _REDSHIFT_MAX_PROBES,
        RedshiftProbeLimitError,
    )

    # 1 role × 15000 schemas × 2 privs = 30k probes — over the 20k cap.
    schemas = [(f"ns_{i}",) for i in range(15000)]
    cursor = FakeCursorNoRBAC(
        [
            [],  # information_schema.table_privileges — no rows
            [],  # information_schema.routine_privileges — no rows
            schemas,  # candidate schemas (too many to probe safely)
        ]
    )
    with pytest.raises(RedshiftProbeLimitError, match="schema-privilege probes"):
        fetch_effective_privileges(
            cursor, closure={"etl", "public"}, engine=SqlEngine.redshift
        )
    assert _REDSHIFT_MAX_PROBES == 20_000


def test_redshift_user_password_state_is_unknown_not_false() -> None:
    """Redshift masks pg_user.passwd exactly as PostgreSQL masks rolpassword.

    Reporting False claimed "this login has no password" for every Redshift
    user, which is the same falsehood that was fixed on the PostgreSQL path.
    Reporting None withdraws the claim without inventing Redshift SQL that
    cannot be executed from CI.
    """
    cursor = FakeCursor([(True, False, False, None)])

    attrs = fetch_attributes(cursor, "analyst", SqlEngine.redshift)

    assert attrs.password_set is None
    assert attrs.can_login is True


def test_redshift_group_password_state_is_false_not_unknown() -> None:
    """A Redshift group has no password to hold, so False is a real answer."""
    cursor = FakeCursor([])

    attrs = fetch_attributes(cursor, "readers", SqlEngine.redshift)

    assert attrs.password_set is False
    assert attrs.can_login is False


def test_redshift_attributes_run_no_authid_probe() -> None:
    """pg_authid does not exist on Redshift; probing for it would error."""
    cursor = FakeCursor([(True, False, False, None)])

    fetch_attributes(cursor, "analyst", SqlEngine.redshift)

    assert all("pg_authid" not in q for q, _ in cursor.queries), cursor.queries
