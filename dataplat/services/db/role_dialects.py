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

import contextlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from psycopg import sql

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

    user = "user"       # Redshift login user
    group = "group"     # legacy Redshift group
    role = "role"       # RBAC role (Redshift) / plain role (Postgres)
    absent = "absent"   # parent does not exist


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
        self, name: str, parent: str, kind: ParentKind, *,
        member_is_role: bool = False,
    ) -> SqlOp: ...

    @abstractmethod
    def grant_role_to(
        self, name: str, target: str, kind: ParentKind, *,
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
                s=sql.Identifier(schema), r=grantee,
            ),
        )

    def grant_schema_create(
        self, name: str, schema: str, *, as_role: bool = False
    ) -> SqlOp:
        grantee, label = self._grantee(name, as_role)
        return SqlOp(
            description=f"GRANT CREATE ON SCHEMA {schema} TO {label}",
            statement=sql.SQL("GRANT CREATE ON SCHEMA {s} TO {r}").format(
                s=sql.Identifier(schema), r=grantee,
            ),
        )

    def grant_table_select(
        self, name: str, schema: str, *, as_role: bool = False
    ) -> SqlOp:
        grantee, label = self._grantee(name, as_role)
        return SqlOp(
            description=f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {label}",
            statement=sql.SQL(
                "GRANT SELECT ON ALL TABLES IN SCHEMA {s} TO {r}"
            ).format(s=sql.Identifier(schema), r=grantee),
        )

    def grant_table_all(
        self, name: str, schema: str, *, as_role: bool = False
    ) -> SqlOp:
        grantee, label = self._grantee(name, as_role)
        return SqlOp(
            description=f"GRANT ALL ON ALL TABLES IN SCHEMA {schema} TO {label}",
            statement=sql.SQL(
                "GRANT ALL ON ALL TABLES IN SCHEMA {s} TO {r}"
            ).format(s=sql.Identifier(schema), r=grantee),
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
                "ALTER DEFAULT PRIVILEGES IN SCHEMA {s} "
                "GRANT SELECT ON TABLES TO {r}"
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
                "ALTER DEFAULT PRIVILEGES IN SCHEMA {s} "
                "GRANT ALL ON TABLES TO {r}"
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
                role=sql.Identifier(name), pw=sql.Literal(password),
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
        self, name: str, parent: str, kind: ParentKind, *,
        member_is_role: bool = False,
    ) -> SqlOp:
        return SqlOp(
            description=f"GRANT {parent} TO {name}",
            statement=sql.SQL("GRANT {parent} TO {role}").format(
                parent=sql.Identifier(parent), role=sql.Identifier(name),
            ),
        )

    def grant_role_to(
        self, name: str, target: str, kind: ParentKind, *,
        name_is_role: bool = False,
    ) -> SqlOp:
        return SqlOp(
            description=f"GRANT {name} TO {target}",
            statement=sql.SQL("GRANT {role} TO {target}").format(
                role=sql.Identifier(name), target=sql.Identifier(target),
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


class RedshiftDialect(RoleDialect):
    engine = SqlEngine.redshift

    def create_login(self, name: str, password: str) -> SqlOp:
        return SqlOp(
            description=f"CREATE USER {name} PASSWORD '<random>'",
            statement=sql.SQL("CREATE USER {user} PASSWORD {pw}").format(
                user=sql.Identifier(name), pw=sql.Literal(password),
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
        self, name: str, parent: str, kind: ParentKind, *,
        member_is_role: bool = False,
    ) -> SqlOp:
        if kind == ParentKind.group:
            # Only login users fit in legacy groups; role members must be
            # rejected by the plan builder before reaching here.
            return SqlOp(
                description=f"ALTER GROUP {parent} ADD USER {name}",
                statement=sql.SQL("ALTER GROUP {g} ADD USER {u}").format(
                    g=sql.Identifier(parent), u=sql.Identifier(name),
                ),
            )
        # RBAC role (ParentKind.absent must be caught by the CLI before here).
        grantee, label = self._grantee(name, member_is_role)
        return SqlOp(
            description=f"GRANT ROLE {parent} TO {label}",
            statement=sql.SQL("GRANT ROLE {r} TO {u}").format(
                r=sql.Identifier(parent), u=grantee,
            ),
        )

    def grant_role_to(
        self, name: str, target: str, kind: ParentKind, *,
        name_is_role: bool = False,
    ) -> SqlOp:
        # Only RBAC roles are grantable on Redshift; the plan builder rejects
        # login users (name_is_role=False) and group targets before this.
        grantee, label = self._grantee(target, kind is ParentKind.role)
        return SqlOp(
            description=f"GRANT ROLE {name} TO {label}",
            statement=sql.SQL("GRANT ROLE {r} TO {t}").format(
                r=sql.Identifier(name), t=grantee,
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

    def _rbac_role_exists(self, cursor: Any, parent: str) -> bool:
        """Probe svv_roles for an RBAC role, guarded so a missing view on a
        pre-RBAC cluster does not poison the outer transaction."""
        probe = "dna_parent_probe"
        try:
            cursor.execute(f"SAVEPOINT {probe}")
        except Exception:  # noqa: BLE001  connection-level failure, bail
            return False
        try:
            cursor.execute(
                "SELECT 1 FROM svv_roles WHERE role_name = %s", (parent,)
            )
            found = cursor.fetchone() is not None
        except Exception:  # noqa: BLE001  view missing / no permission
            with contextlib.suppress(Exception):
                cursor.execute(f"ROLLBACK TO SAVEPOINT {probe}")
            return False
        with contextlib.suppress(Exception):
            cursor.execute(f"RELEASE SAVEPOINT {probe}")
        return found

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
                per_db.append(SqlOp(
                    description=f"ALTER SCHEMA {schema} OWNER TO {reassign_to or default_owner}",
                    statement=sql.SQL("ALTER SCHEMA {s} OWNER TO {o}").format(
                        s=sql.Identifier(schema), o=owner,
                    ),
                ))
            for schema, relname, relkind in owned.relations:
                kw = "VIEW" if relkind == "v" else "TABLE"
                per_db.append(SqlOp(
                    description=(
                        f"ALTER {kw} {schema}.{relname} "
                        f"OWNER TO {reassign_to or default_owner}"
                    ),
                    statement=sql.SQL(
                        "ALTER " + kw + " {s}.{r} OWNER TO {o}"
                    ).format(
                        s=sql.Identifier(schema), r=sql.Identifier(relname), o=owner,
                    ),
                ))
        cluster: list[SqlOp] = []
        for group in groups:
            cluster.append(SqlOp(
                description=f"ALTER GROUP {group} DROP USER {name}",
                statement=sql.SQL("ALTER GROUP {g} DROP USER {u}").format(
                    g=sql.Identifier(group), u=user,
                ),
            ))
        cluster.append(SqlOp(
            description=f"DROP USER {name}",
            statement=sql.SQL("DROP USER {u}").format(u=user),
        ))
        return DropOps(pre_cluster_ops=[], per_database_ops=per_db, cluster_ops=cluster)


def dialect_for(engine: SqlEngine) -> RoleDialect:
    """Return the dialect for ``engine`` (defaults to Postgres)."""
    if engine == SqlEngine.redshift:
        return RedshiftDialect()
    return PostgresDialect()
