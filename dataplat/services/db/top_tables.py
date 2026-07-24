"""Rank the largest tables in schemas matching one or more prefixes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from dataplat.services.db.connection import SqlEngine


@dataclass(frozen=True)
class TopTableRow:
    """One row in the top-tables ranking."""

    schema: str
    name: str
    kind: str  # 'r' = table, 'p' = partitioned, 'm' = matview
    owner: str | None
    row_estimate: int | None
    size_bytes: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TopTablesResult:
    """Top-N rows, totals across matched schemas, and whole-database size.

    - ``matched_bytes`` / ``matched_count``: sum/count across *all* tables
        whose schema matches the requested prefixes.
    - ``disk_bytes``: total on-disk size used by the current database
        (Postgres: ``pg_database_size``; Redshift: sum of
        ``svv_table_info.size``). Used as the denominator for
        percentage-of-disk reporting.
    """

    rows: list[TopTableRow] = field(default_factory=list)
    matched_bytes: int = 0
    matched_count: int = 0
    disk_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": [r.to_dict() for r in self.rows],
            "matched_bytes": self.matched_bytes,
            "matched_count": self.matched_count,
            "disk_bytes": self.disk_bytes,
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


def _like_escape(prefix: str) -> str:
    """Escape SQL LIKE metacharacters so the prefix matches literally."""
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _build_schema_where(column: str, prefixes: list[str]) -> tuple[str, list[str]]:
    """Return a ``<col> LIKE %s ESCAPE '\\' OR ...`` clause and its params."""
    clauses: list[str] = []
    params: list[str] = []
    for p in prefixes:
        clauses.append(f"{column} LIKE %s ESCAPE '\\'")
        params.append(f"{_like_escape(p)}%")
    return " OR ".join(clauses), params


def _sql_templates(engine: SqlEngine) -> tuple[str, str, str]:
    """Return ``(rows_sql, totals_sql, schema_column)`` for the engine."""
    if engine is SqlEngine.redshift:
        return _REDSHIFT_ROWS_SQL, _REDSHIFT_TOTALS_SQL, '"schema"'
    return _POSTGRES_ROWS_SQL, _POSTGRES_TOTALS_SQL, "n.nspname"


def fetch_top_tables(
    cursor: Any,
    engine: SqlEngine,
    schema_prefixes: list[str],
    limit: int,
) -> TopTablesResult:
    """Return the largest tables whose schema starts with any given prefix.

    Rows are ordered by total size descending and capped at ``limit``;
    ``total_bytes`` and ``total_count`` are the sums across *all* matching
    tables (not just the top-N), which lets callers compute each row's
    share of the matched universe.

    Postgres size includes heap + indexes + toast via
    ``pg_total_relation_size``; Redshift reports compressed on-cluster size
    from ``svv_table_info.size`` (MB → bytes).
    """
    if not schema_prefixes or limit <= 0:
        return TopTablesResult()

    rows_sql, totals_sql, schema_col = _sql_templates(engine)
    where, where_params = _build_schema_where(schema_col, schema_prefixes)

    cursor.execute(totals_sql.format(schema_where=where), tuple(where_params))
    totals_row = cursor.fetchone() or (0, 0, 0)
    matched_bytes = int(totals_row[0] or 0)
    matched_count = int(totals_row[1] or 0)
    disk_bytes = int(totals_row[2] or 0)

    if matched_count == 0:
        return TopTablesResult(disk_bytes=disk_bytes)

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
    """Quote a SQL identifier, escaping embedded double quotes."""
    return '"' + name.replace('"', '""') + '"'


def drop_statement(row: TopTableRow) -> str:
    """Return a ``DROP ...`` statement for the given row (no execution)."""
    qualified = f"{_quote_ident(row.schema)}.{_quote_ident(row.name)}"
    if row.kind == "m":
        return f"DROP MATERIALIZED VIEW IF EXISTS {qualified};"
    return f"DROP TABLE IF EXISTS {qualified};"
