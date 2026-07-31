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
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from psycopg import sql

from dataplat.core.errors import ValidationError
from dataplat.services.db._savepoint import guarded_fetch
from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.grantees import PUBLIC, render_grantee
from dataplat.services.db.role_dialects import ParentKind, SqlOp
from dataplat.services.db.schema_admin import SchemaSummary

if TYPE_CHECKING:
    from dataplat.services.db.schema_admin import SchemaPrivilege

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

# Direct schema-level ACL entries. aclexplode unpacks nspacl into one row per
# (grantee, privilege); a NULL nspacl simply yields no rows. Joined to pg_roles by
# OID, which is why the Postgres path needs no identity pinning.
_HELD_SCHEMA_POSTGRES = """
SELECT n.nspname, r.rolname, a.privilege_type
FROM pg_namespace n
CROSS JOIN LATERAL aclexplode(n.nspacl) AS a
JOIN pg_roles r ON r.oid = a.grantee
WHERE n.nspname = ANY(%s) AND r.rolname = ANY(%s)
"""

# Availability varies by cluster: RBAC views are absent pre-RBAC and may be
# permission-denied. Always guarded; absence means "no detection", which costs at
# most a redundant idempotent GRANT. ``{pin}`` comes from _held_identity_pin.
_HELD_SCHEMA_REDSHIFT = """
SELECT namespace_name, identity_name, privilege_type
FROM svv_schema_privileges
WHERE namespace_name = ANY(%s) AND ({pin})
"""

# svv_schema_privileges's identity_type spelling for each ParentKind.
_REDSHIFT_IDENTITY_TYPE_FOR_KIND: dict[ParentKind, str] = {
    ParentKind.user: "user",
    ParentKind.group: "group",
    ParentKind.role: "role",
}


def _held_identity_pin(
    grantees: Sequence[str], kinds: Mapping[str, ParentKind] | None
) -> tuple[str, tuple[str, ...]]:
    """Build the ``(identity_name, identity_type)`` predicate for ``grantees``.

    Every arm pins the name and the type *together*. Redshift permits a group and
    an RBAC role to share one name, and svv_schema_privileges matches on identity
    name alone — so an unpinned match merges two different principals' privileges
    into one answer and reports access nobody has. This is the schema-privilege
    analogue of the same pin ``role.py`` carries for effective privileges.

    ``PUBLIC`` needs its own arm: its rows carry ``identity_type = 'public'``, so a
    name+type predicate alone would drop it.

    A grantee whose kind is unknown matches nothing rather than falling back to a
    name-only match: under-detecting "held" costs one redundant idempotent GRANT,
    while matching on name alone is exactly the bug this pin exists to close.
    """
    known = kinds or {}
    arms = ["identity_type = 'public'"]
    params: list[str] = []
    for name in grantees:
        if name.upper() == PUBLIC:
            continue  # covered by the identity_type = 'public' arm above
        identity_type = _REDSHIFT_IDENTITY_TYPE_FOR_KIND.get(
            known.get(name, ParentKind.absent), ""
        )
        if not identity_type:
            continue  # unknown kind: never matches, so "held" defaults to no
        arms.append("(identity_name = %s AND identity_type = %s)")
        params.extend([name, identity_type])
    return " OR ".join(arms), tuple(params)


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
    #: Whether ``CREATE SCHEMA ... AUTHORIZATION`` parses at all.
    supports_authorization: bool = True
    #: Keyword introducing the grantor in ALTER DEFAULT PRIVILEGES. Postgres says
    #: FOR ROLE; Redshift says FOR USER.
    default_privileges_grantor_keyword = "ROLE"
    #: privilege_type values held-detection recognises, lowered to the CLI
    #: vocabulary. Schema-scoped only — see ``held_schema_privileges``.
    _HELD_PRIVILEGES = {"USAGE": "usage", "CREATE": "create"}

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

    # --- op builders (pure) ---------------------------------------------

    def create_schema(
        self,
        name: str,
        *,
        owner: str | None = None,
        quota: str | None = None,
        if_not_exists: bool = False,
    ) -> SqlOp:
        """``CREATE SCHEMA [IF NOT EXISTS] name [AUTHORIZATION owner]``.

        ``quota`` is ignored here — only Redshift supports it, and the plan
        builder has already warned. Redshift overrides this to append the clause.
        """
        parts = ["CREATE SCHEMA"]
        label = ["CREATE SCHEMA"]
        params: dict[str, sql.Composable] = {"s": sql.Identifier(name)}
        if if_not_exists:
            parts.append("IF NOT EXISTS")
            label.append("IF NOT EXISTS")
        parts.append("{s}")
        label.append(name)
        if owner:
            parts.append("AUTHORIZATION {o}")
            label.append(f"AUTHORIZATION {owner}")
            params["o"] = sql.Identifier(owner)
        return SqlOp(
            description=" ".join(label),
            statement=sql.SQL(" ".join(parts)).format(**params),
        )

    def alter_owner(self, name: str, owner: str) -> SqlOp:
        return SqlOp(
            description=f"ALTER SCHEMA {name} OWNER TO {owner}",
            statement=sql.SQL("ALTER SCHEMA {s} OWNER TO {o}").format(
                s=sql.Identifier(name), o=sql.Identifier(owner)
            ),
        )

    def rename_schema(self, name: str, new_name: str) -> SqlOp:
        return SqlOp(
            description=f"ALTER SCHEMA {name} RENAME TO {new_name}",
            statement=sql.SQL("ALTER SCHEMA {s} RENAME TO {n}").format(
                s=sql.Identifier(name), n=sql.Identifier(new_name)
            ),
        )

    def alter_quota(self, name: str, quota: str) -> SqlOp | None:
        """``None`` — only Redshift has schema quotas."""
        return None

    def privilege_op(
        self,
        privilege: SchemaPrivilege,
        schema: str,
        grantee: str,
        kind: ParentKind,
        *,
        revoke: bool = False,
        grantor: str | None = None,
        cascade: bool = False,
    ) -> SqlOp | None:
        """One GRANT/REVOKE statement, or ``None`` if the engine cannot express it.

        ``None`` is the established "skip and warn" signal — the plan builder
        turns it into a one-time warning rather than failing the whole run.
        """
        # Imported here, not at module top: schema_admin imports this module.
        from dataplat.services.db.schema_admin import (
            DEFAULT_LEVEL,
            SCHEMA_LEVEL,
            TABLE_LEVEL,
        )
        from dataplat.services.db.schema_admin import (
            SchemaPrivilege as Priv,
        )

        grantee_sql, grantee_label = render_grantee(self.engine, grantee, kind)
        verb = "REVOKE" if revoke else "GRANT"
        preposition = "FROM" if revoke else "TO"
        tail = " CASCADE" if (revoke and cascade) else ""

        if privilege in SCHEMA_LEVEL:
            keyword = privilege.value.upper()
            target = "SCHEMA {s}"
            target_label = f"SCHEMA {schema}"
        elif privilege in TABLE_LEVEL:
            keyword = "ALL" if privilege is Priv.table_all else privilege.value.upper()
            target = "ALL TABLES IN SCHEMA {s}"
            target_label = f"ALL TABLES IN SCHEMA {schema}"
        elif privilege is Priv.sequence_usage:
            keyword = "USAGE"
            target = "ALL SEQUENCES IN SCHEMA {s}"
            target_label = f"ALL SEQUENCES IN SCHEMA {schema}"
        elif privilege is Priv.function_execute:
            keyword = "EXECUTE"
            target = "ALL FUNCTIONS IN SCHEMA {s}"
            target_label = f"ALL FUNCTIONS IN SCHEMA {schema}"
        elif privilege in DEFAULT_LEVEL:
            return self._default_privileges_op(
                privilege,
                schema,
                grantee_sql,
                grantee_label,
                verb=verb,
                preposition=preposition,
                grantor=grantor,
                tail=tail,
            )
        else:  # pragma: no cover - the enum has no other members
            raise ValidationError(f"unhandled privilege: {privilege}")

        statement = sql.SQL(
            f"{verb} {keyword} ON {target} {preposition} {{g}}{tail}"
        ).format(s=sql.Identifier(schema), g=grantee_sql)
        return SqlOp(
            description=(
                f"{verb} {keyword} ON {target_label} "
                f"{preposition} {grantee_label}{tail}"
            ),
            statement=statement,
        )

    def _default_privileges_op(
        self,
        privilege: SchemaPrivilege,
        schema: str,
        grantee_sql: sql.Composable,
        grantee_label: str,
        *,
        verb: str,
        preposition: str,
        grantor: str | None,
        tail: str,
    ) -> SqlOp | None:
        """``ALTER DEFAULT PRIVILEGES`` with an explicit grantor.

        The grantor clause is mandatory here by design. Without ``FOR ROLE`` /
        ``FOR USER`` the statement binds to whoever is connected, so tables later
        created by dbt or by the schema owner inherit nothing — the single most
        common way default privileges silently fail. Refusing is the only honest
        option: the statement would succeed and do nothing.
        """
        from dataplat.services.db.schema_admin import SchemaPrivilege as Priv

        if not grantor:
            raise ValidationError(
                f'"{privilege.value}" needs a grantor: pass --default-for naming '
                "the role that will create the tables. Without it the grant "
                "would bind to the connecting user and silently do nothing."
            )
        keyword = "ALL" if privilege is Priv.default_all else "SELECT"
        word = self.default_privileges_grantor_keyword
        statement = sql.SQL(
            f"ALTER DEFAULT PRIVILEGES FOR {word} {{o}} IN SCHEMA {{s}} "
            f"{verb} {keyword} ON TABLES {preposition} {{g}}{tail}"
        ).format(o=sql.Identifier(grantor), s=sql.Identifier(schema), g=grantee_sql)
        return SqlOp(
            description=(
                f"ALTER DEFAULT PRIVILEGES FOR {word} {grantor} IN SCHEMA {schema} "
                f"{verb} {keyword} ON TABLES {preposition} {grantee_label}{tail}"
            ),
            statement=statement,
        )

    # --- held detection -------------------------------------------------

    def held_schema_privileges(
        self,
        cursor: Any,
        schemas: Sequence[str],
        grantees: Sequence[str],
        kinds: Mapping[str, ParentKind] | None = None,
    ) -> set[tuple[str, str, str]]:
        """``(schema, grantee, privilege)`` triples already in effect.

        Schema-scoped privileges only. Across a fan-out like ``ON ALL TABLES``
        "held" has no single answer, and ``GRANT`` is idempotent, so re-issuing
        those costs nothing but output noise.

        ``kinds`` is unused on this Postgres path: ``aclexplode`` joins
        ``pg_roles`` by OID, so a name shared by two kinds of principal cannot be
        confused the way it can on Redshift's name-keyed SVV views — see the
        Redshift override, which does need it.

        Held-detection is cosmetic, never load-bearing (a missed "held" costs one
        redundant, idempotent GRANT), so a failure here must not abort the
        surrounding transaction — hence the savepoint.
        """
        if not schemas or not grantees:
            return set()
        rows = guarded_fetch(
            cursor,
            _HELD_SCHEMA_POSTGRES,
            (list(schemas), list(grantees)),
            savepoint="dp_schema_held_postgres",
        )
        if rows is None:
            return set()  # degrade to "nothing held" rather than abort
        return self._map_held(rows)

    def _map_held(self, rows: Sequence[tuple[Any, ...]]) -> set[tuple[str, str, str]]:
        out: set[tuple[str, str, str]] = set()
        for schema, grantee, privilege_type in rows:
            token = self._HELD_PRIVILEGES.get(privilege_type)
            if token is not None:
                out.add((schema, grantee, token))
        return out

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
    default_privileges_grantor_keyword = "USER"

    def create_schema(
        self,
        name: str,
        *,
        owner: str | None = None,
        quota: str | None = None,
        if_not_exists: bool = False,
    ) -> SqlOp:
        op = super().create_schema(
            name, owner=owner, quota=quota, if_not_exists=if_not_exists
        )
        if not quota:
            return op
        # A quota is neither an identifier nor a bindable parameter in DDL, so it
        # is interpolated as text. schema_admin.parse_quota has already reduced it
        # to digits + one space + MB/GB/TB, or the single keyword UNLIMITED, and
        # rebuilds the string from parsed groups — there is nothing else it can
        # contain. CreateSchemaSpec re-normalizes so that holds by construction.
        return SqlOp(
            description=f"{op.description} QUOTA {quota}",
            statement=op.statement + sql.SQL(f" QUOTA {quota}"),
        )

    def alter_quota(self, name: str, quota: str) -> SqlOp | None:
        # Pre-normalized by schema_admin.parse_quota — see create_schema above.
        return SqlOp(
            description=f"ALTER SCHEMA {name} QUOTA {quota}",
            statement=sql.SQL("ALTER SCHEMA {s} ").format(s=sql.Identifier(name))
            + sql.SQL(f"QUOTA {quota}"),
        )

    def privilege_op(
        self,
        privilege: SchemaPrivilege,
        schema: str,
        grantee: str,
        kind: ParentKind,
        *,
        revoke: bool = False,
        grantor: str | None = None,
        cascade: bool = False,
    ) -> SqlOp | None:
        from dataplat.services.db.schema_admin import (
            DEFAULT_LEVEL,
        )
        from dataplat.services.db.schema_admin import (
            SchemaPrivilege as Priv,
        )

        if privilege is Priv.sequence_usage:
            return None  # Redshift has no sequences at all
        if (
            privilege in DEFAULT_LEVEL
            and kind is ParentKind.role
            and grantee.upper() != PUBLIC
        ):
            # Redshift's ALTER DEFAULT PRIVILEGES grants to a user or a GROUP;
            # there is no TO ROLE form. PUBLIC is excluded from this skip because
            # resolve_grantee_kinds assigns it ParentKind.role as a *placeholder*
            # (render_grantee special-cases PUBLIC by name and ignores the kind),
            # and Redshift's ALTER DEFAULT PRIVILEGES does support TO PUBLIC — so
            # the placeholder must not leak into this decision.
            return None
        return super().privilege_op(
            privilege,
            schema,
            grantee,
            kind,
            revoke=revoke,
            grantor=grantor,
            cascade=cascade,
        )

    def held_schema_privileges(
        self,
        cursor: Any,
        schemas: Sequence[str],
        grantees: Sequence[str],
        kinds: Mapping[str, ParentKind] | None = None,
    ) -> set[tuple[str, str, str]]:
        """See the base docstring. ``kinds`` pins each grantee's identity_type —
        see :func:`_held_identity_pin` for why an unpinned name match is unsafe.
        """
        if not schemas or not grantees:
            return set()
        pin, pin_params = _held_identity_pin(grantees, kinds)
        rows = guarded_fetch(
            cursor,
            _HELD_SCHEMA_REDSHIFT.format(pin=pin),
            (list(schemas), *pin_params),
            savepoint="dp_schema_held",
        )
        if rows is None:
            return set()  # view unavailable -> issue every grant
        return self._map_held(rows)

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
    """DuckDB creates and drops schemas; it does not grant or alter them.

    Probed against duckdb 1.5.5, and each refusal below quotes what the engine
    actually said:

    - ``CREATE SCHEMA s AUTHORIZATION bob`` → ``ParserException`` at
      ``AUTHORIZATION``. There is no owner to authorize.
    - ``GRANT USAGE ON SCHEMA s TO bob`` → ``ParserException`` at ``GRANT``. The
      keyword does not exist, because there is nobody to grant to.
    - ``ALTER SCHEMA s RENAME TO t`` → ``NotImplementedException``: "Altering
      schemas is not yet supported".

    ``CREATE SCHEMA [IF NOT EXISTS]`` and ``DROP SCHEMA ... RESTRICT|CASCADE``
    all work, which is why those two subcommands are not refused here. The CLI
    gates the rest on :class:`~dataplat.services.db.capabilities.Capability`, so
    the user gets the engine's reason rather than a parser error; the overrides
    below are the service-layer half, so no caller can build a statement this
    engine cannot parse.
    """

    engine = SqlEngine.duckdb
    system_predicate = _HIDE_SYSTEM_DUCKDB
    placeholder = "?"
    supports_authorization = False

    def list_schemas(
        self, cursor: Any, *, include_system: bool = False, like: str | None = None
    ) -> list[SchemaSummary]:
        return self._roster(
            cursor, _LIST_DUCKDB, include_system=include_system, like=like
        )

    def alter_owner(self, name: str, owner: str) -> SqlOp:
        raise ValidationError(
            "DuckDB has no schema owners: every connection is the same implicit "
            "user, and ALTER SCHEMA is not implemented"
        )

    def rename_schema(self, name: str, new_name: str) -> SqlOp:
        raise ValidationError(
            "DuckDB does not implement ALTER SCHEMA — the engine answers "
            "'Altering schemas is not yet supported'"
        )

    def privilege_op(
        self,
        privilege: SchemaPrivilege,
        schema: str,
        grantee: str,
        kind: ParentKind,
        *,
        revoke: bool = False,
        grantor: str | None = None,
        cascade: bool = False,
    ) -> SqlOp | None:
        raise ValidationError(
            "DuckDB has no GRANT statement: the keyword does not parse, because "
            "there are no users or roles to grant anything to"
        )

    def held_schema_privileges(
        self,
        cursor: Any,
        schemas: Sequence[str],
        grantees: Sequence[str],
        kinds: Mapping[str, ParentKind] | None = None,
    ) -> set[tuple[str, str, str]]:
        return set()  # nothing can be held where nothing can be granted


def schema_dialect_for(engine: SqlEngine) -> SchemaDialect:
    """Return the schema dialect for ``engine`` (defaults to Postgres)."""
    if engine == SqlEngine.redshift:
        return RedshiftSchemaDialect()
    if engine == SqlEngine.duckdb:
        return DuckDbSchemaDialect()
    return PostgresSchemaDialect()
