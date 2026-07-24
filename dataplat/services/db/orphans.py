"""Orphan dbt table rename operations.

Renames tables/views/materialized views with a ``_deprecated`` suffix so
they can be dropped after a grace period, and reverts them later. Raw
schemas (``raw`` on Postgres, ``_raw`` on Redshift) and the dbt_artifacts
schema itself are always excluded.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Literal, TypedDict

import psycopg
from psycopg import sql

from dataplat.core.errors import ConfigError
from dataplat.services.db.connection import (
    DbConnectionParams,
    SqlEngine,
    resolve_connection_params,
)

DEPRECATED_SUFFIX = "_deprecated"
DBT_ARTIFACTS_SCHEMA = "dbt_artifacts"

_DEFAULT_EXCLUDED_SCHEMAS: frozenset[str] = frozenset(
    {"raw", "_raw", DBT_ARTIFACTS_SCHEMA}
)


def excluded_schemas() -> frozenset[str]:
    """Schemas never scanned for orphans.

    ``DP_DBT_ORPHANS_EXCLUDE_SCHEMAS`` (comma-separated) replaces the
    default set (``raw``, ``_raw``, ``dbt_artifacts``) when set.
    """
    raw = os.getenv("DP_DBT_ORPHANS_EXCLUDE_SCHEMAS")
    if raw is None:
        return _DEFAULT_EXCLUDED_SCHEMAS
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


def node_prefix() -> str:
    """dbt node-id prefix (``model.<project>.``) from ``DP_DBT_PROJECT``."""
    project = os.getenv("DP_DBT_PROJECT", "").strip()
    if not project:
        raise ConfigError(
            "DP_DBT_PROJECT must be set (your dbt project name) to scan for "
            "dbt orphans."
        )
    return f"model.{project}."


def invocation_command() -> str | None:
    """Optional ``invocation_command`` filter from ``DP_DBT_INVOCATION_COMMAND``.

    When unset, all ``dbt build`` invocations count.
    """
    return os.getenv("DP_DBT_INVOCATION_COMMAND") or None
LIVE_STATUSES: frozenset[str] = frozenset({"success", "error"})

ObjectKind = Literal["table", "view", "matview"]

# Excludes partition-child tables (rows in pg_inherits whose parent has
# relkind='p'). The orphan-rename flow treats them as standalone tables
# otherwise, which silently detaches them from their partitioned parent.
# Redshift has no declarative partitioning, so this filter is Postgres-only.
_PARTITION_CHILD_FILTER = """
AND NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_inherits inh
    JOIN pg_catalog.pg_class child_cls ON child_cls.oid = inh.inhrelid
    JOIN pg_catalog.pg_namespace child_ns ON child_ns.oid = child_cls.relnamespace
    JOIN pg_catalog.pg_class parent_cls ON parent_cls.oid = inh.inhparent
    WHERE child_ns.nspname = t.table_schema
      AND child_cls.relname = t.table_name
      AND parent_cls.relkind = 'p'
)
"""


class RenameEntry(TypedDict):
    """Serialized record of a single rename, written to the audit log."""

    database: str
    schema: str
    old_name: str
    new_name: str
    kind: ObjectKind


class DropEntry(TypedDict):
    """Serialized record of a single drop, written to the purge log."""

    database: str
    schema: str
    name: str
    kind: ObjectKind


def resolve_orphans_connection_params(
    engine: SqlEngine, *, env_prefix: str
) -> DbConnectionParams | None:
    """Resolve connection params using the repo's ``<PREFIX>_*`` env convention.

    Delegates to :func:`resolve_connection_params` so ``dp db dbt-orphans``
    shares the same credential source as ``dp db query`` (the target's
    ``<PREFIX>_*`` env vars).

    Returns ``None`` if host/user/password/database are missing so callers can
    skip the engine instead of failing the whole run. Raises ``ConfigError``
    for other configuration problems (e.g., non-integer port).
    """
    prefix = env_prefix.strip().upper().rstrip("_")
    host = os.getenv(f"{prefix}_HOST")
    user = os.getenv(f"{prefix}_USER")
    password = os.getenv(f"{prefix}_PASSWORD")
    database = (
        os.getenv(f"{prefix}_DATABASE")
        or os.getenv(f"{prefix}_DB")
        or os.getenv(f"{prefix}_NAME")
    )
    if not (host and user and password and database):
        return None

    return resolve_connection_params(
        engine=engine,
        env_prefix=prefix,
        user=None,
        password=None,
        host=None,
        port=None,
        database=None,
        sslmode=None,
    )


@contextmanager
def open_transactional_connection(
    params: DbConnectionParams, *, dry_run: bool
) -> Iterator[psycopg.Connection]:
    """Open a psycopg connection that commits on clean exit (rolls back on dry-run)."""
    kwargs = params.as_psycopg_kwargs()
    conn = psycopg.connect(**kwargs)  # type: ignore[arg-type]
    # Disable auto-preparation: Redshift does not support ``DEALLOCATE ALL``,
    # which psycopg issues during rollback to clean up prepared statements.
    conn.prepare_threshold = None
    try:
        yield conn
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def classify_object(
    cur: Any, schema: str, name: str, *, is_redshift: bool
) -> ObjectKind | None:
    """Return the object kind for ``schema.name`` or ``None`` if absent."""
    cur.execute(
        """
        SELECT table_type
        FROM information_schema.tables
        WHERE table_schema = %s AND table_name = %s
        """,
        (schema, name),
    )
    row = cur.fetchone()
    if row:
        return "view" if row[0] == "VIEW" else "table"

    if not is_redshift:
        cur.execute(
            "SELECT 1 FROM pg_matviews WHERE schemaname = %s AND matviewname = %s",
            (schema, name),
        )
        if cur.fetchone():
            return "matview"

    return None


_RENAME_KEYWORDS: dict[ObjectKind, str] = {
    "table": "ALTER TABLE",
    "view": "ALTER VIEW",
    "matview": "ALTER MATERIALIZED VIEW",
}


def build_rename_statement(
    schema: str,
    old_name: str,
    new_name: str,
    kind: ObjectKind,
    *,
    is_redshift: bool = False,
) -> sql.Composed:
    """Build an ``ALTER … RENAME TO …`` statement for the given object kind.

    Redshift does not support ``ALTER VIEW`` or ``ALTER MATERIALIZED VIEW``
    rename syntax — ``ALTER TABLE`` works for both tables and views there,
    so on Redshift we always emit ``ALTER TABLE``.
    """
    keyword = "ALTER TABLE" if is_redshift else _RENAME_KEYWORDS[kind]
    return sql.SQL("{keyword} {schema}.{old} RENAME TO {new}").format(
        keyword=sql.SQL(keyword),
        schema=sql.Identifier(schema),
        old=sql.Identifier(old_name),
        new=sql.Identifier(new_name),
    )


def rename_object(
    cur: Any,
    schema: str,
    old_name: str,
    new_name: str,
    kind: ObjectKind,
    *,
    is_redshift: bool = False,
) -> None:
    cur.execute(
        build_rename_statement(
            schema, old_name, new_name, kind, is_redshift=is_redshift
        )
    )


_DROP_KEYWORDS: dict[ObjectKind, str] = {
    "table": "DROP TABLE",
    "view": "DROP VIEW",
    "matview": "DROP MATERIALIZED VIEW",
}


def build_drop_statement(
    schema: str, name: str, kind: ObjectKind
) -> sql.Composed:
    """Build a ``DROP …`` statement for the given object kind."""
    return sql.SQL("{keyword} {schema}.{name}").format(
        keyword=sql.SQL(_DROP_KEYWORDS[kind]),
        schema=sql.Identifier(schema),
        name=sql.Identifier(name),
    )


def drop_object(
    cur: Any, schema: str, name: str, kind: ObjectKind
) -> None:
    cur.execute(build_drop_statement(schema, name, kind))


def fetch_deprecated_objects(
    cur: Any,
    *,
    is_redshift: bool,
    excluded_schemas: frozenset[str],
) -> list[tuple[str, str, ObjectKind]]:
    """Return ``(schema, name, kind)`` for every object ending in ``_deprecated``.

    Scans ``information_schema.tables`` plus ``pg_matviews`` on Postgres.
    Filters out ``information_schema``/``pg_*`` system schemas and any schema
    in ``excluded_schemas``.
    """
    suffix_pattern = f"%{DEPRECATED_SUFFIX}"
    results: list[tuple[str, str, ObjectKind]] = []

    partition_child_filter = "" if is_redshift else _PARTITION_CHILD_FILTER
    cur.execute(
        f"""
        SELECT table_schema, table_name, table_type
        FROM information_schema.tables t
        WHERE table_name LIKE %s
          AND table_schema NOT LIKE 'pg_%%'
          AND table_schema <> 'information_schema'
          {partition_child_filter}
        """,
        (suffix_pattern,),
    )
    for schema, name, table_type in cur.fetchall():
        if schema in excluded_schemas:
            continue
        kind: ObjectKind = "view" if table_type == "VIEW" else "table"
        results.append((schema, name, kind))

    if not is_redshift:
        cur.execute(
            """
            SELECT schemaname, matviewname
            FROM pg_matviews
            WHERE matviewname LIKE %s
              AND schemaname NOT LIKE 'pg_%%'
            """,
            (suffix_pattern,),
        )
        for schema, name in cur.fetchall():
            if schema in excluded_schemas:
                continue
            results.append((schema, name, "matview"))

    return results


def fetch_live_model_relations(
    cur: Any,
    *,
    invocation_command: str | None,
    node_prefix: str,
    statuses: frozenset[str],
    since: datetime,
) -> dict[str, set[str]]:
    """Return ``{schema: {names}}`` built by any matching invocation since ``since``.

    We union the model set across **all** matching ``dbt build`` invocations
    within the window rather than picking a single one. A single invocation is
    an unreliable source of truth because selective builds (e.g. CI partials)
    may only touch a handful of models, which would flag everything else as
    orphaned. Aggregating over a window captures both the latest full nightly
    and any touch-ups since.
    """
    invocation_filter = ""
    params: list[Any] = []
    if invocation_command:
        invocation_filter = "AND i.invocation_args LIKE %s"
        params.append(f'%"invocation_command": "{invocation_command}"%')
    cur.execute(
        f"""
        SELECT DISTINCT m.schema, m.name
        FROM dbt_artifacts.model_executions m
        JOIN dbt_artifacts.invocations i
          ON m.command_invocation_id = i.command_invocation_id
        WHERE i.dbt_command = 'build'
          {invocation_filter}
          AND i.run_started_at >= %s
          AND m.node_id LIKE %s
          AND m.status = ANY(%s)
        """,
        (
            *params,
            since,
            f"{node_prefix}%",
            sorted(statuses),
        ),
    )
    grouped: dict[str, set[str]] = {}
    for schema, name in cur.fetchall():
        grouped.setdefault(schema, set()).add(name)
    return grouped


def fetch_existing_relations(
    cur: Any, schemas: list[str], *, is_redshift: bool
) -> dict[str, set[str]]:
    """Return ``{schema: {names}}`` of objects present in the given schemas.

    Tables and views come from ``information_schema.tables``. On Postgres we
    additionally merge materialized views from ``pg_matviews`` (Redshift has
    no equivalent catalog).
    """
    if not schemas:
        return {}

    grouped: dict[str, set[str]] = {}
    partition_child_filter = "" if is_redshift else _PARTITION_CHILD_FILTER
    cur.execute(
        f"""
        SELECT table_schema, table_name, table_type
        FROM information_schema.tables t
        WHERE table_schema = ANY(%s)
          {partition_child_filter}
        """,
        (list(schemas),),
    )
    for schema, name, _table_type in cur.fetchall():
        grouped.setdefault(schema, set()).add(name)

    if not is_redshift:
        cur.execute(
            """
            SELECT schemaname, matviewname
            FROM pg_matviews
            WHERE schemaname = ANY(%s)
            """,
            (list(schemas),),
        )
        for schema, name in cur.fetchall():
            grouped.setdefault(schema, set()).add(name)

    return grouped


def diff_orphans(
    *,
    live: dict[str, set[str]],
    existing: dict[str, set[str]],
    excluded_schemas: frozenset[str],
    excluded_user_schemas: frozenset[str],
    excluded_user_relations: frozenset[tuple[str, str]],
) -> dict[str, list[str]]:
    """Return ``{schema: [names]}`` of orphans after applying all exclusions.

    An object is an orphan when it exists in the warehouse but is not in the
    live dbt model set. Names already ending in ``DEPRECATED_SUFFIX`` are
    skipped, as are the excluded schemas and user-excluded relations.
    """
    orphans: dict[str, list[str]] = {}
    for schema, names in existing.items():
        if schema in excluded_schemas or schema in excluded_user_schemas:
            continue
        live_names = live.get(schema, set())
        candidates = {
            name
            for name in names
            if name not in live_names
            and not name.endswith(DEPRECATED_SUFFIX)
            and (schema, name) not in excluded_user_relations
        }
        if candidates:
            orphans[schema] = sorted(candidates)
    return orphans
