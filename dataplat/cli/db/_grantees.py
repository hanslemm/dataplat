"""The CLI's grantee-kind option, shared by the role and schema grant commands.

Its own module because both need it and neither owns it — importing a private
name out of a sibling command module would be the alternative.
"""

from __future__ import annotations

from enum import Enum

from dataplat.services.db.role_dialects import ParentKind

__all__ = ["GranteeKind", "parent_kind_for"]


class GranteeKind(str, Enum):
    """The three things a ``--kind`` / ``--to-kind`` can name.

    Deliberately not :class:`~dataplat.services.db.role.RoleKind`, which has two
    members because it answers a different question — ``role list`` and
    ``role show`` classify by "can it log in", so a Redshift RBAC role reads as
    ``group`` there. And deliberately not :class:`ParentKind`, whose ``absent``
    member Typer would happily offer as a valid choice on the command line.
    """

    user = "user"
    group = "group"
    role = "role"


_PARENT_KIND: dict[GranteeKind, ParentKind] = {
    GranteeKind.user: ParentKind.user,
    GranteeKind.group: ParentKind.group,
    GranteeKind.role: ParentKind.role,
}


def parent_kind_for(kind: GranteeKind | None) -> ParentKind | None:
    """Translate the CLI enum to the service one, passing ``None`` through."""
    return None if kind is None else _PARENT_KIND[kind]
