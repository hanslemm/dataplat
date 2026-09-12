"""Who a person is, and what they are called on each system.

The two conventions this was written against differ inside one company --
`bd_hlemm` on one warehouse, `h_lemm` on another -- so neither is in the code.
A template per target is, and these tests are where its edges are pinned.
"""

from __future__ import annotations

import pytest

from dataplat.core.errors import ValidationError
from dataplat.services.people.identity import (
    parse_identity,
    render_username,
    username_template,
)

EVA = "eva.germeshausen@betterdoc.de"


def test_the_dotted_local_part_carries_a_first_and_last_name() -> None:
    identity = parse_identity(EVA)

    assert identity.local == "eva.germeshausen"
    assert identity.first == "eva"
    assert identity.last == "germeshausen"


def test_explicit_names_win_over_the_email() -> None:
    """The email is a guess; what someone is called is not ours to insist on."""
    identity = parse_identity(EVA, first_name="Eva-Maria", last_name="Germeshausen")

    assert identity.first == "Eva-Maria"
    assert identity.last == "Germeshausen"


def test_a_local_part_with_no_dot_has_no_last_name() -> None:
    identity = parse_identity("eva@betterdoc.de")

    assert identity.first == "eva"
    assert identity.last is None


def test_only_the_last_segment_is_the_last_name() -> None:
    """A username cannot hold the space that "der berg" would introduce."""
    identity = parse_identity("jan.van.der.berg@betterdoc.de")

    assert identity.first == "jan"
    assert identity.last == "berg"


def test_an_address_without_an_at_is_not_an_email() -> None:
    with pytest.raises(ValidationError, match="not an email"):
        parse_identity("eva.germeshausen")


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("bd_{first_initial}{last}", "bd_egermeshausen"),
        ("{first_initial}_{last}", "e_germeshausen"),
        ("{local}", "eva.germeshausen"),
        ("{first}.{last}", "eva.germeshausen"),
        ("{last}{first_initial}", "germeshausene"),
    ],
)
def test_a_template_renders_the_convention_it_was_given(
    template: str, expected: str
) -> None:
    assert render_username(template, parse_identity(EVA)) == expected


def test_a_rendered_username_is_lowercased() -> None:
    """Warehouses fold case inconsistently; the tool does not have to."""
    identity = parse_identity("Eva.Germeshausen@BetterDoc.de")

    assert render_username("bd_{first_initial}{last}", identity) == "bd_egermeshausen"


def test_a_template_that_needs_a_last_name_says_so_rather_than_guessing() -> None:
    """`bd_e` is a plausible username and the wrong one; refusing beats inventing."""
    identity = parse_identity("eva@betterdoc.de")

    with pytest.raises(ValidationError) as excinfo:
        render_username("bd_{first_initial}{last}", identity)

    assert "last" in str(excinfo.value)
    assert "eva@betterdoc.de" in str(excinfo.value)


def test_an_unknown_placeholder_is_named() -> None:
    with pytest.raises(ValidationError) as excinfo:
        render_username("bd_{nope}", parse_identity(EVA))

    assert "nope" in str(excinfo.value)


def test_the_template_comes_from_the_targets_own_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATAOCEAN_USERNAME_TEMPLATE", "bd_{first_initial}{last}")

    assert username_template("DATAOCEAN") == "bd_{first_initial}{last}"


def test_a_target_with_no_template_has_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not an error and not a default: it is how a target opts out entirely."""
    monkeypatch.delenv("BETTERDATA_USERNAME_TEMPLATE", raising=False)

    assert username_template("BETTERDATA") is None


def test_a_default_applies_only_where_one_is_offered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SUPERSET_USERNAME_TEMPLATE", raising=False)

    assert username_template("SUPERSET", default="{local}") == "{local}"
