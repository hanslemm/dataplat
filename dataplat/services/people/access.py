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
from dataplat.services.db.role_dialects import dialect_for

__all__ = [
    "db_membership_holders",
    "db_memberships",
    "db_user_exists",
    "superset_access",
    "superset_membership_holders",
]


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


def _names(items: object) -> tuple[str, ...]:
    if not isinstance(items, list):
        return ()
    return tuple(
        str(item.get("name"))
        for item in items
        if isinstance(item, dict) and item.get("name")
    )


def superset_access(
    users: Iterable[dict], username: str
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """``(role_names, group_names)`` for ``username``, or ``None`` if absent.

    Names rather than ids, because a plan is read by a person before it is
    executed and "roles: 2, 5" tells nobody whether the copy is right. The
    create call resolves names to ids through the same helpers that validate
    them, so carrying ids here would buy nothing and cost the review.

    ``None`` rather than two empty tuples: "no such account" and "an account
    with nothing granted" lead to different decisions, and collapsing them
    would let a typo'd ``--like`` silently onboard someone with no access.
    """
    for user in users:
        if str(user.get("username", "")).lower() == username.lower():
            return _names(user.get("roles")), _names(user.get("groups"))
    return None


def db_membership_holders(cursor: Any, engine: SqlEngine) -> tuple[dict[str, int], int]:
    """How many accounts hold each role, and how many accounts there are.

    Only used to notice that a copied grant is an unusual one, so the counts
    need to be indicative rather than exact -- which is why one listing answers
    it instead of a membership query per role.
    """
    rows = dialect_for(engine).list_roles(cursor)
    holders = {row.name: row.members_count for row in rows if not row.can_login}
    population = sum(1 for row in rows if row.can_login)
    return holders, population


def superset_membership_holders(
    users: Iterable[dict],
) -> tuple[dict[str, int], int]:
    """The same counts for Superset, from the listing already in hand."""
    holders: dict[str, int] = {}
    population = 0
    for user in users:
        population += 1
        for name in (*_names(user.get("roles")), *_names(user.get("groups"))):
            holders[name] = holders.get(name, 0) + 1
    return holders, population
