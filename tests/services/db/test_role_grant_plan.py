"""The ``role grant`` plan builder: what it refuses, and why.

Most of these assert on a *refusal*. That is the point of the module: every
combination rejected here is one that Redshift would reject at execution time
with a raw SQL error, halfway through a batch that may already have created
users. Failing before the first statement is the whole value.
"""

from __future__ import annotations

import pytest

from dataplat.core.errors import ExitCode, ValidationError
from dataplat.services.db.role_admin import (
    GrantPair,
    build_grant_plan,
    resolve_grantee_kinds,
)
from dataplat.services.db.role_dialects import (
    ParentKind,
    PostgresDialect,
    RedshiftDialect,
)


def _plan(**kwargs):
    """build_grant_plan with the boring arguments defaulted."""
    kwargs.setdefault("held", set())
    kwargs.setdefault("create_missing_users", False)
    return build_grant_plan(**kwargs)


# ---------------------------------------------------------------------------
# The cross product
# ---------------------------------------------------------------------------


def test_every_role_is_granted_to_every_target() -> None:
    """Two roles and three people is one invocation, not six."""
    plan = _plan(
        roles={"analyst": ParentKind.role, "reader": ParentKind.role},
        targets={"ana": ParentKind.user, "bo": ParentKind.user, "cy": ParentKind.user},
    )

    assert len(plan.grants) == 6
    assert plan.creates == ()
    assert plan.already_held == ()
    # Sorted on both axes, so the rendered plan is stable to read and to diff.
    assert [(p.role, p.target) for p in plan.grants] == [
        ("analyst", "ana"),
        ("analyst", "bo"),
        ("analyst", "cy"),
        ("reader", "ana"),
        ("reader", "bo"),
        ("reader", "cy"),
    ]


def test_both_kinds_are_carried_on_the_pair() -> None:
    """The SQL depends on both sides: one picks the statement, one the grantee."""
    plan = _plan(
        roles={"legacy": ParentKind.group},
        targets={"ana": ParentKind.user},
    )

    assert plan.grants == (
        GrantPair(
            role="legacy",
            role_kind=ParentKind.group,
            target="ana",
            target_kind=ParentKind.user,
        ),
    )


def test_a_grant_already_in_effect_is_reported_not_reissued() -> None:
    plan = _plan(
        roles={"analyst": ParentKind.role},
        targets={"ana": ParentKind.user, "bo": ParentKind.user},
        held={("analyst", "ana")},
    )

    assert [(p.role, p.target) for p in plan.grants] == [("analyst", "bo")]
    assert [(p.role, p.target) for p in plan.already_held] == [("analyst", "ana")]


def test_held_is_matched_per_pair_not_per_role() -> None:
    """A role held by one person must still be granted to the next.

    Guards the obvious wrong shape — treating `held` as a set of role names —
    which would silently skip everyone once anyone held the role.
    """
    plan = _plan(
        roles={"analyst": ParentKind.role, "reader": ParentKind.role},
        targets={"ana": ParentKind.user, "bo": ParentKind.user},
        held={("analyst", "ana"), ("reader", "bo")},
    )

    assert {(p.role, p.target) for p in plan.grants} == {
        ("analyst", "bo"),
        ("reader", "ana"),
    }


# ---------------------------------------------------------------------------
# Missing names
# ---------------------------------------------------------------------------


def test_an_absent_role_is_refused_with_a_way_forward() -> None:
    """You cannot create the role being granted — only the recipients."""
    with pytest.raises(ValidationError) as exc:
        _plan(
            roles={"analyst": ParentKind.role, "typo": ParentKind.absent},
            targets={"ana": ParentKind.user},
        )

    assert "typo" in str(exc.value)
    assert "dp db role create" in str(exc.value)
    assert exc.value.exit_code == ExitCode.INVALID_INPUT


def test_an_absent_target_names_the_flag_that_allows_it() -> None:
    with pytest.raises(ValidationError) as exc:
        _plan(
            roles={"analyst": ParentKind.role},
            targets={"newhire": ParentKind.absent},
        )

    assert "newhire" in str(exc.value)
    assert "--create-missing-users" in str(exc.value)


def test_absent_targets_become_creates_and_are_granted_as_users() -> None:
    plan = _plan(
        roles={"analyst": ParentKind.role},
        targets={"newhire": ParentKind.absent, "ana": ParentKind.user},
        create_missing_users=True,
    )

    assert plan.creates == ("newhire",)
    # Created as a login user, so the grantee is spelled as a user — not left
    # `absent`, which would render as neither.
    assert {(p.target, p.target_kind) for p in plan.grants} == {
        ("newhire", ParentKind.user),
        ("ana", ParentKind.user),
    }


def test_validation_happens_before_any_create_is_planned() -> None:
    """A typo in the last argument must not cost a created user.

    The refusal here is for the *role*, while a target is also missing: if the
    builder resolved creates first and validated later, the caller would learn
    about the bad role only after `newhire` existed.
    """
    with pytest.raises(ValidationError):
        _plan(
            roles={"typo": ParentKind.absent},
            targets={"newhire": ParentKind.absent},
            create_missing_users=True,
        )


# ---------------------------------------------------------------------------
# Engine refusals
# ---------------------------------------------------------------------------


def test_a_redshift_group_cannot_hold_a_role() -> None:
    """`ALTER GROUP g ADD USER r` is not a thing; groups hold login users."""
    with pytest.raises(ValidationError) as exc:
        _plan(
            roles={"legacy": ParentKind.group},
            targets={"analyst": ParentKind.role},
            dialect=RedshiftDialect(),
        )

    message = str(exc.value)
    assert "legacy" in message
    assert "analyst" in message
    assert "login users only" in message


def test_a_role_cannot_be_granted_to_a_redshift_group() -> None:
    """The reverse edge: Redshift has no GRANT ROLE ... TO GROUP form."""
    with pytest.raises(ValidationError) as exc:
        _plan(
            roles={"analyst": ParentKind.role},
            targets={"legacy": ParentKind.group},
            dialect=RedshiftDialect(),
        )

    assert "GROUP" in str(exc.value).upper()


def test_a_redshift_login_user_is_not_grantable() -> None:
    """`GRANT ROLE <a user> TO ...` is invalid — users hold no members.

    Without this the statement is built happily and fails at execution with a
    driver-level error, after any --create-missing-users work has run.
    """
    with pytest.raises(ValidationError) as exc:
        _plan(
            roles={"ana": ParentKind.user},
            targets={"bo": ParentKind.user},
            dialect=RedshiftDialect(),
        )

    message = str(exc.value)
    assert "ana" in message
    assert "--kind role" in message


def test_postgres_grants_a_login_role_to_another_role() -> None:
    """The same shape is legal on Postgres, where users and roles are one thing.

    This is why that refusal is engine-gated and the two group checks are not:
    a `group` kind cannot arise on Postgres at all.
    """
    plan = _plan(
        roles={"ana": ParentKind.user},
        targets={"bo": ParentKind.user},
        dialect=PostgresDialect(),
    )

    assert [(p.role, p.target) for p in plan.grants] == [("ana", "bo")]


def test_the_default_dialect_is_postgres() -> None:
    """Matches build_create_plan, so a caller that omits it gets no surprise."""
    plan = _plan(
        roles={"ana": ParentKind.user},
        targets={"bo": ParentKind.user},
    )

    assert len(plan.grants) == 1


# ---------------------------------------------------------------------------
# Kind resolution
# ---------------------------------------------------------------------------


class _KindCursor:
    """Cursor that answers `grantable_kinds` from a name -> kinds mapping."""

    def __init__(self, kinds: dict[str, tuple[ParentKind, ...]]) -> None:
        self._kinds = kinds
        self._result: list[tuple] = []

    def execute(self, query, params=None) -> None:
        text = query if isinstance(query, str) else str(query)
        if text.startswith(("SAVEPOINT", "ROLLBACK", "RELEASE")):
            self._result = []
            return
        name = params[0] if params else None
        found = self._kinds.get(name, ())
        if "pg_user" in text or "rolcanlogin" in text:
            # rolcanlogin is the Postgres shape: one row, boolean column.
            if "rolcanlogin" in text:
                self._result = [(ParentKind.user in found,)] if found else []
            else:
                self._result = [(1,)] if ParentKind.user in found else []
        elif "pg_group" in text:
            self._result = [(1,)] if ParentKind.group in found else []
        elif "svv_roles" in text:
            self._result = [(1,)] if ParentKind.role in found else []
        else:  # pragma: no cover - defensive
            self._result = []

    def fetchall(self) -> list[tuple]:
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None


def test_an_unambiguous_name_resolves_without_a_flag() -> None:
    cursor = _KindCursor({"ana": (ParentKind.user,)})

    kinds = resolve_grantee_kinds(
        RedshiftDialect(), cursor, ["ana"], None, flag="--to-kind"
    )

    assert kinds == {"ana": ParentKind.user}


def test_a_missing_name_resolves_to_absent_rather_than_raising() -> None:
    """The plan builder decides what absence means; this only reports it."""
    cursor = _KindCursor({})

    kinds = resolve_grantee_kinds(
        RedshiftDialect(), cursor, ["ghost"], None, flag="--to-kind"
    )

    assert kinds == {"ghost": ParentKind.absent}


def test_an_ambiguous_name_names_the_flag_that_resolves_it() -> None:
    """On Redshift one name can be a user and a group and a role at once."""
    cursor = _KindCursor({"finance": (ParentKind.group, ParentKind.role)})

    with pytest.raises(ValidationError) as exc:
        resolve_grantee_kinds(
            RedshiftDialect(), cursor, ["finance"], None, flag="--to-kind"
        )

    message = str(exc.value)
    assert "group, role" in message
    assert "--to-kind" in message


def test_a_forced_kind_resolves_the_ambiguity() -> None:
    cursor = _KindCursor({"finance": (ParentKind.group, ParentKind.role)})

    kinds = resolve_grantee_kinds(
        RedshiftDialect(), cursor, ["finance"], ParentKind.role, flag="--to-kind"
    )

    assert kinds == {"finance": ParentKind.role}


def test_a_forced_kind_the_name_is_not_is_refused() -> None:
    """--kind is for disambiguating, not for asserting something untrue."""
    cursor = _KindCursor({"finance": (ParentKind.group,)})

    with pytest.raises(ValidationError) as exc:
        resolve_grantee_kinds(
            RedshiftDialect(), cursor, ["finance"], ParentKind.role, flag="--kind"
        )

    message = str(exc.value)
    assert "is not a role" in message
    assert "found: group" in message


@pytest.mark.parametrize("name", ["PUBLIC", "public", "Public"])
def test_public_is_refused_in_any_casing(name: str) -> None:
    """Verified on PostgreSQL 16: `GRANT <role> TO PUBLIC` errors with
    'role "public" does not exist'. PUBLIC is a grantee for object privileges,
    not for role membership.

    Refused by name rather than left to the catalog lookup, because it would
    otherwise resolve to `absent` — and with --create-missing-users that means
    trying to CREATE a user called PUBLIC.
    """
    cursor = _KindCursor({})

    with pytest.raises(ValidationError) as exc:
        resolve_grantee_kinds(RedshiftDialect(), cursor, [name], None, flag="--to-kind")

    assert "PUBLIC" in str(exc.value)
    assert "object privileges" in str(exc.value)
