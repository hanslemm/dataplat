"""``dataplat.services.db.describe`` executed against a live PostgreSQL.

Why this file exists: every other test of this module drives a hand-written
fake cursor, so it proves a fetcher *called* ``execute`` and unpacked the row
shape it was handed -- never that the ``pg_catalog`` SQL it builds is valid or
that the numbers coming back are true. Here the server is the judge. Every
assertion compares a fetcher's output either to a literal that the fixture DDL
guarantees, or to the server's own answer to the same question (``COUNT(*)``,
``pg_total_relation_size``), so a wrong-but-plausible query cannot pass.

Only the PostgreSQL branches are reachable from here; the Redshift branches
need a Redshift cluster and are recorded as an explicit gap in the report that
accompanies this file.

``psycopg`` is imported inside the helpers and tests that need it, never at
module level: it ships in the optional ``db`` extra, and a module-level import
would turn its absence into a collection error instead of the skip the harness
promises.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, LiteralString

import pytest

from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.describe import (
    ObjectKind,
    TargetNotFoundError,
    TargetRef,
    describe_schema,
    describe_table,
    describe_view,
    fetch_columns,
    fetch_constraints,
    fetch_dependencies,
    fetch_indexes,
    fetch_partitioning,
    fetch_policies,
    fetch_relation_header,
    fetch_relation_privileges,
    fetch_schema_contents,
    fetch_schema_default_privileges,
    fetch_schema_header,
    fetch_schema_privileges,
    fetch_triggers,
    fetch_view_definition,
    resolve_target,
)
from tests.integration.conftest import SAMPLE_RELATIONS, TempRoleFactory

if TYPE_CHECKING:
    from psycopg import Cursor
    from psycopg.rows import TupleRow

pytestmark = pytest.mark.integration

PG = SqlEngine.postgresql

# The label ``fetch_schema_contents`` must attach to each ``pg_class.relkind``
# that ``sample_schema`` promises. Spelled out here rather than imported from
# the service, so the test checks the mapping instead of agreeing with itself.
_EXPECTED_KIND_LABEL = {
    "r": "table",
    "v": "view",
    "m": "matview",
    "p": "partitioned table",
    "S": "sequence",
}


# --- helpers ---------------------------------------------------------------


def _ddl(cursor: Cursor[TupleRow], *statements: LiteralString, **idents: str) -> None:
    """Run extra fixture DDL, substituting ``{name}`` with a quoted identifier.

    Statements are ``LiteralString`` on purpose: identifiers arrive through
    psycopg's quoting, and anything the caller interpolated would bypass it.
    """
    from psycopg import sql

    quoted = {key: sql.Identifier(value) for key, value in idents.items()}
    for statement in statements:
        cursor.execute(sql.SQL(statement).format(**quoted))


def _scalar(
    cursor: Cursor[TupleRow], query: LiteralString, *params: Any, **idents: str
) -> Any:
    """Run a single-value query and return that value (None when no row).

    Accepts the same ``{name}`` identifier substitution as ``_ddl`` so a test
    can ask the server for ground truth about a uniquely-named fixture object.
    """
    from psycopg import sql

    quoted = {key: sql.Identifier(value) for key, value in idents.items()}
    cursor.execute(sql.SQL(query).format(**quoted), params or None)
    row = cursor.fetchone()
    return None if row is None else row[0]


def _count(cursor: Cursor[TupleRow], schema: str, relname: str) -> int:
    """Real row count of a fixture relation -- ground truth for estimates."""
    count = _scalar(cursor, "SELECT count(*)::bigint FROM {s}.{t}", s=schema, t=relname)
    assert isinstance(count, int)
    return count


def _ref(cursor: Cursor[TupleRow], schema: str, name: str | None = None) -> TargetRef:
    """Resolve ``schema`` or ``schema.name`` through the code under test."""
    target = schema if name is None else f"{schema}.{name}"
    return resolve_target(cursor, PG, target)


def _oid(cursor: Cursor[TupleRow], schema: str, name: str) -> int:
    oid = _ref(cursor, schema, name).oid
    assert oid is not None
    return oid


def _sibling_schema(schema: str) -> str:
    """A second, distinct schema name derived from a sample schema's name.

    Truncated so the suffix stays inside PostgreSQL's 63-byte identifier limit;
    the uniqueness-bearing prefix (counter + pid) survives the cut.
    """
    return f"{schema[:40]}_alt"


# --- resolve_target --------------------------------------------------------


def test_resolve_target_schema_reports_no_oid(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    assert _ref(pg_cursor, sample_schema) == TargetRef(
        kind=ObjectKind.schema, schema=sample_schema, name=None, oid=None
    )


@pytest.mark.parametrize(
    ("relname", "expected_kind"),
    [
        ("orgs", ObjectKind.table),
        ("customers", ObjectKind.table),
        ("events", ObjectKind.table),  # relkind 'p' folds into "table"
        ("events_2024", ObjectKind.table),
        ("active_customers", ObjectKind.view),
        ("customers_per_org", ObjectKind.matview),
    ],
)
def test_resolve_target_maps_relkind_and_returns_real_oid(
    pg_cursor: Cursor[TupleRow],
    sample_schema: str,
    relname: str,
    expected_kind: ObjectKind,
) -> None:
    ref = _ref(pg_cursor, sample_schema, relname)
    assert ref.kind is expected_kind
    assert (ref.schema, ref.name) == (sample_schema, relname)
    # The oid is the whole point of the lookup -- everything downstream keys
    # off it -- so compare against the server's own regclass resolution.
    assert ref.oid == _scalar(
        pg_cursor, "SELECT %s::regclass::oid", f"{sample_schema}.{relname}"
    )


@pytest.mark.parametrize(
    ("relname", "relkind"),
    [("invoice_number_seq", "S"), ("customers_org_id_idx", "i")],
)
def test_resolve_target_rejects_unsupported_relkinds(
    pg_cursor: Cursor[TupleRow], sample_schema: str, relname: str, relkind: str
) -> None:
    # These objects exist, so the pg_class lookup succeeds: this exercises the
    # kind_map miss, not the not-found path.
    with pytest.raises(TargetNotFoundError, match=f"unsupported kind '{relkind}'"):
        _ref(pg_cursor, sample_schema, relname)


def test_resolve_target_missing_schema(pg_cursor: Cursor[TupleRow]) -> None:
    with pytest.raises(TargetNotFoundError, match='schema "no_such_schema" not found'):
        _ref(pg_cursor, "no_such_schema")


def test_resolve_target_missing_relation_suggests_the_schema(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    with pytest.raises(TargetNotFoundError) as excinfo:
        _ref(pg_cursor, sample_schema, "nope")
    assert f"dp db describe {sample_schema}" in str(excinfo.value)


def test_resolve_target_is_schema_scoped(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    # Same relation name in two schemas: the namespace join must return the one
    # asked for, not whichever pg_class row the planner reaches first.
    other = _sibling_schema(sample_schema)
    _ddl(pg_cursor, "CREATE SCHEMA {a}", "CREATE TABLE {a}.customers (id int)", a=other)
    here = _ref(pg_cursor, sample_schema, "customers")
    there = _ref(pg_cursor, other, "customers")
    assert here.oid != there.oid
    assert [c.name for c in fetch_columns(pg_cursor, there.oid or 0, PG)] == ["id"]


# --- relation header -------------------------------------------------------


def test_relation_header_metadata_matches_catalog(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    header = fetch_relation_header(
        pg_cursor, _oid(pg_cursor, sample_schema, "customers"), PG
    )
    assert header.schema == sample_schema
    assert header.name == "customers"
    assert header.owner == _scalar(pg_cursor, "SELECT current_user")
    # No tablespace was named in the DDL, so reltablespace is 0 and the
    # COALESCE arm has to supply the cluster default.
    assert header.tablespace == "pg_default"
    assert header.comment == "Customer master (integration fixture)."


def test_relation_header_sizes_are_real_for_a_plain_table(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    # Regression guard. pg_partition_tree() returns NO rows for a relation that
    # is neither a partition nor partitioned, so the size aggregates collapsed
    # to 0 for every ordinary table -- i.e. for almost everything a user would
    # describe.
    qualified = f"{sample_schema}.customers"
    header = fetch_relation_header(
        pg_cursor, _oid(pg_cursor, sample_schema, "customers"), PG
    )
    assert header.total_size == _scalar(
        pg_cursor, "SELECT pg_total_relation_size(%s::regclass)", qualified
    )
    assert header.table_size == _scalar(
        pg_cursor, "SELECT pg_relation_size(%s::regclass)", qualified
    )
    assert header.index_size == _scalar(
        pg_cursor, "SELECT pg_indexes_size(%s::regclass)", qualified
    )
    assert header.total_size is not None and header.total_size > 0
    assert header.table_size is not None and header.table_size > 0
    assert header.index_size is not None and header.index_size > 0
    assert header.toast_size == (
        header.total_size - header.table_size - header.index_size
    )
    # RelationHeader annotates these as int, but SUM() over bigint yields
    # numeric, which psycopg hands back as Decimal unless the SQL casts.
    assert type(header.total_size) is int
    assert type(header.index_size) is int


def test_relation_header_row_estimate_matches_an_analyzed_table(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    # sample_schema ANALYZEs its tables, so the estimate is exact here and the
    # comparison against COUNT(*) is meaningful rather than approximate.
    header = fetch_relation_header(
        pg_cursor, _oid(pg_cursor, sample_schema, "customers"), PG
    )
    assert header.row_estimate == _count(pg_cursor, sample_schema, "customers")


def test_relation_header_partitioned_parent_aggregates_its_partitions(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    header = fetch_relation_header(
        pg_cursor, _oid(pg_cursor, sample_schema, "events"), PG
    )
    # The parent has no storage of its own, so every reported byte must come
    # from walking the partition tree.
    assert (
        _scalar(
            pg_cursor,
            "SELECT pg_total_relation_size(%s::regclass)",
            f"{sample_schema}.events",
        )
        == 0
    )
    assert header.total_size == _scalar(
        pg_cursor,
        "SELECT SUM(pg_total_relation_size(c.oid))::bigint FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = %s AND c.relname IN ('events_2024', 'events_2025')",
        sample_schema,
    )
    assert header.total_size is not None and header.total_size > 0
    # Regression guard: pg_class.reltuples on an analyzed parent already
    # aggregates its partitions, so summing parent + children double-counted
    # every row (30 real rows were reported as 60).
    assert header.row_estimate == _count(pg_cursor, sample_schema, "events")


def test_relation_header_of_matview_and_view(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    matview = fetch_relation_header(
        pg_cursor, _oid(pg_cursor, sample_schema, "customers_per_org"), PG
    )
    # A populated matview has heap storage...
    assert matview.table_size is not None and matview.table_size > 0
    # ...but was never ANALYZEd, so its reltuples is -1 ("unknown") and that
    # sentinel must not leak out as a row count.
    assert matview.row_estimate is None

    view = fetch_relation_header(
        pg_cursor, _oid(pg_cursor, sample_schema, "active_customers"), PG
    )
    assert view.total_size == 0
    assert view.row_estimate is None
    assert view.comment == "Non-churned customers."


def test_relation_header_of_a_partition_child_counts_only_itself(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    # Walking the tree from a leaf must not climb to the parent: if it did,
    # this child would also report its sibling's bytes and rows.
    header = fetch_relation_header(
        pg_cursor, _oid(pg_cursor, sample_schema, "events_2024"), PG
    )
    assert header.total_size == _scalar(
        pg_cursor,
        "SELECT pg_total_relation_size(%s::regclass)",
        f"{sample_schema}.events_2024",
    )
    assert header.row_estimate == _count(pg_cursor, sample_schema, "events_2024")


def test_relation_header_of_an_unanalyzed_table_sizes_without_rows(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    _ddl(
        pg_cursor,
        "CREATE TABLE {s}.fresh (a integer)",
        "INSERT INTO {s}.fresh SELECT generate_series(1, 100)",
        s=sample_schema,
    )
    header = fetch_relation_header(
        pg_cursor, _oid(pg_cursor, sample_schema, "fresh"), PG
    )
    # reltuples stays -1 until ANALYZE, and -1 must read as "unknown"...
    assert header.row_estimate is None
    # ...while the byte counts are known regardless of statistics.
    assert header.table_size is not None and header.table_size > 0


def test_relation_header_reports_new_owner_after_alter(
    pg_cursor: Cursor[TupleRow], sample_schema: str, temp_role: TempRoleFactory
) -> None:
    role = temp_role("owner")
    _ddl(pg_cursor, "ALTER TABLE {s}.orgs OWNER TO {r}", s=sample_schema, r=role)
    header = fetch_relation_header(
        pg_cursor, _oid(pg_cursor, sample_schema, "orgs"), PG
    )
    assert header.owner == role


def test_relation_header_unknown_oid_raises(pg_cursor: Cursor[TupleRow]) -> None:
    free_oid = 2147483000
    assert (
        _scalar(pg_cursor, "SELECT count(*) FROM pg_class WHERE oid = %s", free_oid)
        == 0
    )
    with pytest.raises(TargetNotFoundError, match="not found"):
        fetch_relation_header(pg_cursor, free_oid, PG)


# --- columns ---------------------------------------------------------------


def test_columns_of_customers_match_the_ddl(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    columns = fetch_columns(pg_cursor, _oid(pg_cursor, sample_schema, "customers"), PG)
    shape = [
        (c.ordinal, c.name, c.data_type, c.nullable, c.default, c.is_primary_key)
        for c in columns
    ]
    assert shape == [
        (1, "id", "bigint", False, "GENERATED BY DEFAULT AS IDENTITY", True),
        (2, "org_id", "integer", False, None, False),
        (3, "email", "text", False, None, False),
        (4, "status", "text", False, "'active'::text", False),
        (5, "lifetime_value", "numeric(12,2)", True, "0", False),
        (6, "created_at", "timestamp with time zone", False, "now()", False),
        (7, "updated_at", "timestamp with time zone", True, None, False),
    ]
    by_name = {c.name: c for c in columns}
    assert by_name["email"].comment == "Primary contact address."
    assert by_name["id"].comment is None
    # PostgreSQL has no column encodings; that field is Redshift-only.
    assert all(c.encoding is None for c in columns)


def test_columns_report_foreign_key_target(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    columns = fetch_columns(pg_cursor, _oid(pg_cursor, sample_schema, "customers"), PG)
    org_id = next(c for c in columns if c.name == "org_id")
    assert org_id.fk_target_table == f"{sample_schema}.orgs"
    assert org_id.fk_target_column == "id"
    # Non-FK columns must not inherit the lateral's row.
    assert all(c.fk_target_table is None for c in columns if c.name != "org_id")


def test_columns_pair_each_fk_column_with_its_own_target(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    # The FK column order is deliberately the reverse of the referenced order,
    # so a query that pairs by position instead of by array_position() gets it
    # wrong in a way a same-order fixture would hide.
    _ddl(
        pg_cursor,
        "CREATE TABLE {s}.pair_parent (x integer, y integer, UNIQUE (x, y))",
        """
        CREATE TABLE {s}.pair_child (
            b integer,
            a integer,
            FOREIGN KEY (b, a) REFERENCES {s}.pair_parent (y, x)
        )
        """,
        s=sample_schema,
    )
    columns = fetch_columns(pg_cursor, _oid(pg_cursor, sample_schema, "pair_child"), PG)
    assert [(c.name, c.fk_target_column) for c in columns] == [("b", "y"), ("a", "x")]
    assert {c.fk_target_table for c in columns} == {f"{sample_schema}.pair_parent"}


def test_columns_flag_every_member_of_a_composite_primary_key(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    columns = fetch_columns(pg_cursor, _oid(pg_cursor, sample_schema, "events"), PG)
    assert [(c.name, c.is_primary_key) for c in columns] == [
        ("event_id", True),
        ("occurred_at", True),
        ("kind", False),
        ("payload", False),
    ]


def test_columns_identity_and_serial_defaults(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    _ddl(
        pg_cursor,
        """
        CREATE TABLE {s}.idents (
            always_id bigint GENERATED ALWAYS AS IDENTITY,
            legacy_id bigserial,
            plain integer DEFAULT 7
        )
        """,
        s=sample_schema,
    )
    defaults = {
        c.name: c.default
        for c in fetch_columns(pg_cursor, _oid(pg_cursor, sample_schema, "idents"), PG)
    }
    assert defaults["always_id"] == "GENERATED ALWAYS AS IDENTITY"
    # A serial is a plain nextval() default, not an identity column, and the
    # CASE has to fall through to pg_get_expr for it.
    assert defaults["legacy_id"] is not None
    assert defaults["legacy_id"].startswith("nextval(")
    assert defaults["plain"] == "7"


def test_columns_of_a_generated_stored_column_show_the_expression(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    # Recorded gap, not an endorsement: identity columns get a "GENERATED ..."
    # label but a STORED generated column surfaces only its raw expression, so
    # the report renders it in the Default column, indistinguishable from a
    # plain DEFAULT. Pinned here so a future fix has to update this test
    # deliberately.
    _ddl(
        pg_cursor,
        """
        CREATE TABLE {s}.gen (
            price numeric,
            qty integer,
            total numeric GENERATED ALWAYS AS (price * qty) STORED
        )
        """,
        s=sample_schema,
    )
    total = next(
        c
        for c in fetch_columns(pg_cursor, _oid(pg_cursor, sample_schema, "gen"), PG)
        if c.name == "total"
    )
    assert total.default == "(price * (qty)::numeric)"


def test_columns_of_a_view_are_nullable_and_defaultless(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    columns = fetch_columns(
        pg_cursor, _oid(pg_cursor, sample_schema, "active_customers"), PG
    )
    assert [c.name for c in columns] == ["id", "org_id", "email", "lifetime_value"]
    assert all(c.nullable for c in columns)
    assert all(c.default is None for c in columns)
    assert all(not c.is_primary_key for c in columns)


def test_columns_skip_dropped_columns(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    _ddl(
        pg_cursor,
        "CREATE TABLE {s}.dropped (a integer, b text, c date)",
        "ALTER TABLE {s}.dropped DROP COLUMN b",
        s=sample_schema,
    )
    columns = fetch_columns(pg_cursor, _oid(pg_cursor, sample_schema, "dropped"), PG)
    # attnum is not renumbered by DROP COLUMN, so the surviving ordinals keep
    # the hole. What matters is that the dropped column is gone entirely.
    assert [(c.ordinal, c.name) for c in columns] == [(1, "a"), (3, "c")]


# --- constraints -----------------------------------------------------------


def test_constraints_of_customers(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    bundle = fetch_constraints(
        pg_cursor, _oid(pg_cursor, sample_schema, "customers"), PG
    )
    assert bundle.primary_key is not None
    assert bundle.primary_key.name == "customers_pkey"
    assert bundle.primary_key.columns == ["id"]

    assert len(bundle.foreign_keys) == 1
    fk = bundle.foreign_keys[0]
    assert fk.name == "customers_org_id_fkey"
    assert fk.columns == ["org_id"]
    assert fk.referenced_table == f"{sample_schema}.orgs"
    assert fk.referenced_columns == ["id"]
    assert fk.on_delete == "CASCADE"
    assert fk.on_update == "RESTRICT"
    assert fk.deferrable is False

    assert [c.name for c in bundle.check_constraints] == ["customers_status_check"]
    definition = bundle.check_constraints[0].definition
    assert definition.startswith("CHECK (")
    assert "status" in definition and "churned" in definition
    assert bundle.unique_constraints == []


def test_constraints_report_unique_constraint(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    bundle = fetch_constraints(pg_cursor, _oid(pg_cursor, sample_schema, "orgs"), PG)
    assert [(c.name, c.definition) for c in bundle.unique_constraints] == [
        ("orgs_name_key", "UNIQUE (name)")
    ]
    # The unique constraint must not also be reported as a check.
    assert bundle.check_constraints == []


def test_constraints_map_every_referential_action(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    _ddl(
        pg_cursor,
        """
        CREATE TABLE {s}.actions (
            set_null integer REFERENCES {s}.orgs (id)
                ON DELETE SET NULL ON UPDATE CASCADE,
            set_default integer DEFAULT 1 REFERENCES {s}.orgs (id)
                ON DELETE SET DEFAULT ON UPDATE NO ACTION,
            deferred integer REFERENCES {s}.orgs (id)
                DEFERRABLE INITIALLY DEFERRED
        )
        """,
        s=sample_schema,
    )
    fks = fetch_constraints(
        pg_cursor, _oid(pg_cursor, sample_schema, "actions"), PG
    ).foreign_keys
    by_column = {fk.columns[0]: fk for fk in fks}
    assert (by_column["set_null"].on_delete, by_column["set_null"].on_update) == (
        "SET NULL",
        "CASCADE",
    )
    assert (by_column["set_default"].on_delete, by_column["set_default"].on_update) == (
        "SET DEFAULT",
        "NO ACTION",
    )
    # An omitted action is 'a' in confdeltype, which must read as NO ACTION and
    # not be mistaken for a missing value.
    assert by_column["deferred"].on_delete == "NO ACTION"
    assert by_column["deferred"].deferrable is True
    assert by_column["set_null"].deferrable is False


def test_constraints_preserve_composite_column_order(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    _ddl(
        pg_cursor,
        "CREATE TABLE {s}.pair_parent (x integer, y integer, UNIQUE (x, y))",
        """
        CREATE TABLE {s}.pair_child (
            b integer,
            a integer,
            CONSTRAINT pair_child_fk
                FOREIGN KEY (b, a) REFERENCES {s}.pair_parent (y, x)
        )
        """,
        s=sample_schema,
    )
    fk = fetch_constraints(
        pg_cursor, _oid(pg_cursor, sample_schema, "pair_child"), PG
    ).foreign_keys[0]
    # conkey and confkey are parallel arrays; unnesting either one WITHOUT
    # ORDINALITY would silently sort them by attnum instead.
    assert fk.columns == ["b", "a"]
    assert fk.referenced_columns == ["y", "x"]


def test_constraints_are_ordered_pk_unique_fk_check(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    _ddl(
        pg_cursor,
        """
        CREATE TABLE {s}.mixed (
            id integer PRIMARY KEY,
            code text,
            org_id integer REFERENCES {s}.orgs (id),
            CONSTRAINT mixed_code_key UNIQUE (code),
            CONSTRAINT mixed_id_positive CHECK (id > 0)
        )
        """,
        s=sample_schema,
    )
    bundle = fetch_constraints(pg_cursor, _oid(pg_cursor, sample_schema, "mixed"), PG)
    assert bundle.primary_key is not None
    assert [c.name for c in bundle.unique_constraints] == ["mixed_code_key"]
    assert [c.name for c in bundle.foreign_keys] == ["mixed_org_id_fkey"]
    assert [c.name for c in bundle.check_constraints] == ["mixed_id_positive"]


def test_constraints_empty_for_a_bare_table(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    _ddl(pg_cursor, "CREATE TABLE {s}.bare (a integer, b text)", s=sample_schema)
    bundle = fetch_constraints(pg_cursor, _oid(pg_cursor, sample_schema, "bare"), PG)
    assert bundle.primary_key is None
    assert (bundle.foreign_keys, bundle.unique_constraints) == ([], [])
    assert bundle.check_constraints == []


# --- indexes ---------------------------------------------------------------


def test_indexes_of_customers(pg_cursor: Cursor[TupleRow], sample_schema: str) -> None:
    indexes = fetch_indexes(pg_cursor, _oid(pg_cursor, sample_schema, "customers"), PG)
    assert [i.name for i in indexes] == [
        # ORDER BY indisprimary DESC, indisunique DESC, relname
        "customers_pkey",
        "customers_active_email_idx",
        "customers_org_id_idx",
    ]
    pkey, partial, plain = indexes
    assert (pkey.unique, pkey.primary, pkey.columns) == (True, True, ["id"])
    assert pkey.predicate is None
    assert (partial.unique, partial.primary, partial.columns) == (
        False,
        False,
        ["email"],
    )
    # pg_get_expr on indpred: the whole point of the partial index in the
    # fixture.
    assert partial.predicate == "status = 'active'::text"
    assert (plain.columns, plain.predicate) == (["org_id"], None)
    assert {i.method for i in indexes} == {"btree"}


def test_index_sizes_are_real(pg_cursor: Cursor[TupleRow], sample_schema: str) -> None:
    # Regression guard, same root cause as the relation header: an index that is
    # neither partitioned nor a partition has an empty pg_partition_tree(), so
    # every ordinary index reported 0 bytes.
    indexes = fetch_indexes(pg_cursor, _oid(pg_cursor, sample_schema, "customers"), PG)
    for index in indexes:
        assert index.size_bytes == _scalar(
            pg_cursor,
            "SELECT pg_relation_size(%s::regclass)",
            f"{sample_schema}.{index.name}",
        )
        assert index.size_bytes is not None and index.size_bytes > 0
        assert type(index.size_bytes) is int


def test_indexes_report_unique_non_primary(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    # orgs has both a PK index and the index backing UNIQUE (name).
    indexes = {
        i.name: i
        for i in fetch_indexes(pg_cursor, _oid(pg_cursor, sample_schema, "orgs"), PG)
    }
    assert indexes["orgs_name_key"].unique is True
    assert indexes["orgs_name_key"].primary is False
    assert indexes["orgs_name_key"].columns == ["name"]


def test_indexes_report_access_method_and_expressions(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    _ddl(
        pg_cursor,
        "CREATE INDEX events_payload_gin ON {s}.events_2024 USING gin (payload)",
        "CREATE INDEX events_kind_hash ON {s}.events_2024 USING hash (kind)",
        "CREATE INDEX events_lower_kind ON {s}.events_2024 (lower(kind))",
        "CREATE INDEX events_multi ON {s}.events_2024 (kind, occurred_at DESC)",
        s=sample_schema,
    )
    indexes = {
        i.name: i
        for i in fetch_indexes(
            pg_cursor, _oid(pg_cursor, sample_schema, "events_2024"), PG
        )
    }
    assert indexes["events_payload_gin"].method == "gin"
    assert indexes["events_kind_hash"].method == "hash"
    # pg_get_indexdef(oid, colno, true) renders an expression key as its
    # expression text.
    assert indexes["events_lower_kind"].columns == ["lower(kind)"]
    # Recorded gap: the per-column form drops the ordering options, so a DESC
    # key is indistinguishable from ASC in the report.
    assert indexes["events_multi"].columns == ["kind", "occurred_at"]


def test_indexes_of_a_partitioned_parent_sum_their_partitions(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    parent = fetch_indexes(pg_cursor, _oid(pg_cursor, sample_schema, "events"), PG)
    assert [i.name for i in parent] == ["events_pkey"]
    # The parent index is storage-free; its reported size has to come from the
    # partitioned index tree.
    assert (
        _scalar(
            pg_cursor,
            "SELECT pg_relation_size(%s::regclass)",
            f"{sample_schema}.events_pkey",
        )
        == 0
    )
    assert parent[0].size_bytes == _scalar(
        pg_cursor,
        "SELECT SUM(pg_relation_size(p.relid))::bigint"
        " FROM pg_partition_tree(%s::regclass) p",
        f"{sample_schema}.events_pkey",
    )
    assert parent[0].size_bytes is not None and parent[0].size_bytes > 0
    assert parent[0].columns == ["event_id", "occurred_at"]


def test_indexes_empty_for_a_table_without_any(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    _ddl(pg_cursor, "CREATE TABLE {s}.noidx (a integer)", s=sample_schema)
    assert fetch_indexes(pg_cursor, _oid(pg_cursor, sample_schema, "noidx"), PG) == []


# --- views, matviews, dependency graph -------------------------------------


def test_view_definition_of_a_simple_view(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    definition = fetch_view_definition(
        pg_cursor, _oid(pg_cursor, sample_schema, "active_customers"), PG
    )
    # pg_get_viewdef returns the parsed-and-deparsed form, so assert on the
    # structure the server is guaranteed to emit, not on the fixture's layout.
    assert "SELECT" in definition.sql
    assert f"FROM {sample_schema}.customers" in definition.sql
    assert "status = 'active'::text" in definition.sql
    # A single-table projection with no aggregate is auto-updatable.
    assert definition.is_updatable is True
    # information_schema reports check_option NONE here, which the fetcher must
    # normalise to None so the CLI omits the row.
    assert definition.check_option is None


def test_view_definition_reports_check_option(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    _ddl(
        pg_cursor,
        """
        CREATE VIEW {s}.checked AS
        SELECT id, status FROM {s}.customers WHERE status = 'active'
        WITH CASCADED CHECK OPTION
        """,
        s=sample_schema,
    )
    definition = fetch_view_definition(
        pg_cursor, _oid(pg_cursor, sample_schema, "checked"), PG
    )
    assert definition.check_option == "CASCADED"
    assert definition.is_updatable is True


def test_view_definition_marks_aggregate_view_not_updatable(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    _ddl(
        pg_cursor,
        "CREATE VIEW {s}.per_status AS"
        " SELECT status, count(*) AS n FROM {s}.customers GROUP BY status",
        s=sample_schema,
    )
    definition = fetch_view_definition(
        pg_cursor, _oid(pg_cursor, sample_schema, "per_status"), PG
    )
    assert definition.is_updatable is False
    assert "count(*)" in definition.sql


def test_view_definition_of_a_matview(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    definition = fetch_view_definition(
        pg_cursor, _oid(pg_cursor, sample_schema, "customers_per_org"), PG
    )
    assert "GROUP BY org_id" in definition.sql
    # information_schema.views has no row for a materialized view, so both
    # scalar subqueries return NULL and must degrade to these values.
    assert definition.is_updatable is False
    assert definition.check_option is None


@pytest.mark.xfail(
    strict=True,
    reason=(
        "pg_get_viewdef() returns NULL for a non-view oid, and unlike the "
        "Redshift branch the PostgreSQL branch has no None guard: it returns "
        "ViewDefinition(sql=None), violating its own `sql: str` annotation. "
        "Not fixed here -- see report."
    ),
)
def test_view_definition_on_a_table_should_raise(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    with pytest.raises(TargetNotFoundError):
        fetch_view_definition(
            pg_cursor, _oid(pg_cursor, sample_schema, "customers"), PG
        )


def test_dependencies_upstream_of_a_view(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    edges = fetch_dependencies(
        pg_cursor,
        _oid(pg_cursor, sample_schema, "active_customers"),
        direction="upstream",
        engine=PG,
    )
    # Every view's rewrite rule also depends on the view itself; the query has
    # to exclude that self-edge.
    assert [(e.qualified_name, e.kind) for e in edges] == [
        (f"{sample_schema}.customers", "table")
    ]


def test_dependencies_downstream_of_a_table(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    edges = fetch_dependencies(
        pg_cursor,
        _oid(pg_cursor, sample_schema, "customers"),
        direction="downstream",
        engine=PG,
    )
    assert [(e.qualified_name, e.kind) for e in edges] == [
        (f"{sample_schema}.active_customers", "view"),
        (f"{sample_schema}.customers_per_org", "matview"),
    ]


def test_dependencies_are_one_hop(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    # A view on a view: pg_rewrite records only the direct reference, so the
    # graph is one hop per call and the CLI walks it.
    _ddl(
        pg_cursor,
        "CREATE VIEW {s}.acme AS SELECT * FROM {s}.active_customers WHERE org_id = 1",
        s=sample_schema,
    )
    upstream = fetch_dependencies(
        pg_cursor,
        _oid(pg_cursor, sample_schema, "acme"),
        direction="upstream",
        engine=PG,
    )
    assert [e.qualified_name for e in upstream] == [f"{sample_schema}.active_customers"]
    downstream = fetch_dependencies(
        pg_cursor,
        _oid(pg_cursor, sample_schema, "active_customers"),
        direction="downstream",
        engine=PG,
    )
    assert [e.qualified_name for e in downstream] == [f"{sample_schema}.acme"]


def test_dependencies_of_a_plain_table_are_empty_upstream(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    assert (
        fetch_dependencies(
            pg_cursor,
            _oid(pg_cursor, sample_schema, "orgs"),
            direction="upstream",
            engine=PG,
        )
        == []
    )


def test_dependencies_rejects_unknown_direction(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    with pytest.raises(ValueError, match="direction must be"):
        fetch_dependencies(
            pg_cursor,
            _oid(pg_cursor, sample_schema, "orgs"),
            direction="sideways",
            engine=PG,
        )


# --- partitioning ----------------------------------------------------------


def test_partitioning_of_a_range_parent(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    info = fetch_partitioning(pg_cursor, _oid(pg_cursor, sample_schema, "events"), PG)
    assert info.parent is None
    assert info.strategy == "RANGE"
    assert info.partition_key == "RANGE (occurred_at)"
    assert info.children == [
        (
            f"{sample_schema}.events_2024",
            "FOR VALUES FROM ('2024-01-01') TO ('2025-01-01')",
        ),
        (
            f"{sample_schema}.events_2025",
            "FOR VALUES FROM ('2025-01-01') TO ('2026-01-01')",
        ),
    ]


def test_partitioning_of_a_child_names_its_parent(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    info = fetch_partitioning(
        pg_cursor, _oid(pg_cursor, sample_schema, "events_2024"), PG
    )
    assert info.parent == f"{sample_schema}.events"
    # A leaf is not itself partitioned.
    assert (info.strategy, info.partition_key, info.children) == (None, None, [])


def test_partitioning_of_a_plain_table_is_empty(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    info = fetch_partitioning(
        pg_cursor, _oid(pg_cursor, sample_schema, "customers"), PG
    )
    assert info.parent is None
    assert info.strategy is None
    # pg_get_partkeydef() returns NULL rather than erroring for a plain table.
    assert info.partition_key is None
    assert info.children == []


def test_partitioning_list_strategy_and_default_partition(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    _ddl(
        pg_cursor,
        "CREATE TABLE {s}.by_list (id integer, region text) PARTITION BY LIST (region)",
        "CREATE TABLE {s}.by_list_eu PARTITION OF {s}.by_list"
        " FOR VALUES IN ('eu','uk')",
        "CREATE TABLE {s}.by_list_rest PARTITION OF {s}.by_list DEFAULT",
        s=sample_schema,
    )
    info = fetch_partitioning(pg_cursor, _oid(pg_cursor, sample_schema, "by_list"), PG)
    assert info.strategy == "LIST"
    assert info.partition_key == "LIST (region)"
    assert info.children == [
        (f"{sample_schema}.by_list_eu", "FOR VALUES IN ('eu', 'uk')"),
        (f"{sample_schema}.by_list_rest", "DEFAULT"),
    ]


def test_partitioning_hash_strategy(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    _ddl(
        pg_cursor,
        "CREATE TABLE {s}.by_hash (id integer) PARTITION BY HASH (id)",
        "CREATE TABLE {s}.by_hash_0 PARTITION OF {s}.by_hash"
        " FOR VALUES WITH (MODULUS 2, REMAINDER 0)",
        s=sample_schema,
    )
    info = fetch_partitioning(pg_cursor, _oid(pg_cursor, sample_schema, "by_hash"), PG)
    assert info.strategy == "HASH"
    assert info.partition_key == "HASH (id)"
    assert info.children == [
        (f"{sample_schema}.by_hash_0", "FOR VALUES WITH (modulus 2, remainder 0)")
    ]


def test_partitioning_of_a_subpartitioned_level(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    # A middle level is both a partition and partitioned; every field has to be
    # populated at once.
    _ddl(
        pg_cursor,
        "CREATE TABLE {s}.multi (a integer, b date) PARTITION BY RANGE (a)",
        "CREATE TABLE {s}.multi_lo PARTITION OF {s}.multi"
        " FOR VALUES FROM (0) TO (10) PARTITION BY RANGE (b)",
        "CREATE TABLE {s}.multi_lo_2024 PARTITION OF {s}.multi_lo"
        " FOR VALUES FROM ('2024-01-01') TO ('2025-01-01')",
        s=sample_schema,
    )
    info = fetch_partitioning(pg_cursor, _oid(pg_cursor, sample_schema, "multi_lo"), PG)
    assert info.parent == f"{sample_schema}.multi"
    assert info.strategy == "RANGE"
    assert info.partition_key == "RANGE (b)"
    assert info.children == [
        (
            f"{sample_schema}.multi_lo_2024",
            "FOR VALUES FROM ('2024-01-01') TO ('2025-01-01')",
        )
    ]


# --- triggers --------------------------------------------------------------


def test_triggers_of_customers(pg_cursor: Cursor[TupleRow], sample_schema: str) -> None:
    triggers = fetch_triggers(
        pg_cursor, _oid(pg_cursor, sample_schema, "customers"), PG
    )
    assert len(triggers) == 1
    trigger = triggers[0]
    assert trigger.name == "customers_touch_updated_at"
    assert trigger.timing == "BEFORE"
    assert trigger.events == "UPDATE"
    # The field is named `function` but holds the full pg_get_triggerdef text.
    assert trigger.function.startswith("CREATE TRIGGER customers_touch_updated_at")
    assert f"{sample_schema}.touch_updated_at()" in trigger.function


def test_triggers_exclude_internal_constraint_triggers(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    # customers has a foreign key, so PostgreSQL created internal RI triggers
    # on both ends. They must not show up as user triggers.
    total = _scalar(
        pg_cursor,
        "SELECT count(*) FROM pg_trigger WHERE tgrelid = %s::regclass",
        f"{sample_schema}.customers",
    )
    assert total > 1
    assert (
        len(fetch_triggers(pg_cursor, _oid(pg_cursor, sample_schema, "customers"), PG))
        == 1
    )
    # The FK target only has internal triggers, so its list is empty.
    assert fetch_triggers(pg_cursor, _oid(pg_cursor, sample_schema, "orgs"), PG) == []


def test_triggers_decode_every_event_bit(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    _ddl(
        pg_cursor,
        "CREATE FUNCTION {s}.noop() RETURNS trigger LANGUAGE plpgsql AS"
        " 'BEGIN RETURN NULL; END;'",
        "CREATE TABLE {s}.trg (a integer)",
        "CREATE TRIGGER trg_all AFTER INSERT OR DELETE OR UPDATE OR TRUNCATE"
        " ON {s}.trg FOR EACH STATEMENT EXECUTE FUNCTION {s}.noop()",
        s=sample_schema,
    )
    trigger = fetch_triggers(pg_cursor, _oid(pg_cursor, sample_schema, "trg"), PG)[0]
    assert trigger.timing == "AFTER"
    assert trigger.events == "INSERT OR DELETE OR UPDATE OR TRUNCATE"


def test_triggers_report_instead_of_on_a_view(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    # tgtype for INSTEAD OF has the BEFORE bit clear and bit 64 set; the CASE
    # tests BEFORE first, so this is the branch that would be misreported.
    _ddl(
        pg_cursor,
        "CREATE FUNCTION {s}.noop() RETURNS trigger LANGUAGE plpgsql AS"
        " 'BEGIN RETURN NEW; END;'",
        "CREATE TRIGGER acme_ins INSTEAD OF INSERT ON {s}.active_customers"
        " FOR EACH ROW EXECUTE FUNCTION {s}.noop()",
        s=sample_schema,
    )
    trigger = fetch_triggers(
        pg_cursor, _oid(pg_cursor, sample_schema, "active_customers"), PG
    )[0]
    assert trigger.timing == "INSTEAD OF"
    assert trigger.events == "INSERT"


# --- row level security ----------------------------------------------------


def test_policies_of_the_rls_table(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    policies, enabled = fetch_policies(
        pg_cursor, _oid(pg_cursor, sample_schema, "secrets"), PG
    )
    assert enabled is True
    assert len(policies) == 1
    policy = policies[0]
    assert policy.name == "secrets_owner_only"
    assert policy.command == "ALL"
    # polroles is {0} for TO PUBLIC, which matches no pg_roles row, so the
    # COALESCE has to supply the label.
    assert policy.roles == ["public"]
    assert policy.using == "owner_role = CURRENT_USER"
    assert policy.with_check == "owner_role = CURRENT_USER"


def test_policies_absent_when_rls_is_off(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    policies, enabled = fetch_policies(
        pg_cursor, _oid(pg_cursor, sample_schema, "customers"), PG
    )
    assert (policies, enabled) == ([], False)


def test_policies_map_commands_and_named_roles(
    pg_cursor: Cursor[TupleRow], sample_schema: str, temp_role: TempRoleFactory
) -> None:
    role = temp_role("policy")
    _ddl(
        pg_cursor,
        "CREATE POLICY p_select ON {s}.secrets FOR SELECT TO {r} USING (true)",
        "CREATE POLICY p_insert ON {s}.secrets FOR INSERT TO {r}"
        " WITH CHECK (body <> '')",
        "CREATE POLICY p_update ON {s}.secrets FOR UPDATE TO {r} USING (true)",
        "CREATE POLICY p_delete ON {s}.secrets FOR DELETE TO {r} USING (true)",
        s=sample_schema,
        r=role,
    )
    policies = {
        p.name: p
        for p in fetch_policies(
            pg_cursor, _oid(pg_cursor, sample_schema, "secrets"), PG
        )[0]
    }
    assert policies["p_select"].command == "SELECT"
    assert policies["p_insert"].command == "INSERT"
    assert policies["p_update"].command == "UPDATE"
    assert policies["p_delete"].command == "DELETE"
    assert policies["p_select"].roles == [role]
    # A FOR INSERT policy has no USING clause at all, so polqual is NULL.
    assert policies["p_insert"].using is None
    assert policies["p_insert"].with_check == "body <> ''::text"
    assert policies["p_select"].with_check is None


# --- privileges ------------------------------------------------------------


def test_relation_privileges_include_an_owner_row(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    grants = fetch_relation_privileges(pg_cursor, sample_schema, "customers")
    owner = _scalar(pg_cursor, "SELECT current_user")
    owner_rows = [g for g in grants if g.privilege == "OWNER"]
    assert len(owner_rows) == 1
    assert owner_rows[0].grantee == owner
    assert owner_rows[0].grantor == ""
    # The owner also holds every table privilege WITH GRANT OPTION.
    assert {g.privilege for g in grants if g.grantee == owner} >= {
        "OWNER",
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
        "TRUNCATE",
        "REFERENCES",
        "TRIGGER",
    }


def test_relation_privileges_reflect_grants_and_revokes(
    pg_cursor: Cursor[TupleRow], sample_schema: str, temp_role: TempRoleFactory
) -> None:
    role = temp_role("grantee")
    _ddl(
        pg_cursor,
        "GRANT SELECT ON {s}.customers TO {r}",
        "GRANT UPDATE ON {s}.customers TO {r} WITH GRANT OPTION",
        "GRANT SELECT ON {s}.customers TO PUBLIC",
        s=sample_schema,
        r=role,
    )
    grants = fetch_relation_privileges(pg_cursor, sample_schema, "customers")
    mine = {(g.privilege, g.with_grant_option) for g in grants if g.grantee == role}
    assert ("SELECT", False) in mine
    assert ("UPDATE", True) in mine
    assert any(g.grantee == "PUBLIC" and g.privilege == "SELECT" for g in grants)
    assert all(g.grantor for g in grants if g.privilege != "OWNER")

    _ddl(pg_cursor, "REVOKE SELECT ON {s}.customers FROM {r}", s=sample_schema, r=role)
    after = fetch_relation_privileges(pg_cursor, sample_schema, "customers")
    assert {g.privilege for g in after if g.grantee == role} == {"UPDATE"}


def test_relation_privileges_are_grouped_and_sorted_per_grantee(
    pg_cursor: Cursor[TupleRow], sample_schema: str, temp_role: TempRoleFactory
) -> None:
    _ddl(
        pg_cursor,
        "GRANT INSERT, SELECT ON {s}.customers TO {r}",
        s=sample_schema,
        r=temp_role("ordering"),
    )
    grants = fetch_relation_privileges(pg_cursor, sample_schema, "customers")
    # ORDER BY grantee, privilege has to survive the UNION ALL that splices in
    # the OWNER row. Asserted as "one contiguous block per grantee, privileges
    # ascending inside it": the database collates grantee names by its own
    # locale, so comparing the whole key list against Python's sort order would
    # be testing glibc, not the query.
    blocks: list[tuple[str, list[str]]] = []
    for grant in grants:
        if blocks and blocks[-1][0] == grant.grantee:
            blocks[-1][1].append(grant.privilege)
        else:
            blocks.append((grant.grantee, [grant.privilege]))
    grantees = [grantee for grantee, _ in blocks]
    assert len(grantees) == len(set(grantees))
    for _, privileges in blocks:
        assert privileges == sorted(privileges)


def test_schema_privileges_report_a_create_grant(
    pg_cursor: Cursor[TupleRow], sample_schema: str, temp_role: TempRoleFactory
) -> None:
    role = temp_role("creator")
    before = fetch_schema_privileges(pg_cursor, sample_schema)
    assert role not in {g.grantee for g in before}
    # The owner always has CREATE, so the fetcher must already return a row.
    assert any(
        g.privilege == "CREATE"
        and g.grantee == _scalar(pg_cursor, "SELECT current_user")
        for g in before
    )

    _ddl(pg_cursor, "GRANT CREATE ON SCHEMA {s} TO {r}", s=sample_schema, r=role)
    after = fetch_schema_privileges(pg_cursor, sample_schema)
    assert ("CREATE", role) in {(g.privilege, g.grantee) for g in after}


@pytest.mark.xfail(
    strict=True,
    reason=(
        "information_schema.usage_privileges never contains object_type "
        "'SCHEMA' on PostgreSQL (only DOMAIN/COLLATION/FDW/server/sequence), "
        "so the USAGE half of the UNION always returns zero rows and a "
        "GRANT USAGE ON SCHEMA is invisible in the report. The fetcher takes no "
        "engine argument, so the SQL is shared with Redshift and is not fixed "
        "here -- see report."
    ),
)
def test_schema_privileges_should_report_a_usage_grant(
    pg_cursor: Cursor[TupleRow], sample_schema: str, temp_role: TempRoleFactory
) -> None:
    role = temp_role("usage")
    _ddl(pg_cursor, "GRANT USAGE ON SCHEMA {s} TO {r}", s=sample_schema, r=role)
    # The grant is really there...
    assert _scalar(
        pg_cursor, "SELECT has_schema_privilege(%s, %s, 'USAGE')", role, sample_schema
    )
    grants = fetch_schema_privileges(pg_cursor, sample_schema)
    assert ("USAGE", role) in {(g.privilege, g.grantee) for g in grants}


def test_schema_default_privileges_from_the_fixture(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    grants = fetch_schema_default_privileges(pg_cursor, sample_schema, PG)
    assert len(grants) == 1
    grant = grants[0]
    # aclexplode reports grantee oid 0 for PUBLIC, which has no pg_roles row.
    assert grant.grantee == "PUBLIC"
    assert grant.object_type == "TABLE"
    assert grant.privileges == ["SELECT"]
    assert grant.with_grant_option is False
    assert grant.grantor == _scalar(pg_cursor, "SELECT current_user")


def test_schema_default_privileges_group_by_object_type(
    pg_cursor: Cursor[TupleRow], sample_schema: str, temp_role: TempRoleFactory
) -> None:
    role = temp_role("defacl")
    _ddl(
        pg_cursor,
        "ALTER DEFAULT PRIVILEGES IN SCHEMA {s}"
        " GRANT USAGE, SELECT ON SEQUENCES TO {r}",
        "ALTER DEFAULT PRIVILEGES IN SCHEMA {s}"
        " GRANT INSERT, SELECT ON TABLES TO {r} WITH GRANT OPTION",
        s=sample_schema,
        r=role,
    )
    grants = fetch_schema_default_privileges(pg_cursor, sample_schema, PG)
    keyed = {(g.object_type, g.grantee): g for g in grants}
    assert keyed[("SEQUENCE", role)].privileges == ["SELECT", "USAGE"]
    assert keyed[("SEQUENCE", role)].with_grant_option is False
    assert keyed[("TABLE", role)].privileges == ["INSERT", "SELECT"]
    assert keyed[("TABLE", role)].with_grant_option is True
    # ORDER BY object_type, grantee. Only the object_type half is asserted:
    # grantee ordering depends on the database's collation, not on the query.
    object_types = [g.object_type for g in grants]
    assert object_types == sorted(object_types)
    # The fixture's PUBLIC/TABLE default is still there alongside the new rows.
    assert ("TABLE", "PUBLIC") in keyed


def test_schema_default_privileges_empty_when_none_defined(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    other = _sibling_schema(sample_schema)
    _ddl(pg_cursor, "CREATE SCHEMA {a}", a=other)
    assert fetch_schema_default_privileges(pg_cursor, other, PG) == []


# --- schema level report ---------------------------------------------------


def test_schema_header(pg_cursor: Cursor[TupleRow], sample_schema: str) -> None:
    header = fetch_schema_header(pg_cursor, sample_schema)
    assert header.name == sample_schema
    assert header.owner == _scalar(pg_cursor, "SELECT current_user")
    assert header.comment == "dataplat integration fixture"


def test_schema_header_without_a_comment(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    other = _sibling_schema(sample_schema)
    _ddl(pg_cursor, "CREATE SCHEMA {a}", a=other)
    assert fetch_schema_header(pg_cursor, other).comment is None


def test_schema_header_missing_schema_raises(pg_cursor: Cursor[TupleRow]) -> None:
    with pytest.raises(TargetNotFoundError, match='schema "no_such_schema" not found'):
        fetch_schema_header(pg_cursor, "no_such_schema")


def test_schema_contents_label_every_promised_relation(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    contents = fetch_schema_contents(pg_cursor, sample_schema, PG)
    found = {(item.name, item.kind) for item in contents}
    expected = {
        (name, _EXPECTED_KIND_LABEL[relkind])
        for name, relkind in SAMPLE_RELATIONS.items()
    }
    assert expected <= found
    # Identity columns own sequences, so the listing is larger than the fixture
    # inventory -- but every row still belongs to this schema's owner.
    owner = _scalar(pg_cursor, "SELECT current_user")
    assert all(item.owner == owner for item in contents)


def test_schema_contents_exclude_indexes(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    names = {item.name for item in fetch_schema_contents(pg_cursor, sample_schema, PG)}
    assert "customers_pkey" not in names
    assert "customers_org_id_idx" not in names
    # ...and the index really is in this schema, so the exclusion is the filter
    # at work rather than an empty search.
    assert (
        _scalar(
            pg_cursor,
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n"
            " ON n.oid = c.relnamespace"
            " WHERE n.nspname = %s AND c.relname = 'customers_pkey'",
            sample_schema,
        )
        == 1
    )


def test_schema_contents_are_ordered_by_kind_then_name(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    contents = fetch_schema_contents(pg_cursor, sample_schema, PG)
    # ORDER BY relkind, relname: each kind arrives as one contiguous block with
    # its names ascending. The blocks themselves are ordered by relkind, which
    # is not the same order as the human-readable labels, so only contiguity
    # and the within-block sort are asserted.
    blocks: list[tuple[str, list[str]]] = []
    for item in contents:
        if blocks and blocks[-1][0] == item.kind:
            blocks[-1][1].append(item.name)
        else:
            blocks.append((item.kind, [item.name]))
    kinds = [kind for kind, _ in blocks]
    assert len(kinds) == len(set(kinds))
    for _, names in blocks:
        assert names == sorted(names)


def test_schema_contents_supply_the_highlight_inputs(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    # The CLI's "Highlights" section is a top-5-by-size_bytes over exactly these
    # rows, so the service has to provide comparable sizes and real estimates.
    contents = {i.name: i for i in fetch_schema_contents(pg_cursor, sample_schema, PG)}
    assert contents["customers"].size_bytes == _scalar(
        pg_cursor,
        "SELECT pg_total_relation_size(%s::regclass)",
        f"{sample_schema}.customers",
    )
    assert contents["customers"].row_estimate == _count(
        pg_cursor, sample_schema, "customers"
    )
    # A view has no storage and no rows, so both are None and it drops out of
    # the ranking rather than sorting as zero.
    assert contents["active_customers"].size_bytes is None
    assert contents["active_customers"].row_estimate is None
    sized = [i for i in contents.values() if i.size_bytes]
    assert len(sized) >= 5
    top = max(sized, key=lambda i: i.size_bytes or 0)
    assert top.kind in {"table", "matview", "sequence"}


def test_describe_schema_composes_the_whole_report(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    description = describe_schema(pg_cursor, _ref(pg_cursor, sample_schema), PG)
    assert description.header.name == sample_schema
    assert description.contents
    assert description.privileges
    assert [g.grantee for g in description.default_privileges] == ["PUBLIC"]
    # Four statements ran on one cursor; the transaction must still be usable.
    assert _scalar(pg_cursor, "SELECT 1") == 1


def test_describe_schema_of_an_empty_schema(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    other = _sibling_schema(sample_schema)
    _ddl(pg_cursor, "CREATE SCHEMA {a}", a=other)
    description = describe_schema(pg_cursor, _ref(pg_cursor, other), PG)
    assert description.contents == []
    assert description.default_privileges == []
    assert description.header.name == other


# --- top level composition -------------------------------------------------


def test_describe_table_end_to_end(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    description = describe_table(
        pg_cursor, _ref(pg_cursor, sample_schema, "customers"), PG
    )
    assert description.header.name == "customers"
    assert [c.name for c in description.columns][:2] == ["id", "org_id"]
    assert description.constraints.primary_key is not None
    assert len(description.indexes) == 3
    assert description.privileges
    assert len(description.triggers) == 1
    assert (description.policies, description.policies_enabled) == ([], False)
    assert description.partitioning.children == []
    # Redshift-only extras stay None on the PostgreSQL path, and a plain table
    # gets no stored definition.
    assert description.redshift_distribution is None
    assert description.redshift_stats is None
    assert description.definition is None
    assert _scalar(pg_cursor, "SELECT 1") == 1


def test_describe_table_of_a_partitioned_parent(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    description = describe_table(
        pg_cursor, _ref(pg_cursor, sample_schema, "events"), PG
    )
    assert description.partitioning.strategy == "RANGE"
    assert len(description.partitioning.children) == 2
    assert description.header.row_estimate == _count(pg_cursor, sample_schema, "events")
    assert [c.name for c in description.columns if c.is_primary_key] == [
        "event_id",
        "occurred_at",
    ]


def test_describe_table_of_a_matview_includes_its_definition(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    ref = _ref(pg_cursor, sample_schema, "customers_per_org")
    assert ref.kind is ObjectKind.matview
    description = describe_table(pg_cursor, ref, PG)
    assert description.definition is not None
    assert "GROUP BY org_id" in description.definition
    assert [c.name for c in description.columns] == [
        "org_id",
        "customer_count",
        "total_value",
    ]
    # A matview carries no constraints or triggers, but the fetchers still have
    # to run cleanly against it.
    assert description.constraints.primary_key is None
    assert description.triggers == []


def test_describe_view_end_to_end(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    description = describe_view(
        pg_cursor, _ref(pg_cursor, sample_schema, "active_customers"), PG
    )
    assert description.header.name == "active_customers"
    assert description.definition.is_updatable is True
    assert [e.qualified_name for e in description.upstream] == [
        f"{sample_schema}.customers"
    ]
    assert description.downstream == []
    assert description.triggers == []
    assert description.privileges
    assert _scalar(pg_cursor, "SELECT 1") == 1
