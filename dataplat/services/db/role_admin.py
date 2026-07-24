"""Plan builders for ``dp db role create`` / ``dp db role drop``.

This module produces structured SQL plans (lists of :class:`SqlOp`) for
creating and dropping roles. It does **not** open connections or execute
SQL — the CLI layer iterates over databases and runs each plan.

Two scopes per plan:
- ``cluster_ops``: run once on any one DB connection (e.g. ``CREATE ROLE``,
  ``DROP ROLE``); these mutate the global ``pg_authid`` catalog.
- ``per_database_ops``: run once per database in the target list. Privileges
  and ``REASSIGN OWNED`` / ``DROP OWNED`` live in per-DB catalogs.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from psycopg import sql  # noqa: I001

from dataplat.services.db.role_dialects import (  # noqa: F401  (re-export)
    OwnedForDrop,
    ParentKind,
    PostgresDialect,
    RedshiftDialect,
    RoleDialect,
    SqlOp,
)

# Per-database default owner used by ``drop`` when ``--reassign-to`` is not
# passed. Empty by default; the CLI layer derives the effective owner from
# the target's ``<PREFIX>_REASSIGN_OWNER`` env var. Library callers can pass
# their own map via ``defaults=``.
DEFAULT_REASSIGN_OWNERS: Mapping[str, str] = {}


@dataclass(frozen=True)
class CreateRoleSpec:
    """Permissions to apply when creating a single role.

    ``password=None`` creates a passwordless NOLOGIN role (Postgres) /
    RBAC role (Redshift) instead of a login role.

    Each tuple holds schema names. ``table_select`` for example means
    ``GRANT SELECT ON ALL TABLES IN SCHEMA <schema>`` for every schema
    in the tuple, plus ``GRANT USAGE ON SCHEMA <schema>`` automatically
    (you can't read tables without USAGE on the schema).

    ``member_of`` makes the new role a member of existing parents
    (``GRANT parent TO name``); ``grant_to`` is the reverse edge — it
    makes existing roles/users members of the new role
    (``GRANT name TO target``).
    """

    name: str
    password: str | None
    member_of: tuple[str, ...] = ()
    grant_to: tuple[str, ...] = ()
    schema_usage: tuple[str, ...] = ()
    schema_create: tuple[str, ...] = ()
    table_select: tuple[str, ...] = ()
    table_all: tuple[str, ...] = ()
    sequence_usage: tuple[str, ...] = ()
    default_table_select: tuple[str, ...] = ()
    default_table_all: tuple[str, ...] = ()


@dataclass(frozen=True)
class CreatePlan:
    """Plan for creating one role."""

    role: str
    cluster_ops: list[SqlOp] = field(default_factory=list)
    per_database_ops: dict[str, list[SqlOp]] = field(default_factory=dict)


@dataclass(frozen=True)
class DropPlan:
    """Plan for dropping one role.

    Execution order: ``pre_cluster_ops`` (e.g. granting role membership to
    the executor so REASSIGN/DROP OWNED is permitted) → ``per_database_ops``
    (REASSIGN OWNED, DROP OWNED) → ``cluster_ops`` (DROP ROLE).
    """

    role: str
    pre_cluster_ops: list[SqlOp] = field(default_factory=list)
    per_database_ops: dict[str, list[SqlOp]] = field(default_factory=dict)
    cluster_ops: list[SqlOp] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Password
# ---------------------------------------------------------------------------


def generate_password(length: int = 32) -> str:
    """Return a URL-safe random password.

    ``secrets.token_urlsafe`` returns ~1.3 chars per byte; we slice to the
    requested length so the output is predictable for output formatting.
    """
    if length < 16:
        raise ValueError("password length must be >= 16")
    raw = secrets.token_urlsafe(length)
    return raw[:length]


# ---------------------------------------------------------------------------
# CREATE plan
# ---------------------------------------------------------------------------


def _schema_usage_set(spec: CreateRoleSpec) -> tuple[str, ...]:
    """Schemas that need USAGE — explicit + implied by table/sequence grants."""
    seen: dict[str, None] = {}
    for s in (
        *spec.schema_usage,
        *spec.schema_create,
        *spec.table_select,
        *spec.table_all,
        *spec.sequence_usage,
    ):
        seen.setdefault(s, None)
    return tuple(seen)


def _warn_once(warnings: list[str], message: str) -> None:
    if message not in warnings:
        warnings.append(message)


def build_create_plan(
    spec: CreateRoleSpec,
    databases: Sequence[str],
    dialect: RoleDialect | None = None,
    *,
    parent_kinds: Mapping[str, ParentKind] | None = None,
    grantee_kinds: Mapping[str, ParentKind] | None = None,
    warnings: list[str] | None = None,
) -> CreatePlan:
    """Build a :class:`CreatePlan` for ``spec`` across ``databases``."""
    if not spec.name.strip():
        raise ValueError("role name must not be empty")
    if not databases:
        raise ValueError("at least one database is required")

    dialect = dialect or PostgresDialect()
    kinds = parent_kinds or {}
    target_kinds = grantee_kinds or {}
    warnings = warnings if warnings is not None else []
    redshift = isinstance(dialect, RedshiftDialect)

    name = spec.name
    password = spec.password
    # password=None → passwordless NOLOGIN role; on Redshift that maps to an
    # RBAC role, which changes the grantee syntax (TO ROLE ...) below.
    is_role = password is None
    if password is None:
        cluster: list[SqlOp] = [dialect.create_nologin(name)]
    else:
        cluster = [dialect.create_login(name, password)]

    for parent in spec.member_of:
        kind = kinds.get(parent, ParentKind.role)
        if redshift and is_role and kind is ParentKind.group:
            raise ValueError(
                f'cannot add role "{name}" to legacy group "{parent}" on '
                f"Redshift (groups hold only login users)"
            )
        cluster.append(
            dialect.grant_membership(name, parent, kind, member_is_role=is_role)
        )

    if spec.grant_to and redshift and not is_role:
        raise ValueError(
            "--grant-to on Redshift requires --no-login "
            "(login users cannot be granted to other principals)"
        )
    for target in spec.grant_to:
        kind = target_kinds.get(target, ParentKind.role)
        if redshift and kind is ParentKind.group:
            raise ValueError(
                f'cannot grant role "{name}" to legacy group "{target}" on '
                f"Redshift (groups hold only login users)"
            )
        cluster.append(
            dialect.grant_role_to(name, target, kind, name_is_role=is_role)
        )

    per_db: dict[str, list[SqlOp]] = {}
    usage_schemas = _schema_usage_set(spec)
    for db in databases:
        ops: list[SqlOp] = []
        for schema in usage_schemas:
            ops.append(dialect.grant_schema_usage(name, schema, as_role=is_role))
        for schema in spec.schema_create:
            ops.append(dialect.grant_schema_create(name, schema, as_role=is_role))
        for schema in spec.table_select:
            ops.append(dialect.grant_table_select(name, schema, as_role=is_role))
        for schema in spec.table_all:
            ops.append(dialect.grant_table_all(name, schema, as_role=is_role))
        for schema in spec.sequence_usage:
            op = dialect.grant_sequence_usage(name, schema, as_role=is_role)
            if op is None:
                _warn_once(
                    warnings,
                    "skipping --sequence-usage on Redshift (no sequences)",
                )
                continue
            ops.append(op)
        for schema in spec.default_table_select:
            op = dialect.alter_default_table_select(name, schema, as_role=is_role)
            if op is None:
                _warn_once(
                    warnings,
                    "skipping --default-table-select for --no-login on Redshift "
                    "(ALTER DEFAULT PRIVILEGES does not accept roles)",
                )
                continue
            ops.append(op)
        for schema in spec.default_table_all:
            op = dialect.alter_default_table_all(name, schema, as_role=is_role)
            if op is None:
                _warn_once(
                    warnings,
                    "skipping --default-table-all for --no-login on Redshift "
                    "(ALTER DEFAULT PRIVILEGES does not accept roles)",
                )
                continue
            ops.append(op)
        per_db[db] = ops

    return CreatePlan(role=name, cluster_ops=cluster, per_database_ops=per_db)


# ---------------------------------------------------------------------------
# DROP plan
# ---------------------------------------------------------------------------


class MissingReassignOwnerError(Exception):
    """Raised when a database has no default reassign-to owner and none was passed."""


def resolve_reassign_owner(
    database: str,
    *,
    explicit: str | None,
    defaults: Mapping[str, str] = DEFAULT_REASSIGN_OWNERS,
) -> str:
    """Pick the role to receive ownership transfer for ``database``.

    Precedence: explicit flag > built-in default for the DB > error.
    """
    if explicit:
        return explicit
    fallback = defaults.get(database)
    if fallback:
        return fallback
    raise MissingReassignOwnerError(
        f'no default reassign-to owner configured for database "{database}". '
        f"Pass --reassign-to <role> or --no-reassign."
    )


def build_drop_plan(
    name: str,
    databases: Sequence[str],
    dialect: RoleDialect | None = None,
    *,
    reassign_to: str | None = None,
    no_reassign: bool = False,
    grant_membership_to: str | None = None,
    owned: OwnedForDrop | None = None,
    groups: list[str] | None = None,
    defaults: Mapping[str, str] = DEFAULT_REASSIGN_OWNERS,
) -> DropPlan:
    """Build a :class:`DropPlan` for ``name`` across ``databases``.

    ``grant_membership_to`` (typically the connection user) prepends a
    cluster-level ``GRANT <role> TO <user>`` so the executor satisfies the
    membership requirement Postgres enforces on ``REASSIGN OWNED`` and
    ``DROP OWNED``. Pass ``None`` to skip (e.g. when running as superuser).

    ``dialect=None`` takes the Postgres path below. Redshift (no
    ``REASSIGN OWNED``/``DROP OWNED``) routes through
    ``dialect.build_drop_ops`` instead, using the caller-supplied ``owned``/
    ``groups`` (enumerated by the CLI layer via ``enumerate_owned``/
    ``groups_of``).
    """
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("role name must not be empty")
    if not databases:
        raise ValueError("at least one database is required")
    if reassign_to and no_reassign:
        raise ValueError("--reassign-to and --no-reassign are mutually exclusive")

    if isinstance(dialect, RedshiftDialect):
        # Redshift: single connected database; no REASSIGN/DROP OWNED.
        db = databases[0]
        default_owner = resolve_reassign_owner(
            db, explicit=reassign_to, defaults=defaults,
        ) if not no_reassign else (reassign_to or "")
        drop_ops = dialect.build_drop_ops(
            cleaned, db,
            reassign_to=reassign_to, no_reassign=no_reassign,
            owned=owned or OwnedForDrop(), groups=groups or [],
            default_owner=default_owner,
        )
        return DropPlan(
            role=cleaned,
            pre_cluster_ops=drop_ops.pre_cluster_ops,
            per_database_ops={db: drop_ops.per_database_ops},
            cluster_ops=drop_ops.cluster_ops,
        )

    # --- Postgres path (unchanged from before) ---
    role_id = sql.Identifier(cleaned)

    pre_cluster: list[SqlOp] = []
    if grant_membership_to:
        pre_cluster.append(SqlOp(
            description=(
                f"GRANT {cleaned} TO {grant_membership_to} "
                f"(needed for REASSIGN/DROP OWNED)"
            ),
            statement=sql.SQL("GRANT {role} TO {grantee}").format(
                role=role_id, grantee=sql.Identifier(grant_membership_to),
            ),
        ))

    per_db: dict[str, list[SqlOp]] = {}
    for db in databases:
        ops: list[SqlOp] = []
        if not no_reassign:
            owner = resolve_reassign_owner(
                db, explicit=reassign_to, defaults=defaults,
            )
            ops.append(SqlOp(
                description=f"REASSIGN OWNED BY {cleaned} TO {owner}",
                statement=sql.SQL("REASSIGN OWNED BY {role} TO {owner}").format(
                    role=role_id, owner=sql.Identifier(owner),
                ),
            ))
        ops.append(SqlOp(
            description=f"DROP OWNED BY {cleaned}",
            statement=sql.SQL("DROP OWNED BY {role}").format(role=role_id),
        ))
        per_db[db] = ops

    cluster = [SqlOp(
        description=f"DROP ROLE {cleaned}",
        statement=sql.SQL("DROP ROLE {role}").format(role=role_id),
    )]
    return DropPlan(
        role=cleaned,
        pre_cluster_ops=pre_cluster,
        per_database_ops=per_db,
        cluster_ops=cluster,
    )


# ---------------------------------------------------------------------------
# Helpers used by the CLI
# ---------------------------------------------------------------------------


_LIST_DATABASES_SQL = """
SELECT datname
FROM pg_database
WHERE datallowconn
  AND NOT datistemplate
  AND datname NOT IN ('postgres', 'rdsadmin')
ORDER BY datname
"""


def list_databases(cursor: Any) -> list[str]:
    """Return user databases on the cluster (excluding templates and ``postgres``)."""
    cursor.execute(_LIST_DATABASES_SQL)
    return [name for (name,) in cursor.fetchall()]


@dataclass(frozen=True)
class RoleSummary:
    name: str
    can_login: bool
    superuser: bool
    create_db: bool
    create_role: bool
    member_of_count: int     # parents (roles this role is a member of)
    members_count: int       # children (roles that are members of this role)


# pg_roles is shared across the cluster, so the result is identical from any
# DB connection. Membership counts come from pg_auth_members; LEFT JOIN +
# COUNT lets us return a row per role even when there are no edges.
_LIST_ROLES_SQL = """
SELECT r.rolname,
       r.rolcanlogin,
       r.rolsuper,
       r.rolcreatedb,
       r.rolcreaterole,
       (SELECT COUNT(*) FROM pg_auth_members am WHERE am.member = r.oid)::int
         AS member_of_count,
       (SELECT COUNT(*) FROM pg_auth_members am WHERE am.roleid = r.oid)::int
         AS members_count
FROM pg_roles r
WHERE r.rolname NOT LIKE 'pg\\_%' ESCAPE '\\'
ORDER BY r.rolname
"""


def list_roles(cursor: Any) -> list[RoleSummary]:
    """Return all non-builtin roles on the cluster.

    Excludes ``pg_*`` system roles. Result is identical regardless of which
    database the cursor is connected to.
    """
    cursor.execute(_LIST_ROLES_SQL)
    return [
        RoleSummary(
            name=name,
            can_login=bool(can_login),
            superuser=bool(superuser),
            create_db=bool(create_db),
            create_role=bool(create_role),
            member_of_count=int(member_of_count),
            members_count=int(members_count),
        )
        for name, can_login, superuser, create_db, create_role,
        member_of_count, members_count in cursor.fetchall()
    ]


def role_exists(cursor: Any, name: str) -> bool:
    """Check whether a login role / group with ``name`` exists."""
    cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
    return cursor.fetchone() is not None


def parse_csv_flag(values: Iterable[str] | None) -> tuple[str, ...]:
    """Flatten repeated ``--flag a,b --flag c`` invocations to a tuple."""
    if not values:
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        for piece in value.split(","):
            cleaned = piece.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                out.append(cleaned)
    return tuple(out)
