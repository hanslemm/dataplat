"""Metadata fetchers for ``dp db describe``.

Returns plain dataclasses so the CLI layer can render them without being
coupled to psycopg. Each fetcher takes an open cursor; callers manage the
connection lifecycle.

Three dialects live here. PostgreSQL and Redshift are servers reached over
libpq; DuckDB is a database file opened in this process, and it is not a
smaller PostgreSQL. The ``_*_SQL_DUCKDB`` constants exist where they do because
each was measured against duckdb 1.5.5 first -- reuse was the goal, and this is
what survived it:

- **Placeholders are part of the statement.** DuckDB binds ``?`` where psycopg
  binds ``%s``, and :class:`~dataplat.cli.db._common.DuckDbCursor` refuses to
  translate (rewriting means deciding which ``%`` are literals). So even a body
  that is portable is spelled twice. ``pg_namespace`` and ``pg_class`` are the
  only two -- see :func:`resolve_target`.
- **Several pg_catalog entries are present but empty or constant, which is
  worse than absent.** ``pg_attrdef`` exists and holds no rows, so the
  PostgreSQL columns query silently loses every ``DEFAULT``.
  ``pg_get_constraintdef()`` returns NULL. ``pg_constraint.conname`` holds the
  constraint *text* (``'PRIMARY KEY(id)'``) rather than a name, its ``conkey``
  is 0-based, and ``confrelid`` is always 0. ``pg_class.relrowsecurity`` is
  false for every relation. None of these raise, so the DuckDB path uses the
  ``duckdb_*()`` catalogs instead, which carry the real answers.
- **format_type() loses the type.** ``pg_catalog.format_type()`` renders
  ``VARCHAR[]`` as ``list``, ``STRUCT(a INTEGER, b VARCHAR)`` as ``struct``,
  ``MAP(VARCHAR, INTEGER)`` as ``map`` and ``JSON`` as ``varchar``.
  ``duckdb_columns().data_type`` gives DuckDB's own spelling back exactly.
- **Genuinely absent, so refused rather than emulated:** ``pg_trigger``,
  ``pg_policy``, ``pg_inherits``, ``pg_partitioned_table``, ``pg_matviews``,
  ``pg_rewrite``, ``pg_roles``, ``pg_default_acl``,
  ``information_schema.role_table_grants``, ``aclexplode()``,
  ``pg_get_userbyid()``, ``pg_get_indexdef()``, ``quote_ident()``,
  ``generate_subscripts()`` and the ``pg_*_size()`` family. Those sections are
  reported as :class:`NotApplicable` rather than as empty results -- an empty
  privileges table reads as "nobody has access", which is a different and false
  statement (CONTRIBUTING: prefer "unknown" to a confident falsehood).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from dataplat.services.db.capabilities import Capability, capabilities_for
from dataplat.services.db.connection import SqlEngine


class ObjectKind(str, Enum):
    """Kind of target the user asked to describe."""

    schema = "schema"
    table = "table"
    view = "view"
    matview = "matview"


class TargetNotFoundError(Exception):
    """Raised when the target schema/object does not exist."""


@dataclass(frozen=True)
class NotApplicable:
    """A report section the target engine has no concept of.

    The distinction this carries is the whole point: a *missing* Triggers
    section and an *empty* one say different things, and only one of them is
    true on DuckDB. Rendering nothing would tell the reader "no triggers are
    configured"; rendering an empty privileges table would tell them "nobody
    has access". Both are false about an engine that has no triggers and no
    users, so the reason travels with the absence.
    """

    # Matches the heading the section would have had, so the reader can connect
    # "Privileges" here to the Privileges section they expected.
    section: str
    # Phrased as the tail of "<engine> ...", present tense, no trailing period.
    reason: str


def _duckdb_reason(capability: Capability) -> str:
    """DuckDB's own declared reason for lacking ``capability``.

    Sourced from :mod:`dataplat.services.db.capabilities` rather than restated,
    so ``dp db role list`` refusing a DuckDB target and this report explaining
    an absent section give the same reason in the same words.
    """
    return capabilities_for(SqlEngine.duckdb).support(capability).reason


# Reasons with no capability behind them, because no command turns on them --
# they are report sections, not whole commands. Each was probed on duckdb 1.5.5,
# and each is phrased like a capability reason ("it has no ...") so a section
# note and a command refusal read in one voice.
_DUCKDB_NO_RELATION_SIZE = (
    "it reports no per-relation byte size — pg_total_relation_size() does not "
    "exist, and duckdb_tables().estimated_size is a row count rather than a "
    "size, so reading it as bytes would be wrong by orders of magnitude"
)
_DUCKDB_NO_TRIGGERS = "it has no trigger system at all, and no pg_trigger catalog"
_DUCKDB_NO_RLS = (
    "it has no row-level security: there is no pg_policy catalog, and "
    "pg_class.relrowsecurity is false for every relation rather than reflecting "
    "a setting that could be turned on"
)
_DUCKDB_NO_PARTITIONS = (
    "it has no declarative partitioning: neither pg_inherits nor "
    "pg_partitioned_table exists, so no relation can be a partition or a parent"
)
_DUCKDB_NO_VIEW_LINEAGE = (
    "it has no pg_rewrite catalog, and duckdb_dependencies() records only "
    "foreign-key and index dependencies, so what a view reads and what reads it "
    "cannot be listed"
)
_DUCKDB_NO_DEFAULT_PRIVILEGES = (
    "it has no pg_default_acl, and nothing to grant to: future objects cannot "
    "inherit privileges an engine without users never had"
)


# Why only DuckDB appears below: Redshift lacks several of these catalogs too
# and has always reported them as silently empty. Saying so there is the same
# one-line addition -- the mechanism takes an engine -- but it changes the
# output of a path with no integration suite, so it belongs to whoever can
# state the evidence for it (CONTRIBUTING, "Dialect changes"). This function
# deliberately answers [] for it rather than guessing.
def table_not_applicable(engine: SqlEngine) -> list[NotApplicable]:
    """Sections a table/matview report cannot have on ``engine``."""
    if engine is not SqlEngine.duckdb:
        return []
    return [
        NotApplicable("Size", _DUCKDB_NO_RELATION_SIZE),
        NotApplicable("Privileges", _duckdb_reason(Capability.acl_introspection)),
        NotApplicable("Triggers", _DUCKDB_NO_TRIGGERS),
        NotApplicable("Row-level security", _DUCKDB_NO_RLS),
        NotApplicable("Partitioning", _DUCKDB_NO_PARTITIONS),
    ]


def view_not_applicable(engine: SqlEngine) -> list[NotApplicable]:
    """Sections a view report cannot have on ``engine``."""
    if engine is not SqlEngine.duckdb:
        return []
    return [
        NotApplicable("Privileges", _duckdb_reason(Capability.acl_introspection)),
        NotApplicable("Dependencies", _DUCKDB_NO_VIEW_LINEAGE),
        NotApplicable("Triggers", _DUCKDB_NO_TRIGGERS),
    ]


def schema_not_applicable(engine: SqlEngine) -> list[NotApplicable]:
    """Sections a schema report cannot have on ``engine``."""
    if engine is not SqlEngine.duckdb:
        return []
    return [
        NotApplicable("Privileges", _duckdb_reason(Capability.acl_introspection)),
        NotApplicable("Default privileges", _DUCKDB_NO_DEFAULT_PRIVILEGES),
        NotApplicable("Size", _DUCKDB_NO_RELATION_SIZE),
        # Listed because a reader scanning Contents for their matviews needs to
        # know none can exist here -- resolve_target cannot even return
        # ObjectKind.matview on DuckDB, since relkind 'm' never occurs.
        NotApplicable("Materialized views", _duckdb_reason(Capability.matview_catalog)),
    ]


@dataclass(frozen=True)
class TargetRef:
    """Resolved target: kind + identifiers + Postgres oid for object lookups."""

    kind: ObjectKind
    schema: str
    name: str | None  # None for schema targets
    oid: int | None  # None for schema targets


def parse_target(target: str) -> tuple[str, str | None]:
    """Split ``schema`` or ``schema.object`` into components.

    Raises ``ValueError`` for any other shape.
    """
    cleaned = (target or "").strip()
    if not cleaned:
        raise ValueError('target must be "<schema>" or "<schema>.<object>"')
    parts = cleaned.split(".")
    if len(parts) == 1:
        return parts[0], None
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0], parts[1]
    raise ValueError('target must be "<schema>" or "<schema>.<object>"')


# The two statements whose body really is portable to DuckDB: pg_namespace and
# pg_class are present there with real relkind values ('r', 'v', 'S', 'i' -- no
# 'p' and no 'm', so a partitioned table or a matview simply never resolves).
# They are still spelled twice because the placeholder is part of the
# statement: DuckDB binds '?', psycopg binds '%s', and nothing in this codebase
# translates between them (dataplat/cli/db/_common.py explains why not).
_RESOLVE_SCHEMA_SQL = "SELECT 1 FROM pg_namespace WHERE nspname = %s"
_RESOLVE_SCHEMA_SQL_DUCKDB = "SELECT 1 FROM pg_namespace WHERE nspname = ?"

_RESOLVE_RELATION_SQL = """
SELECT c.oid, c.relkind
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND c.relname = %s
"""

_RESOLVE_RELATION_SQL_DUCKDB = """
SELECT c.oid, c.relkind
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = ? AND c.relname = ?
"""


def resolve_target(cursor: Any, engine: SqlEngine, target: str) -> TargetRef:
    """Resolve a dotted target into a ``TargetRef`` with kind + oid.

    Queries ``pg_namespace`` for schemas and ``pg_class`` for relations. Both
    exist on DuckDB, and the oid it reports there is the same one every
    ``duckdb_*()`` catalog keys on (``duckdb_tables().table_oid``,
    ``duckdb_views().view_oid``, ``duckdb_indexes().index_oid`` and
    ``duckdb_constraints().table_oid`` all equal ``pg_class.oid`` -- probed on
    1.5.5), which is what lets the fetchers below keep taking an oid.

    Raises ``TargetNotFoundError`` with a user-facing message on miss.
    """
    schema, obj = parse_target(target)
    is_duckdb = engine is SqlEngine.duckdb

    if obj is None:
        cursor.execute(
            _RESOLVE_SCHEMA_SQL_DUCKDB if is_duckdb else _RESOLVE_SCHEMA_SQL,
            (schema,),
        )
        row = cursor.fetchone()
        if row is None:
            raise TargetNotFoundError(f'schema "{schema}" not found')
        return TargetRef(kind=ObjectKind.schema, schema=schema, name=None, oid=None)

    cursor.execute(
        _RESOLVE_RELATION_SQL_DUCKDB if is_duckdb else _RESOLVE_RELATION_SQL,
        (schema, obj),
    )
    row = cursor.fetchone()
    if row is None:
        raise TargetNotFoundError(
            f'"{schema}.{obj}" not found. Try: dp db describe {schema}'
        )
    oid, relkind = row
    kind_map = {
        "r": ObjectKind.table,
        "p": ObjectKind.table,
        "v": ObjectKind.view,
        "m": ObjectKind.matview,
    }
    if relkind not in kind_map:
        raise TargetNotFoundError(f'"{schema}.{obj}" has unsupported kind {relkind!r}')
    return TargetRef(kind=kind_map[relkind], schema=schema, name=obj, oid=oid)


@dataclass(frozen=True)
class ColumnInfo:
    ordinal: int
    name: str
    data_type: str
    nullable: bool
    # Default expression, or "GENERATED ALWAYS/BY DEFAULT AS IDENTITY" for
    # identity columns on PostgreSQL.
    default: str | None
    is_primary_key: bool
    fk_target_table: str | None  # fully qualified, e.g. "public.orgs"
    fk_target_column: str | None
    comment: str | None
    encoding: str | None = None  # Redshift only


_COLUMNS_SQL_POSTGRES = """
SELECT
    a.attnum AS ordinal,
    a.attname AS name,
    pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
    NOT a.attnotnull AS nullable,
    CASE a.attidentity
        WHEN 'a' THEN 'GENERATED ALWAYS AS IDENTITY'
        WHEN 'd' THEN 'GENERATED BY DEFAULT AS IDENTITY'
        ELSE pg_get_expr(ad.adbin, ad.adrelid)
    END AS default_expr,
    COALESCE(pk.is_pk, false) AS is_primary_key,
    fk.target_table AS fk_target_table,
    fk.target_column AS fk_target_column,
    col_description(a.attrelid, a.attnum) AS comment
FROM pg_attribute a
LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
LEFT JOIN LATERAL (
    SELECT true AS is_pk
    FROM pg_index i
    WHERE i.indrelid = a.attrelid
      AND i.indisprimary
      AND a.attnum = ANY(i.indkey)
    LIMIT 1
) pk ON true
LEFT JOIN LATERAL (
    SELECT
        quote_ident(fn.nspname) || '.' || quote_ident(fc.relname) AS target_table,
        fa.attname AS target_column
    FROM pg_constraint con
    JOIN pg_class fc ON fc.oid = con.confrelid
    JOIN pg_namespace fn ON fn.oid = fc.relnamespace
    JOIN pg_attribute fa
      ON fa.attrelid = con.confrelid
     AND fa.attnum = con.confkey[
       array_position(con.conkey, a.attnum)
     ]
    WHERE con.contype = 'f'
      AND con.conrelid = a.attrelid
      AND a.attnum = ANY(con.conkey)
    LIMIT 1
) fk ON true
WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum
"""

_COLUMNS_SQL_REDSHIFT = """
SELECT
    a.attnum AS ordinal,
    a.attname AS name,
    pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
    NOT a.attnotnull AS nullable,
    pg_get_expr(ad.adbin, ad.adrelid) AS default_expr,
    false AS is_primary_key,
    NULL::text AS fk_target_table,
    NULL::text AS fk_target_column,
    col_description(a.attrelid, a.attnum) AS comment,
    (SELECT encoding FROM pg_catalog.pg_attribute_encoding ae
     WHERE ae.attrelid = a.attrelid AND ae.attnum = a.attnum) AS encoding
FROM pg_attribute a
LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum
"""


# Not the pg_attribute form above, and not because it errors -- because it
# would answer, wrongly, three times over (all probed on duckdb 1.5.5):
#
#   - pg_attrdef exists and is *empty*, so every LEFT JOIN against it yields
#     NULL and a column declared `DEFAULT now()` would report no default.
#     duckdb_columns().column_default has it.
#   - pg_catalog.format_type() flattens DuckDB's own types: VARCHAR[] becomes
#     'list', STRUCT(a INTEGER, b VARCHAR) becomes 'struct', MAP(...) becomes
#     'map', JSON becomes 'varchar'. duckdb_columns().data_type round-trips the
#     declared spelling.
#   - quote_ident() does not exist, so the PostgreSQL query's FK subquery cannot
#     even parse.
#
# The two correlated subqueries replace that LATERAL: DuckDB has no
# pg_constraint worth reading (conkey is 0-based and confrelid is always 0), and
# duckdb_constraints() names the columns directly. list_position() pairs a local
# column with its referenced one positionally, which is what conkey/confkey do
# on PostgreSQL. The referenced table is qualified with the constraint's *own*
# schema because DuckDB refuses a cross-schema foreign key outright ("Creating
# foreign keys across different schemas or catalogs is not supported"), so there
# is no other schema it could be in. LIMIT 1 matches the PostgreSQL query's
# behaviour for a column carried by more than one foreign key.
_COLUMNS_SQL_DUCKDB = """
SELECT
    col.column_index AS ordinal,
    col.column_name AS name,
    col.data_type AS data_type,
    col.is_nullable AS nullable,
    col.column_default AS default_expr,
    EXISTS (
        SELECT 1
        FROM duckdb_constraints() pk
        WHERE pk.table_oid = col.table_oid
          AND pk.constraint_type = 'PRIMARY KEY'
          AND list_contains(pk.constraint_column_names, col.column_name)
    ) AS is_primary_key,
    (SELECT fk.schema_name || '.' || fk.referenced_table
       FROM duckdb_constraints() fk
      WHERE fk.table_oid = col.table_oid
        AND fk.constraint_type = 'FOREIGN KEY'
        AND list_contains(fk.constraint_column_names, col.column_name)
      LIMIT 1) AS fk_target_table,
    (SELECT fk.referenced_column_names[
                list_position(fk.constraint_column_names, col.column_name)]
       FROM duckdb_constraints() fk
      WHERE fk.table_oid = col.table_oid
        AND fk.constraint_type = 'FOREIGN KEY'
        AND list_contains(fk.constraint_column_names, col.column_name)
      LIMIT 1) AS fk_target_column,
    col.comment AS comment
FROM duckdb_columns() col
WHERE col.table_oid = ?
ORDER BY col.column_index
"""


def fetch_columns(cursor: Any, oid: int, engine: SqlEngine) -> list[ColumnInfo]:
    """Return ordinal-ordered columns for the given relation oid.

    Covers views as well as tables on every engine: ``duckdb_columns()`` lists a
    view's columns too, and reports no primary or foreign key for them.
    """
    if engine is SqlEngine.duckdb:
        sql = _COLUMNS_SQL_DUCKDB
    elif engine is SqlEngine.redshift:
        sql = _COLUMNS_SQL_REDSHIFT
    else:
        sql = _COLUMNS_SQL_POSTGRES
    cursor.execute(sql, (oid,))
    return [ColumnInfo(*row) for row in cursor.fetchall()]


@dataclass(frozen=True)
class PrimaryKeyInfo:
    name: str
    columns: list[str]


@dataclass(frozen=True)
class ForeignKeyInfo:
    name: str
    columns: list[str]
    referenced_table: str  # schema.name
    referenced_columns: list[str]
    on_update: str
    on_delete: str
    deferrable: bool


@dataclass(frozen=True)
class ConstraintInfo:
    name: str
    definition: str


@dataclass(frozen=True)
class ConstraintBundle:
    primary_key: PrimaryKeyInfo | None
    foreign_keys: list[ForeignKeyInfo]
    unique_constraints: list[ConstraintInfo]
    check_constraints: list[ConstraintInfo]


_FK_ACTION_MAP = {
    "a": "NO ACTION",
    "r": "RESTRICT",
    "c": "CASCADE",
    "n": "SET NULL",
    "d": "SET DEFAULT",
}


_CONSTRAINTS_SQL = """
SELECT
    c.conname AS name,
    c.contype AS kind,
    pg_get_constraintdef(c.oid, true) AS definition,
    COALESCE(
      (SELECT array_agg(a.attname ORDER BY k.ord)
       FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
       JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum),
      '{}'::text[]
    ) AS local_columns,
    CASE WHEN c.contype = 'f' THEN
      quote_ident(fn.nspname) || '.' || quote_ident(fc.relname)
    END AS referenced_table,
    CASE WHEN c.contype = 'f' THEN
      (SELECT array_agg(fa.attname ORDER BY k.ord)
       FROM unnest(c.confkey) WITH ORDINALITY AS k(attnum, ord)
       JOIN pg_attribute fa ON fa.attrelid = c.confrelid AND fa.attnum = k.attnum)
    END AS referenced_columns,
    CASE WHEN c.contype = 'f' THEN c.confdeltype END AS confdeltype,
    CASE WHEN c.contype = 'f' THEN c.confupdtype END AS confupdtype,
    CASE WHEN c.contype = 'f' THEN c.condeferrable END AS deferrable
FROM pg_constraint c
LEFT JOIN pg_class fc ON fc.oid = c.confrelid
LEFT JOIN pg_namespace fn ON fn.oid = fc.relnamespace
WHERE c.conrelid = %s
ORDER BY CASE c.contype WHEN 'p' THEN 0 WHEN 'u' THEN 1 WHEN 'f' THEN 2 ELSE 3 END,
         c.conname
"""


# DuckDB has a pg_constraint, and it is a trap rather than a shortcut: conname
# holds the constraint *text* ('PRIMARY KEY(id)') instead of a name, conkey is
# 0-based where PostgreSQL's is 1-based, confrelid is always 0 so a foreign key's
# target is unreachable, and pg_get_constraintdef() returns NULL. Every one of
# those answers looks valid. duckdb_constraints() carries the real names,
# columns and targets, so it is what this reads.
#
# The column list matches _CONSTRAINTS_SQL position for position, so
# fetch_constraints unpacks both dialects the same way. Three tails are constant
# rather than read:
#
#   - confdeltype/confupdtype are NULL, which _FK_ACTION_MAP maps to NO ACTION.
#     That is not a fallback: DuckDB *refuses* referential actions at parse time
#     ("FOREIGN KEY constraints cannot use CASCADE, SET NULL or SET DEFAULT"),
#     so NO ACTION is the only thing a DuckDB foreign key can do.
#   - deferrable is false; DuckDB has no DEFERRABLE constraints.
#
# NOT NULL is excluded on purpose. duckdb_constraints() reports it as a
# constraint row, but PostgreSQL keeps it in pg_attribute.attnotnull and this
# report shows it in the Columns section -- listing it in both places for one
# engine only would make the same table look different per dialect.
_CONSTRAINTS_SQL_DUCKDB = """
SELECT
    con.constraint_name AS name,
    CASE con.constraint_type
        WHEN 'PRIMARY KEY' THEN 'p'
        WHEN 'UNIQUE' THEN 'u'
        WHEN 'FOREIGN KEY' THEN 'f'
        WHEN 'CHECK' THEN 'c'
    END AS kind,
    con.constraint_text AS definition,
    con.constraint_column_names AS local_columns,
    CASE WHEN con.constraint_type = 'FOREIGN KEY'
         THEN con.schema_name || '.' || con.referenced_table
    END AS referenced_table,
    CASE WHEN con.constraint_type = 'FOREIGN KEY'
         THEN con.referenced_column_names
    END AS referenced_columns,
    NULL::VARCHAR AS confdeltype,
    NULL::VARCHAR AS confupdtype,
    false AS deferrable
FROM duckdb_constraints() con
WHERE con.table_oid = ?
  AND con.constraint_type IN ('PRIMARY KEY', 'UNIQUE', 'FOREIGN KEY', 'CHECK')
ORDER BY CASE con.constraint_type
             WHEN 'PRIMARY KEY' THEN 0
             WHEN 'UNIQUE' THEN 1
             WHEN 'FOREIGN KEY' THEN 2
             ELSE 3
         END,
         con.constraint_name
"""


@dataclass(frozen=True)
class IndexInfo:
    name: str
    columns: list[str]
    unique: bool
    primary: bool
    method: str
    size_bytes: int | None
    predicate: str | None


_INDEXES_SQL_POSTGRES = """
SELECT
    ic.relname AS name,
    ARRAY(
      SELECT pg_get_indexdef(i.indexrelid, k + 1, true)
      FROM generate_subscripts(i.indkey, 1) AS k
    ) AS columns,
    i.indisunique AS unique,
    i.indisprimary AS primary,
    am.amname AS method,
    -- Same pg_partition_tree() caveat as the relation header: the tree is
    -- empty for an index that is neither partitioned nor a partition, so
    -- without the fallback every ordinary index reported 0 bytes.
    COALESCE(
      (SELECT SUM(pg_relation_size(p.relid))
       FROM pg_partition_tree(i.indexrelid) p),
      pg_relation_size(i.indexrelid)
    )::bigint AS size_bytes,
    CASE WHEN i.indpred IS NOT NULL
         THEN pg_get_expr(i.indpred, i.indrelid, true)
    END AS predicate
FROM pg_index i
JOIN pg_class ic ON ic.oid = i.indexrelid
JOIN pg_am am ON am.oid = ic.relam
WHERE i.indrelid = %s
ORDER BY i.indisprimary DESC, i.indisunique DESC, ic.relname
"""

_INDEXES_SQL_REDSHIFT = """
SELECT ic.relname, ARRAY[]::text[], i.indisunique, i.indisprimary,
       'btree', NULL::bigint, NULL::text
FROM pg_index i
JOIN pg_class ic ON ic.oid = i.indexrelid
WHERE i.indrelid = %s
ORDER BY ic.relname
"""


# pg_index exists on DuckDB but carries no key columns (indkey is NULL), and
# neither pg_get_indexdef() nor generate_subscripts() nor pg_relation_size()
# exists, so the PostgreSQL query cannot run at all. duckdb_indexes() has what
# is left.
#
# `expressions` is a VARCHAR holding DuckDB's own rendering of a list --
# '[lo, hi]' for two plain columns, '[''(concat("a", b))'']' for one expression
# that needs quoting. Casting it back to VARCHAR[] hands the parsing to the
# engine that wrote it, which is exact where splitting on ', ' would tear
# `concat(a, b)` in half. COALESCE keeps the column non-NULL so IndexInfo.columns
# is always a list.
#
# Two honest constants: 'art' is the only index type DuckDB has (pg_am holds the
# single row 'art', an adaptive radix tree), and predicate is NULL because there
# are no partial indexes -- pg_index.indpred is NULL for every row.
#
# What is *absent* here is worth knowing: DuckDB does not expose the indexes it
# builds for PRIMARY KEY and UNIQUE constraints. duckdb_tables().index_count
# counts them, duckdb_indexes() does not list them, so this section shows
# explicitly created indexes and the Constraints section shows the rest. is_unique
# therefore reflects CREATE UNIQUE INDEX, and is_primary is false throughout.
_INDEXES_SQL_DUCKDB = """
SELECT
    idx.index_name AS name,
    COALESCE(idx.expressions::VARCHAR[], []::VARCHAR[]) AS columns,
    idx.is_unique AS is_unique,
    idx.is_primary AS is_primary,
    'art' AS method,
    NULL::BIGINT AS size_bytes,
    NULL::VARCHAR AS predicate
FROM duckdb_indexes() idx
WHERE idx.table_oid = ?
ORDER BY idx.is_primary DESC, idx.is_unique DESC, idx.index_name
"""


def fetch_indexes(cursor: Any, oid: int, engine: SqlEngine) -> list[IndexInfo]:
    """Return indexes for the given relation oid."""
    if engine is SqlEngine.duckdb:
        sql = _INDEXES_SQL_DUCKDB
    elif engine is SqlEngine.redshift:
        sql = _INDEXES_SQL_REDSHIFT
    else:
        sql = _INDEXES_SQL_POSTGRES
    cursor.execute(sql, (oid,))
    return [IndexInfo(*row) for row in cursor.fetchall()]


@dataclass(frozen=True)
class RelationHeader:
    schema: str
    name: str
    owner: str
    tablespace: str | None
    comment: str | None
    row_estimate: int | None
    total_size: int | None
    table_size: int | None
    index_size: int | None
    toast_size: int | None


@dataclass(frozen=True)
class PrivilegeGrant:
    grantee: str
    privilege: str  # "SELECT", "INSERT", ..., or "OWNER" for ownership row
    with_grant_option: bool
    grantor: str


_RELATION_HEADER_SQL_POSTGRES = """
SELECT
    n.nspname,
    c.relname,
    pg_get_userbyid(c.relowner) AS owner,
    COALESCE(t.spcname, 'pg_default') AS tablespace,
    obj_description(c.oid, 'pg_class') AS comment,
    -- pg_partition_tree() returns NO rows for a relation that is neither a
    -- partition nor partitioned, so each aggregate below needs the fallback
    -- arm of its COALESCE: without one an ordinary table reported 0 bytes and
    -- an unknown row count. Partitioned parents are excluded from the row sum
    -- because pg_class.reltuples on a parent already aggregates its
    -- partitions -- adding both counts every row twice.
    COALESCE(
      (SELECT SUM(pc.reltuples)::bigint
         FROM pg_partition_tree(c.oid) p
         JOIN pg_class pc ON pc.oid = p.relid
        WHERE pc.reltuples >= 0 AND pc.relkind <> 'p'),
      CASE WHEN c.reltuples >= 0 THEN c.reltuples::bigint END
    ) AS row_estimate,
    COALESCE(
      (SELECT SUM(pg_total_relation_size(p.relid))
         FROM pg_partition_tree(c.oid) p),
      pg_total_relation_size(c.oid)
    )::bigint AS total_size,
    COALESCE(
      (SELECT SUM(pg_relation_size(p.relid))
         FROM pg_partition_tree(c.oid) p),
      pg_relation_size(c.oid)
    )::bigint AS table_size,
    COALESCE(
      (SELECT SUM(pg_indexes_size(p.relid))
         FROM pg_partition_tree(c.oid) p),
      pg_indexes_size(c.oid)
    )::bigint AS index_size,
    COALESCE(
      (SELECT SUM(
                pg_total_relation_size(p.relid)
              - pg_relation_size(p.relid)
              - pg_indexes_size(p.relid)
              )
         FROM pg_partition_tree(c.oid) p),
      pg_total_relation_size(c.oid)
      - pg_relation_size(c.oid)
      - pg_indexes_size(c.oid)
    )::bigint AS toast_size
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_tablespace t ON t.oid = c.reltablespace
WHERE c.oid = %s
"""

_RELATION_HEADER_SQL_REDSHIFT = """
SELECT
    n.nspname,
    c.relname,
    pg_get_userbyid(c.relowner) AS owner,
    NULL::text AS tablespace,
    obj_description(c.oid, 'pg_class') AS comment,
    (SELECT tbl_rows FROM svv_table_info
     WHERE "schema" = n.nspname AND "table" = c.relname) AS row_estimate,
    (SELECT size::bigint * 1024 * 1024 FROM svv_table_info
     WHERE "schema" = n.nspname AND "table" = c.relname) AS total_size,
    NULL::bigint, NULL::bigint, NULL::bigint
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.oid = %s
"""


# The UNION is what makes one query serve both relation kinds: DuckDB keeps
# tables and views in separate catalogs, and this fetcher is called for each.
# Exactly one arm can match, since an oid identifies one relation.
#
# Four fields are constant, and each is a withdrawn claim rather than a guess
# (CONTRIBUTING, evidence class 3):
#
#   - owner is '' because DuckDB has no users. pg_tables.tableowner and
#     pg_views.viewowner do say 'duckdb', but printing "Owner: duckdb" invites
#     the reader to look for the other users, and there are none; the report
#     says so once, in its Privileges note, instead of implying a user model on
#     every line. The renderer omits an empty owner.
#   - tablespace is NULL: DuckDB has no tablespaces (pg_tablespace holds one
#     synthetic 'pg_default' row and reltablespace is always 0).
#   - the four size columns are NULL. duckdb_tables().estimated_size is a *row
#     count*, not a byte count -- probed on 1.5.5, a 10,000-row table reports
#     10000 while its file is 1.5 MiB -- so it feeds row_estimate here and
#     nothing feeds size. NotApplicable("Size", ...) says why in the report.
#
# row_estimate is left NULL for a view rather than read: DuckDB's
# pg_class.reltuples is 0.0 for every view, which would render as "0 rows".
_RELATION_HEADER_SQL_DUCKDB = """
SELECT
    t.schema_name AS schema,
    t.table_name AS name,
    '' AS owner,
    NULL::VARCHAR AS tablespace,
    t.comment AS comment,
    t.estimated_size::BIGINT AS row_estimate,
    NULL::BIGINT AS total_size,
    NULL::BIGINT AS table_size,
    NULL::BIGINT AS index_size,
    NULL::BIGINT AS toast_size
FROM duckdb_tables() t
WHERE t.table_oid = ?
UNION ALL
SELECT
    v.schema_name, v.view_name, '', NULL::VARCHAR, v.comment,
    NULL::BIGINT, NULL::BIGINT, NULL::BIGINT, NULL::BIGINT, NULL::BIGINT
FROM duckdb_views() v
WHERE v.view_oid = ?
"""


def fetch_relation_header(cursor: Any, oid: int, engine: SqlEngine) -> RelationHeader:
    if engine is SqlEngine.duckdb:
        # Two placeholders, one per UNION arm -- see _RELATION_HEADER_SQL_DUCKDB.
        cursor.execute(_RELATION_HEADER_SQL_DUCKDB, (oid, oid))
    elif engine is SqlEngine.redshift:
        cursor.execute(_RELATION_HEADER_SQL_REDSHIFT, (oid,))
    else:
        cursor.execute(_RELATION_HEADER_SQL_POSTGRES, (oid,))
    row = cursor.fetchone()
    if row is None:
        raise TargetNotFoundError(f"relation with oid {oid} not found")
    return RelationHeader(*row)


_PRIVILEGES_SQL = """
SELECT grantee, privilege_type AS privilege,
       is_grantable = 'YES' AS with_grant_option,
       grantor
FROM information_schema.role_table_grants
WHERE table_schema = %s AND table_name = %s
UNION ALL
SELECT pg_get_userbyid(c.relowner), 'OWNER', false, ''
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND c.relname = %s
ORDER BY grantee, privilege
"""


def fetch_relation_privileges(
    cursor: Any, schema: str, name: str, engine: SqlEngine = SqlEngine.postgresql
) -> list[PrivilegeGrant]:
    """Return grants on the relation, including a synthetic OWNER row.

    ``engine`` decides only whether the engine has a grant catalog at all, which
    is why it defaults to a libpq engine: :data:`_PRIVILEGES_SQL` runs unchanged
    on both PostgreSQL and Redshift. DuckDB has neither
    ``information_schema.role_table_grants`` nor ``pg_get_userbyid()``, and no
    users to be grantees, so it gets ``[]`` and the caller pairs that with
    :func:`table_not_applicable`'s Privileges entry -- an empty grant table on
    its own would read as "nobody has access".
    """
    if engine is SqlEngine.duckdb:
        return []
    cursor.execute(_PRIVILEGES_SQL, (schema, name, schema, name))
    return [PrivilegeGrant(*row) for row in cursor.fetchall()]


@dataclass(frozen=True)
class TriggerInfo:
    name: str
    timing: str  # BEFORE / AFTER / INSTEAD OF
    events: str  # e.g. "INSERT OR UPDATE"
    function: str


@dataclass(frozen=True)
class PolicyInfo:
    name: str
    command: str  # ALL, SELECT, INSERT, UPDATE, DELETE
    roles: list[str]
    using: str | None
    with_check: str | None


@dataclass(frozen=True)
class PartitioningInfo:
    parent: str | None
    strategy: str | None  # RANGE / LIST / HASH
    partition_key: str | None
    children: list[tuple[str, str]]  # (name, bounds)


_TRIGGERS_SQL = """
SELECT
    t.tgname AS name,
    CASE WHEN (t.tgtype & 2) <> 0 THEN 'BEFORE'
         WHEN (t.tgtype & 64) <> 0 THEN 'INSTEAD OF'
         ELSE 'AFTER' END AS timing,
    pg_catalog.array_to_string(ARRAY[
        CASE WHEN (t.tgtype & 4)  <> 0 THEN 'INSERT' END,
        CASE WHEN (t.tgtype & 8)  <> 0 THEN 'DELETE' END,
        CASE WHEN (t.tgtype & 16) <> 0 THEN 'UPDATE' END,
        CASE WHEN (t.tgtype & 32) <> 0 THEN 'TRUNCATE' END
    ], ' OR ') AS events,
    pg_get_triggerdef(t.oid, true) AS function
FROM pg_trigger t
WHERE t.tgrelid = %s AND NOT t.tgisinternal
ORDER BY t.tgname
"""


def fetch_triggers(cursor: Any, oid: int, engine: SqlEngine) -> list[TriggerInfo]:
    """Return non-internal triggers on the relation.

    Empty without a query on Redshift (no ``pg_trigger``) and on DuckDB (no
    trigger system at all -- see :func:`table_not_applicable`, which is what
    tells the reader those two emptinesses mean different things).
    """
    if engine in {SqlEngine.redshift, SqlEngine.duckdb}:
        return []
    cursor.execute(_TRIGGERS_SQL, (oid,))
    return [TriggerInfo(*row) for row in cursor.fetchall()]


_RLS_ENABLED_SQL = "SELECT relrowsecurity FROM pg_class WHERE oid = %s"
_POLICIES_SQL = """
SELECT
    pol.polname AS name,
    CASE pol.polcmd WHEN 'r' THEN 'SELECT' WHEN 'a' THEN 'INSERT'
                    WHEN 'w' THEN 'UPDATE' WHEN 'd' THEN 'DELETE'
                    ELSE 'ALL' END AS command,
    COALESCE(
      (SELECT array_agg(rolname) FROM pg_roles WHERE oid = ANY(pol.polroles)),
      ARRAY['public']
    ) AS roles,
    pg_get_expr(pol.polqual, pol.polrelid, true) AS using_expr,
    pg_get_expr(pol.polwithcheck, pol.polrelid, true) AS with_check
FROM pg_policy pol
WHERE pol.polrelid = %s
ORDER BY pol.polname
"""


def fetch_policies(
    cursor: Any, oid: int, engine: SqlEngine
) -> tuple[list[PolicyInfo], bool]:
    """Return row-level security policies and whether RLS is enabled.

    DuckDB is refused rather than probed even though ``pg_class.relrowsecurity``
    happens to exist there: it is false for every relation because there is no
    row-level security to enable, so reading it would turn a missing feature into
    a report saying "RLS: disabled" -- which implies it could be enabled.
    """
    if engine in {SqlEngine.redshift, SqlEngine.duckdb}:
        return [], False
    cursor.execute(_RLS_ENABLED_SQL, (oid,))
    row = cursor.fetchone()
    enabled = bool(row[0]) if row else False
    cursor.execute(_POLICIES_SQL, (oid,))
    policies = [PolicyInfo(*r) for r in cursor.fetchall()]
    return policies, enabled


_PARTITION_CHILD_PARENT_SQL = """
SELECT quote_ident(pn.nspname) || '.' || quote_ident(pc.relname)
FROM pg_inherits inh
JOIN pg_class pc ON pc.oid = inh.inhparent
JOIN pg_namespace pn ON pn.oid = pc.relnamespace
WHERE inh.inhrelid = %s
"""


_PARTITION_ROOT_STRATEGY_SQL = """
SELECT CASE partstrat
    WHEN 'r' THEN 'RANGE'
    WHEN 'l' THEN 'LIST'
    WHEN 'h' THEN 'HASH'
END
FROM pg_partitioned_table
WHERE partrelid = %s
"""

_PARTITION_CHILDREN_SQL = """
SELECT
    quote_ident(cn.nspname) || '.' || quote_ident(cc.relname) AS child,
    pg_get_expr(cc.relpartbound, cc.oid, true) AS bounds
FROM pg_inherits inh
JOIN pg_class cc ON cc.oid = inh.inhrelid
JOIN pg_namespace cn ON cn.oid = cc.relnamespace
WHERE inh.inhparent = %s
ORDER BY cc.relname
"""


def fetch_partitioning(cursor: Any, oid: int, engine: SqlEngine) -> PartitioningInfo:
    """Return the relation's place in a partition tree, if any.

    Four statements on PostgreSQL, none on Redshift or DuckDB: DuckDB has
    neither ``pg_inherits`` nor ``pg_partitioned_table`` nor
    ``pg_get_partkeydef()``, because it has no declarative partitioning.
    """
    if engine in {SqlEngine.redshift, SqlEngine.duckdb}:
        return PartitioningInfo(
            parent=None, strategy=None, partition_key=None, children=[]
        )
    cursor.execute(_PARTITION_CHILD_PARENT_SQL, (oid,))
    parent_row = cursor.fetchone()
    parent = parent_row[0] if parent_row else None
    cursor.execute(_PARTITION_ROOT_STRATEGY_SQL, (oid,))
    strat_row = cursor.fetchone()
    strategy = strat_row[0] if strat_row else None
    cursor.execute("SELECT pg_get_partkeydef(%s)", (oid,))
    pk_row = cursor.fetchone()
    partition_key = pk_row[0] if pk_row and pk_row[0] else None
    cursor.execute(_PARTITION_CHILDREN_SQL, (oid,))
    children = [(r[0], r[1]) for r in cursor.fetchall()]
    return PartitioningInfo(
        parent=parent, strategy=strategy, partition_key=partition_key, children=children
    )


@dataclass(frozen=True)
class ViewDefinition:
    sql: str
    is_updatable: bool
    check_option: str | None


@dataclass(frozen=True)
class DependencyEdge:
    qualified_name: str
    kind: str


_VIEW_DEFINITION_SQL = """
SELECT
    pg_get_viewdef(c.oid, true) AS sql,
    (SELECT is_updatable FROM information_schema.views
     WHERE table_schema = n.nspname AND table_name = c.relname) AS is_updatable,
    (SELECT check_option FROM information_schema.views
     WHERE table_schema = n.nspname AND table_name = c.relname) AS check_option
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.oid = %s
"""


_VIEW_DEFINITION_SQL_REDSHIFT = "SELECT pg_get_viewdef(%s, true) AS sql"


# DuckDB has a pg_get_viewdef(), but only the one-argument macro: the
# two-argument call above fails with "Macro pg_get_viewdef() does not support the
# supplied arguments". duckdb_views().sql is the same text without the
# arity guess, and it is the catalog DuckDB documents for the purpose.
#
# One difference the reader will notice: DuckDB returns the whole
# `CREATE VIEW x AS SELECT ...;` statement where PostgreSQL returns the bare
# SELECT. It is passed through verbatim -- it is the engine's own answer, and
# trimming a prefix off catalog text is how a report starts lying about what is
# stored.
_VIEW_DEFINITION_SQL_DUCKDB = "SELECT v.sql FROM duckdb_views() v WHERE v.view_oid = ?"


_DEPS_UPSTREAM_SQL = """
SELECT DISTINCT
    quote_ident(sn.nspname) || '.' || quote_ident(sc.relname) AS name,
    sc.relkind AS kind
FROM pg_rewrite r
JOIN pg_depend d ON d.objid = r.oid AND d.classid = 'pg_rewrite'::regclass
JOIN pg_class sc ON sc.oid = d.refobjid
JOIN pg_namespace sn ON sn.oid = sc.relnamespace
WHERE r.ev_class = %s
  AND d.refobjid <> %s
  AND sc.relkind IN ('r','v','m','f','p')
ORDER BY name
"""


_DEPS_DOWNSTREAM_SQL = """
SELECT DISTINCT
    quote_ident(tn.nspname) || '.' || quote_ident(tc.relname) AS name,
    tc.relkind AS kind
FROM pg_rewrite r
JOIN pg_depend d ON d.objid = r.oid AND d.classid = 'pg_rewrite'::regclass
JOIN pg_class tc ON tc.oid = r.ev_class
JOIN pg_namespace tn ON tn.oid = tc.relnamespace
WHERE d.refobjid = %s
  AND r.ev_class <> %s
  AND tc.relkind IN ('v','m')
ORDER BY name
"""


_KIND_LABEL = {
    "r": "table",
    "v": "view",
    "m": "matview",
    "f": "foreign table",
    "p": "partitioned table",
    "S": "sequence",
    "i": "index",
}


def fetch_view_definition(cursor: Any, oid: int, engine: SqlEngine) -> ViewDefinition:
    """Return the SQL definition + updatability info for a view/matview.

    ``is_updatable`` and ``check_option`` are constant on the two non-PostgreSQL
    dialects rather than queried. On DuckDB that is a read, not an assumption:
    ``information_schema.views`` exists there and answers ``is_updatable='NO'``
    and ``check_option='NONE'`` for every view, because DuckDB has neither
    updatable views nor ``WITH CHECK OPTION``.
    """
    if engine is SqlEngine.duckdb:
        cursor.execute(_VIEW_DEFINITION_SQL_DUCKDB, (oid,))
        row = cursor.fetchone()
        if row is None or row[0] is None:
            raise TargetNotFoundError(f"view with oid {oid} not found")
        return ViewDefinition(sql=row[0], is_updatable=False, check_option=None)
    if engine == SqlEngine.redshift:
        cursor.execute(_VIEW_DEFINITION_SQL_REDSHIFT, (oid,))
        row = cursor.fetchone()
        if row is None:
            raise TargetNotFoundError(f"view with oid {oid} not found")
        (sql,) = row
        if sql is None:
            raise TargetNotFoundError(f"view with oid {oid} not found")
        return ViewDefinition(sql=sql, is_updatable=False, check_option=None)
    cursor.execute(_VIEW_DEFINITION_SQL, (oid,))
    row = cursor.fetchone()
    if row is None:
        raise TargetNotFoundError(f"view with oid {oid} not found")
    sql, is_updatable, check_option = row
    # pg_get_viewdef() does not error on a non-view oid, it returns NULL, so a
    # table's oid produces a row whose definition is missing. Reject it the way
    # the Redshift branch above does: returning ViewDefinition(sql=None) would
    # break this dataclass's own `sql: str` contract for every caller.
    if sql is None:
        raise TargetNotFoundError(f"view with oid {oid} not found")
    updatable = isinstance(is_updatable, str) and is_updatable.upper() == "YES"
    if check_option is None or check_option == "NONE":
        check_option_value: str | None = None
    else:
        check_option_value = check_option
    return ViewDefinition(
        sql=sql, is_updatable=updatable, check_option=check_option_value
    )


def fetch_dependencies(
    cursor: Any, oid: int, direction: str, engine: SqlEngine
) -> list[DependencyEdge]:
    """Return upstream or downstream dependency edges for the given relation.

    Both directions read ``pg_rewrite``, which neither Redshift nor DuckDB has.
    DuckDB's ``duckdb_dependencies()`` is not a substitute: probed on 1.5.5 it
    records foreign-key and index dependencies only, and a view that reads a
    table produces no row at all -- so there is nothing to build lineage from.
    """
    if engine in {SqlEngine.redshift, SqlEngine.duckdb}:
        return []
    if direction == "upstream":
        sql = _DEPS_UPSTREAM_SQL
    elif direction == "downstream":
        sql = _DEPS_DOWNSTREAM_SQL
    else:
        raise ValueError(
            f"direction must be 'upstream' or 'downstream', got {direction!r}"
        )
    cursor.execute(sql, (oid, oid))
    return [
        DependencyEdge(qualified_name=name, kind=_KIND_LABEL.get(kind, kind))
        for name, kind in cursor.fetchall()
    ]


@dataclass(frozen=True)
class SchemaHeader:
    name: str
    owner: str
    comment: str | None


@dataclass(frozen=True)
class SchemaContentItem:
    name: str
    kind: str
    owner: str
    row_estimate: int | None
    size_bytes: int | None


_SCHEMA_HEADER_SQL = """
SELECT n.nspname, pg_get_userbyid(n.nspowner),
       obj_description(n.oid, 'pg_namespace')
FROM pg_namespace n
WHERE n.nspname = %s
"""


# pg_namespace exists on DuckDB, but nspowner is always 0 and there is no
# pg_get_userbyid() to resolve it with; obj_description() exists and returns NULL
# for everything. duckdb_schemas() has the comment column instead -- always NULL
# today, because DuckDB refuses COMMENT ON SCHEMA ("Adding comments to schemas is
# not implemented"), so this reads it rather than hardcoding None: the day that
# lands, the report picks it up.
#
# The database_name filter is load-bearing. duckdb_schemas() lists every attached
# catalog, and 'main' exists in the user database, in 'system' and in 'temp'; the
# unfiltered query would return three rows for it and the header would describe
# whichever came first.
_SCHEMA_HEADER_SQL_DUCKDB = """
SELECT s.schema_name, '' AS owner, s.comment
FROM duckdb_schemas() s
WHERE s.schema_name = ? AND s.database_name = current_database()
"""


_SCHEMA_CONTENTS_SQL_POSTGRES = """
SELECT
    c.relname AS name,
    c.relkind AS kind,
    pg_get_userbyid(c.relowner) AS owner,
    CASE WHEN c.relkind IN ('r','m','p') THEN c.reltuples::bigint END AS row_estimate,
    CASE WHEN c.relkind IN ('r','m','p','i','S')
         THEN pg_total_relation_size(c.oid)
    END AS size_bytes
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s
  AND c.relkind IN ('r','v','m','p','S','f')
ORDER BY c.relkind, c.relname
"""


_SCHEMA_CONTENTS_SQL_REDSHIFT = """
SELECT
    c.relname, c.relkind, pg_get_userbyid(c.relowner),
    (SELECT tbl_rows FROM svv_table_info
     WHERE "schema" = %s AND "table" = c.relname),
    (SELECT size::bigint * 1024 * 1024 FROM svv_table_info
     WHERE "schema" = %s AND "table" = c.relname)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s
  AND c.relkind IN ('r','v','m','S','f')
ORDER BY c.relkind, c.relname
"""


# The closest the DuckDB path comes to reusing a PostgreSQL query: pg_class here
# is genuinely right, so this keeps its shape and drops the two expressions
# DuckDB cannot evaluate.
#
#   - pg_get_userbyid() does not exist and there is no owner to resolve -- see
#     _RELATION_HEADER_SQL_DUCKDB on why that becomes '' rather than 'duckdb'.
#   - pg_total_relation_size() does not exist, and nothing replaces it per
#     relation; NotApplicable("Size", ...) is what tells the reader so.
#
# reltuples is real here (it matches duckdb_tables().estimated_size row for row)
# but is only reported for a table: DuckDB reports 0.0 for a view, which would
# render as "0 rows". The relkind filter drops 'm', 'p' and 'f' -- DuckDB has no
# materialized views, no partitioned tables and no foreign tables -- and 'i',
# which it does have and which never belonged in a schema listing.
_SCHEMA_CONTENTS_SQL_DUCKDB = """
SELECT
    c.relname AS name,
    c.relkind AS kind,
    '' AS owner,
    CASE WHEN c.relkind = 'r' THEN c.reltuples::BIGINT END AS row_estimate,
    NULL::BIGINT AS size_bytes
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = ?
  AND c.relkind IN ('r', 'v', 'S')
ORDER BY c.relkind, c.relname
"""


# PostgreSQL exposes schema ACLs only through pg_namespace.nspacl; there is no
# information_schema view for them. usage_privileges reports DOMAIN, COLLATION,
# FDW, foreign server and sequence object types and never 'SCHEMA', so the
# USAGE half of the old shared query silently matched zero rows and every
# GRANT USAGE ON SCHEMA was missing from the report.
#
# The COALESCE is load-bearing: nspacl stays NULL until the first GRANT or
# REVOKE touches the schema, and NULL means "the built-in default" -- the owner
# holding USAGE + CREATE -- not "nobody holds anything". acldefault('n', owner)
# materialises that default so a freshly created schema still reports its
# owner's privileges.
#
# Reading the ACL also makes grantor and WITH GRANT OPTION truthful: the CREATE
# half of the old query derived grantees from has_schema_privilege() and had to
# hardcode grantor='' and with_grant_option=false.
#
# The grant option needs more than the raw ACL bit, though. An owner holds every
# grant option implicitly and PostgreSQL never writes it down: acldefault('n',
# owner) is `owner=UC/owner`, no `*`. Reporting that bit alone said "cannot
# delegate" about a role that can, and said it only for schemas -- the relation
# half of this report reads information_schema.role_table_grants, which
# synthesises is_grantable='YES' for an owner, so one authority was reported two
# ways depending on which object you asked about.
#
# pg_has_role((acl).grantee, n.nspowner, 'USAGE') is the server's own rule
# (aclmask(): "owner always implicitly has all grant options", tested with
# has_privs_of_role(roleid, ownerId)) and is the very expression
# information_schema.table_privileges uses, so both halves now answer one
# question one way. Verified against PostgreSQL 16: has_schema_privilege(owner,
# s, 'USAGE WITH GRANT OPTION') is true, and `SET ROLE owner; GRANT USAGE ON
# SCHEMA s TO other` succeeds. PUBLIC is grantee oid 0, which is no role at all;
# pg_has_role returns false for it rather than erroring, exactly as
# information_schema relies on.
_SCHEMA_PRIVILEGES_SQL_POSTGRES = """
SELECT
    CASE WHEN (acl).grantee = 0 THEN 'PUBLIC'
         ELSE pg_get_userbyid((acl).grantee) END AS grantee,
    (acl).privilege_type AS privilege_type,
    ((acl).is_grantable OR pg_has_role((acl).grantee, n.nspowner, 'USAGE'))
        AS with_grant_option,
    pg_get_userbyid((acl).grantor) AS grantor
FROM pg_namespace n
CROSS JOIN LATERAL aclexplode(COALESCE(n.nspacl, acldefault('n', n.nspowner))) AS acl
WHERE n.nspname = %s
ORDER BY grantee, privilege_type
"""


# Not the aclexplode form above: Redshift has no aclexplode(), and no cluster is
# available to test a replacement against.
#
# The USAGE half used to read information_schema.usage_privileges filtered to
# object_type = 'SCHEMA', which the SQL standard defines over domains,
# collations and sequences — never schemas. It returned zero rows on every
# server, so a GRANT USAGE ON SCHEMA was invisible here exactly as it was on
# PostgreSQL. It is replaced by a has_schema_privilege() scan mirroring the
# CREATE half directly below, which this same query has always run against
# Redshift: same function, same catalog, same shape, only the privilege string
# differs (CONTRIBUTING, evidence 2 — internal precedent on this very path).
#
# Known limitation, shared with the CREATE half and unchanged: a privilege scan
# cannot report a grantor or a grant option, so both are reported empty, and
# roles holding the privilege only through membership appear as their own rows.
# The PostgreSQL path reads the ACL and does better on both counts.
#
# That gap includes the owner's implicit grant option, which the PostgreSQL half
# above now reports through pg_has_role(). This constant stays as it is: closing
# the gap here would mean new Redshift SQL with no cluster to test it against,
# whereas the PostgreSQL fix touches no Redshift SQL at all (CONTRIBUTING,
# evidence class 1).
_SCHEMA_PRIVILEGES_SQL_REDSHIFT = """
SELECT grantee, 'USAGE', false, ''
FROM (
    SELECT r.rolname AS grantee
    FROM pg_roles r
    WHERE has_schema_privilege(r.rolname, %s, 'USAGE')
) u
UNION ALL
SELECT grantee, 'CREATE', false, ''
FROM (
    SELECT r.rolname AS grantee
    FROM pg_roles r
    WHERE has_schema_privilege(r.rolname, %s, 'CREATE')
) c
ORDER BY grantee, privilege_type
"""


_SCHEMA_DEFAULT_PRIVILEGES_SQL = """
SELECT
    CASE WHEN (acl).grantee = 0 THEN 'PUBLIC'
         ELSE pg_get_userbyid((acl).grantee) END AS grantee,
    CASE d.defaclobjtype
        WHEN 'r' THEN 'TABLE'
        WHEN 'S' THEN 'SEQUENCE'
        WHEN 'f' THEN 'FUNCTION'
        WHEN 'T' THEN 'TYPE'
        WHEN 'n' THEN 'SCHEMA'
        ELSE d.defaclobjtype::text
    END AS object_type,
    array_agg((acl).privilege_type ORDER BY (acl).privilege_type) AS privileges,
    bool_or((acl).is_grantable) AS with_grant_option,
    pg_get_userbyid(d.defaclrole) AS grantor
FROM pg_default_acl d
JOIN pg_namespace n ON n.oid = d.defaclnamespace
CROSS JOIN LATERAL aclexplode(d.defaclacl) AS acl
WHERE n.nspname = %s
GROUP BY (acl).grantee, d.defaclobjtype, d.defaclrole
ORDER BY object_type, grantee
"""


def fetch_schema_header(
    cursor: Any, schema: str, engine: SqlEngine = SqlEngine.postgresql
) -> SchemaHeader:
    """Return the schema header (name, owner, comment).

    ``engine`` decides only whether the engine has a schema owner to name, which
    is why it defaults to a libpq engine: :data:`_SCHEMA_HEADER_SQL` runs
    unchanged on PostgreSQL and Redshift, and only DuckDB needs the other query.
    """
    if engine is SqlEngine.duckdb:
        cursor.execute(_SCHEMA_HEADER_SQL_DUCKDB, (schema,))
    else:
        cursor.execute(_SCHEMA_HEADER_SQL, (schema,))
    row = cursor.fetchone()
    if row is None:
        raise TargetNotFoundError(f'schema "{schema}" not found')
    return SchemaHeader(*row)


def fetch_schema_contents(
    cursor: Any, schema: str, engine: SqlEngine
) -> list[SchemaContentItem]:
    """Return relations contained in the schema, labelled by kind."""
    if engine is SqlEngine.duckdb:
        cursor.execute(_SCHEMA_CONTENTS_SQL_DUCKDB, (schema,))
    elif engine == SqlEngine.redshift:
        cursor.execute(_SCHEMA_CONTENTS_SQL_REDSHIFT, (schema, schema, schema))
    else:
        cursor.execute(_SCHEMA_CONTENTS_SQL_POSTGRES, (schema,))
    return [
        SchemaContentItem(
            name=name,
            kind=_KIND_LABEL.get(kind, kind),
            owner=owner,
            row_estimate=row_estimate,
            size_bytes=size_bytes,
        )
        for name, kind, owner, row_estimate, size_bytes in cursor.fetchall()
    ]


def fetch_schema_privileges(
    cursor: Any, schema: str, engine: SqlEngine
) -> list[PrivilegeGrant]:
    """Return USAGE + CREATE grants for the schema.

    On PostgreSQL these come from ``pg_namespace.nspacl``, which lists explicit
    ACL entries plus the owner's implicit default; a role that only holds the
    privilege through membership in a granted role is not a separate row.
    ``with_grant_option`` follows the server's rule rather than the ACL bit, so
    an owner reports the grant option it really has -- the same answer
    :func:`fetch_relation_privileges` gets from ``information_schema``.

    DuckDB gets ``[]`` and no query. It has ``pg_namespace.nspacl`` (always NULL)
    but no ``aclexplode()`` to expand one and no ``pg_roles`` to scan, and its
    ``has_schema_privilege()`` returns true unconditionally for the single
    implicit user -- so the Redshift scan would report exactly one grantee that
    means nothing. :func:`schema_not_applicable` says that instead.
    """
    if engine is SqlEngine.duckdb:
        return []
    if engine == SqlEngine.redshift:
        cursor.execute(_SCHEMA_PRIVILEGES_SQL_REDSHIFT, (schema, schema))
    else:
        cursor.execute(_SCHEMA_PRIVILEGES_SQL_POSTGRES, (schema,))
    return [PrivilegeGrant(*row) for row in cursor.fetchall()]


@dataclass(frozen=True)
class DefaultPrivilegeGrant:
    """A row from ``pg_default_acl`` — privileges that future objects inherit.

    Set via
    ``ALTER DEFAULT PRIVILEGES IN SCHEMA <schema> GRANT ... ON TABLES TO <role>``.
    """

    grantee: str
    object_type: str  # TABLE / SEQUENCE / FUNCTION / TYPE / SCHEMA
    privileges: list[str]
    with_grant_option: bool
    grantor: str  # the role whose creations the defaults apply to


def fetch_schema_default_privileges(
    cursor: Any, schema: str, engine: SqlEngine
) -> list[DefaultPrivilegeGrant]:
    """Return default privileges defined for future objects in the schema.

    Reads ``pg_default_acl`` aggregated by (grantee, object type, grantor role).
    Returns ``[]`` on Redshift (lacks ``pg_default_acl`` semantics) and on DuckDB
    (no ``pg_default_acl``, and no roles for a default grant to name).
    """
    if engine in {SqlEngine.redshift, SqlEngine.duckdb}:
        return []
    cursor.execute(_SCHEMA_DEFAULT_PRIVILEGES_SQL, (schema,))
    return [
        DefaultPrivilegeGrant(
            grantee=grantee,
            object_type=object_type,
            privileges=list(privileges or []),
            with_grant_option=bool(with_grant_option),
            grantor=grantor or "",
        )
        for (
            grantee,
            object_type,
            privileges,
            with_grant_option,
            grantor,
        ) in cursor.fetchall()
    ]


@dataclass(frozen=True)
class RedshiftDistribution:
    diststyle: str
    distkey: str | None
    sortkey_style: str | None
    sortkeys: list[str]


@dataclass(frozen=True)
class RedshiftTableStats:
    skew_rows: float | None
    unsorted_pct: float | None
    stats_off: bool | None


_REDSHIFT_DIST_SQL = """
SELECT
    diststyle, distkey, sortkey1 AS sortkey_style,
    ARRAY(
      SELECT column_name
      FROM svv_redshift_columns
      WHERE schema_name = %s AND table_name = %s AND sortkey_position > 0
      ORDER BY sortkey_position
    ) AS sortkeys
FROM svv_table_info
WHERE "schema" = %s AND "table" = %s
"""


_REDSHIFT_STATS_SQL = """
SELECT skew_rows, unsorted, stats_off
FROM svv_table_info
WHERE "schema" = %s AND "table" = %s
"""


def fetch_redshift_distribution(
    cursor: Any, schema: str, name: str
) -> RedshiftDistribution | None:
    """Return Redshift distribution + sort key info, or None if no row."""
    cursor.execute(_REDSHIFT_DIST_SQL, (schema, name, schema, name))
    row = cursor.fetchone()
    if row is None:
        return None
    diststyle, distkey, sortkey_style, sortkeys = row
    return RedshiftDistribution(
        diststyle=diststyle,
        distkey=distkey,
        sortkey_style=sortkey_style,
        sortkeys=list(sortkeys or []),
    )


def fetch_redshift_table_stats(
    cursor: Any, schema: str, name: str
) -> RedshiftTableStats | None:
    """Return Redshift table stats (skew, unsorted %, stats_off), or None if no row."""
    cursor.execute(_REDSHIFT_STATS_SQL, (schema, name))
    row = cursor.fetchone()
    if row is None:
        return None
    skew_rows, unsorted_pct, stats_off = row
    return RedshiftTableStats(
        skew_rows=skew_rows, unsorted_pct=unsorted_pct, stats_off=stats_off
    )


@dataclass(frozen=True)
class TableDescription:
    ref: TargetRef
    header: RelationHeader
    columns: list[ColumnInfo]
    constraints: ConstraintBundle
    indexes: list[IndexInfo]
    privileges: list[PrivilegeGrant]
    triggers: list[TriggerInfo]
    policies: list[PolicyInfo]
    policies_enabled: bool
    partitioning: PartitioningInfo
    redshift_distribution: RedshiftDistribution | None
    redshift_stats: RedshiftTableStats | None
    definition: str | None  # mview only
    # Sections this engine has no concept of. Defaulted so a libpq description
    # is built exactly as before, and empty for those engines today -- see
    # table_not_applicable.
    not_applicable: list[NotApplicable] = field(default_factory=list)


@dataclass(frozen=True)
class ViewDescription:
    ref: TargetRef
    header: RelationHeader
    columns: list[ColumnInfo]
    definition: ViewDefinition
    upstream: list[DependencyEdge]
    downstream: list[DependencyEdge]
    privileges: list[PrivilegeGrant]
    triggers: list[TriggerInfo]
    not_applicable: list[NotApplicable] = field(default_factory=list)


@dataclass(frozen=True)
class SchemaDescription:
    header: SchemaHeader
    privileges: list[PrivilegeGrant]
    contents: list[SchemaContentItem]
    default_privileges: list[DefaultPrivilegeGrant] = field(default_factory=list)
    not_applicable: list[NotApplicable] = field(default_factory=list)


def describe_table(cursor: Any, ref: TargetRef, engine: SqlEngine) -> TableDescription:
    """Compose a full table/matview description by invoking each fetcher."""
    assert ref.oid is not None and ref.name is not None
    header = fetch_relation_header(cursor, ref.oid, engine)
    columns = fetch_columns(cursor, ref.oid, engine)
    constraints = fetch_constraints(cursor, ref.oid, engine)
    indexes = fetch_indexes(cursor, ref.oid, engine)
    privileges = fetch_relation_privileges(cursor, ref.schema, ref.name, engine)
    triggers = fetch_triggers(cursor, ref.oid, engine)
    policies, policies_enabled = fetch_policies(cursor, ref.oid, engine)
    partitioning = fetch_partitioning(cursor, ref.oid, engine)

    redshift_distribution: RedshiftDistribution | None = None
    redshift_stats: RedshiftTableStats | None = None
    if engine == SqlEngine.redshift:
        redshift_distribution = fetch_redshift_distribution(
            cursor, ref.schema, ref.name
        )
        redshift_stats = fetch_redshift_table_stats(cursor, ref.schema, ref.name)

    definition: str | None = None
    if ref.kind == ObjectKind.matview and engine == SqlEngine.postgresql:
        definition = fetch_view_definition(cursor, ref.oid, engine).sql

    return TableDescription(
        ref=ref,
        header=header,
        columns=columns,
        constraints=constraints,
        indexes=indexes,
        privileges=privileges,
        triggers=triggers,
        policies=policies,
        policies_enabled=policies_enabled,
        partitioning=partitioning,
        redshift_distribution=redshift_distribution,
        redshift_stats=redshift_stats,
        definition=definition,
        not_applicable=table_not_applicable(engine),
    )


def describe_view(cursor: Any, ref: TargetRef, engine: SqlEngine) -> ViewDescription:
    """Compose a full view description by invoking each fetcher."""
    assert ref.oid is not None and ref.name is not None
    header = fetch_relation_header(cursor, ref.oid, engine)
    columns = fetch_columns(cursor, ref.oid, engine)
    definition = fetch_view_definition(cursor, ref.oid, engine)
    upstream = fetch_dependencies(cursor, ref.oid, direction="upstream", engine=engine)
    downstream = fetch_dependencies(
        cursor, ref.oid, direction="downstream", engine=engine
    )
    privileges = fetch_relation_privileges(cursor, ref.schema, ref.name, engine)
    triggers = fetch_triggers(cursor, ref.oid, engine)
    return ViewDescription(
        ref=ref,
        header=header,
        columns=columns,
        definition=definition,
        upstream=upstream,
        downstream=downstream,
        privileges=privileges,
        triggers=triggers,
        not_applicable=view_not_applicable(engine),
    )


def describe_schema(
    cursor: Any, ref: TargetRef, engine: SqlEngine
) -> SchemaDescription:
    """Compose a full schema description by invoking each fetcher."""
    header = fetch_schema_header(cursor, ref.schema, engine)
    privileges = fetch_schema_privileges(cursor, ref.schema, engine)
    default_privileges = fetch_schema_default_privileges(cursor, ref.schema, engine)
    contents = fetch_schema_contents(cursor, ref.schema, engine)
    return SchemaDescription(
        header=header,
        privileges=privileges,
        contents=contents,
        default_privileges=default_privileges,
        not_applicable=schema_not_applicable(engine),
    )


def fetch_constraints(cursor: Any, oid: int, engine: SqlEngine) -> ConstraintBundle:
    """Return the relation's primary key, foreign keys, uniques and checks.

    Redshift declares constraints but does not enforce or record them usefully,
    so it returns an empty bundle with no query. DuckDB enforces them and
    ``duckdb_constraints()`` records them fully -- see
    :data:`_CONSTRAINTS_SQL_DUCKDB` for why its ``pg_constraint`` is not used.
    """
    if engine == SqlEngine.redshift:
        return ConstraintBundle(
            primary_key=None,
            foreign_keys=[],
            unique_constraints=[],
            check_constraints=[],
        )
    if engine is SqlEngine.duckdb:
        cursor.execute(_CONSTRAINTS_SQL_DUCKDB, (oid,))
    else:
        cursor.execute(_CONSTRAINTS_SQL, (oid,))
    pk: PrimaryKeyInfo | None = None
    fks: list[ForeignKeyInfo] = []
    uniques: list[ConstraintInfo] = []
    checks: list[ConstraintInfo] = []
    for (
        name,
        kind,
        defn,
        local_cols,
        ref_table,
        ref_cols,
        del_t,
        upd_t,
        deferrable,
    ) in cursor.fetchall():
        if kind == "p":
            pk = PrimaryKeyInfo(name=name, columns=list(local_cols))
        elif kind == "f":
            fks.append(
                ForeignKeyInfo(
                    name=name,
                    columns=list(local_cols),
                    referenced_table=ref_table,
                    referenced_columns=list(ref_cols or []),
                    on_update=_FK_ACTION_MAP.get(upd_t, "NO ACTION"),
                    on_delete=_FK_ACTION_MAP.get(del_t, "NO ACTION"),
                    deferrable=bool(deferrable),
                )
            )
        elif kind == "u":
            uniques.append(ConstraintInfo(name=name, definition=defn))
        elif kind == "c":
            checks.append(ConstraintInfo(name=name, definition=defn))
    return ConstraintBundle(pk, fks, uniques, checks)
