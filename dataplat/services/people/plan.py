"""What onboarding would do, assembled before any of it is done.

Three systems and no transaction spanning them. The plan is the only place the
whole intent is visible at once, so everything that can be refused is refused
here -- while refusing still costs nothing, because no account exists yet.

Assembly is pure: the caller reads the facts (who exists where, what they
hold), this turns them into the plan. That split is what lets the interesting
cases -- an area with no template, a reference person absent from one warehouse
-- be tested without a database or a Superset.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from dataplat.services.people.identity import PersonIdentity, render_username

__all__ = ["AreaAccount", "AreaFacts", "OnboardPlan", "build_onboard_plan"]


@dataclass(frozen=True)
class AreaFacts:
    """What one area answered about the reference person and the new username.

    ``template`` is ``None`` when the area configured none, which is how it
    declines to have accounts created on it at all.
    """

    scope: str
    template: str | None
    reference_username: str
    reference_exists: bool
    reference_memberships: tuple[str, ...]
    target_exists: bool


@dataclass(frozen=True)
class AreaAccount:
    """One account the plan would create, or found already there."""

    scope: str
    username: str
    memberships: tuple[str, ...]
    already_exists: bool


@dataclass(frozen=True)
class OnboardPlan:
    identity: PersonIdentity
    reference: PersonIdentity
    accounts: tuple[AreaAccount, ...]
    skipped: tuple[tuple[str, str], ...]

    @property
    def to_create(self) -> tuple[AreaAccount, ...]:
        """The accounts execution would actually touch."""
        return tuple(a for a in self.accounts if not a.already_exists)

    @property
    def nothing_to_do(self) -> bool:
        return not self.to_create


def build_onboard_plan(
    identity: PersonIdentity,
    reference: PersonIdentity,
    facts: Iterable[AreaFacts],
) -> OnboardPlan:
    """Turn per-area facts into one plan, skipping what cannot be copied.

    Two things get skipped rather than improvised. An area with no template
    configured is opting out, and an area the reference person has no account
    on has nothing to copy -- creating an empty account there would leave
    someone to discover later that it grants nothing.
    """
    accounts: list[AreaAccount] = []
    skipped: list[tuple[str, str]] = []

    for fact in facts:
        if fact.template is None:
            skipped.append((fact.scope, "no username template configured"))
            continue
        if not fact.reference_exists:
            skipped.append((fact.scope, f"{reference.email} has no account here"))
            continue

        accounts.append(
            AreaAccount(
                scope=fact.scope,
                username=render_username(fact.template, identity),
                memberships=fact.reference_memberships,
                already_exists=fact.target_exists,
            )
        )

    return OnboardPlan(
        identity=identity,
        reference=reference,
        accounts=tuple(accounts),
        skipped=tuple(skipped),
    )
