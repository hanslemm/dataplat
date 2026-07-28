from __future__ import annotations

from typing import Any

import duckdb
import pytest

from dataplat.services.db.capabilities import Capability, capabilities_for
from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.describe import (
    ColumnInfo,
    DependencyEdge,
    ForeignKeyInfo,
    IndexInfo,
    NotApplicable,
    ObjectKind,
    PartitioningInfo,
    PolicyInfo,
    PrimaryKeyInfo,
    PrivilegeGrant,
    RedshiftDistribution,
    RedshiftTableStats,
    RelationHeader,
    SchemaContentItem,
    SchemaHeader,
    TargetNotFoundError,
    TargetRef,
    TriggerInfo,
    ViewDefinition,
    ViewDescription,
    describe_schema,
    describe_table,
    describe_view,
    fetch_columns,
    fetch_constraints,
    fetch_dependencies,
    fetch_indexes,
    fetch_partitioning,
    fetch_policies,
    fetch_redshift_distribution,
    fetch_redshift_table_stats,
    fetch_relation_header,
    fetch_relation_privileges,
    fetch_schema_contents,
    fetch_schema_default_privileges,
    fetch_schema_header,
    fetch_schema_privileges,
    fetch_triggers,
    fetch_view_definition,
    parse_target,
    resolve_target,
    schema_not_applicable,
    table_not_applicable,
    view_not_applicable,
)


class FakeCursor:
    """Cursor stub — queues rows for fetchone/fetchall, records queries."""

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


def test_parse_target_schema_only() -> None:
    assert parse_target("public") == ("public", None)


def test_parse_target_dotted() -> None:
    assert parse_target("public.users") == ("public", "users")


@pytest.mark.parametrize("bad", ["", "   ", "a.b.c", ".foo", "foo."])
def test_parse_target_rejects_malformed(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_target(bad)


def test_resolve_target_schema_found() -> None:
    cursor = FakeCursor([(1,)])
    ref = resolve_target(cursor, SqlEngine.postgresql, "public")
    assert ref == TargetRef(
        kind=ObjectKind.schema, schema="public", name=None, oid=None
    )


def test_resolve_target_schema_missing() -> None:
    cursor = FakeCursor([None])
    with pytest.raises(TargetNotFoundError, match='schema "nope"'):
        resolve_target(cursor, SqlEngine.postgresql, "nope")


@pytest.mark.parametrize(
    "relkind,expected_kind",
    [
        ("r", ObjectKind.table),
        ("p", ObjectKind.table),
        ("v", ObjectKind.view),
        ("m", ObjectKind.matview),
    ],
)
def test_resolve_target_relation_kinds(relkind: str, expected_kind: ObjectKind) -> None:
    cursor = FakeCursor([(42, relkind)])
    ref = resolve_target(cursor, SqlEngine.postgresql, "public.users")
    assert ref == TargetRef(kind=expected_kind, schema="public", name="users", oid=42)


def test_resolve_target_relation_missing() -> None:
    cursor = FakeCursor([None])
    with pytest.raises(TargetNotFoundError, match='"public.users" not found'):
        resolve_target(cursor, SqlEngine.postgresql, "public.users")


def test_resolve_target_unsupported_relkind() -> None:
    cursor = FakeCursor([(99, "i")])  # index
    with pytest.raises(TargetNotFoundError, match="unsupported kind"):
        resolve_target(cursor, SqlEngine.postgresql, "public.some_index")


def test_fetch_columns_postgres() -> None:
    rows = [
        (1, "id", "bigint", False, "nextval('users_id_seq')", True, None, None, None),
        (2, "email", "text", False, None, False, None, None, "User email"),
        (3, "org_id", "bigint", True, None, False, "public.orgs", "id", None),
    ]
    cursor = FakeCursor([rows])
    cols = fetch_columns(cursor, 42, SqlEngine.postgresql)
    assert cols == [
        ColumnInfo(
            1, "id", "bigint", False, "nextval('users_id_seq')", True, None, None, None
        ),
        ColumnInfo(2, "email", "text", False, None, False, None, None, "User email"),
        ColumnInfo(3, "org_id", "bigint", True, None, False, "public.orgs", "id", None),
    ]


def test_fetch_columns_redshift_includes_encoding() -> None:
    rows = [
        (1, "id", "bigint", False, None, True, None, None, None, "az64"),
    ]
    cursor = FakeCursor([rows])
    cols = fetch_columns(cursor, 42, SqlEngine.redshift)
    assert cols[0].encoding == "az64"


def test_fetch_columns_postgres_dispatches_postgres_sql() -> None:
    cursor = FakeCursor([[]])
    fetch_columns(cursor, 42, SqlEngine.postgresql)
    assert len(cursor.queries) == 1
    query_text, params = cursor.queries[0]
    assert "pg_attribute_encoding" not in query_text
    assert "LATERAL" in query_text
    assert "attidentity" in query_text
    assert params == (42,)


def test_fetch_columns_postgres_identity_default() -> None:
    # The SQL pre-computes the effective default, so the fetcher just returns
    # what the server produced. Simulate the two identity variants + a plain
    # nextval() expression and confirm they round-trip.
    rows = [
        (
            1,
            "id",
            "bigint",
            False,
            "GENERATED ALWAYS AS IDENTITY",
            True,
            None,
            None,
            None,
        ),
        (
            2,
            "org_id",
            "bigint",
            False,
            "GENERATED BY DEFAULT AS IDENTITY",
            False,
            None,
            None,
            None,
        ),
        (
            3,
            "legacy_id",
            "bigint",
            False,
            "nextval('legacy_seq'::regclass)",
            False,
            None,
            None,
            None,
        ),
    ]
    cursor = FakeCursor([rows])
    cols = fetch_columns(cursor, 42, SqlEngine.postgresql)
    assert cols[0].default == "GENERATED ALWAYS AS IDENTITY"
    assert cols[1].default == "GENERATED BY DEFAULT AS IDENTITY"
    assert cols[2].default == "nextval('legacy_seq'::regclass)"


def test_fetch_columns_redshift_dispatches_redshift_sql() -> None:
    cursor = FakeCursor([[]])
    fetch_columns(cursor, 42, SqlEngine.redshift)
    assert len(cursor.queries) == 1
    query_text, params = cursor.queries[0]
    assert "pg_attribute_encoding" in query_text
    assert "LATERAL" not in query_text
    assert params == (42,)


def test_fetch_constraints_parses_pk_fk_unique_check() -> None:
    rows = [
        ("users_pkey", "p", "PRIMARY KEY (id)", ["id"], None, None, None, None, None),
        (
            "users_org_fk",
            "f",
            "FOREIGN KEY (org_id) REFERENCES orgs(id) ON DELETE CASCADE",
            ["org_id"],
            "public.orgs",
            ["id"],
            "c",  # confdeltype: CASCADE
            "a",  # confupdtype: NO ACTION
            False,
        ),
        (
            "users_email_uq",
            "u",
            "UNIQUE (email)",
            ["email"],
            None,
            None,
            None,
            None,
            None,
        ),
        ("users_age_chk", "c", "CHECK ((age >= 0))", [], None, None, None, None, None),
    ]
    cursor = FakeCursor([rows])
    bundle = fetch_constraints(cursor, 42, SqlEngine.postgresql)
    assert bundle.primary_key == PrimaryKeyInfo(name="users_pkey", columns=["id"])
    assert bundle.foreign_keys == [
        ForeignKeyInfo(
            name="users_org_fk",
            columns=["org_id"],
            referenced_table="public.orgs",
            referenced_columns=["id"],
            on_update="NO ACTION",
            on_delete="CASCADE",
            deferrable=False,
        )
    ]
    assert [c.name for c in bundle.unique_constraints] == ["users_email_uq"]
    assert [c.name for c in bundle.check_constraints] == ["users_age_chk"]


def test_fetch_constraints_no_pk() -> None:
    rows = [
        (
            "users_email_uq",
            "u",
            "UNIQUE (email)",
            ["email"],
            None,
            None,
            None,
            None,
            None,
        ),
    ]
    cursor = FakeCursor([rows])
    bundle = fetch_constraints(cursor, 42, SqlEngine.postgresql)
    assert bundle.primary_key is None
    assert bundle.unique_constraints[0].name == "users_email_uq"


def test_fetch_constraints_redshift_returns_empty_bundle() -> None:
    cursor = FakeCursor()
    bundle = fetch_constraints(cursor, 42, SqlEngine.redshift)
    assert bundle.primary_key is None
    assert bundle.foreign_keys == []
    assert bundle.unique_constraints == []
    assert bundle.check_constraints == []
    assert cursor.queries == []  # no SQL sent on Redshift


def test_fetch_indexes() -> None:
    rows = [
        ("users_pkey", ["id"], True, True, "btree", 16384, None),
        ("users_email_idx", ["email"], True, False, "btree", 8192, None),
        (
            "users_active_idx",
            ["email"],
            False,
            False,
            "btree",
            4096,
            "(active IS TRUE)",
        ),
    ]
    cursor = FakeCursor([rows])
    idx = fetch_indexes(cursor, 42, SqlEngine.postgresql)
    assert idx[0] == IndexInfo("users_pkey", ["id"], True, True, "btree", 16384, None)
    assert idx[2].predicate == "(active IS TRUE)"


def test_fetch_indexes_dispatches_postgres_sql() -> None:
    cursor = FakeCursor([[]])
    fetch_indexes(cursor, 42, SqlEngine.postgresql)
    assert len(cursor.queries) == 1
    query_text, params = cursor.queries[0]
    assert "pg_get_indexdef" in query_text
    assert params == (42,)


def test_fetch_indexes_dispatches_redshift_sql() -> None:
    cursor = FakeCursor([[]])
    fetch_indexes(cursor, 42, SqlEngine.redshift)
    assert len(cursor.queries) == 1
    query_text, params = cursor.queries[0]
    assert "pg_get_indexdef" not in query_text
    assert params == (42,)


def test_fetch_relation_header() -> None:
    cursor = FakeCursor(
        [
            (
                "public",  # schema
                "users",  # name
                "dbadmin",  # owner
                "pg_default",  # tablespace
                "App users",  # comment
                12345,  # row estimate
                1048576,  # total size
                524288,  # table size
                262144,  # index size
                262144,  # toast size
            ),
        ]
    )
    hdr = fetch_relation_header(cursor, 42, SqlEngine.postgresql)
    assert hdr == RelationHeader(
        schema="public",
        name="users",
        owner="dbadmin",
        tablespace="pg_default",
        comment="App users",
        row_estimate=12345,
        total_size=1048576,
        table_size=524288,
        index_size=262144,
        toast_size=262144,
    )


def test_fetch_relation_privileges() -> None:
    rows = [
        ("dbadmin", "OWNER", False, ""),
        ("analyst", "SELECT", False, "dbadmin"),
        ("app", "SELECT", True, "dbadmin"),
        ("app", "INSERT", True, "dbadmin"),
    ]
    cursor = FakeCursor([rows])
    grants = fetch_relation_privileges(cursor, "public", "users")
    assert PrivilegeGrant("analyst", "SELECT", False, "dbadmin") in grants
    assert PrivilegeGrant("app", "INSERT", True, "dbadmin") in grants


def test_fetch_triggers_returns_empty_on_redshift() -> None:
    cursor = FakeCursor()
    assert fetch_triggers(cursor, 42, SqlEngine.redshift) == []
    assert cursor.queries == []


def test_fetch_triggers_postgres() -> None:
    rows = [("trg_audit", "AFTER", "INSERT OR UPDATE", "audit_row()")]
    cursor = FakeCursor([rows])
    assert fetch_triggers(cursor, 42, SqlEngine.postgresql) == [
        TriggerInfo("trg_audit", "AFTER", "INSERT OR UPDATE", "audit_row()"),
    ]


def test_fetch_policies_returns_empty_on_redshift() -> None:
    cursor = FakeCursor()
    policies, enabled = fetch_policies(cursor, 42, SqlEngine.redshift)
    assert policies == []
    assert enabled is False
    assert cursor.queries == []


def test_fetch_policies_postgres() -> None:
    cursor = FakeCursor(
        [
            (True,),  # relrowsecurity
            [
                (
                    "tenant_isolation",
                    "ALL",
                    ["app"],
                    "(tenant_id = current_setting('app.tenant')::int)",
                    None,
                )
            ],
        ]
    )
    policies, enabled = fetch_policies(cursor, 42, SqlEngine.postgresql)
    assert enabled is True
    assert policies == [
        PolicyInfo(
            "tenant_isolation",
            "ALL",
            ["app"],
            "(tenant_id = current_setting('app.tenant')::int)",
            None,
        ),
    ]


def test_fetch_partitioning_returns_empty_on_redshift() -> None:
    cursor = FakeCursor()
    result = fetch_partitioning(cursor, 42, SqlEngine.redshift)
    assert result == PartitioningInfo(
        parent=None, strategy=None, partition_key=None, children=[]
    )
    assert cursor.queries == []


def test_fetch_partitioning_not_partitioned() -> None:
    # parent lookup: None, strategy lookup: None, partkeydef: (None,), children: []
    cursor = FakeCursor([None, None, (None,), []])
    result = fetch_partitioning(cursor, 42, SqlEngine.postgresql)
    assert result == PartitioningInfo(
        parent=None, strategy=None, partition_key=None, children=[]
    )


def test_fetch_partitioning_root_reports_strategy() -> None:
    # Partition root: no parent, has strategy, has partkeydef, has children.
    cursor = FakeCursor(
        [
            None,  # parent lookup
            ("RANGE",),  # strategy lookup
            ("RANGE (created_at)",),  # partkeydef
            [("public.events_2024", "FOR VALUES FROM (...) TO (...)")],
        ]
    )
    result = fetch_partitioning(cursor, 42, SqlEngine.postgresql)
    assert result == PartitioningInfo(
        parent=None,
        strategy="RANGE",
        partition_key="RANGE (created_at)",
        children=[("public.events_2024", "FOR VALUES FROM (...) TO (...)")],
    )


def test_fetch_partitioning_child_reports_parent() -> None:
    # Partition child: has parent, no strategy (not a partitioned table itself).
    cursor = FakeCursor(
        [
            ("public.events",),  # parent lookup
            None,  # strategy lookup
            (None,),  # partkeydef
            [],  # children
        ]
    )
    result = fetch_partitioning(cursor, 42, SqlEngine.postgresql)
    assert result == PartitioningInfo(
        parent="public.events", strategy=None, partition_key=None, children=[]
    )


def test_fetch_view_definition() -> None:
    cursor = FakeCursor([("SELECT id, email FROM users;", "YES", None)])
    vd = fetch_view_definition(cursor, 42, SqlEngine.postgresql)
    assert vd == ViewDefinition(
        sql="SELECT id, email FROM users;", is_updatable=True, check_option=None
    )


def test_fetch_view_definition_missing_raises() -> None:
    cursor = FakeCursor([None])
    with pytest.raises(TargetNotFoundError):
        fetch_view_definition(cursor, 42, SqlEngine.postgresql)


def test_fetch_view_definition_null_definition_raises() -> None:
    # pg_get_viewdef() returns NULL rather than erroring for an oid that is not
    # a view, so the row is present and only the definition is missing. Passing
    # that through would produce ViewDefinition(sql=None).
    cursor = FakeCursor([(None, None, None)])
    with pytest.raises(TargetNotFoundError, match="view with oid 42 not found"):
        fetch_view_definition(cursor, 42, SqlEngine.postgresql)


def test_fetch_view_definition_check_option_none_str() -> None:
    cursor = FakeCursor([("SELECT 1;", "NO", "NONE")])
    vd = fetch_view_definition(cursor, 42, SqlEngine.postgresql)
    assert vd.is_updatable is False
    assert vd.check_option is None

    cursor = FakeCursor([("SELECT 1;", "NO", "LOCAL")])
    vd = fetch_view_definition(cursor, 42, SqlEngine.postgresql)
    assert vd.check_option == "LOCAL"


def test_fetch_view_definition_redshift_uses_simple_query() -> None:
    cursor = FakeCursor([("SELECT 1;",)])
    vd = fetch_view_definition(cursor, 42, SqlEngine.redshift)
    assert vd == ViewDefinition(sql="SELECT 1;", is_updatable=False, check_option=None)
    assert len(cursor.queries) == 1
    query_text, params = cursor.queries[0]
    assert "information_schema.views" not in query_text
    assert "pg_get_viewdef" in query_text
    assert params == (42,)


def test_fetch_dependencies_upstream() -> None:
    cursor = FakeCursor([[("public.users", "r"), ("public.orgs", "r")]])
    deps = fetch_dependencies(
        cursor, 42, direction="upstream", engine=SqlEngine.postgresql
    )
    assert deps == [
        DependencyEdge("public.users", "table"),
        DependencyEdge("public.orgs", "table"),
    ]


def test_fetch_dependencies_downstream() -> None:
    cursor = FakeCursor([[("public.user_view", "v")]])
    deps = fetch_dependencies(
        cursor, 42, direction="downstream", engine=SqlEngine.postgresql
    )
    assert deps == [DependencyEdge("public.user_view", "view")]
    query_text, _ = cursor.queries[0]
    assert "r.ev_class <> %s" in query_text
    assert "tc.relkind IN ('v','m')" in query_text


def test_fetch_dependencies_invalid_direction() -> None:
    cursor = FakeCursor()
    with pytest.raises(ValueError):
        fetch_dependencies(
            cursor, 42, direction="sideways", engine=SqlEngine.postgresql
        )


def test_fetch_dependencies_redshift_returns_empty() -> None:
    cursor = FakeCursor()
    assert (
        fetch_dependencies(cursor, 42, direction="upstream", engine=SqlEngine.redshift)
        == []
    )
    assert (
        fetch_dependencies(
            cursor, 42, direction="downstream", engine=SqlEngine.redshift
        )
        == []
    )
    assert cursor.queries == []


def test_fetch_schema_header() -> None:
    cursor = FakeCursor([("public", "dbadmin", "Default schema")])
    hdr = fetch_schema_header(cursor, "public")
    assert hdr == SchemaHeader("public", "dbadmin", "Default schema")


def test_fetch_schema_header_missing_raises() -> None:
    cursor = FakeCursor([None])
    with pytest.raises(TargetNotFoundError):
        fetch_schema_header(cursor, "nope")


def test_fetch_schema_contents() -> None:
    rows = [
        ("users", "r", "dbadmin", 1000, 524288),
        ("users_view", "v", "dbadmin", None, None),
        ("daily_summary", "m", "etl", 50000, 1048576),
    ]
    cursor = FakeCursor([rows])
    items = fetch_schema_contents(cursor, "public", SqlEngine.postgresql)
    assert items[0] == SchemaContentItem("users", "table", "dbadmin", 1000, 524288)
    assert items[1].kind == "view"
    assert items[2].kind == "matview"


def test_fetch_schema_contents_redshift_uses_redshift_sql() -> None:
    cursor = FakeCursor([[]])
    fetch_schema_contents(cursor, "public", SqlEngine.redshift)
    assert len(cursor.queries) == 1
    query_text, params = cursor.queries[0]
    assert "svv_table_info" in query_text
    assert params == ("public", "public", "public")


def test_fetch_schema_privileges() -> None:
    rows = [("analyst", "USAGE", False, "dbadmin"), ("app", "CREATE", True, "dbadmin")]
    cursor = FakeCursor([rows])
    grants = fetch_schema_privileges(cursor, "public", SqlEngine.postgresql)
    assert PrivilegeGrant("analyst", "USAGE", False, "dbadmin") in grants
    assert len(cursor.queries) == 1
    query_text, params = cursor.queries[0]
    # information_schema.usage_privileges has no 'SCHEMA' rows on PostgreSQL, so
    # the ACL in pg_namespace is the only place USAGE grants can be read from.
    assert "aclexplode" in query_text
    assert "usage_privileges" not in query_text
    assert params == ("public",)


def test_fetch_schema_privileges_redshift_uses_redshift_sql() -> None:
    # Redshift has no aclexplode(), so that branch must never receive the
    # PostgreSQL query. It takes the schema name twice, once per privilege scan.
    cursor = FakeCursor([[]])
    fetch_schema_privileges(cursor, "public", SqlEngine.redshift)
    assert len(cursor.queries) == 1
    query_text, params = cursor.queries[0]
    assert "aclexplode" not in query_text
    assert params == ("public", "public")


def test_fetch_schema_default_privileges_postgres() -> None:
    from dataplat.services.db.describe import DefaultPrivilegeGrant

    rows = [
        ("analyst", "TABLE", ["SELECT"], False, "dbadmin"),
        ("app", "TABLE", ["DELETE", "INSERT", "SELECT", "UPDATE"], False, "dbadmin"),
    ]
    cursor = FakeCursor([rows])
    grants = fetch_schema_default_privileges(cursor, "public", SqlEngine.postgresql)
    assert grants == [
        DefaultPrivilegeGrant("analyst", "TABLE", ["SELECT"], False, "dbadmin"),
        DefaultPrivilegeGrant(
            "app", "TABLE", ["DELETE", "INSERT", "SELECT", "UPDATE"], False, "dbadmin"
        ),
    ]
    query_text, params = cursor.queries[0]
    assert "pg_default_acl" in query_text
    assert params == ("public",)


def test_fetch_schema_default_privileges_redshift_returns_empty() -> None:
    cursor = FakeCursor([])
    assert fetch_schema_default_privileges(cursor, "public", SqlEngine.redshift) == []
    assert cursor.queries == []


def test_fetch_redshift_distribution() -> None:
    cursor = FakeCursor([("KEY", "user_id", "COMPOUND", ["created_at", "user_id"])])
    d = fetch_redshift_distribution(cursor, "public", "events")
    assert d == RedshiftDistribution(
        "KEY", "user_id", "COMPOUND", ["created_at", "user_id"]
    )


def test_fetch_redshift_distribution_missing_returns_none() -> None:
    cursor = FakeCursor([None])
    assert fetch_redshift_distribution(cursor, "public", "events") is None


def test_fetch_redshift_table_stats() -> None:
    cursor = FakeCursor([(1.2, 3.4, False)])
    stats = fetch_redshift_table_stats(cursor, "public", "events")
    assert stats == RedshiftTableStats(skew_rows=1.2, unsorted_pct=3.4, stats_off=False)


def test_fetch_redshift_table_stats_missing_returns_none() -> None:
    cursor = FakeCursor([None])
    assert fetch_redshift_table_stats(cursor, "public", "events") is None


def test_describe_table_composes_all_sections_postgres() -> None:
    cursor = FakeCursor(
        [
            # fetch_relation_header
            (
                "public",
                "users",
                "dbadmin",
                "pg_default",
                None,
                100,
                1024,
                512,
                256,
                256,
            ),
            # fetch_columns
            [(1, "id", "bigint", False, None, True, None, None, None)],
            # fetch_constraints
            [
                (
                    "users_pkey",
                    "p",
                    "PRIMARY KEY (id)",
                    ["id"],
                    None,
                    None,
                    None,
                    None,
                    None,
                )
            ],
            # fetch_indexes
            [("users_pkey", ["id"], True, True, "btree", 16384, None)],
            # fetch_relation_privileges
            [("dbadmin", "OWNER", False, "")],
            # fetch_triggers
            [],
            # fetch_policies — relrowsecurity row
            (False,),
            # fetch_policies — policies rows
            [],
            # fetch_partitioning — parent lookup
            None,
            # fetch_partitioning — strategy lookup
            None,
            # fetch_partitioning — partkeydef
            (None,),
            # fetch_partitioning — children
            [],
        ]
    )
    ref = TargetRef(ObjectKind.table, "public", "users", 42)
    desc = describe_table(cursor, ref, SqlEngine.postgresql)
    assert desc.header.name == "users"
    assert [c.name for c in desc.columns] == ["id"]
    assert desc.constraints.primary_key is not None
    assert desc.constraints.primary_key.columns == ["id"]
    assert desc.redshift_distribution is None
    assert desc.redshift_stats is None


def test_describe_view_composes_postgres() -> None:
    cursor = FakeCursor(
        [
            # fetch_relation_header
            (
                "public",
                "user_view",
                "dbadmin",
                "pg_default",
                "View over users",
                None,
                None,
                None,
                None,
                None,
            ),
            # fetch_columns
            [(1, "id", "bigint", True, None, False, None, None, None)],
            # fetch_view_definition
            ("SELECT id FROM users;", "YES", None),
            # fetch_dependencies upstream
            [("public.users", "r")],
            # fetch_dependencies downstream
            [("public.user_summary", "v")],
            # fetch_relation_privileges
            [("dbadmin", "OWNER", False, "")],
            # fetch_triggers
            [],
        ]
    )
    ref = TargetRef(ObjectKind.view, "public", "user_view", 7)
    desc = describe_view(cursor, ref, SqlEngine.postgresql)
    assert isinstance(desc, ViewDescription)
    assert desc.header.name == "user_view"
    assert [c.name for c in desc.columns] == ["id"]
    assert desc.definition.sql == "SELECT id FROM users;"
    assert desc.definition.is_updatable is True
    assert desc.upstream == [DependencyEdge("public.users", "table")]
    assert desc.downstream == [DependencyEdge("public.user_summary", "view")]
    assert desc.privileges == [PrivilegeGrant("dbadmin", "OWNER", False, "")]
    assert desc.triggers == []


def test_describe_table_redshift_populates_distribution_and_stats() -> None:
    # On Redshift: header, columns, constraints (no query), indexes, privileges,
    # triggers (no query), policies (no query), partitioning (no query),
    # then redshift_distribution and redshift_stats.
    cursor = FakeCursor(
        [
            # fetch_relation_header (Redshift path)
            (
                "public",
                "events",
                "dbadmin",
                None,
                None,
                5000,
                1048576,
                None,
                None,
                None,
            ),
            # fetch_columns (Redshift path, includes encoding)
            [(1, "id", "bigint", False, None, False, None, None, None, "az64")],
            # fetch_indexes (Redshift path)
            [],
            # fetch_relation_privileges
            [("dbadmin", "OWNER", False, "")],
            # fetch_redshift_distribution
            ("KEY", "id", "COMPOUND", ["id"]),
            # fetch_redshift_table_stats
            (0.1, 2.5, False),
        ]
    )
    ref = TargetRef(ObjectKind.table, "public", "events", 99)
    desc = describe_table(cursor, ref, SqlEngine.redshift)
    assert desc.header.name == "events"
    assert desc.columns[0].encoding == "az64"
    # Redshift constraints / triggers / policies / partitioning skip SQL entirely.
    assert desc.constraints.primary_key is None
    assert desc.triggers == []
    assert desc.policies == []
    assert desc.policies_enabled is False
    assert desc.partitioning == PartitioningInfo(
        parent=None, strategy=None, partition_key=None, children=[]
    )
    assert desc.redshift_distribution == RedshiftDistribution(
        "KEY", "id", "COMPOUND", ["id"]
    )
    assert desc.redshift_stats == RedshiftTableStats(
        skew_rows=0.1, unsorted_pct=2.5, stats_off=False
    )


def test_describe_schema_composes_postgres() -> None:
    cursor = FakeCursor(
        [
            # fetch_schema_header
            ("public", "dbadmin", None),
            # fetch_schema_privileges
            [],
            # fetch_schema_default_privileges
            [],
            # fetch_schema_contents
            [],
        ]
    )
    ref = TargetRef(ObjectKind.schema, "public", None, None)
    desc = describe_schema(cursor, ref, SqlEngine.postgresql)
    assert desc.header.name == "public"
    assert desc.contents == []
    assert desc.default_privileges == []


def test_fetch_schema_privileges_redshift_scans_usage_and_create() -> None:
    """The Redshift branch reports USAGE, and does it the way it already reported
    CREATE.

    information_schema.usage_privileges never covers schemas, so the old USAGE
    half returned nothing on every server. It is now a has_schema_privilege()
    scan mirroring the CREATE half that this query has always run against
    Redshift. No cluster is reachable from CI, so asserting the emitted SQL is
    the only coverage this path can have.
    """
    cursor = FakeCursor([[]])

    fetch_schema_privileges(cursor, "analytics", SqlEngine.redshift)

    sql, params = cursor.queries[0]
    assert "usage_privileges" not in sql, "the dead information_schema half is back"
    assert sql.count("has_schema_privilege") == 2
    assert "'USAGE'" in sql and "'CREATE'" in sql
    # One placeholder per scan, so the call site's (schema, schema) still fits.
    assert params == ("analytics", "analytics")


def test_fetch_schema_privileges_postgres_is_untouched_by_the_redshift_fix() -> None:
    """Guard against fixing both branches when only one was meant."""
    cursor = FakeCursor([[]])

    fetch_schema_privileges(cursor, "analytics", SqlEngine.postgresql)

    sql, params = cursor.queries[0]
    assert "aclexplode" in sql
    assert "has_schema_privilege" not in sql
    assert params == ("analytics",)


def test_fetch_schema_privileges_postgres_derives_the_owners_grant_option() -> None:
    """The grant option cannot come from the ACL bit alone.

    An ACL never records an owner's grant option, but the owner has one; the
    relation half of the report gets that from information_schema, so the schema
    half applies the same rule with pg_has_role(). Live proof is in
    tests/integration/test_describe_pg.py -- this only pins the emitted SQL, so
    the expression cannot be dropped as redundant.
    """
    cursor = FakeCursor([[]])

    fetch_schema_privileges(cursor, "analytics", SqlEngine.postgresql)

    sql, _ = cursor.queries[0]
    assert "pg_has_role((acl).grantee, n.nspowner, 'USAGE')" in sql


def test_fetch_schema_privileges_redshift_keeps_the_raw_grant_option() -> None:
    """The owner-grant-option fix must not have leaked onto the Redshift path.

    Redshift has no aclexplode() and no cluster is reachable to test a
    replacement, so that branch keeps its has_schema_privilege() scans and their
    documented gap: a hardcoded false grant option, one per scan.
    """
    cursor = FakeCursor([[]])

    fetch_schema_privileges(cursor, "analytics", SqlEngine.redshift)

    sql, params = cursor.queries[0]
    assert "pg_has_role" not in sql
    assert "aclexplode" not in sql
    assert sql.count("false") == 2
    assert params == ("analytics", "analytics")


# =========================================================================
# DuckDB: a third dialect, measured against a real database.
#
# Nothing below is faked, because nothing needs to be: DuckDB runs in this
# process. That matters more here than it would for a driver test, because the
# defects this dialect invites are *silent* — DuckDB has a pg_attrdef with no
# rows in it, a pg_constraint whose conname is the constraint's text, and a
# format_type() that renders VARCHAR[] as "list". A fake cursor would have
# accepted the PostgreSQL queries and reported all three wrong answers as
# passing. The FakeCursor tests that remain are the ones asserting an *absence*:
# that a fetcher sent no SQL at all, which a real database cannot show.
# =========================================================================

DDB = SqlEngine.duckdb


class DuckDbTestCursor:
    """The cursor surface the fetchers use, over one real DuckDB connection.

    Deliberately not ``dataplat.cli.db._common.DuckDbCursor``: these tests are
    about the SQL, and a local three-method adapter keeps the services suite
    from importing the CLI layer. It records statements so a test can assert
    what the dialect emitted as well as what came back.
    """

    def __init__(self, connection: Any) -> None:
        self._connection = connection
        self.queries: list[str] = []

    def execute(self, query: str, params: Any = None) -> DuckDbTestCursor:
        self.queries.append(query)
        self._connection.execute(query, params)
        return self

    def fetchone(self) -> Any:
        return self._connection.fetchone()

    def fetchall(self) -> list[Any]:
        return list(self._connection.fetchall())


_DDB_FIXTURE = (
    "CREATE SCHEMA ddb",
    "CREATE TABLE ddb.orgs (id BIGINT PRIMARY KEY, name VARCHAR NOT NULL)",
    "CREATE TABLE ddb.pair (k1 INTEGER, k2 VARCHAR, PRIMARY KEY (k1, k2))",
    # Every column here earns its place: the nested/parameterised types are what
    # format_type() flattens, `created_at` is what an empty pg_attrdef loses, and
    # (k1, k2) is a composite foreign key, whose target columns have to be paired
    # positionally rather than by name.
    """
    CREATE TABLE ddb.users (
        id BIGINT PRIMARY KEY,
        email VARCHAR NOT NULL UNIQUE,
        org_id BIGINT REFERENCES ddb.orgs(id),
        k1 INTEGER,
        k2 VARCHAR,
        age INTEGER CHECK (age >= 0),
        created_at TIMESTAMP DEFAULT now(),
        tags VARCHAR[],
        profile STRUCT(a INTEGER, b VARCHAR),
        amount DECIMAL(10, 2),
        payload JSON,
        FOREIGN KEY (k1, k2) REFERENCES ddb.pair (k1, k2)
    )
    """,
    "COMMENT ON TABLE ddb.users IS 'application users'",
    "COMMENT ON COLUMN ddb.users.email IS 'login email'",
    "CREATE INDEX users_org_created ON ddb.users (org_id, created_at)",
    "CREATE UNIQUE INDEX users_amount_uq ON ddb.users (amount)",
    "CREATE VIEW ddb.adults AS SELECT id, email FROM ddb.users WHERE age > 18",
    "COMMENT ON VIEW ddb.adults IS 'adults only'",
    "CREATE SEQUENCE ddb.user_seq",
    "INSERT INTO ddb.orgs VALUES (1, 'acme'), (2, 'globex')",
    "INSERT INTO ddb.pair VALUES (1, 'x')",
    "INSERT INTO ddb.users VALUES "
    "(1, 'a@x.y', 1, 1, 'x', 30, now(), ['t'], {'a': 1, 'b': 'z'}, 1.50, '{}'), "
    "(2, 'b@x.y', 2, 1, 'x', 12, now(), NULL, NULL, NULL, NULL)",
    # In 'main', because that is the schema a DuckDB file has by default and so
    # the one a user describes first -- and the one name that exists in three
    # attached catalogs at once.
    "CREATE TABLE unqualified (id INTEGER)",
)


@pytest.fixture(scope="module")
def ddb() -> Any:
    """One in-memory DuckDB, shared: every test below only reads."""
    connection = duckdb.connect(":memory:")
    for statement in _DDB_FIXTURE:
        connection.execute(statement)
    yield connection
    connection.close()


@pytest.fixture
def ddb_cursor(ddb: Any) -> DuckDbTestCursor:
    return DuckDbTestCursor(ddb)


def _ddb_ref(cursor: DuckDbTestCursor, target: str) -> TargetRef:
    return resolve_target(cursor, DDB, target)


# --- resolve_target ------------------------------------------------------


def test_duckdb_resolve_target_schema(ddb_cursor: DuckDbTestCursor) -> None:
    assert _ddb_ref(ddb_cursor, "ddb") == TargetRef(
        kind=ObjectKind.schema, schema="ddb", name=None, oid=None
    )


def test_duckdb_resolve_target_table_and_view(ddb_cursor: DuckDbTestCursor) -> None:
    table = _ddb_ref(ddb_cursor, "ddb.users")
    view = _ddb_ref(ddb_cursor, "ddb.adults")
    assert table.kind is ObjectKind.table
    assert view.kind is ObjectKind.view
    # The oid is what every duckdb_*() catalog keys on, so it has to be real.
    assert isinstance(table.oid, int) and table.oid > 0
    assert table.oid != view.oid


def test_duckdb_resolve_target_missing(ddb_cursor: DuckDbTestCursor) -> None:
    with pytest.raises(TargetNotFoundError, match='schema "nope"'):
        _ddb_ref(ddb_cursor, "nope")
    with pytest.raises(TargetNotFoundError, match='"ddb.nope" not found'):
        _ddb_ref(ddb_cursor, "ddb.nope")


def test_duckdb_resolve_target_rejects_a_sequence(ddb_cursor: DuckDbTestCursor) -> None:
    """relkind 'S' exists on DuckDB, and a sequence has nothing to describe."""
    with pytest.raises(TargetNotFoundError, match="unsupported kind 'S'"):
        _ddb_ref(ddb_cursor, "ddb.user_seq")


# --- columns -------------------------------------------------------------


def _ddb_columns(cursor: DuckDbTestCursor, target: str) -> dict[str, ColumnInfo]:
    ref = _ddb_ref(cursor, target)
    assert ref.oid is not None
    return {c.name: c for c in fetch_columns(cursor, ref.oid, DDB)}


def test_duckdb_columns_keep_duckdbs_own_type_spelling(
    ddb_cursor: DuckDbTestCursor,
) -> None:
    """The reason _COLUMNS_SQL_DUCKDB does not use format_type().

    pg_catalog.format_type() exists on DuckDB and answers for every one of these
    — with 'list', 'struct', 'numeric(10,2)' and 'varchar'. Those are not what
    the user declared, and three of them are not even DuckDB type names.
    """
    columns = _ddb_columns(ddb_cursor, "ddb.users")
    assert columns["tags"].data_type == "VARCHAR[]"
    assert columns["profile"].data_type == "STRUCT(a INTEGER, b VARCHAR)"
    assert columns["amount"].data_type == "DECIMAL(10,2)"
    assert columns["payload"].data_type == "JSON"
    assert columns["id"].data_type == "BIGINT"


def test_duckdb_columns_report_defaults(ddb_cursor: DuckDbTestCursor) -> None:
    """The reason it does not join pg_attrdef, which exists and holds no rows."""
    columns = _ddb_columns(ddb_cursor, "ddb.users")
    assert columns["created_at"].default == "now()"
    assert columns["id"].default is None


def test_duckdb_columns_report_nullability_and_comments(
    ddb_cursor: DuckDbTestCursor,
) -> None:
    columns = _ddb_columns(ddb_cursor, "ddb.users")
    assert columns["email"].nullable is False
    assert columns["age"].nullable is True
    assert columns["email"].comment == "login email"
    assert columns["age"].comment is None
    # encoding is a Redshift column and must stay unset elsewhere.
    assert columns["email"].encoding is None


def test_duckdb_columns_flag_the_primary_key(ddb_cursor: DuckDbTestCursor) -> None:
    columns = _ddb_columns(ddb_cursor, "ddb.users")
    assert columns["id"].is_primary_key is True
    assert columns["email"].is_primary_key is False


def test_duckdb_columns_pair_a_composite_fk_positionally(
    ddb_cursor: DuckDbTestCursor,
) -> None:
    """k1 -> pair.k1 and k2 -> pair.k2, which is what list_position() is for.

    DuckDB's pg_constraint cannot answer this at all: confrelid is 0 for every
    row, so the referenced relation is unreachable there.
    """
    columns = _ddb_columns(ddb_cursor, "ddb.users")
    assert (columns["k1"].fk_target_table, columns["k1"].fk_target_column) == (
        "ddb.pair",
        "k1",
    )
    assert (columns["k2"].fk_target_table, columns["k2"].fk_target_column) == (
        "ddb.pair",
        "k2",
    )
    # A single-column key still resolves, and a plain column reports nothing.
    assert columns["org_id"].fk_target_table == "ddb.orgs"
    assert columns["org_id"].fk_target_column == "id"
    assert columns["age"].fk_target_table is None


def test_duckdb_columns_of_a_view(ddb_cursor: DuckDbTestCursor) -> None:
    columns = _ddb_columns(ddb_cursor, "ddb.adults")
    assert list(columns) == ["id", "email"]
    assert all(
        not c.is_primary_key and c.fk_target_table is None for c in columns.values()
    )


# --- constraints ---------------------------------------------------------


def test_duckdb_constraints_read_duckdb_constraints(
    ddb_cursor: DuckDbTestCursor,
) -> None:
    ref = _ddb_ref(ddb_cursor, "ddb.users")
    assert ref.oid is not None
    bundle = fetch_constraints(ddb_cursor, ref.oid, DDB)

    assert bundle.primary_key == PrimaryKeyInfo(name="users_id_pkey", columns=["id"])
    composite = next(fk for fk in bundle.foreign_keys if fk.columns == ["k1", "k2"])
    assert composite.referenced_table == "ddb.pair"
    assert composite.referenced_columns == ["k1", "k2"]
    # DuckDB refuses CASCADE/SET NULL/SET DEFAULT at parse time, so NO ACTION is
    # the only referential action a DuckDB foreign key can have.
    assert (composite.on_delete, composite.on_update) == ("NO ACTION", "NO ACTION")
    assert composite.deferrable is False
    assert [u.name for u in bundle.unique_constraints] == ["users_email_key"]
    assert [c.definition for c in bundle.check_constraints] == ["CHECK((age >= 0))"]


def test_duckdb_constraints_omit_not_null(ddb_cursor: DuckDbTestCursor) -> None:
    """duckdb_constraints() reports NOT NULL; the report shows it in Columns.

    Listing it here as well would make the same table look different per engine,
    since PostgreSQL keeps NOT NULL in pg_attribute rather than pg_constraint.
    """
    ref = _ddb_ref(ddb_cursor, "ddb.users")
    assert ref.oid is not None
    bundle = fetch_constraints(ddb_cursor, ref.oid, DDB)
    names = [c.name for c in bundle.check_constraints + bundle.unique_constraints]
    assert not any("not_null" in n for n in names)


# --- indexes -------------------------------------------------------------


def test_duckdb_indexes_split_the_expression_list(
    ddb_cursor: DuckDbTestCursor,
) -> None:
    """`expressions` is DuckDB's own rendering of a list, cast back by DuckDB.

    Splitting the text on ', ' in Python would tear `concat(a, b)` in half; the
    ::VARCHAR[] cast hands the parsing to the engine that wrote it.
    """
    ref = _ddb_ref(ddb_cursor, "ddb.users")
    assert ref.oid is not None
    indexes = {i.name: i for i in fetch_indexes(ddb_cursor, ref.oid, DDB)}

    assert indexes["users_org_created"].columns == ["org_id", "created_at"]
    assert indexes["users_org_created"].unique is False
    assert indexes["users_amount_uq"].unique is True
    # One index type (pg_am holds only 'art'), no partial indexes, no size.
    assert indexes["users_org_created"].method == "art"
    assert indexes["users_org_created"].predicate is None
    assert indexes["users_org_created"].size_bytes is None


def test_duckdb_indexes_omit_constraint_indexes(
    ddb_cursor: DuckDbTestCursor,
) -> None:
    """DuckDB does not expose the index behind a PRIMARY KEY or UNIQUE.

    duckdb_tables().index_count counts them and duckdb_indexes() does not list
    them, so `primary` is false throughout and the Constraints section is where
    a reader finds the key. Pinned because a future release exposing them would
    change this section's meaning.
    """
    ref = _ddb_ref(ddb_cursor, "ddb.users")
    assert ref.oid is not None
    indexes = fetch_indexes(ddb_cursor, ref.oid, DDB)
    assert sorted(i.name for i in indexes) == ["users_amount_uq", "users_org_created"]
    assert not any(i.primary for i in indexes)


def test_duckdb_indexes_none_on_a_table_without_any(
    ddb_cursor: DuckDbTestCursor,
) -> None:
    ref = _ddb_ref(ddb_cursor, "ddb.orgs")
    assert ref.oid is not None
    assert fetch_indexes(ddb_cursor, ref.oid, DDB) == []


# --- relation header -----------------------------------------------------


def test_duckdb_relation_header_of_a_table(ddb_cursor: DuckDbTestCursor) -> None:
    ref = _ddb_ref(ddb_cursor, "ddb.users")
    assert ref.oid is not None
    header = fetch_relation_header(ddb_cursor, ref.oid, DDB)

    assert (header.schema, header.name) == ("ddb", "users")
    assert header.comment == "application users"
    assert header.row_estimate == 2
    # No owner (no users), no tablespace, and no byte size anywhere: reading
    # duckdb_tables().estimated_size as bytes would report 2 bytes for this table.
    assert header.owner == ""
    assert header.tablespace is None
    assert (header.total_size, header.table_size) == (None, None)
    assert (header.index_size, header.toast_size) == (None, None)


def test_duckdb_relation_header_of_a_view(ddb_cursor: DuckDbTestCursor) -> None:
    """The UNION's second arm, and why row_estimate stays NULL for a view.

    DuckDB's pg_class.reltuples is 0.0 for every view, which would render as
    "0 rows" for a view over a populated table.
    """
    ref = _ddb_ref(ddb_cursor, "ddb.adults")
    assert ref.oid is not None
    header = fetch_relation_header(ddb_cursor, ref.oid, DDB)

    assert (header.schema, header.name) == ("ddb", "adults")
    assert header.comment == "adults only"
    assert header.row_estimate is None


def test_duckdb_relation_header_unknown_oid_raises(
    ddb_cursor: DuckDbTestCursor,
) -> None:
    with pytest.raises(TargetNotFoundError, match="oid 987654"):
        fetch_relation_header(ddb_cursor, 987654, DDB)


# --- view definition -----------------------------------------------------


def test_duckdb_view_definition(ddb_cursor: DuckDbTestCursor) -> None:
    """duckdb_views().sql, because pg_get_viewdef() has no two-argument form.

    DuckDB returns the whole CREATE VIEW statement where PostgreSQL returns the
    bare SELECT; it is passed through as the engine wrote it.
    """
    ref = _ddb_ref(ddb_cursor, "ddb.adults")
    assert ref.oid is not None
    definition = fetch_view_definition(ddb_cursor, ref.oid, DDB)

    assert definition.sql.startswith("CREATE VIEW ddb.adults AS")
    assert "age > 18" in definition.sql
    # information_schema.views answers is_updatable='NO' and check_option='NONE'
    # for every DuckDB view, so these two are read from the engine's own answer.
    assert definition.is_updatable is False
    assert definition.check_option is None


def test_duckdb_view_definition_of_a_table_raises(
    ddb_cursor: DuckDbTestCursor,
) -> None:
    """A table's oid is not in duckdb_views(), so the row is missing, not NULL."""
    ref = _ddb_ref(ddb_cursor, "ddb.users")
    assert ref.oid is not None
    with pytest.raises(TargetNotFoundError):
        fetch_view_definition(ddb_cursor, ref.oid, DDB)


# --- schema --------------------------------------------------------------


def test_duckdb_schema_header(ddb_cursor: DuckDbTestCursor) -> None:
    header = fetch_schema_header(ddb_cursor, "ddb", DDB)
    assert header.name == "ddb"
    assert header.owner == ""
    # DuckDB refuses COMMENT ON SCHEMA, so this is always None today; the query
    # reads the column rather than hardcoding it so the day that lands it works.
    assert header.comment is None


def test_duckdb_schema_header_does_not_duplicate_main(
    ddb_cursor: DuckDbTestCursor,
) -> None:
    """duckdb_schemas() lists 'main' once per attached catalog.

    Without the current_database() filter the header would describe whichever of
    the user, 'system' and 'temp' catalogs came back first.
    """
    ddb_cursor._connection.execute(
        "SELECT count(*) FROM duckdb_schemas() WHERE schema_name = 'main'"
    )
    assert ddb_cursor._connection.fetchone()[0] > 1

    assert fetch_schema_header(ddb_cursor, "main", DDB).name == "main"
    # One row, so the fetcher is not silently picking from several.
    ddb_cursor._connection.execute(
        "SELECT count(*) FROM duckdb_schemas() "
        "WHERE schema_name = 'main' AND database_name = current_database()"
    )
    assert ddb_cursor._connection.fetchone()[0] == 1


def test_duckdb_schema_header_missing_raises(ddb_cursor: DuckDbTestCursor) -> None:
    with pytest.raises(TargetNotFoundError, match='schema "nope" not found'):
        fetch_schema_header(ddb_cursor, "nope", DDB)


def test_duckdb_schema_header_defaults_to_the_libpq_query() -> None:
    """The engine argument only asks "is this DuckDB", so libpq is the default.

    Pre-DuckDB call sites pass two arguments, and this keeps them sending the
    pg_get_userbyid() query they always sent.
    """
    cursor = FakeCursor([("public", "dbadmin", None)])
    assert fetch_schema_header(cursor, "public").owner == "dbadmin"
    sql, params = cursor.queries[0]
    assert "pg_get_userbyid" in sql
    assert params == ("public",)


def test_duckdb_schema_contents(ddb_cursor: DuckDbTestCursor) -> None:
    items = {i.name: i for i in fetch_schema_contents(ddb_cursor, "ddb", DDB)}

    assert items["users"].kind == "table"
    assert items["adults"].kind == "view"
    assert items["user_seq"].kind == "sequence"
    # reltuples is real for a table and meaningless for a view, so only the
    # table reports one. Nothing reports a size.
    assert items["users"].row_estimate == 2
    assert items["adults"].row_estimate is None
    assert all(i.size_bytes is None for i in items.values())
    assert all(i.owner == "" for i in items.values())


def test_duckdb_schema_contents_of_main_lists_only_the_users_own(
    ddb_cursor: DuckDbTestCursor,
) -> None:
    """'main' is the default schema of every DuckDB file, and the ambiguous one.

    pg_namespace holds a 'main' for the user database, for 'system' and for
    'temp'; only the first has anything a reader asked about.
    """
    items = fetch_schema_contents(ddb_cursor, "main", DDB)
    assert [i.name for i in items] == ["unqualified"]
    assert items[0].kind == "table"


def test_duckdb_schema_contents_excludes_indexes(
    ddb_cursor: DuckDbTestCursor,
) -> None:
    """DuckDB puts indexes in pg_class under the user's schema (relkind 'i')."""
    names = {i.name for i in fetch_schema_contents(ddb_cursor, "ddb", DDB)}
    assert "users_org_created" not in names
    assert names == {"orgs", "pair", "users", "adults", "user_seq"}


# --- the fetchers that refuse ---------------------------------------------


def test_duckdb_privileges_and_acl_fetchers_send_no_sql() -> None:
    """No aclexplode(), no pg_roles, no role_table_grants, and no grantees.

    Asserted with a fake cursor because the claim is about SQL *not* being sent:
    a real DuckDB would only show that nothing came back, which is also what a
    query returning no rows looks like — and an empty grant table reads as
    "nobody has access".
    """
    cursor = FakeCursor()
    assert fetch_relation_privileges(cursor, "ddb", "users", DDB) == []
    assert fetch_schema_privileges(cursor, "ddb", DDB) == []
    assert fetch_schema_default_privileges(cursor, "ddb", DDB) == []
    assert cursor.queries == []


def test_duckdb_relation_privileges_defaults_to_the_libpq_query() -> None:
    cursor = FakeCursor([[("dbadmin", "OWNER", False, "")]])
    assert fetch_relation_privileges(cursor, "public", "users") == [
        PrivilegeGrant("dbadmin", "OWNER", False, "")
    ]
    assert "role_table_grants" in cursor.queries[0][0]


def test_duckdb_trigger_policy_partition_and_dependency_fetchers_send_no_sql() -> None:
    cursor = FakeCursor()
    assert fetch_triggers(cursor, 42, DDB) == []
    assert fetch_policies(cursor, 42, DDB) == ([], False)
    assert fetch_partitioning(cursor, 42, DDB) == PartitioningInfo(
        parent=None, strategy=None, partition_key=None, children=[]
    )
    assert fetch_dependencies(cursor, 42, direction="upstream", engine=DDB) == []
    assert fetch_dependencies(cursor, 42, direction="downstream", engine=DDB) == []
    assert cursor.queries == []


def test_duckdb_policies_do_not_read_relrowsecurity() -> None:
    """pg_class.relrowsecurity exists on DuckDB and is false for every relation.

    Reading it would turn "this engine has no row-level security" into
    "RLS: disabled", which says the setting exists and could be turned on.
    """
    cursor = FakeCursor([(False,), []])
    fetch_policies(cursor, 42, DDB)
    assert cursor.queries == []


# --- composers -----------------------------------------------------------


def test_duckdb_describe_table_end_to_end(ddb_cursor: DuckDbTestCursor) -> None:
    ref = _ddb_ref(ddb_cursor, "ddb.users")
    desc = describe_table(ddb_cursor, ref, DDB)

    assert desc.header.name == "users"
    assert [c.name for c in desc.columns][:2] == ["id", "email"]
    assert desc.constraints.primary_key is not None
    assert len(desc.constraints.foreign_keys) == 2
    assert len(desc.indexes) == 2
    assert desc.privileges == []
    assert desc.triggers == []
    assert desc.policies == [] and desc.policies_enabled is False
    assert desc.partitioning.children == []
    assert desc.redshift_distribution is None and desc.redshift_stats is None
    # No matview definition: DuckDB has no relkind 'm', so resolve_target can
    # never hand describe_table a matview in the first place.
    assert desc.definition is None
    assert [na.section for na in desc.not_applicable] == [
        "Size",
        "Privileges",
        "Triggers",
        "Row-level security",
        "Partitioning",
    ]


def test_duckdb_describe_view_end_to_end(ddb_cursor: DuckDbTestCursor) -> None:
    ref = _ddb_ref(ddb_cursor, "ddb.adults")
    desc = describe_view(ddb_cursor, ref, DDB)

    assert isinstance(desc, ViewDescription)
    assert desc.definition.sql.startswith("CREATE VIEW ddb.adults AS")
    assert desc.upstream == [] and desc.downstream == []
    assert desc.privileges == [] and desc.triggers == []
    assert [na.section for na in desc.not_applicable] == [
        "Privileges",
        "Dependencies",
        "Triggers",
    ]


def test_duckdb_describe_schema_end_to_end(ddb_cursor: DuckDbTestCursor) -> None:
    ref = _ddb_ref(ddb_cursor, "ddb")
    desc = describe_schema(ddb_cursor, ref, DDB)

    assert desc.header.name == "ddb"
    assert {i.name for i in desc.contents} >= {"users", "adults", "user_seq"}
    assert desc.privileges == [] and desc.default_privileges == []
    assert [na.section for na in desc.not_applicable] == [
        "Privileges",
        "Default privileges",
        "Size",
        "Materialized views",
    ]


def test_duckdb_describe_never_emits_a_libpq_placeholder(
    ddb_cursor: DuckDbTestCursor,
) -> None:
    """The invariant that keeps a PostgreSQL or Redshift constant from leaking.

    Every libpq statement in the module binds ``%s``, which DuckDB rejects as a
    parser error, and nothing translates between the two spellings. So "no
    DuckDB-bound statement contains %s" is the same claim as "the DuckDB branch
    sent none of the other dialects' SQL" — checked over the whole report rather
    than fetcher by fetcher, so a dispatch added later is covered too.
    """
    for target in ("ddb", "ddb.users", "ddb.adults"):
        ddb_cursor.queries.clear()
        ref = _ddb_ref(ddb_cursor, target)
        if ref.kind is ObjectKind.schema:
            describe_schema(ddb_cursor, ref, DDB)
        elif ref.kind is ObjectKind.view:
            describe_view(ddb_cursor, ref, DDB)
        else:
            describe_table(ddb_cursor, ref, DDB)
        assert ddb_cursor.queries, target
        for sql in ddb_cursor.queries:
            assert "%s" not in sql, (target, sql)


# --- NotApplicable, and the engines it must not touch --------------------


@pytest.mark.parametrize(
    "builder", [table_not_applicable, view_not_applicable, schema_not_applicable]
)
@pytest.mark.parametrize("engine", [SqlEngine.postgresql, SqlEngine.redshift])
def test_not_applicable_is_empty_on_the_libpq_engines(builder, engine) -> None:
    """Redshift lacks several of these too and has always said nothing.

    Saying so there changes the output of a path with no integration suite, so it
    is left to whoever can state the evidence (CONTRIBUTING). This pins the
    current answer so the change is deliberate when it happens.
    """
    assert builder(engine) == []


def test_not_applicable_reasons_come_from_the_capability_declaration() -> None:
    """One voice: the same words refuse a command and explain a missing section.

    Derived rather than duplicated, so rewording capabilities.py cannot leave
    this report saying something different about the same fact.
    """
    caps = capabilities_for(SqlEngine.duckdb)
    privileges = next(
        na for na in table_not_applicable(DDB) if na.section == "Privileges"
    )
    assert privileges.reason == caps.support(Capability.acl_introspection).reason

    matviews = next(
        na for na in schema_not_applicable(DDB) if na.section == "Materialized views"
    )
    assert matviews.reason == caps.support(Capability.matview_catalog).reason


def test_not_applicable_size_reason_does_not_repeat_the_estimated_size_claim() -> None:
    """The one reason deliberately not taken from capabilities.py.

    Its relation_size_functions reason says size "comes from
    duckdb_tables().estimated_size", which is right for a row count and wrong for
    bytes: probed on duckdb 1.5.5, a 10,000-row table reports 10000 while its
    database file is 1.5 MiB. This report says the size is unavailable instead of
    printing a row count formatted as KiB.
    """
    size = next(na for na in table_not_applicable(DDB) if na.section == "Size")
    assert "row count" in size.reason
    assert all(isinstance(na, NotApplicable) for na in table_not_applicable(DDB))


def test_not_applicable_reasons_are_phrased_as_one_sentence_tail() -> None:
    """Rendered as "<Section> — it has no ...", so every reason starts alike."""
    every = [
        *table_not_applicable(DDB),
        *view_not_applicable(DDB),
        *schema_not_applicable(DDB),
    ]
    assert every
    for na in every:
        assert na.reason.startswith("it "), na
        assert not na.reason.endswith("."), na
        assert na.section and na.section[0].isupper(), na
