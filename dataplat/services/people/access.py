"""What a person already has, per area.

``--like`` copies access rather than inventing it, which makes these readers
the entire policy of onboarding: whatever they return is what the next person
gets. Nothing here writes, and the tests assert that.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.role import (
    RoleNotFoundError,
    fetch_memberships_out,
    resolve_role,
)
from dataplat.services.superset.client import user_group_ids, user_role_ids

__all__ = ["db_memberships", "db_user_exists", "superset_access"]


def db_memberships(cursor: Any, engine: SqlEngine, username: str) -> tuple[str, ...]:
    """The roles ``username`` is a direct member of, in listing order.

    Direct only. A parent reached at depth > 1 is something the reference
    person holds *through* a group, and granting it explicitly to the copy
    would outlive the group: remove them from ``team_dna`` later and the copy
    keeps what ``team_dna`` gave them, with nothing recording why.

    An absent user answers ``()``. Every target is asked about the reference
    person, and someone deliberately kept off one warehouse is not an error.
    """
    try:
        ref = resolve_role(cursor, engine, username)
    except (RoleNotFoundError, ValueError):
        return ()

    return tuple(
        edge.role
        for edge in fetch_memberships_out(cursor, ref.oid, engine)
        if edge.depth == 1
    )


def db_user_exists(cursor: Any, engine: SqlEngine, username: str) -> bool:
    """Whether anything already holds ``username`` on this target.

    A group holding the name counts: the name is taken either way, and a
    create that collides with a group fails in a way worth reporting as "this
    already exists" rather than as a database error.
    """
    try:
        resolve_role(cursor, engine, username)
    except (RoleNotFoundError, ValueError):
        return False
    return True


def superset_access(
    users: Iterable[dict], username: str
) -> tuple[tuple[int, ...], tuple[int, ...]] | None:
    """``(role_ids, group_ids)`` for ``username``, or ``None`` if there is no such user.

    ``None`` rather than two empty tuples: "no such account" and "an account
    with nothing granted" lead to different decisions, and collapsing them
    would let a typo'd ``--like`` silently onboard someone with no access.
    """
    for user in users:
        if str(user.get("username", "")).lower() == username.lower():
            return tuple(user_role_ids(user)), tuple(user_group_ids(user))
    return None
