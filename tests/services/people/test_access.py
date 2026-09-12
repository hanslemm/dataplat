"""What an existing person already has, per area.

`--like` copies access rather than inventing it, so these readers are the whole
of the policy: whatever they return is what the new person gets.
"""

from __future__ import annotations

from typing import Any

import pytest

from dataplat.services.db.connection import SqlEngine
from dataplat.services.people.access import (
    db_memberships,
    db_user_exists,
    superset_access,
)


class _Cursor:
    """Fake cursor routing by SQL substring, as the real queries are two.

    ``resolve_role`` reads with fetchone and the membership walk with fetchall,
    so a fake serving only one of them would decide which code path is
    testable.
    """

    def __init__(
        self, *, lookup: tuple | None, memberships: list[tuple] | None = None
    ) -> None:
        self._lookup = lookup
        self._memberships = memberships or []
        self._last = ""
        self.executed: list[str] = []

    def execute(self, sql_text: Any, params: Any = None) -> None:
        self._last = str(sql_text)
        self.executed.append(self._last)

    def fetchone(self) -> tuple | None:
        return self._lookup

    def fetchall(self) -> list[tuple]:
        return self._memberships


def test_a_postgres_users_direct_memberships_are_read() -> None:
    cursor = _Cursor(
        lookup=(16384, True, False),
        memberships=[
            ("team_dna", True, 1, "bd_hlemm"),
            ("pii_users", True, 1, "bd_hlemm"),
        ],
    )

    assert db_memberships(cursor, SqlEngine.postgresql, "bd_hlemm") == (
        "team_dna",
        "pii_users",
    )


def test_only_direct_memberships_are_copied() -> None:
    """An inherited parent is not something this person was granted.

    Reproducing depth > 1 would grant explicitly what the reference person
    holds only through a group -- so removing them from that group later would
    no longer remove it from the copy.
    """
    cursor = _Cursor(
        lookup=(16384, True, False),
        memberships=[
            ("team_dna", True, 1, "bd_hlemm"),
            ("inherited_parent", True, 2, "team_dna"),
        ],
    )

    assert db_memberships(cursor, SqlEngine.postgresql, "bd_hlemm") == ("team_dna",)


def test_a_redshift_users_groups_are_read_the_same_way() -> None:
    """Verified against a real cluster: the Redshift query answers depth 1."""
    cursor = _Cursor(
        lookup=(110, True, False),
        memberships=[
            ("pii_users", True, 1, ""),
            ("team_dna", True, 1, ""),
        ],
    )

    assert db_memberships(cursor, SqlEngine.redshift, "h_lemm") == (
        "pii_users",
        "team_dna",
    )


def test_an_absent_user_has_no_memberships_rather_than_raising() -> None:
    """`--like` naming someone who is not on this target is answered, not thrown.

    Every target is asked about the reference person, and a company that keeps
    someone off one warehouse on purpose is not an error condition.
    """
    cursor = _Cursor(lookup=None)

    assert db_memberships(cursor, SqlEngine.postgresql, "ghost") == ()


def test_existence_is_reported_for_a_name_held_by_anything() -> None:
    """A group holding the name blocks the username just as a user would."""
    group = _Cursor(lookup=(16390, False, False))
    user = _Cursor(lookup=(16384, True, False))
    absent = _Cursor(lookup=None)

    assert db_user_exists(group, SqlEngine.postgresql, "team_dna") is True
    assert db_user_exists(user, SqlEngine.postgresql, "bd_hlemm") is True
    assert db_user_exists(absent, SqlEngine.postgresql, "nobody") is False


SUPERSET_USERS = [
    {
        "id": 7,
        "username": "hans.lemm",
        "roles": [{"id": 2, "name": "Gamma"}, {"id": 5, "name": "sql_lab"}],
        "groups": [{"id": 10, "name": "analysts"}],
    },
    {"id": 8, "username": "someone.else", "roles": [], "groups": []},
]


def test_a_superset_users_roles_and_groups_are_read() -> None:
    assert superset_access(SUPERSET_USERS, "hans.lemm") == ((2, 5), (10,))


def test_a_superset_username_is_matched_case_insensitively() -> None:
    assert superset_access(SUPERSET_USERS, "Hans.Lemm") == ((2, 5), (10,))


def test_an_absent_superset_user_is_none_rather_than_empty() -> None:
    """Absent and "has nothing" are different answers, and the caller acts on it."""
    assert superset_access(SUPERSET_USERS, "ghost") is None
    assert superset_access(SUPERSET_USERS, "someone.else") == ((), ())


@pytest.mark.parametrize("engine", [SqlEngine.postgresql, SqlEngine.redshift])
def test_the_reference_lookup_never_writes(engine: SqlEngine) -> None:
    """Reading who someone is must not be able to change anything."""
    cursor = _Cursor(lookup=(1, True, False), memberships=[])

    db_memberships(cursor, engine, "someone")

    assert all(sql.strip().upper().startswith(("SELECT", "WITH")) for sql in cursor.executed)
