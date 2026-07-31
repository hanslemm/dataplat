"""Grantee kinds and the engine-specific ``TO`` clause.

:class:`ParentKind` lives in ``role_dialects`` for history's sake and is
re-exported there; this module owns the part both the role dialects and the
schema dialects need — how a principal is spelled in a ``GRANT``, which is not
the same on every engine.

Depends only on ``connection.SqlEngine``, so any dialect module can import it
without a cycle.
"""

from __future__ import annotations

from psycopg import sql

from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.role_dialects import ParentKind

__all__ = ["PUBLIC", "render_grantee"]

#: Grantee meaning "every principal". A keyword, not a catalog object.
#:
#: Valid for *object* privileges (``GRANT USAGE ON SCHEMA s TO PUBLIC``) and not
#: for role membership: verified on PostgreSQL 16, ``GRANT <role> TO PUBLIC``
#: fails with ``role "public" does not exist``. That is why
#: :func:`~dataplat.services.db.role_admin.resolve_grantee_kinds` refuses it by
#: default and the schema path opts in.
PUBLIC = "PUBLIC"


def render_grantee(
    engine: SqlEngine, name: str, kind: ParentKind
) -> tuple[sql.Composable, str]:
    """Return the ``TO``-clause fragment for ``name``, plus a human label.

    PostgreSQL names every principal the same way — a bare identifier, because
    users and roles share one namespace. Redshift needs the object class spelled
    out and either errors or grants to the wrong principal without it: ``TO
    GROUP g``, ``TO ROLE r``, ``TO u``.

    ``PUBLIC`` is a keyword on both engines and is never quoted — quoting it
    would turn it into a search for a role actually named ``public``.
    """
    if name.upper() == PUBLIC:
        return sql.SQL("PUBLIC"), PUBLIC
    if engine == SqlEngine.redshift:
        if kind is ParentKind.group:
            return sql.SQL("GROUP {}").format(sql.Identifier(name)), f"GROUP {name}"
        if kind is ParentKind.role:
            return sql.SQL("ROLE {}").format(sql.Identifier(name)), f"ROLE {name}"
    return sql.Identifier(name), name
