"""Metadata fetchers for ``dp db role``.

Returns plain dataclasses; the CLI layer renders them with Rich. Each
fetcher takes an open cursor; callers manage the connection lifecycle.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from enum import Enum
from typing import Any

from dataplat.services.db.connection import SqlEngine


class RoleKind(str, Enum):
    """User = can log in; group = no login."""

    user = "user"
    group = "group"


class RoleNotFoundError(Exception):
    """Raised when the role/user/group does not exist."""


@dataclass(frozen=True)
class RoleRef:
    oid: int
    name: str
    kind: RoleKind


_ROLE_LOOKUP_POSTGRES = """
SELECT oid, rolcanlogin, rolsuper
FROM pg_roles
WHERE rolname = %s
"""

_ROLE_LOOKUP_REDSHIFT_USER = """
SELECT usesysid, true, usesuper
FROM pg_user
WHERE usename = %s
"""

_ROLE_LOOKUP_REDSHIFT_GROUP = """
SELECT grosysid, false, false
FROM pg_group
WHERE groname = %s
"""


def resolve_role(cursor: Any, engine: SqlEngine, name: str) -> RoleRef:
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("role name must not be empty")

    if engine == SqlEngine.redshift:
        cursor.execute(_ROLE_LOOKUP_REDSHIFT_USER, (cleaned,))
        row = cursor.fetchone()
        if row is None:
            cursor.execute(_ROLE_LOOKUP_REDSHIFT_GROUP, (cleaned,))
            row = cursor.fetchone()
        if row is None:
            raise RoleNotFoundError(f'role "{cleaned}" not found')
        oid, can_login, _super = row
        return RoleRef(
            oid=int(oid),
            name=cleaned,
            kind=RoleKind.user if can_login else RoleKind.group,
        )

    cursor.execute(_ROLE_LOOKUP_POSTGRES, (cleaned,))
    row = cursor.fetchone()
    if row is None:
        raise RoleNotFoundError(f'role "{cleaned}" not found')
    oid, can_login, _super = row
    return RoleRef(
        oid=int(oid),
        name=cleaned,
        kind=RoleKind.user if can_login else RoleKind.group,
    )


@dataclass(frozen=True)
class RoleAttributes:
    can_login: bool
    superuser: bool
    create_db: bool
    create_role: bool
    inherit: bool
    replication: bool
    bypass_rls: bool
    connection_limit: int
    password_set: bool
    valid_until: str | None


_ATTRS_SQL_POSTGRES = """
SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole,
       rolinherit, rolreplication, rolbypassrls,
       rolconnlimit,
       (rolpassword IS NOT NULL) AS password_set,
       rolvaliduntil::text
FROM pg_roles
WHERE rolname = %s
"""

_ATTRS_SQL_REDSHIFT = """
SELECT true AS can_login, usesuper, usecreatedb, valuntil
FROM pg_user
WHERE usename = %s
"""


def fetch_attributes(cursor: Any, name: str, engine: SqlEngine) -> RoleAttributes:
    if engine == SqlEngine.redshift:
        cursor.execute(_ATTRS_SQL_REDSHIFT, (name,))
        row = cursor.fetchone()
        if row is None:
            # Group — no login, no attributes table.
            return RoleAttributes(
                can_login=False,
                superuser=False,
                create_db=False,
                create_role=False,
                inherit=True,
                replication=False,
                bypass_rls=False,
                connection_limit=-1,
                password_set=False,
                valid_until=None,
            )
        can_login, superuser, create_db, valid_until = row
        return RoleAttributes(
            can_login=bool(can_login),
            superuser=bool(superuser),
            create_db=bool(create_db),
            create_role=False,
            inherit=True,
            replication=False,
            bypass_rls=False,
            connection_limit=-1,
            password_set=False,
            valid_until=str(valid_until) if valid_until is not None else None,
        )
    cursor.execute(_ATTRS_SQL_POSTGRES, (name,))
    row = cursor.fetchone()
    if row is None:
        raise RoleNotFoundError(f'role "{name}" not found')
    (
        can_login,
        superuser,
        create_db,
        create_role,
        inherit,
        replication,
        bypass_rls,
        conn_limit,
        password_set,
        valid_until,
    ) = row
    return RoleAttributes(
        can_login=bool(can_login),
        superuser=bool(superuser),
        create_db=bool(create_db),
        create_role=bool(create_role),
        inherit=bool(inherit),
        replication=bool(replication),
        bypass_rls=bool(bypass_rls),
        connection_limit=int(conn_limit),
        password_set=bool(password_set),
        valid_until=valid_until,
    )


@dataclass(frozen=True)
class MembershipEdge:
    role: str  # other end of the edge (ancestor or descendant)
    # True when AT LEAST ONE membership path between the target and ``role``
    # inherits end to end. A role reachable by several paths inherits as soon
    # as one of them does, so this is an OR across paths rather than the flag
    # of any single edge -- ``build_closure`` depends on that.
    inherit: bool
    depth: int  # distance from target along the shortest path; 1 = direct
    via: str  # for ancestor walks: parent that granted the shortest-path row


# The recursive walks carry a `path` array of oids already visited so the
# CTE terminates even if catalog corruption produces a cycle.
_MEMBERSHIPS_OUT_POSTGRES = """
WITH RECURSIVE up(member_oid, role_oid, inherit, depth, via, path) AS (
    SELECT am.member, am.roleid, am.inherit_option, 1,
           (SELECT rolname FROM pg_roles WHERE oid = am.member),
           ARRAY[am.member, am.roleid]
    FROM pg_auth_members am
    WHERE am.member = %s
    UNION ALL
    SELECT up.role_oid, am.roleid, am.inherit_option AND up.inherit,
           up.depth + 1,
           (SELECT rolname FROM pg_roles WHERE oid = up.role_oid),
           up.path || am.roleid
    FROM pg_auth_members am
    JOIN up ON am.member = up.role_oid
    WHERE NOT (am.roleid = ANY(up.path))
)
SELECT rolname, inherit, depth, via FROM (
    SELECT DISTINCT ON (r.rolname)
           r.rolname,
           -- One ancestor can be reachable by several paths. Privileges flow
           -- as soon as ONE path inherits end to end, so OR across every path
           -- to this ancestor. Taking the flag off whichever row DISTINCT ON
           -- happened to keep made the answer depend on pg_auth_members' row
           -- order and dropped genuinely inherited ancestors from the closure.
           bool_or(up.inherit) OVER (PARTITION BY r.rolname) AS inherit,
           up.depth, up.via
    FROM up JOIN pg_roles r ON r.oid = up.role_oid
    ORDER BY r.rolname, up.depth
) sub
ORDER BY depth, rolname
"""

_MEMBERSHIPS_IN_POSTGRES = """
WITH RECURSIVE down(role_oid, member_oid, inherit, depth, via, path) AS (
    SELECT am.roleid, am.member, am.inherit_option, 1,
           (SELECT rolname FROM pg_roles WHERE oid = am.roleid),
           ARRAY[am.roleid, am.member]
    FROM pg_auth_members am
    WHERE am.roleid = %s
    UNION ALL
    SELECT down.member_oid, am.member, am.inherit_option AND down.inherit,
           down.depth + 1,
           (SELECT rolname FROM pg_roles WHERE oid = down.member_oid),
           down.path || am.member
    FROM pg_auth_members am
    JOIN down ON am.roleid = down.member_oid
    WHERE NOT (am.member = ANY(down.path))
)
SELECT rolname, inherit, depth, via FROM (
    SELECT DISTINCT ON (r.rolname)
           r.rolname,
           -- Same OR-across-paths rule as the ancestor walk above: a member
           -- inherits this role's privileges if any of its paths inherits.
           bool_or(down.inherit) OVER (PARTITION BY r.rolname) AS inherit,
           down.depth, down.via
    FROM down JOIN pg_roles r ON r.oid = down.member_oid
    ORDER BY r.rolname, down.depth
) sub
ORDER BY depth, rolname
"""

_MEMBERSHIPS_OUT_REDSHIFT = """
SELECT g.groname, true, 1, ''::text
FROM pg_group g
WHERE %s = ANY(g.grolist)
ORDER BY g.groname
"""

_MEMBERSHIPS_IN_REDSHIFT = """
SELECT u.usename, true, 1, ''::text
FROM pg_user u, pg_group g
WHERE g.grosysid = %s
  AND u.usesysid = ANY(g.grolist)
ORDER BY u.usename
"""


def fetch_memberships_out(
    cursor: Any, oid: int, engine: SqlEngine
) -> list[MembershipEdge]:
    sql = (
        _MEMBERSHIPS_OUT_REDSHIFT
        if engine == SqlEngine.redshift
        else _MEMBERSHIPS_OUT_POSTGRES
    )
    cursor.execute(sql, (oid,))
    return [
        MembershipEdge(role=r, inherit=bool(i), depth=int(d), via=v or "")
        for r, i, d, v in cursor.fetchall()
    ]


def fetch_memberships_in(
    cursor: Any, oid: int, engine: SqlEngine
) -> list[MembershipEdge]:
    sql = (
        _MEMBERSHIPS_IN_REDSHIFT
        if engine == SqlEngine.redshift
        else _MEMBERSHIPS_IN_POSTGRES
    )
    cursor.execute(sql, (oid,))
    return [
        MembershipEdge(role=r, inherit=bool(i), depth=int(d), via=v or "")
        for r, i, d, v in cursor.fetchall()
    ]


_KIND_LABEL = {
    "r": "table",
    "p": "table",
    "v": "view",
    "m": "matview",
    "f": "foreign table",
    "S": "sequence",
}


def build_closure(*, self_name: str, ancestors: list[MembershipEdge]) -> set[str]:
    """Roles whose direct grants apply to this role's permissions.

    Includes the role itself, transitively inherited ancestors, and PUBLIC.
    Ancestors reached only through a NOINHERIT edge (``inherit=False``) do
    not contribute privileges and are excluded.
    """
    closure: set[str] = {self_name, "public"}
    for edge in ancestors:
        if edge.inherit:
            closure.add(edge.role)
    return closure


@dataclass(frozen=True)
class OwnedObjectsSummary:
    schemas: list[str]
    relations_by_schema: dict[str, dict[str, int]]
    total_relations: int


_OWNED_SCHEMAS_SQL = """
SELECT nspname
FROM pg_namespace
WHERE nspowner = %s
ORDER BY nspname
"""

_OWNED_RELATIONS_SQL = """
SELECT n.nspname, c.relkind, COUNT(*)::bigint
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relowner = %s
  AND c.relkind IN ('r','v','m','p','S','f')
GROUP BY n.nspname, c.relkind
ORDER BY n.nspname, c.relkind
"""


def fetch_owned_objects(
    cursor: Any, oid: int, engine: SqlEngine
) -> OwnedObjectsSummary:
    cursor.execute(_OWNED_SCHEMAS_SQL, (oid,))
    schemas = [name for (name,) in cursor.fetchall()]
    cursor.execute(_OWNED_RELATIONS_SQL, (oid,))
    by_schema: dict[str, dict[str, int]] = {}
    total = 0
    for schema, relkind, count in cursor.fetchall():
        kind_label = _KIND_LABEL.get(relkind, relkind)
        by_schema.setdefault(schema, {})[kind_label] = int(count)
        total += int(count)
    return OwnedObjectsSummary(
        schemas=schemas,
        relations_by_schema=by_schema,
        total_relations=total,
    )


@dataclass(frozen=True)
class EffectivePrivilege:
    scope: str  # "schema" | "relation" | "sequence" | "function"
    qualified_name: str
    kind: str
    privilege: str
    grantor: str
    via: str
    grantable: bool


_EFFECTIVE_SCHEMAS_SQL = """
SELECT n.nspname,
       acl.privilege_type,
       g.rolname  AS grantor,
       v.rolname  AS via,
       acl.is_grantable
FROM pg_namespace n,
     aclexplode(n.nspacl) acl
LEFT JOIN pg_roles v ON v.oid = acl.grantee
LEFT JOIN pg_roles g ON g.oid = acl.grantor
WHERE COALESCE(v.rolname, 'public') = ANY(%s)
ORDER BY n.nspname, acl.privilege_type
"""

_EFFECTIVE_RELATIONS_SQL = """
SELECT n.nspname,
       c.relname,
       CASE c.relkind
         WHEN 'r' THEN 'table'
         WHEN 'p' THEN 'table'
         WHEN 'v' THEN 'view'
         WHEN 'm' THEN 'matview'
         WHEN 'f' THEN 'foreign table'
       END                   AS kind,
       acl.privilege_type,
       g.rolname             AS grantor,
       v.rolname             AS via,
       acl.is_grantable
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace,
     aclexplode(c.relacl) acl
LEFT JOIN pg_roles v ON v.oid = acl.grantee
LEFT JOIN pg_roles g ON g.oid = acl.grantor
WHERE c.relkind IN ('r','p','v','m','f')
  AND COALESCE(v.rolname, 'public') = ANY(%s)
ORDER BY n.nspname, c.relname, acl.privilege_type
"""

_EFFECTIVE_SEQUENCES_SQL = """
SELECT n.nspname, c.relname,
       acl.privilege_type,
       g.rolname AS grantor,
       v.rolname AS via,
       acl.is_grantable
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace,
     aclexplode(c.relacl) acl
LEFT JOIN pg_roles v ON v.oid = acl.grantee
LEFT JOIN pg_roles g ON g.oid = acl.grantor
WHERE c.relkind = 'S'
  AND COALESCE(v.rolname, 'public') = ANY(%s)
ORDER BY n.nspname, c.relname, acl.privilege_type
"""

_EFFECTIVE_FUNCTIONS_SQL = """
SELECT n.nspname,
       p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')',
       acl.privilege_type,
       g.rolname AS grantor,
       v.rolname AS via,
       acl.is_grantable
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace,
     aclexplode(p.proacl) acl
LEFT JOIN pg_roles v ON v.oid = acl.grantee
LEFT JOIN pg_roles g ON g.oid = acl.grantor
WHERE COALESCE(v.rolname, 'public') = ANY(%s)
ORDER BY n.nspname, p.proname, acl.privilege_type
"""


def fetch_effective_privileges(
    cursor: Any, *, closure: set[str], engine: SqlEngine
) -> list[EffectivePrivilege]:
    if engine == SqlEngine.redshift:
        return _fetch_effective_redshift(cursor, closure)
    closure_list = sorted(closure)
    rows: list[EffectivePrivilege] = []

    cursor.execute(_EFFECTIVE_SCHEMAS_SQL, (closure_list,))
    for name, priv, grantor, via, grantable in cursor.fetchall():
        rows.append(
            EffectivePrivilege(
                scope="schema",
                qualified_name=name,
                kind="schema",
                privilege=priv,
                grantor=grantor or "",
                via=via or "public",
                grantable=bool(grantable),
            )
        )

    cursor.execute(_EFFECTIVE_RELATIONS_SQL, (closure_list,))
    for nspname, relname, kind, priv, grantor, via, grantable in cursor.fetchall():
        rows.append(
            EffectivePrivilege(
                scope="relation",
                qualified_name=f"{nspname}.{relname}",
                kind=kind,
                privilege=priv,
                grantor=grantor or "",
                via=via or "public",
                grantable=bool(grantable),
            )
        )

    cursor.execute(_EFFECTIVE_SEQUENCES_SQL, (closure_list,))
    for nspname, relname, priv, grantor, via, grantable in cursor.fetchall():
        rows.append(
            EffectivePrivilege(
                scope="sequence",
                qualified_name=f"{nspname}.{relname}",
                kind="sequence",
                privilege=priv,
                grantor=grantor or "",
                via=via or "public",
                grantable=bool(grantable),
            )
        )

    cursor.execute(_EFFECTIVE_FUNCTIONS_SQL, (closure_list,))
    for nspname, identity, priv, grantor, via, grantable in cursor.fetchall():
        rows.append(
            EffectivePrivilege(
                scope="function",
                qualified_name=f"{nspname}.{identity}",
                kind="function",
                privilege=priv,
                grantor=grantor or "",
                via=via or "public",
                grantable=bool(grantable),
            )
        )
    return rows


_REDSHIFT_CANDIDATE_SCHEMAS_SQL = """
SELECT nspname
FROM pg_namespace
WHERE nspname NOT IN ('pg_catalog', 'pg_toast', 'information_schema')
  AND nspname NOT LIKE 'pg_temp_%%'
  AND nspname NOT LIKE 'pg_toast_temp_%%'
ORDER BY nspname
"""

_REDSHIFT_CANDIDATE_RELATIONS_SQL = """
SELECT n.nspname, c.relname, c.relkind
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','v','m')
  AND n.nspname NOT IN ('pg_catalog', 'pg_toast', 'information_schema')
  AND n.nspname NOT LIKE 'pg_temp_%%'
ORDER BY n.nspname, c.relname
"""

_REDSHIFT_SCHEMA_PRIV_PROBES = ("USAGE", "CREATE")
_REDSHIFT_RELATION_PRIV_PROBES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "REFERENCES",
)

# Hard cap on the number of ``has_*_privilege`` probes packed into a single
# UNION ALL. Redshift's leader-node planner slows dramatically at tens of
# thousands of branches; above this we refuse rather than wedge the cluster.
# Only reached on non-RBAC clusters where the SVV path isn't available.
_REDSHIFT_MAX_PROBES = 20_000


class RedshiftProbeLimitError(Exception):
    """Raised when the candidate × role × privilege matrix exceeds the cap."""


# Detect RBAC availability. Selecting from svv_relation_privileges at LIMIT 0
# costs nothing when the view exists and raises cleanly when it doesn't — e.g.
# on RA3 clusters before RBAC was enabled, or on DC2 clusters missing the
# feature entirely. The probe doubles as a permission check: non-super users
# on locked-down clusters may also fail here, in which case we fall back.
#
# A SAVEPOINT wraps the probe so the failure doesn't poison the outer
# transaction. Without it, Postgres/Redshift leave the connection in
# "current transaction is aborted" state and every subsequent query fails
# until a full ROLLBACK — which would also throw away the ref/attribute/
# membership queries already issued earlier in describe_role.
_REDSHIFT_RBAC_PROBE_SQL = "SELECT 1 FROM svv_relation_privileges LIMIT 0"
_RBAC_SAVEPOINT = "dna_rbac_probe"


def _redshift_rbac_available(cursor: Any) -> bool:
    try:
        cursor.execute(f"SAVEPOINT {_RBAC_SAVEPOINT}")
    except Exception:  # noqa: BLE001  connection-level failure, just bail
        return False
    try:
        cursor.execute(_REDSHIFT_RBAC_PROBE_SQL)
        cursor.fetchall()
    except Exception:  # noqa: BLE001  view missing / no permission / etc.
        with contextlib.suppress(Exception):
            cursor.execute(f"ROLLBACK TO SAVEPOINT {_RBAC_SAVEPOINT}")
        return False
    with contextlib.suppress(Exception):
        cursor.execute(f"RELEASE SAVEPOINT {_RBAC_SAVEPOINT}")
    return True


# SVV views expose ACLs directly, so ``via`` is the real identity that received
# the grant — not the "self" placeholder the probing path returns.
_REDSHIFT_SVV_SCHEMAS_SQL = """
SELECT namespace_name, privilege_type, identity_name, admin_option
FROM svv_schema_privileges
WHERE identity_name = ANY(%s)
ORDER BY namespace_name, privilege_type
"""

_REDSHIFT_SVV_RELATIONS_SQL = """
SELECT p.namespace_name,
       p.relation_name,
       CASE c.relkind
         WHEN 'r' THEN 'table'
         WHEN 'v' THEN 'view'
         WHEN 'm' THEN 'matview'
         ELSE 'table'
       END AS kind,
       p.privilege_type,
       p.identity_name,
       p.admin_option
FROM svv_relation_privileges p
LEFT JOIN pg_class c ON c.relname = p.relation_name
LEFT JOIN pg_namespace n
  ON n.oid = c.relnamespace AND n.nspname = p.namespace_name
WHERE p.identity_name = ANY(%s)
ORDER BY p.namespace_name, p.relation_name, p.privilege_type
"""

_REDSHIFT_SVV_FUNCTIONS_SQL = """
SELECT namespace_name, function_name, privilege_type,
       identity_name, admin_option
FROM svv_function_privileges
WHERE identity_name = ANY(%s)
ORDER BY namespace_name, function_name, privilege_type
"""


def _fetch_effective_redshift_rbac(
    cursor: Any, closure: set[str]
) -> list[EffectivePrivilege]:
    """Fetch effective privileges via ``svv_*_privileges``. Preserves ``via``."""
    closure_list = sorted(closure)
    rows: list[EffectivePrivilege] = []

    cursor.execute(_REDSHIFT_SVV_SCHEMAS_SQL, (closure_list,))
    for schema, priv, identity, admin in cursor.fetchall():
        rows.append(
            EffectivePrivilege(
                scope="schema",
                qualified_name=schema,
                kind="schema",
                privilege=priv,
                grantor="",
                via=identity or "public",
                grantable=bool(admin),
            )
        )

    cursor.execute(_REDSHIFT_SVV_RELATIONS_SQL, (closure_list,))
    for schema, rel, kind, priv, identity, admin in cursor.fetchall():
        rows.append(
            EffectivePrivilege(
                scope="relation",
                qualified_name=f"{schema}.{rel}",
                kind=kind or "table",
                privilege=priv,
                grantor="",
                via=identity or "public",
                grantable=bool(admin),
            )
        )

    cursor.execute(_REDSHIFT_SVV_FUNCTIONS_SQL, (closure_list,))
    for schema, fname, priv, identity, admin in cursor.fetchall():
        rows.append(
            EffectivePrivilege(
                scope="function",
                qualified_name=f"{schema}.{fname}",
                kind="function",
                privilege=priv,
                grantor="",
                via=identity or "public",
                grantable=bool(admin),
            )
        )
    return rows


# Relations and functions resolve via information_schema — one query per scope,
# available on every Redshift cluster. Schemas still probe because schema-level
# privileges aren't exposed in information_schema in any portable way (schema
# USAGE/CREATE live in pg_namespace.nspacl, which Redshift does not allow us to
# unpack without aclexplode()). Schema count is small in practice (~hundreds)
# so schema probing stays well under _REDSHIFT_MAX_PROBES.
_REDSHIFT_INFO_SCHEMA_RELATIONS_SQL = """
SELECT table_schema, table_name, privilege_type, grantee, is_grantable
FROM information_schema.table_privileges
WHERE LOWER(grantee) = ANY(%s)
ORDER BY table_schema, table_name, privilege_type
"""

_REDSHIFT_INFO_SCHEMA_FUNCTIONS_SQL = """
SELECT routine_schema, routine_name, privilege_type, grantee, is_grantable
FROM information_schema.routine_privileges
WHERE LOWER(grantee) = ANY(%s)
ORDER BY routine_schema, routine_name, privilege_type
"""


def _is_grantable(value: Any) -> bool:
    """Normalize info_schema ``is_grantable`` (``'YES'``/``'NO'``/bool)."""
    if isinstance(value, str):
        return value.upper() == "YES"
    return bool(value)


def _fetch_effective_redshift(
    cursor: Any, closure: set[str]
) -> list[EffectivePrivilege]:
    """Resolve Redshift effective privileges.

    Prefers ``svv_*_privileges`` on RBAC clusters. Otherwise queries
    ``information_schema.table_privileges`` / ``routine_privileges`` for
    relations and functions (one query each, works on every cluster), and
    falls back to ``has_schema_privilege`` probing for schema-level grants
    since schema USAGE/CREATE isn't portably exposed in information_schema.
    """
    if _redshift_rbac_available(cursor):
        return _fetch_effective_redshift_rbac(cursor, closure)

    rows: list[EffectivePrivilege] = []
    closure_lower = sorted(r.lower() for r in closure if r)

    cursor.execute(_REDSHIFT_INFO_SCHEMA_RELATIONS_SQL, (closure_lower,))
    for schema, rel, priv, grantee, grantable in cursor.fetchall():
        rows.append(
            EffectivePrivilege(
                scope="relation",
                qualified_name=f"{schema}.{rel}",
                kind="table",  # info_schema doesn't distinguish table/view/matview
                privilege=priv,
                grantor="",
                via=(grantee or "public").lower(),
                grantable=_is_grantable(grantable),
            )
        )

    cursor.execute(_REDSHIFT_INFO_SCHEMA_FUNCTIONS_SQL, (closure_lower,))
    for schema, fname, priv, grantee, grantable in cursor.fetchall():
        rows.append(
            EffectivePrivilege(
                scope="function",
                qualified_name=f"{schema}.{fname}",
                kind="function",
                privilege=priv,
                grantor="",
                via=(grantee or "public").lower(),
                grantable=_is_grantable(grantable),
            )
        )

    # Schemas still probe — small blast radius, and no portable alternative.
    cursor.execute(_REDSHIFT_CANDIDATE_SCHEMAS_SQL)
    schemas = [s for (s,) in cursor.fetchall()]
    roles = sorted(r for r in closure if r and r != "public")
    if not roles or not schemas:
        return rows

    schema_probes = len(roles) * len(schemas) * len(_REDSHIFT_SCHEMA_PRIV_PROBES)
    if schema_probes > _REDSHIFT_MAX_PROBES:
        raise RedshiftProbeLimitError(
            f"Redshift schema-privilege probes would total {schema_probes:,} "
            f"(cap {_REDSHIFT_MAX_PROBES:,}). "
            f"Closure={len(roles)} roles × schemas={len(schemas)} × "
            f"{len(_REDSHIFT_SCHEMA_PRIV_PROBES)} privs. This only triggers "
            f"on clusters without RBAC and with many thousands of schemas."
        )

    schema_union = " UNION ALL ".join(
        "SELECT %s AS role, %s AS schema_name, %s AS priv, "
        "has_schema_privilege(%s, %s, %s) AS has"
        for _ in range(schema_probes)
    )
    schema_params: list[object] = []
    for role in roles:
        for schema in schemas:
            for priv in _REDSHIFT_SCHEMA_PRIV_PROBES:
                schema_params.extend([role, schema, priv, role, schema, priv])

    if schema_union:
        cursor.execute(schema_union, tuple(schema_params))
        for _role, schema, priv, has in cursor.fetchall():
            if has:
                rows.append(
                    EffectivePrivilege(
                        scope="schema",
                        qualified_name=schema,
                        kind="schema",
                        privilege=priv,
                        grantor="",
                        via="self",
                        grantable=False,
                    )
                )
    return rows


@dataclass(frozen=True)
class DefaultPrivilege:
    owner: str
    schema: str
    object_type: str
    privilege: str
    via: str
    grantable: bool


_DEFAULT_ACL_OBJTYPE = {
    "r": "table",
    "S": "sequence",
    "f": "function",
    "T": "type",
}


_DEFAULT_ACL_SQL = """
SELECT o.rolname        AS owner,
       COALESCE(n.nspname, '') AS schema,
       d.defaclobjtype  AS objtype,
       acl.privilege_type,
       v.rolname        AS via,
       acl.is_grantable
FROM pg_default_acl d
JOIN pg_roles o ON o.oid = d.defaclrole
LEFT JOIN pg_namespace n ON n.oid = d.defaclnamespace,
     aclexplode(d.defaclacl) acl
LEFT JOIN pg_roles v ON v.oid = acl.grantee
WHERE COALESCE(v.rolname, 'public') = ANY(%s)
ORDER BY o.rolname, n.nspname, d.defaclobjtype, acl.privilege_type
"""


def fetch_default_privileges(
    cursor: Any, *, closure: set[str], engine: SqlEngine
) -> list[DefaultPrivilege]:
    cursor.execute(_DEFAULT_ACL_SQL, (sorted(closure),))
    out: list[DefaultPrivilege] = []
    for owner, schema, objtype, priv, via, grantable in cursor.fetchall():
        out.append(
            DefaultPrivilege(
                owner=owner,
                schema=schema or "",
                object_type=_DEFAULT_ACL_OBJTYPE.get(objtype, objtype),
                privilege=priv,
                via=via or "public",
                grantable=bool(grantable),
            )
        )
    return out


@dataclass(frozen=True)
class RoleDescription:
    ref: RoleRef
    attributes: RoleAttributes
    memberships_out: list[MembershipEdge]
    memberships_in: list[MembershipEdge]
    owned: OwnedObjectsSummary
    closure: set[str]
    direct_only: bool
    effective_privileges: list[EffectivePrivilege]
    default_privileges: list[DefaultPrivilege]
    # True when Redshift RBAC (svv_*_privileges) resolved the grants; False
    # when the probing fallback was used; None for Postgres (not applicable).
    redshift_rbac: bool | None = None


def describe_role(
    cursor: Any, name: str, *, engine: SqlEngine, direct_only: bool = False
) -> RoleDescription:
    ref = resolve_role(cursor, engine, name)
    attributes = fetch_attributes(cursor, ref.name, engine)
    memberships_out = fetch_memberships_out(cursor, ref.oid, engine)
    memberships_in = fetch_memberships_in(cursor, ref.oid, engine)
    owned = fetch_owned_objects(cursor, ref.oid, engine)
    ancestors = [] if direct_only else memberships_out
    closure = build_closure(self_name=ref.name, ancestors=ancestors)

    rbac_flag: bool | None = None
    if engine == SqlEngine.redshift:
        rbac_flag = _redshift_rbac_available(cursor)
    effective = fetch_effective_privileges(
        cursor,
        closure=closure,
        engine=engine,
    )
    defaults = (
        []
        if engine == SqlEngine.redshift
        else fetch_default_privileges(cursor, closure=closure, engine=engine)
    )
    return RoleDescription(
        ref=ref,
        attributes=attributes,
        memberships_out=memberships_out,
        memberships_in=memberships_in,
        owned=owned,
        closure=closure,
        direct_only=direct_only,
        effective_privileges=effective,
        default_privileges=defaults,
        redshift_rbac=rbac_flag,
    )
