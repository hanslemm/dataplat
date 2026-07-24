from __future__ import annotations

import typer

from dataplat.core.deps import AREAS
from dataplat.core.registry import all_areas, load_app


def test_all_builtin_areas_present_in_order() -> None:
    assert [m.name for m in all_areas()] == ["db", "ingest", "bi", "cloud", "ci"]


def test_every_deps_contract_comes_from_the_deps_module() -> None:
    for mount in all_areas():
        if mount.deps is not None:
            assert mount.deps is AREAS[mount.name]


def test_load_app_resolves_every_builtin_target() -> None:
    # Dev env has all extras installed, so every target must import.
    for mount in all_areas():
        app = load_app(mount)
        assert isinstance(app, typer.Typer), mount.target


def test_targets_use_entry_point_shape() -> None:
    # "module:attr" is exactly what a plugin's entry point will declare;
    # keep the built-ins on the same contract.
    for mount in all_areas():
        module, sep, attr = mount.target.partition(":")
        assert sep == ":" and module and attr
