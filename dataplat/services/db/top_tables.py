"""Rank the largest tables in schemas matching one or more prefixes.

Three engines answer "how big is this table?" from three different places, and
one of them cannot answer it at all:

- **PostgreSQL** — ``pg_total_relation_size`` (heap + indexes + toast) per
  relation, ``pg_database_size`` for the whole database.
- **Redshift** — ``svv_table_info.size``, the compressed on-cluster size in MB.
- **DuckDB** — *nothing*. It has no per-relation size function and no catalog
  column carrying bytes, so its ranking is by estimated row count and every
  ``size_bytes`` is ``None``. See :data:`SIZE_BASIS` and
  :data:`_DUCKDB_ROWS_SQL` for the evidence; this is the one fact about this
  module a reader has to know before comparing two engines' output.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from dataplat.services.db._like import LIKE_ESCAPE_CLAUSE, like_escape
from dataplat.services.db.connection import SqlEngine


@dataclass(frozen=True)
class TopTableRow:
    """One row in the top-tables ranking.

    ``size_bytes`` is ``None`` when the engine does not report one — always, on
    DuckDB. It is not zero: a table of unknown size is not an empty table, and
    the renderers print "—" for ``None`` where they would print "0 B" for 0.
    """

    schema: str
    name: str
    # 'r' = table, 'p' = partitioned, 'm' = matview. DuckDB is always 'r': it
    # has no partitioned tables and no materialized views, and duckdb_tables()
    # does not list views at all — so drop_statement's 'm' branch is
    # unreachable there, which is what makes a plain DROP TABLE correct.
    kind: str
    owner: str | None
    row_estimate: int | None
    size_bytes: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TopTablesResult:
    """Top-N rows, totals across matched schemas, and whole-database size.

    - ``matched_bytes`` / ``matched_count``: sum/count across *all* tables
        whose schema matches the requested prefixes. ``matched_bytes`` is
        ``None`` when the engine reports no per-table size (DuckDB), for the
        same reason :class:`TopTableRow` uses ``None`` rather than 0.
    - ``disk_bytes``: total on-disk size used by the current database
        (Postgres: ``pg_database_size``; Redshift: sum of
        ``svv_table_info.size``; DuckDB: ``pragma_database_size()``'s
        ``total_blocks * block_size``). Used as the denominator for
        percentage-of-disk reporting — but only where the numerator exists, so
        on DuckDB it is a standalone figure rather than a denominator.
    """

    rows: list[TopTableRow] = field(default_factory=list)
    matched_bytes: int | None = 0
    matched_count: int = 0
    disk_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": [r.to_dict() for r in self.rows],
            "matched_bytes": self.matched_bytes,
            "matched_count": self.matched_count,
            "disk_bytes": self.disk_bytes,
        }


# What the numbers in a result actually are, per engine, in one sentence. Lives
# next to the SQL that produces them so the two cannot drift, and is exported
# because both renderings need it: the text report prints it where a reader will
# see it, and --json ships it so a script summing two engines' output can tell
# that it must not.
SIZE_BASIS: dict[SqlEngine, str] = {
    SqlEngine.postgresql: (
        "sizes are pg_total_relation_size (heap + indexes + toast); disk is "
        "pg_database_size(current_database())"
    ),
    SqlEngine.redshift: (
        "sizes are svv_table_info.size, the compressed on-cluster size in MB; "
        "disk is the sum of svv_table_info.size across the cluster"
    ),
    SqlEngine.duckdb: (
        "DuckDB reports no per-table byte size, so sizes are unknown and rows "
        "are ranked by duckdb_tables().estimated_size, which is an estimated "
        "row count and not bytes; disk is pragma_database_size()'s "
        "total_blocks × block_size — the whole database file, including free "
        "blocks and every schema in it, so it is not comparable to "
        "PostgreSQL's per-relation pg_total_relation_size"
    ),
}


_POSTGRES_ROWS_SQL = """
SELECT
    n.nspname AS schema,
    c.relname AS name,
    c.relkind AS kind,
    pg_get_userbyid(c.relowner) AS owner,
    CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END AS row_estimate,
    pg_total_relation_size(c.oid) AS size_bytes
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'm')
  AND ({schema_where})
ORDER BY size_bytes DESC NULLS LAST, schema, name
LIMIT %s
"""


_POSTGRES_TOTALS_SQL = """
SELECT
    COALESCE(SUM(pg_total_relation_size(c.oid)), 0)::bigint AS matched_bytes,
    COUNT(*)::bigint AS matched_count,
    pg_database_size(current_database())::bigint AS disk_bytes
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'm')
  AND ({schema_where})
"""


_REDSHIFT_ROWS_SQL = """
SELECT
    "schema" AS schema,
    "table" AS name,
    'r'::varchar AS kind,
    NULL::varchar AS owner,
    tbl_rows::bigint AS row_estimate,
    size::bigint * 1024 * 1024 AS size_bytes
FROM svv_table_info
WHERE ({schema_where})
ORDER BY size_bytes DESC NULLS LAST, schema, name
LIMIT %s
"""


_REDSHIFT_TOTALS_SQL = """
SELECT
    COALESCE(SUM(size::bigint), 0) * 1024 * 1024 AS matched_bytes,
    COUNT(*)::bigint AS matched_count,
    (
        SELECT COALESCE(SUM(size::bigint), 0) * 1024 * 1024
        FROM svv_table_info
    ) AS disk_bytes
FROM svv_table_info
WHERE ({schema_where})
"""


# DuckDB has no pg_total_relation_size, no pg_database_size, and — probed on
# 1.5.5 — no catalog column carrying a table's byte size either:
# duckdb_tables().estimated_size is the *cardinality* estimate (a 200k-row table
# of 1 KB strings and a 200k-row table of one bigint both report 200000), and
# pg_class.relpages is 0 for every relation. pragma_storage_info('t') is the
# only per-table storage view, and it is not a size: it lists segments whose
# block_id values are shared between columns and between tables, so summing it
# would double-count, and it needs one statement per table.
#
# So the honest ranking key here is rows, and size_bytes is NULL rather than a
# number that would be read as bytes. Withdrawing the claim beats a confident
# falsehood (CONTRIBUTING: "prefer 'unknown' to a confident falsehood").
#
# `NOT internal` excludes catalog tables, which is what keeps
# `--schema-prefix pg_` from ranking the catalog itself, and the
# `database_name = current_database()` filter keeps an ATTACHed database's
# tables out — they are in another catalog, so the DROP statements this command
# generates would not resolve to them.
_DUCKDB_ROWS_SQL = """
SELECT
    t.schema_name AS schema,
    t.table_name AS name,
    'r' AS kind,
    NULL AS owner,
    t.estimated_size AS row_estimate,
    NULL AS size_bytes
FROM duckdb_tables() t
WHERE t.database_name = current_database()
  AND NOT t.internal
  AND ({schema_where})
ORDER BY row_estimate DESC NULLS LAST, schema, name
LIMIT ?
"""


# matched_bytes is NULL for the reason above; fetch_top_tables turns that into
# None. disk_bytes is the file, not a sum of the matched tables: it is the one
# size figure DuckDB does report, and it is reported per attached database, so
# it needs the same current_database() filter.
_DUCKDB_TOTALS_SQL = """
SELECT
    NULL AS matched_bytes,
    COUNT(*)::bigint AS matched_count,
    (
        SELECT COALESCE(s.block_size * s.total_blocks, 0)::bigint
        FROM pragma_database_size() s
        WHERE s.database_name = current_database()
    ) AS disk_bytes
FROM duckdb_tables() t
WHERE t.database_name = current_database()
  AND NOT t.internal
  AND ({schema_where})
"""


def _build_schema_where(
    column: str, prefixes: list[str], *, marker: str = "%s"
) -> tuple[str, list[str]]:
    """Return a ``<col> LIKE %s ESCAPE '\\' OR ...`` clause and its params.

    ``marker`` is the driver's placeholder: psycopg binds ``%s``, DuckDB binds
    ``?`` and rejects ``%s`` with a ParserException (probed on 1.5.5). Nothing
    translates between them — see :class:`~dataplat.cli.db._common.DuckDbCursor`
    — so the statement has to be built with the right one. ``ESCAPE '\\'``
    itself needs no dialect split: DuckDB accepts it as PostgreSQL and Redshift
    do.
    """
    clauses: list[str] = []
    params: list[str] = []
    for p in prefixes:
        clauses.append(f"{column} LIKE {marker} {LIKE_ESCAPE_CLAUSE}")
        params.append(f"{like_escape(p)}%")
    return " OR ".join(clauses), params


def _sql_templates(engine: SqlEngine) -> tuple[str, str, str, str]:
    """Return ``(rows_sql, totals_sql, schema_column, param_marker)``."""
    if engine is SqlEngine.redshift:
        return _REDSHIFT_ROWS_SQL, _REDSHIFT_TOTALS_SQL, '"schema"', "%s"
    if engine is SqlEngine.duckdb:
        return _DUCKDB_ROWS_SQL, _DUCKDB_TOTALS_SQL, "t.schema_name", "?"
    return _POSTGRES_ROWS_SQL, _POSTGRES_TOTALS_SQL, "n.nspname", "%s"


def fetch_top_tables(
    cursor: Any,
    engine: SqlEngine,
    schema_prefixes: list[str],
    limit: int,
) -> TopTablesResult:
    """Return the largest tables whose schema starts with any given prefix.

    Rows are ordered by total size descending — by estimated row count on
    DuckDB, which has no size — and capped at ``limit``;
    ``matched_bytes`` and ``matched_count`` are the sums across *all* matching
    tables (not just the top-N), which lets callers compute each row's
    share of the matched universe.

    Postgres size includes heap + indexes + toast via
    ``pg_total_relation_size``; Redshift reports compressed on-cluster size
    from ``svv_table_info.size`` (MB → bytes). **DuckDB reports no size at
    all**: rows come back ranked by estimated row count with ``size_bytes`` and
    ``matched_bytes`` set to ``None``, so a caller that renders a size must
    render "unknown" rather than 0. :data:`SIZE_BASIS` is that sentence in a
    form the caller can print.
    """
    if not schema_prefixes or limit <= 0:
        return TopTablesResult()

    rows_sql, totals_sql, schema_col, marker = _sql_templates(engine)
    where, where_params = _build_schema_where(
        schema_col, schema_prefixes, marker=marker
    )

    cursor.execute(totals_sql.format(schema_where=where), tuple(where_params))
    totals_row = cursor.fetchone() or (0, 0, 0)
    # `is None` rather than `or 0`: the engines that can size a table COALESCE
    # to 0 in SQL, so a NULL here means "this engine does not report bytes" and
    # must survive as None all the way to the renderer.
    matched_bytes = None if totals_row[0] is None else int(totals_row[0])
    matched_count = int(totals_row[1] or 0)
    disk_bytes = int(totals_row[2] or 0)

    if matched_count == 0:
        # matched_bytes is carried through rather than defaulted: on DuckDB it
        # is None, and "0 B matched" would be a claim the engine never made.
        return TopTablesResult(matched_bytes=matched_bytes, disk_bytes=disk_bytes)

    cursor.execute(rows_sql.format(schema_where=where), (*where_params, limit))
    rows = [
        TopTableRow(
            schema=row[0],
            name=row[1],
            kind=row[2],
            owner=row[3],
            row_estimate=row[4],
            size_bytes=row[5],
        )
        for row in cursor.fetchall()
    ]
    return TopTablesResult(
        rows=rows,
        matched_bytes=matched_bytes,
        matched_count=matched_count,
        disk_bytes=disk_bytes,
    )


def _quote_ident(name: str) -> str:
    """Quote a SQL identifier, escaping embedded double quotes.

    One spelling for all three engines: DuckDB quotes identifiers the same way
    and accepts the doubled-quote escape (probed on 1.5.5 with a table named
    ``has"quote``).
    """
    return '"' + name.replace('"', '""') + '"'


def drop_statement(row: TopTableRow) -> str:
    """Return a ``DROP ...`` statement for the given row (no execution).

    Valid on all three engines. The matview branch cannot fire for a DuckDB row
    — see :class:`TopTableRow.kind` — which matters because DuckDB rejects
    ``DROP MATERIALIZED VIEW`` outright, with "Cannot drop this type yet".
    """
    qualified = f"{_quote_ident(row.schema)}.{_quote_ident(row.name)}"
    if row.kind == "m":
        return f"DROP MATERIALIZED VIEW IF EXISTS {qualified};"
    return f"DROP TABLE IF EXISTS {qualified};"
