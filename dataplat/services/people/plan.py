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

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

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
    # How many accounts hold each membership, and how many accounts exist.
    # Only used to notice that a copied grant is an unusual one.
    holders: Mapping[str, int] = field(default_factory=dict)
    population: int = 0


@dataclass(frozen=True)
class AreaAccount:
    """One account the plan would create, or found already there."""

    scope: str
    username: str
    memberships: tuple[str, ...]
    already_exists: bool
    # Copied memberships few existing accounts hold, with how many hold each,
    # and the population that count is out of.
    unusual: tuple[tuple[str, int], ...] = ()
    population: int = 0


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


# One in ten. The first version of this asked whether *most* accounts lacked
# the grant, which reads well and is useless: measured against a real Superset
# of 252 accounts, exactly two roles out of some forty were held by a majority,
# so a majority rule fires on nearly every grant there is -- including the
# per-dashboard access roles that are supposed to be narrow. At one in ten the
# warning stayed rare enough to be read, and it still catches the case it
# exists for: Admin, held by 13 of those 252.
_RARE_NUMERATOR = 1
_RARE_DENOMINATOR = 10


def _unusual(
    memberships: tuple[str, ...], holders: Mapping[str, int], population: int
) -> tuple[tuple[str, int], ...]:
    """The copied memberships few accounts hold, each with its holder count.

    ``--like`` copies a colleague's access, and with it their privilege level:
    the person running it reads a username and a status, not a membership list.
    Copying an admin is the case worth catching, and "admin" is a different
    word at every company -- so the signal is how many other accounts hold the
    thing, never its name.

    The count travels with the finding because no threshold is right
    everywhere. "Admin (13 of 252 accounts)" lets the reader judge for
    themselves; a bare warning asks them to trust a constant they cannot see.

    With no population to compare against, nothing is unusual: on a fresh
    platform every grant looks rare, and a warning that always fires is one
    people learn to skip.
    """
    if population <= 0:
        return ()
    return tuple(
        (m, holders.get(m, 0))
        for m in memberships
        if holders.get(m, 0) * _RARE_DENOMINATOR <= population * _RARE_NUMERATOR
    )


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
                unusual=_unusual(
                    fact.reference_memberships, fact.holders, fact.population
                ),
                population=fact.population,
            )
        )

    return OnboardPlan(
        identity=identity,
        reference=reference,
        accounts=tuple(accounts),
        skipped=tuple(skipped),
    )
