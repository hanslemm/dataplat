"""The plan: what onboarding would do, before it does any of it.

Three systems and no transaction between them, so the plan is the only place
the whole intent is visible at once. Everything it can refuse, it refuses here
-- before the first account exists.
"""

from __future__ import annotations

from dataplat.services.people.identity import parse_identity
from dataplat.services.people.plan import (
    AreaAccount,
    AreaFacts,
    build_onboard_plan,
)

EVA = parse_identity("eva.germeshausen@betterdoc.de")
HANS = parse_identity("hans.lemm@betterdoc.de")


def _facts(**kwargs) -> AreaFacts:
    defaults = {
        "scope": "dataocean",
        "template": "bd_{first_initial}{last}",
        "reference_exists": True,
        "reference_memberships": ("team_dna",),
        "target_exists": False,
        "holders": {},
        "population": 0,
    }
    return AreaFacts(**{**defaults, "reference_username": "bd_hlemm", **kwargs})


def test_an_account_takes_its_name_from_the_areas_template() -> None:
    plan = build_onboard_plan(EVA, HANS, [_facts()])

    assert plan.accounts == (
        AreaAccount(
            scope="dataocean",
            username="bd_egermeshausen",
            memberships=("team_dna",),
            already_exists=False,
        ),
    )


def test_each_area_renders_its_own_convention() -> None:
    plan = build_onboard_plan(
        EVA,
        HANS,
        [
            _facts(),
            _facts(
                scope="betterdata",
                template="{first_initial}_{last}",
                reference_username="h_lemm",
                reference_memberships=("pii_users", "team_dna"),
            ),
            _facts(
                scope="superset",
                template="{local}",
                reference_username="hans.lemm",
                reference_memberships=("Gamma",),
            ),
        ],
    )

    assert [a.username for a in plan.accounts] == [
        "bd_egermeshausen",
        "e_germeshausen",
        "eva.germeshausen",
    ]


def test_an_area_with_no_template_is_skipped_with_the_reason() -> None:
    """How an SSO-managed warehouse stays out of a plan: by saying nothing."""
    plan = build_onboard_plan(EVA, HANS, [_facts(template=None)])

    assert plan.accounts == ()
    assert plan.skipped == (("dataocean", "no username template configured"),)


def test_an_area_the_reference_person_is_absent_from_is_skipped() -> None:
    """Copying nobody's access would create an account that can do nothing.

    Better to say the reference is not here than to make an empty account and
    leave someone to discover it has no grants.
    """
    plan = build_onboard_plan(EVA, HANS, [_facts(reference_exists=False)])

    assert plan.accounts == ()
    assert plan.skipped == (
        ("dataocean", "hans.lemm@betterdoc.de has no account here"),
    )


def test_an_existing_account_is_marked_rather_than_refused() -> None:
    """Re-running after a partial failure is normal and must be safe."""
    plan = build_onboard_plan(EVA, HANS, [_facts(target_exists=True)])

    assert plan.accounts[0].already_exists is True
    assert plan.to_create == ()


def test_to_create_is_what_execution_will_touch() -> None:
    plan = build_onboard_plan(
        EVA,
        HANS,
        [
            _facts(),
            _facts(
                scope="betterdata",
                template="{first_initial}_{last}",
                reference_username="h_lemm",
                target_exists=True,
            ),
        ],
    )

    assert [a.scope for a in plan.to_create] == ["dataocean"]
    assert plan.nothing_to_do is False


def test_a_plan_with_nothing_left_says_so() -> None:
    plan = build_onboard_plan(EVA, HANS, [_facts(target_exists=True)])

    assert plan.nothing_to_do is True


# ---------------------------------------------------------------------------
# Unusual grants
# ---------------------------------------------------------------------------
# `--like` copies a colleague's access, which quietly copies their *privilege
# level* too: the person running it reads a username and a status, not the
# membership list. Copying an admin is the case worth catching, and "admin" is
# a different word at every company -- so the signal is how many other accounts
# hold the thing, not its name.


def test_a_membership_hardly_anyone_holds_is_flagged_with_its_count() -> None:
    """The count travels with the finding: no threshold is right everywhere.

    "superuser_ish (2 of 27 accounts)" lets the reader judge for themselves; a
    bare warning asks them to trust a constant they cannot see.
    """
    plan = build_onboard_plan(
        EVA,
        HANS,
        [
            _facts(
                reference_memberships=("team_dna", "superuser_ish"),
                holders={"team_dna": 20, "superuser_ish": 2},
                population=27,
            )
        ],
    )

    assert plan.accounts[0].unusual == (("superuser_ish", 2),)
    assert plan.accounts[0].population == 27


def test_an_ordinary_membership_is_not_flagged() -> None:
    """Warning about the ordinary case trains people to ignore the warning."""
    plan = build_onboard_plan(
        EVA,
        HANS,
        [
            _facts(
                reference_memberships=("team_dna",),
                holders={"team_dna": 20},
                population=27,
            )
        ],
    )

    assert plan.accounts[0].unusual == ()


def test_a_grant_only_a_minority_holds_is_still_not_rare() -> None:
    """Measured, not guessed.

    The first rule asked whether *most* accounts lacked the grant. Against a
    real Superset of 252 accounts, two roles out of some forty were held by a
    majority -- so that rule fires on nearly every grant there is, including
    the per-dashboard roles meant to be narrow. One in ten is where the warning
    stayed rare enough to be read, and it still catches Admin at 13 of 252.
    """
    plan = build_onboard_plan(
        EVA,
        HANS,
        [_facts(reference_memberships=("third",), holders={"third": 7}, population=20)],
    )

    assert plan.accounts[0].unusual == ()


def test_one_in_ten_is_the_boundary() -> None:
    plan = build_onboard_plan(
        EVA,
        HANS,
        [_facts(reference_memberships=("rare",), holders={"rare": 2}, population=20)],
    )

    assert plan.accounts[0].unusual == (("rare", 2),)


def test_nothing_is_flagged_when_there_is_no_population_to_compare_against() -> None:
    """A fresh platform makes every grant look rare; that is not information."""
    plan = build_onboard_plan(
        EVA,
        HANS,
        [_facts(reference_memberships=("team_dna",), holders={}, population=0)],
    )

    assert plan.accounts[0].unusual == ()


def test_an_unknown_membership_counts_as_rare() -> None:
    """Held by nobody the reader listed is as unusual as it gets."""
    plan = build_onboard_plan(
        EVA,
        HANS,
        [
            _facts(
                reference_memberships=("ghost",), holders={"other": 20}, population=27
            )
        ],
    )

    assert plan.accounts[0].unusual == (("ghost", 0),)
