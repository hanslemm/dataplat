from __future__ import annotations

import typer

from dataplat.core.deps import AREAS, AreaDeps
from dataplat.core.registry import (
    AreaMount,
    all_areas,
    area_by_name,
    load_app,
    missing_extra_help,
    mount_help,
)

# A stand-in for a plugin-supplied area: it carries its own AreaDeps and is
# deliberately absent from the AREAS global, which is what used to KeyError.
third_party_app = typer.Typer(name="not-the-mount-name", help="Widget tools")

ABSENT_MODULE = "dataplat_no_such_dependency"

THIRD_PARTY = AreaMount(
    name="widget",
    help_text="Widget tools",
    target="tests.core.test_registry:third_party_app",
    deps=AreaDeps(
        area="widget",
        extra="widget",
        modules=(ABSENT_MODULE,),
        enabled_by=("WIDGET_URL",),
    ),
)


def test_all_builtin_areas_present_in_order() -> None:
    assert [m.name for m in all_areas()] == ["db", "ingest", "bi", "cloud", "ci"]


def test_every_deps_contract_comes_from_the_deps_module() -> None:
    for mount in all_areas():
        if mount.deps is not None:
            assert mount.deps is AREAS[mount.name]


def test_builtin_contracts_agree_with_their_mount_name() -> None:
    # deps.missing_modules/area_ready still index AREAS by area name, so a
    # mount whose name drifted from its contract would report another area's
    # dependencies.
    for mount in all_areas():
        if mount.deps is not None:
            assert mount.deps.area == mount.name


def test_load_app_resolves_every_builtin_target() -> None:
    # Dev env has all extras installed, so every target must import.
    for mount in all_areas():
        app = load_app(mount)
        assert isinstance(app, typer.Typer), mount.target


def test_load_app_resolves_a_third_party_target() -> None:
    assert load_app(THIRD_PARTY) is third_party_app


def test_targets_use_entry_point_shape() -> None:
    # "module:attr" is exactly what a plugin's entry point will declare;
    # keep the built-ins on the same contract.
    for mount in all_areas():
        module, sep, attr = mount.target.partition(":")
        assert sep == ":" and module and attr


def test_area_by_name_finds_every_builtin() -> None:
    for mount in all_areas():
        assert area_by_name(mount.name) is mount


def test_area_by_name_is_none_for_an_unknown_name() -> None:
    assert area_by_name("nope") is None


def test_mount_help_is_the_plain_help_when_dependencies_are_installed() -> None:
    # Dev env has every extra, so no built-in advertises a missing one.
    for mount in all_areas():
        assert mount_help(mount) == mount.help_text


def test_mount_help_flags_the_missing_extra_of_a_third_party_mount() -> None:
    assert THIRD_PARTY.name not in AREAS
    assert mount_help(THIRD_PARTY) == "Widget tools (needs extra: widget)"


def test_missing_extra_help_matches_what_mount_help_produces() -> None:
    assert THIRD_PARTY.deps is not None
    assert missing_extra_help(THIRD_PARTY.help_text, THIRD_PARTY.deps) == mount_help(
        THIRD_PARTY
    )
