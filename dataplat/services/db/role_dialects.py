"""Per-engine SQL dialects for role create/drop/list.

Each :class:`RoleDialect` owns the SQL text that differs between engines.
Op builders are pure (no I/O) and return :class:`SqlOp`; a ``None`` return
means "this construct does not apply to this engine — skip it and warn".
Cursor-taking helpers (``role_exists``/``resolve_parent_kind``/…) live here
too but are called only from the CLI layer.

This module depends only on ``connection.SqlEngine`` so that ``role_admin``
can import from it without a cycle.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from psycopg import sql

from dataplat.services.db._savepoint import guarded_fetch
from dataplat.services.db.connection import SqlEngine


@dataclass(frozen=True)
class SqlOp:
    """A single SQL statement plus a human-readable label.

    ``secret=True`` means the rendered statement contains a password and
    must never be printed verbatim.
    """

    description: str
    statement: sql.Composed
    secret: bool = False


class ParentKind(str, Enum):
    """What a ``--member-of`` parent / ``--grant-to`` target resolves to."""

    user = "user"  # Redshift login user
    group = "group"  # legacy Redshift group
    role = "role"  # RBAC role (Redshift) / plain role (Postgres)
    absent = "absent"  # parent does not exist


@dataclass(frozen=True)
class OwnedForDrop:
    """Objects a Redshift user owns, gathered for ownership transfer."""

    schemas: list[str] = field(default_factory=list)
    # (schema, relation_name, relkind) where relkind in {'r','v'}
    relations: list[tuple[str, str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class DropOps:
    """Engine-specific drop op groups (mirror ``DropPlan``'s three phases)."""

    pre_cluster_ops: list[SqlOp] = field(default_factory=list)
    per_database_ops: list[SqlOp] = field(default_factory=list)
    cluster_ops: list[SqlOp] = field(default_factory=list)


# Role membership on Postgres is one table, so one query answers for every
# role at once. Joined twice against pg_roles because pg_auth_members stores
# OIDs, and the caller wants names. Verified on PostgreSQL 16.
_HELD_GRANTS_POSTGRES = """
SELECT r.rolname, m.rolname
FROM pg_auth_members am
JOIN pg_roles r ON r.oid = am.roleid
JOIN pg_roles m ON m.oid = am.member
WHERE r.rolname = ANY(%s)
"""


class RoleDialect(ABC):
    """Strategy for one SQL engine."""

    engine: SqlEngine

    # --- create-side op builders (pure) ---------------------------------

    @abstractmethod
    def create_login(self, name: str, password: str) -> SqlOp: ...

    @abstractmethod
    def create_nologin(self, name: str) -> SqlOp: ...

    @abstractmethod
    def grant_membership(
        self,
        name: str,
        parent: str,
        kind: ParentKind,
        *,
        member_is_role: bool = False,
    ) -> SqlOp: ...

    @abstractmethod
    def grant_role_to(
        self,
        name: str,
        target: str,
        kind: ParentKind,
        *,
        name_is_role: bool = False,
    ) -> SqlOp: ...

    @abstractmethod
    def grant_sequence_usage(
        self, name: str, schema: str, *, as_role: bool = False
    ) -> SqlOp | None: ...

    def _grantee(self, name: str, as_role: bool) -> tuple[sql.Composable, str]:
        """Render the TO-clause grantee. ``as_role`` matters only on engines
        that syntactically distinguish roles from users (Redshift)."""
        return sql.Identifier(name), name

    # Shared grants — identical on Postgres and Redshift.

    def grant_schema_usage(
        self, name: str, schema: str, *, as_role: bool = False
    ) -> SqlOp:
        grantee, label = self._grantee(name, as_role)
        return SqlOp(
            description=f"GRANT USAGE ON SCHEMA {schema} TO {label}",
            statement=sql.SQL("GRANT USAGE ON SCHEMA {s} TO {r}").format(
                s=sql.Identifier(schema),
                r=grantee,
            ),
        )

    def grant_schema_create(
        self, name: str, schema: str, *, as_role: bool = False
    ) -> SqlOp:
        grantee, label = self._grantee(name, as_role)
        return SqlOp(
            description=f"GRANT CREATE ON SCHEMA {schema} TO {label}",
            statement=sql.SQL("GRANT CREATE ON SCHEMA {s} TO {r}").format(
                s=sql.Identifier(schema),
                r=grantee,
            ),
        )

    def grant_table_select(
        self, name: str, schema: str, *, as_role: bool = False
    ) -> SqlOp:
        grantee, label = self._grantee(name, as_role)
        return SqlOp(
            description=f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {label}",
            statement=sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {s} TO {r}").format(
                s=sql.Identifier(schema), r=grantee
            ),
        )

    def grant_table_all(
        self, name: str, schema: str, *, as_role: bool = False
    ) -> SqlOp:
        grantee, label = self._grantee(name, as_role)
        return SqlOp(
            description=f"GRANT ALL ON ALL TABLES IN SCHEMA {schema} TO {label}",
            statement=sql.SQL("GRANT ALL ON ALL TABLES IN SCHEMA {s} TO {r}").format(
                s=sql.Identifier(schema), r=grantee
            ),
        )

    def alter_default_table_select(
        self, name: str, schema: str, *, as_role: bool = False
    ) -> SqlOp | None:
        grantee, label = self._grantee(name, as_role)
        return SqlOp(
            description=(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
                f"GRANT SELECT ON TABLES TO {label}"
            ),
            statement=sql.SQL(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA {s} GRANT SELECT ON TABLES TO {r}"
            ).format(s=sql.Identifier(schema), r=grantee),
        )

    def alter_default_table_all(
        self, name: str, schema: str, *, as_role: bool = False
    ) -> SqlOp | None:
        grantee, label = self._grantee(name, as_role)
        return SqlOp(
            description=(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
                f"GRANT ALL ON TABLES TO {label}"
            ),
            statement=sql.SQL(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA {s} GRANT ALL ON TABLES TO {r}"
            ).format(s=sql.Identifier(schema), r=grantee),
        )

    # --- cursor helpers (I/O); defaults are Postgres-flavored -----------

    def role_exists(self, cursor: Any, name: str) -> bool:
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
        return cursor.fetchone() is not None

    def resolve_parent_kind(self, cursor: Any, parent: str) -> ParentKind:
        # Postgres does not pre-check parents; treat every parent as a plain
        # role so ``grant_membership`` emits ``GRANT parent TO role`` and any
        # non-existent parent fails at execution time, exactly as today.
        return ParentKind.role

    def resolve_grantee_kind(self, cursor: Any, target: str) -> ParentKind:
        # Same philosophy as ``resolve_parent_kind``: Postgres treats every
        # ``--grant-to`` target as a plain role and lets a missing one fail
        # at execution time.
        return ParentKind.role

    def grantable_kinds(self, cursor: Any, name: str) -> tuple[ParentKind, ...]:
        """Every kind ``name`` exists as, or ``()`` when it does not exist.

        Distinct from :meth:`resolve_grantee_kind`, which answers "what should I
        render in a ``TO`` clause" and on Postgres answers ``role`` without
        looking. This one is a real lookup, because two callers need more than a
        rendering hint: ``--create-missing-users`` has to know a name is *absent*
        rather than assume it will exist by execution time, and ``--kind`` exists
        to resolve names that are genuinely more than one thing.

        Postgres keeps every principal in ``pg_roles`` and distinguishes them
        only by ``rolcanlogin``, so a name is exactly one kind and the tuple
        never holds more than one entry. Redshift overrides this.
        """
        cursor.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", (name,))
        rows = cursor.fetchall()
        if not rows:
            return ()
        return (ParentKind.user if rows[0][0] else ParentKind.role,)

    def held_grants(self, cursor: Any, roles: tuple[str, ...]) -> set[tuple[str, str]]:
        """``(role, target)`` pairs already in effect, for the given roles.

        Lets ``role grant`` report a redundant grant instead of re-issuing it.
        Re-granting is harmless on both engines, so this is about the operator
        reading the plan and seeing what actually changes.
        """
        if not roles:
            return set()
        cursor.execute(_HELD_GRANTS_POSTGRES, (list(roles),))
        return {(role, target) for role, target in cursor.fetchall()}

    def list_roles(self, cursor: Any) -> list[Any]:
        # Postgres: cluster-wide pg_roles. Imported lazily to avoid a cycle.
        from dataplat.services.db import role_admin

        return role_admin.list_roles(cursor)

    # --- drop-side ------------------------------------------------------

    def enumerate_owned(self, cursor: Any, name: str) -> OwnedForDrop:
        return OwnedForDrop()  # Postgres uses REASSIGN/DROP OWNED, not enumeration

    def groups_of(self, cursor: Any, name: str) -> list[str]:
        return []


class PostgresDialect(RoleDialect):
    engine = SqlEngine.postgresql

    def create_login(self, name: str, password: str) -> SqlOp:
        return SqlOp(
            description=f"CREATE ROLE {name} LOGIN PASSWORD '<random>'",
            statement=sql.SQL("CREATE ROLE {role} LOGIN PASSWORD {pw}").format(
                role=sql.Identifier(name),
                pw=sql.Literal(password),
            ),
            secret=True,
        )

    def create_nologin(self, name: str) -> SqlOp:
        return SqlOp(
            description=f"CREATE ROLE {name} NOLOGIN",
            statement=sql.SQL("CREATE ROLE {role} NOLOGIN").format(
                role=sql.Identifier(name),
            ),
        )

    def grant_membership(
        self,
        name: str,
        parent: str,
        kind: ParentKind,
        *,
        member_is_role: bool = False,
    ) -> SqlOp:
        return SqlOp(
            description=f"GRANT {parent} TO {name}",
            statement=sql.SQL("GRANT {parent} TO {role}").format(
                parent=sql.Identifier(parent),
                role=sql.Identifier(name),
            ),
        )

    def grant_role_to(
        self,
        name: str,
        target: str,
        kind: ParentKind,
        *,
        name_is_role: bool = False,
    ) -> SqlOp:
        return SqlOp(
            description=f"GRANT {name} TO {target}",
            statement=sql.SQL("GRANT {role} TO {target}").format(
                role=sql.Identifier(name),
                target=sql.Identifier(target),
            ),
        )

    def grant_sequence_usage(
        self, name: str, schema: str, *, as_role: bool = False
    ) -> SqlOp:
        return SqlOp(
            description=f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA {schema} TO {name}",
            statement=sql.SQL(
                "GRANT USAGE ON ALL SEQUENCES IN SCHEMA {s} TO {r}"
            ).format(s=sql.Identifier(schema), r=sql.Identifier(name)),
        )


# Savepoint names for the guarded probes below. Distinct per probe so that a
# server log reads unambiguously, and `dp`-prefixed because that is what this tool
# is called — these were `dna_*` for a while, inherited from the CLI this code was
# ported from, which is a confusing thing to find in your PostgreSQL log when you
# have never installed anything called dna.
_HELD_SAVEPOINT = "dp_held_grants"
_RBAC_ROLE_SAVEPOINT = "dp_rbac_role_probe"

# Three ways a grant is already in effect on Redshift, because it has three
# kinds of grantee. The group edge reads pg_group, which exists on every
# cluster; the two RBAC edges read svv_* views that a pre-RBAC cluster does not
# have at all, which is why the caller runs them through ``_guarded_fetch``.
_HELD_ROLE_TO_USER_REDSHIFT = """
SELECT role_name, user_name
FROM svv_user_grants
WHERE role_name = ANY(%s)
"""

# svv_role_grants(role_name, granted_role_name) reads "role_name HOLDS
# granted_role_name", so the granted role is the one being looked up and the
# columns come back swapped relative to the pair this returns.
_HELD_ROLE_TO_ROLE_REDSHIFT = """
SELECT granted_role_name, role_name
FROM svv_role_grants
WHERE granted_role_name = ANY(%s)
"""

# grolist is an array of usesysid, so membership is a containment test rather
# than a join key.
_HELD_GROUP_MEMBERS_REDSHIFT = """
SELECT g.groname, u.usename
FROM pg_group g, pg_user u
WHERE g.groname = ANY(%s) AND u.usesysid = ANY(g.grolist)
"""


class RedshiftDialect(RoleDialect):
    engine = SqlEngine.redshift

    def create_login(self, name: str, password: str) -> SqlOp:
        return SqlOp(
            description=f"CREATE USER {name} PASSWORD '<random>'",
            statement=sql.SQL("CREATE USER {user} PASSWORD {pw}").format(
                user=sql.Identifier(name),
                pw=sql.Literal(password),
            ),
            secret=True,
        )

    def create_nologin(self, name: str) -> SqlOp:
        # Redshift has no NOLOGIN users; the passwordless equivalent is an
        # RBAC role (grantable to users and other roles).
        return SqlOp(
            description=f"CREATE ROLE {name}",
            statement=sql.SQL("CREATE ROLE {role}").format(
                role=sql.Identifier(name),
            ),
        )

    def _grantee(self, name: str, as_role: bool) -> tuple[sql.Composable, str]:
        if as_role:
            grantee = sql.SQL("ROLE {}").format(sql.Identifier(name))
            return grantee, f"ROLE {name}"
        return sql.Identifier(name), name

    def grant_membership(
        self,
        name: str,
        parent: str,
        kind: ParentKind,
        *,
        member_is_role: bool = False,
    ) -> SqlOp:
        if kind == ParentKind.group:
            # Only login users fit in legacy groups; role members must be
            # rejected by the plan builder before reaching here.
            return SqlOp(
                description=f"ALTER GROUP {parent} ADD USER {name}",
                statement=sql.SQL("ALTER GROUP {g} ADD USER {u}").format(
                    g=sql.Identifier(parent),
                    u=sql.Identifier(name),
                ),
            )
        # RBAC role (ParentKind.absent must be caught by the CLI before here).
        grantee, label = self._grantee(name, member_is_role)
        return SqlOp(
            description=f"GRANT ROLE {parent} TO {label}",
            statement=sql.SQL("GRANT ROLE {r} TO {u}").format(
                r=sql.Identifier(parent),
                u=grantee,
            ),
        )

    def grant_role_to(
        self,
        name: str,
        target: str,
        kind: ParentKind,
        *,
        name_is_role: bool = False,
    ) -> SqlOp:
        # Only RBAC roles are grantable on Redshift; the plan builder rejects
        # login users (name_is_role=False) and group targets before this.
        grantee, label = self._grantee(target, kind is ParentKind.role)
        return SqlOp(
            description=f"GRANT ROLE {name} TO {label}",
            statement=sql.SQL("GRANT ROLE {r} TO {t}").format(
                r=sql.Identifier(name),
                t=grantee,
            ),
        )

    def grant_sequence_usage(
        self, name: str, schema: str, *, as_role: bool = False
    ) -> SqlOp | None:
        return None  # Redshift has no sequences

    def alter_default_table_select(
        self, name: str, schema: str, *, as_role: bool = False
    ) -> SqlOp | None:
        if as_role:
            return None  # Redshift ALTER DEFAULT PRIVILEGES has no TO ROLE
        return super().alter_default_table_select(name, schema)

    def alter_default_table_all(
        self, name: str, schema: str, *, as_role: bool = False
    ) -> SqlOp | None:
        if as_role:
            return None  # Redshift ALTER DEFAULT PRIVILEGES has no TO ROLE
        return super().alter_default_table_all(name, schema)

    def role_exists(self, cursor: Any, name: str) -> bool:
        cursor.execute("SELECT 1 FROM pg_user WHERE usename = %s", (name,))
        if cursor.fetchone() is not None:
            return True
        cursor.execute("SELECT 1 FROM pg_group WHERE groname = %s", (name,))
        if cursor.fetchone() is not None:
            return True
        return self._rbac_role_exists(cursor, name)

    def resolve_parent_kind(self, cursor: Any, parent: str) -> ParentKind:
        # Prefer legacy groups — that's what `dp db role show` reads back.
        cursor.execute("SELECT 1 FROM pg_group WHERE groname = %s", (parent,))
        if cursor.fetchone() is not None:
            return ParentKind.group
        if self._rbac_role_exists(cursor, parent):
            return ParentKind.role
        return ParentKind.absent

    def resolve_grantee_kind(self, cursor: Any, target: str) -> ParentKind:
        cursor.execute("SELECT 1 FROM pg_user WHERE usename = %s", (target,))
        if cursor.fetchone() is not None:
            return ParentKind.user
        cursor.execute("SELECT 1 FROM pg_group WHERE groname = %s", (target,))
        if cursor.fetchone() is not None:
            return ParentKind.group
        if self._rbac_role_exists(cursor, target):
            return ParentKind.role
        return ParentKind.absent

    def grantable_kinds(self, cursor: Any, name: str) -> tuple[ParentKind, ...]:
        """Every kind ``name`` exists as — possibly more than one.

        Redshift keeps users, legacy groups and RBAC roles in three separate
        catalogs with no shared namespace, so one name can be a user *and* a
        group *and* a role, carrying different privileges in each. Unlike
        :meth:`resolve_grantee_kind`, which returns the first hit and would
        silently pick one, this reports all of them so the caller can insist on
        ``--kind`` rather than grant to whichever catalog happened to be probed
        first.
        """
        found: list[ParentKind] = []
        cursor.execute("SELECT 1 FROM pg_user WHERE usename = %s", (name,))
        if cursor.fetchall():
            found.append(ParentKind.user)
        cursor.execute("SELECT 1 FROM pg_group WHERE groname = %s", (name,))
        if cursor.fetchall():
            found.append(ParentKind.group)
        if self._rbac_role_exists(cursor, name):
            found.append(ParentKind.role)
        return tuple(found)

    def held_grants(self, cursor: Any, roles: tuple[str, ...]) -> set[tuple[str, str]]:
        """``(role, target)`` pairs already in effect across all three edges."""
        if not roles:
            return set()
        names = list(roles)
        held: set[tuple[str, str]] = set()
        # pg_group exists on every cluster, so this one needs no guard.
        cursor.execute(_HELD_GROUP_MEMBERS_REDSHIFT, (names,))
        held.update((group, user) for group, user in cursor.fetchall())
        for sql_text in (_HELD_ROLE_TO_USER_REDSHIFT, _HELD_ROLE_TO_ROLE_REDSHIFT):
            # `or []` collapses guarded_fetch's None into an empty result on
            # purpose. Held-detection is cosmetic — a pair it misses costs one
            # redundant, idempotent GRANT — so "the view is not there" and "the
            # view says nothing" lead to the same plan here. The callers that
            # must tell those apart keep the None.
            rows = guarded_fetch(cursor, sql_text, (names,), savepoint=_HELD_SAVEPOINT)
            held.update((role, target) for role, target in rows or [])
        return held

    def _rbac_role_exists(self, cursor: Any, parent: str) -> bool:
        """Probe svv_roles for an RBAC role.

        Guarded, so a missing view on a pre-RBAC cluster does not poison the
        outer transaction. Both "no such view" (``None``) and "view present, no
        such role" (``[]``) mean the role does not exist as far as this answers,
        so the two collapse into ``False`` — and a cluster without RBAC has no
        RBAC roles, which makes that the true answer rather than a fallback.
        """
        rows = guarded_fetch(
            cursor,
            "SELECT 1 FROM svv_roles WHERE role_name = %s",
            (parent,),
            savepoint=_RBAC_ROLE_SAVEPOINT,
        )
        return bool(rows)

    def list_roles(self, cursor: Any) -> list[Any]:
        from dataplat.services.db.role_admin import RoleSummary

        cursor.execute(
            "SELECT usename, usesuper, usecreatedb FROM pg_user ORDER BY usename"
        )
        rows = [
            RoleSummary(
                name=name,
                can_login=True,
                superuser=bool(usesuper),
                create_db=bool(usecreatedb),
                create_role=False,
                member_of_count=0,
                members_count=0,
            )
            for name, usesuper, usecreatedb in cursor.fetchall()
        ]
        cursor.execute(
            "SELECT groname, COALESCE(array_length(grolist, 1), 0) "
            "FROM pg_group ORDER BY groname"
        )
        rows.extend(
            RoleSummary(
                name=groname,
                can_login=False,
                superuser=False,
                create_db=False,
                create_role=False,
                member_of_count=0,
                members_count=int(count or 0),
            )
            for groname, count in cursor.fetchall()
        )
        return rows

    # --- drop-side ------------------------------------------------------

    def enumerate_owned(self, cursor: Any, name: str) -> OwnedForDrop:
        cursor.execute("SELECT usesysid FROM pg_user WHERE usename = %s", (name,))
        row = cursor.fetchone()
        if row is None:
            raise ValueError(
                f'"{name}" is not a Redshift user (drop targets login users)'
            )
        usesysid = int(row[0])
        cursor.execute(
            "SELECT nspname FROM pg_namespace WHERE nspowner = %s ORDER BY nspname",
            (usesysid,),
        )
        schemas = [s for (s,) in cursor.fetchall()]
        cursor.execute(
            "SELECT n.nspname, c.relname, c.relkind "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relowner = %s AND c.relkind IN ('r', 'v') "
            "ORDER BY n.nspname, c.relname",
            (usesysid,),
        )
        relations = [(s, r, k) for s, r, k in cursor.fetchall()]
        return OwnedForDrop(schemas=schemas, relations=relations)

    def groups_of(self, cursor: Any, name: str) -> list[str]:
        cursor.execute("SELECT usesysid FROM pg_user WHERE usename = %s", (name,))
        row = cursor.fetchone()
        if row is None:
            return []
        cursor.execute(
            "SELECT groname FROM pg_group WHERE %s = ANY(grolist) ORDER BY groname",
            (int(row[0]),),
        )
        return [g for (g,) in cursor.fetchall()]

    def build_drop_ops(
        self,
        name: str,
        database: str,
        *,
        reassign_to: str | None,
        no_reassign: bool,
        owned: OwnedForDrop,
        groups: list[str],
        default_owner: str,
    ) -> DropOps:
        user = sql.Identifier(name)
        owner = sql.Identifier(reassign_to or default_owner)
        per_db: list[SqlOp] = []
        if not no_reassign:
            for schema in owned.schemas:
                per_db.append(
                    SqlOp(
                        description=(
                            f"ALTER SCHEMA {schema} "
                            f"OWNER TO {reassign_to or default_owner}"
                        ),
                        statement=sql.SQL("ALTER SCHEMA {s} OWNER TO {o}").format(
                            s=sql.Identifier(schema),
                            o=owner,
                        ),
                    )
                )
            for schema, relname, relkind in owned.relations:
                kw = "VIEW" if relkind == "v" else "TABLE"
                per_db.append(
                    SqlOp(
                        description=(
                            f"ALTER {kw} {schema}.{relname} "
                            f"OWNER TO {reassign_to or default_owner}"
                        ),
                        statement=sql.SQL(
                            "ALTER " + kw + " {s}.{r} OWNER TO {o}"
                        ).format(
                            s=sql.Identifier(schema),
                            r=sql.Identifier(relname),
                            o=owner,
                        ),
                    )
                )
        cluster: list[SqlOp] = []
        for group in groups:
            cluster.append(
                SqlOp(
                    description=f"ALTER GROUP {group} DROP USER {name}",
                    statement=sql.SQL("ALTER GROUP {g} DROP USER {u}").format(
                        g=sql.Identifier(group),
                        u=user,
                    ),
                )
            )
        cluster.append(
            SqlOp(
                description=f"DROP USER {name}",
                statement=sql.SQL("DROP USER {u}").format(u=user),
            )
        )
        return DropOps(pre_cluster_ops=[], per_database_ops=per_db, cluster_ops=cluster)


def dialect_for(engine: SqlEngine) -> RoleDialect:
    """Return the dialect for ``engine`` (defaults to Postgres)."""
    if engine == SqlEngine.redshift:
        return RedshiftDialect()
    return PostgresDialect()
