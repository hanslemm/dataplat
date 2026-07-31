"""Per-engine SQL for schema inspection.

Three engines, three catalogs that only look alike. PostgreSQL resolves a
schema's owner through ``pg_roles``; Redshift through ``pg_user``, and adds
quotas that live in a view not every cluster has; DuckDB has ``pg_namespace`` and
``pg_class`` but **no ``pg_roles`` at all**, so the Postgres statement fails on
it outright rather than degrading.

That last one is measured, not assumed::

    SELECT rolname FROM pg_roles LIMIT 1
    CatalogException: Table with name pg_roles does not exist!

DuckDB also binds ``?`` rather than ``%s``, and this codebase deliberately does
not translate placeholders (see ``DuckDbCursor``) — so its constants are written
separately rather than shared and patched, which is the same house rule that
keeps the Redshift text split from the Postgres text.
"""

from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod
from typing import Any

from dataplat.services.db._savepoint import guarded_fetch
from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.schema_admin import SchemaSummary

__all__ = [
    "DuckDbSchemaDialect",
    "PostgresSchemaDialect",
    "RedshiftSchemaDialect",
    "SchemaDialect",
    "schema_dialect_for",
]

# Hide the catalogs nobody administers. `pg_%` covers pg_catalog, pg_toast and
# the pg_temp_* / pg_toast_temp_* families in one predicate.
#
# The escape character is `#`, not a backslash, for the reason spelled out at
# length in _like.py: Redshift runs with `standard_conforming_strings` off, where
# a backslash escapes its own closing quote and the statement never parses.
#
# The wildcard is doubled (`%%`) because this text only ever reaches the server
# through _roster, which always calls `cursor.execute(sql, params)` with a params
# tuple — _where_clause returns `()` at minimum, never None. psycopg 3 parses
# %-placeholders client-side whenever `params is not None`, so `%%` is what
# reduces back to a literal `%`. A caller that executed this text with
# `params=None` would send a literal `%%` and match nothing.
_HIDE_SYSTEM = (
    "n.nspname NOT LIKE 'pg#_%%' ESCAPE '#' AND n.nspname <> 'information_schema'"
)

# Object counts as one grouped subquery rather than a correlated count per
# schema. SUM(CASE …) rather than COUNT(*) FILTER: Redshift's leader node is
# PostgreSQL 8.0.2 and FILTER arrived in 9.4.
#
# Bucketed by what a DROP cares about rather than by the two relkinds this
# started with. 'r' (table), 'p' (partitioned) and 'f' (foreign) all read as "a
# table" to an operator; 'v' and 'm' both read as "a view"; everything else that
# still blocks RESTRICT and is still destroyed by CASCADE — 'S' (sequence), 'c'
# (composite type) — lands in `other` so a drop pre-flight can never report a
# schema as emptier than it is.
_RELATION_COUNTS = """
    SELECT relnamespace,
           SUM(CASE WHEN relkind IN ('r', 'p', 'f') THEN 1 ELSE 0 END) AS tables,
           SUM(CASE WHEN relkind IN ('v', 'm') THEN 1 ELSE 0 END) AS views,
           SUM(CASE WHEN relkind IN ('S', 'c') THEN 1 ELSE 0 END) AS other
    FROM pg_class
    GROUP BY relnamespace
"""

_LIST_POSTGRES = f"""
SELECT n.nspname,
       COALESCE(r.rolname, '?') AS owner,
       COALESCE(c.tables, 0) AS tables,
       COALESCE(c.views, 0) AS views,
       COALESCE(c.other, 0) AS other
FROM pg_namespace n
LEFT JOIN pg_roles r ON r.oid = n.nspowner
LEFT JOIN ({_RELATION_COUNTS}) c ON c.relnamespace = n.oid
{{where}}
ORDER BY n.nspname
"""

# Redshift resolves the owner through pg_user, which has no rows for a group or
# an RBAC role — a schema owned by one reports '?' rather than a wrong name.
_LIST_REDSHIFT = f"""
SELECT n.nspname,
       COALESCE(u.usename, '?') AS owner,
       COALESCE(c.tables, 0) AS tables,
       COALESCE(c.views, 0) AS views,
       COALESCE(c.other, 0) AS other
FROM pg_namespace n
LEFT JOIN pg_user u ON u.usesysid = n.nspowner
LEFT JOIN ({_RELATION_COUNTS}) c ON c.relnamespace = n.oid
{{where}}
ORDER BY n.nspname
"""

# No owner join: DuckDB has no pg_roles, and every connection is the same
# implicit user, so ownership is a question the engine cannot answer differently
# for two schemas. `?` placeholders, per this codebase's no-translation rule.
_LIST_DUCKDB = f"""
SELECT n.nspname,
       'duckdb' AS owner,
       COALESCE(c.tables, 0) AS tables,
       COALESCE(c.views, 0) AS views,
       COALESCE(c.other, 0) AS other
FROM pg_namespace n
LEFT JOIN ({_RELATION_COUNTS}) c ON c.relnamespace = n.oid
{{where}}
ORDER BY n.nspname
"""

# Named rather than pattern-matched: DuckDB has no pg_temp_* families, and its
# catalog schemas share no prefix.
#
# `main` is deliberately NOT in this list. It is DuckDB's *default* schema — the
# analogue of Postgres's `public`, which this module also leaves visible — so a
# database whose tables all live in `main` must not list as empty. That was a real
# bug in the first version of this predicate, caught by the DuckDB tier.
#
# In practice DuckDB's `pg_namespace` returns only `main` and user schemas:
# `information_schema`, `pg_catalog` and temp schemas are flagged `internal` in
# `duckdb_schemas()` and never surface here at all. The names below are therefore
# defensive rather than load-bearing today, and `--include-system` has nothing
# extra to reveal on this engine.
_HIDE_SYSTEM_DUCKDB = "n.nspname NOT IN ('information_schema', 'pg_catalog', 'temp')"

# Availability varies by cluster version and by grantee, so this is always
# guarded and its absence degrades to unknown rather than failing the listing.
_QUOTA_STATE_REDSHIFT = """
SELECT schema_name, quota, disk_usage
FROM svv_schema_quota_state
"""


def _where_clause(
    *, include_system: bool, like: str | None, system_predicate: str, placeholder: str
) -> tuple[str, tuple[str, ...]]:
    """Compose the WHERE clause and its bound parameters."""
    predicates: list[str] = []
    params: list[str] = []
    if not include_system:
        predicates.append(system_predicate)
    if like is not None:
        predicates.append(f"n.nspname LIKE {placeholder}")
        params.append(like)
    if not predicates:
        return "", ()
    return "WHERE " + " AND ".join(predicates), tuple(params)


class SchemaDialect(ABC):
    """Strategy for one SQL engine."""

    engine: SqlEngine

    #: Predicate hiding the catalogs nobody administers on this engine.
    system_predicate: str = _HIDE_SYSTEM
    #: The engine's bound-parameter placeholder.
    placeholder: str = "%s"

    def schema_exists(self, cursor: Any, name: str) -> bool:
        """``pg_namespace`` is present and means the same thing on all three."""
        cursor.execute(
            f"SELECT 1 FROM pg_namespace WHERE nspname = {self.placeholder}", (name,)
        )
        return cursor.fetchone() is not None

    @abstractmethod
    def list_schemas(
        self, cursor: Any, *, include_system: bool = False, like: str | None = None
    ) -> list[SchemaSummary]: ...

    def _roster(
        self, cursor: Any, query: str, *, include_system: bool, like: str | None
    ) -> list[SchemaSummary]:
        where, params = _where_clause(
            include_system=include_system,
            like=like,
            system_predicate=self.system_predicate,
            placeholder=self.placeholder,
        )
        cursor.execute(query.format(where=where), params)
        return [
            SchemaSummary(
                name=name,
                owner=owner,
                tables=int(tables or 0),
                views=int(views or 0),
                other=int(other or 0),
            )
            for name, owner, tables, views, other in cursor.fetchall()
        ]


class PostgresSchemaDialect(SchemaDialect):
    engine = SqlEngine.postgresql

    def list_schemas(
        self, cursor: Any, *, include_system: bool = False, like: str | None = None
    ) -> list[SchemaSummary]:
        return self._roster(
            cursor, _LIST_POSTGRES, include_system=include_system, like=like
        )


class RedshiftSchemaDialect(SchemaDialect):
    engine = SqlEngine.redshift

    def list_schemas(
        self, cursor: Any, *, include_system: bool = False, like: str | None = None
    ) -> list[SchemaSummary]:
        rows = self._roster(
            cursor, _LIST_REDSHIFT, include_system=include_system, like=like
        )
        quota = self._quota_state(cursor)
        if not quota:
            return rows
        return [
            dataclasses.replace(
                row,
                quota_mb=quota.get(row.name, (None, None))[0],
                used_mb=quota.get(row.name, (None, None))[1],
            )
            for row in rows
        ]

    def _quota_state(self, cursor: Any) -> dict[str, tuple[int | None, int | None]]:
        """``{schema: (quota_mb, used_mb)}``; empty when the view is unavailable.

        ``svv_schema_quota_state`` reports both values in MB. A cluster without
        the view yields ``{}``, so every schema keeps ``None`` and renders as
        unknown — never as zero, which would read as "no quota set".
        """
        rows = guarded_fetch(cursor, _QUOTA_STATE_REDSHIFT, savepoint="dp_schema_quota")
        if rows is None:
            return {}
        return {
            name: (
                int(quota_mb) if quota_mb is not None else None,
                int(used_mb) if used_mb is not None else None,
            )
            for name, quota_mb, used_mb in rows
        }


class DuckDbSchemaDialect(SchemaDialect):
    engine = SqlEngine.duckdb
    system_predicate = _HIDE_SYSTEM_DUCKDB
    placeholder = "?"

    def list_schemas(
        self, cursor: Any, *, include_system: bool = False, like: str | None = None
    ) -> list[SchemaSummary]:
        return self._roster(
            cursor, _LIST_DUCKDB, include_system=include_system, like=like
        )


def schema_dialect_for(engine: SqlEngine) -> SchemaDialect:
    """Return the schema dialect for ``engine`` (defaults to Postgres)."""
    if engine == SqlEngine.redshift:
        return RedshiftSchemaDialect()
    if engine == SqlEngine.duckdb:
        return DuckDbSchemaDialect()
    return PostgresSchemaDialect()
