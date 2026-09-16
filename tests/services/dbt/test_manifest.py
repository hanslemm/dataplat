from __future__ import annotations

import json
from pathlib import Path

import pytest

from dataplat.services.dbt.manifest import is_partition_of, relation_names
from dataplat.services.dbt.projects import DbtProject, resolve_project

# Every test builds its own project under tmp_path rather than pointing at
# tests/fixtures/demo_project, which is tracked in git: writing a
# target/manifest.json into a tracked fixture directory would leave residue
# behind after the test run. DP_DBT_PROJECTS and monkeypatch both reset
# after each test, so a throwaway project name is safe to reuse across tests.


def _project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str = "temp"
) -> DbtProject:
    """A throwaway DbtProject rooted at ``tmp_path``, registered for this test."""
    project_dir = tmp_path / name
    project_dir.mkdir()
    (project_dir / "dbt_project.yml").write_text(f"name: {name}\n")
    monkeypatch.setenv("DP_DBT_PROJECTS", name)
    monkeypatch.setenv(f"{name.upper()}_DBT_PATH", str(project_dir))
    return resolve_project(name)


def _write_manifest(project_path: Path, manifest: dict) -> None:
    target = project_path / "target"
    target.mkdir(exist_ok=True)
    (target / "manifest.json").write_text(json.dumps(manifest))


def _write_nodes(project_path: Path, nodes: dict) -> None:
    _write_manifest(project_path, {"nodes": nodes})


def test_alias_wins_over_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(tmp_path, monkeypatch)
    _write_nodes(
        project.path,
        {"model.demo.a": {"name": "a", "alias": "a_aliased", "resource_type": "model"}},
    )
    assert relation_names(project) == {"a_aliased"}


def test_identifier_wins_over_name_when_no_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, monkeypatch)
    _write_nodes(
        project.path,
        {
            "model.demo.b": {
                "name": "b",
                "identifier": "b_ident",
                "resource_type": "model",
            }
        },
    )
    assert relation_names(project) == {"b_ident"}


def test_name_used_when_neither_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, monkeypatch)
    _write_nodes(
        project.path, {"model.demo.c": {"name": "c", "resource_type": "model"}}
    )
    assert relation_names(project) == {"c"}


def test_alias_wins_over_identifier_when_both_are_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The precedence this command depends on is alias, THEN identifier, THEN
    # name -- and the brief's own tests only ever set one override at a time.
    # A model with both set is exactly how dbt behaves when `alias` is a
    # custom config and `identifier` still carries the resolved name, so this
    # is not a hypothetical: get the order backwards here and a real project
    # reports its own live tables as orphans.
    project = _project(tmp_path, monkeypatch)
    _write_nodes(
        project.path,
        {
            "model.demo.d": {
                "name": "d",
                "alias": "d_alias",
                "identifier": "d_identifier",
                "resource_type": "model",
            }
        },
    )
    assert relation_names(project) == {"d_alias"}


def test_missing_manifest_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, monkeypatch)
    with pytest.raises(FileNotFoundError, match="manifest.json"):
        relation_names(project)


def test_present_manifest_with_no_nodes_is_empty_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Distinguishes "never compiled" (raises) from "compiled, genuinely
    # produces nothing yet" (a legitimate empty result) -- a manifest that
    # exists and says nothing is a different fact than no manifest at all.
    project = _project(tmp_path, monkeypatch)
    _write_nodes(project.path, {})
    assert relation_names(project) == set()


def test_manifest_missing_nodes_key_entirely_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, monkeypatch)
    _write_manifest(project.path, {})
    assert relation_names(project) == set()


def test_malformed_json_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, monkeypatch)
    target = project.path / "target"
    target.mkdir()
    (target / "manifest.json").write_text("{not valid json")
    with pytest.raises(json.JSONDecodeError):
        relation_names(project)


def test_sources_are_never_included(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The single worst mistake this function can make: a source is a
    # pre-existing table dbt reads, not one it produces. If it ever leaked
    # into the produced set, the caller would conclude dbt makes things it
    # does not, which defeats the reason schema exclusion exists.
    project = _project(tmp_path, monkeypatch)
    _write_manifest(
        project.path,
        {
            "nodes": {
                "model.demo.e": {"name": "e", "resource_type": "model"},
            },
            "sources": {
                "source.demo.raw.orders": {
                    "name": "orders",
                    "identifier": "orders",
                    "resource_type": "source",
                },
            },
        },
    )
    assert relation_names(project) == {"e"}


def test_non_producing_resource_types_are_excluded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, monkeypatch)
    _write_nodes(
        project.path,
        {
            "model.demo.f": {"name": "f", "resource_type": "model"},
            "test.demo.not_null_f": {"name": "not_null_f", "resource_type": "test"},
            "analysis.demo.g": {"name": "g", "resource_type": "analysis"},
            "operation.demo.h": {"name": "h", "resource_type": "operation"},
        },
    )
    assert relation_names(project) == {"f"}


def test_node_missing_resource_type_is_excluded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, monkeypatch)
    _write_nodes(project.path, {"model.demo.i": {"name": "i"}})
    assert relation_names(project) == set()


def test_seeds_and_snapshots_are_producing_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, monkeypatch)
    _write_nodes(
        project.path,
        {
            "model.demo.j": {"name": "j", "resource_type": "model"},
            "seed.demo.k": {"name": "k", "resource_type": "seed"},
            "snapshot.demo.l": {"name": "l", "resource_type": "snapshot"},
        },
    )
    assert relation_names(project) == {"j", "k", "l"}


def test_names_are_lowercased(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(tmp_path, monkeypatch)
    _write_nodes(
        project.path,
        {"model.demo.m": {"name": "M", "alias": "M_Aliased", "resource_type": "model"}},
    )
    assert relation_names(project) == {"m_aliased"}


def test_two_nodes_producing_the_same_relation_name_collapse_to_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, monkeypatch)
    _write_nodes(
        project.path,
        {
            "model.demo.n": {"name": "n", "alias": "shared", "resource_type": "model"},
            "model.demo.o": {"name": "shared", "resource_type": "model"},
        },
    )
    assert relation_names(project) == {"shared"}


# =========================================================================
# is_partition_of
# =========================================================================


def test_partition_child_is_recognised() -> None:
    assert is_partition_of("fct_events_p_trello", {"fct_events"}) is True


def test_unrelated_table_is_not_a_partition() -> None:
    assert is_partition_of("fct_unrelated", {"fct_events"}) is False


def test_partition_without_a_live_parent_is_not_spared() -> None:
    assert is_partition_of("fct_gone_p_trello", {"fct_events"}) is False


def test_bare_marker_with_no_partition_key_is_not_a_partition() -> None:
    # A relation that is only the parent name plus the marker, with nothing
    # after it, has no partition key -- it is not a real partition name
    # under this (or the macro's) convention, so it is not spared.
    assert is_partition_of("fct_events_p_", {"fct_events"}) is False


def test_produced_name_that_itself_contains_the_marker_still_matches() -> None:
    # The defect a naive "split on the first/last _p_" implementation has:
    # splitting fct_events_p_class_p_west at either end never yields a head
    # equal to the produced name "fct_events_p_class", because that name
    # legitimately contains the marker itself. Checking every produced name
    # as a candidate prefix (what the SQL macro this replaces actually does)
    # gets this right regardless of where else "_p_" appears.
    assert is_partition_of("fct_events_p_class_p_west", {"fct_events_p_class"}) is True


def test_partition_match_is_case_insensitive_on_both_sides() -> None:
    assert is_partition_of("FCT_EVENTS_P_TRELLO", {"fct_events"}) is True
    assert is_partition_of("fct_events_p_trello", {"FCT_EVENTS"}) is True


def test_empty_produced_set_is_never_a_partition() -> None:
    assert is_partition_of("fct_events_p_trello", set()) is False
