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
