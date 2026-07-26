from __future__ import annotations

import pytest

from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.describe import (
    ColumnInfo,
    DependencyEdge,
    ForeignKeyInfo,
    IndexInfo,
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
    grants = fetch_schema_privileges(cursor, "public")
    assert PrivilegeGrant("analyst", "USAGE", False, "dbadmin") in grants


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
