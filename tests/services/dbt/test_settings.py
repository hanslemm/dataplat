from __future__ import annotations

import pytest

from dataplat.core.errors import ConfigError
from dataplat.services.dbt.projects import resolve_project
from dataplat.services.dbt.settings import (
    DEFAULT_EXCLUDED_SCHEMAS,
    excluded_schemas,
    invocation_command,
    node_prefix,
)


def test_node_prefix_from_project() -> None:
    project = resolve_project("demo_project")
    assert node_prefix(project) == "model.demo_project_from_yml."


def test_node_prefix_falls_back_to_legacy_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DP_DBT_PROJECT", "legacy")
    assert node_prefix(None) == "model.legacy."


def test_node_prefix_without_project_or_legacy_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DP_DBT_PROJECT", raising=False)
    with pytest.raises(ConfigError, match="DP_DBT_PROJECTS"):
        node_prefix(None)


def test_excluded_schemas_default() -> None:
    assert excluded_schemas(None) == DEFAULT_EXCLUDED_SCHEMAS


def test_excluded_schemas_per_project_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEMO_PROJECT_DBT_ORPHANS_EXCLUDE_SCHEMAS", "one, two")
    project = resolve_project("demo_project")
    assert excluded_schemas(project) == frozenset({"one", "two"})


def test_excluded_schemas_legacy_var_still_honoured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DP_DBT_ORPHANS_EXCLUDE_SCHEMAS", "legacy_only")
    assert excluded_schemas(None) == frozenset({"legacy_only"})


def test_invocation_command_per_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_PROJECT_DBT_INVOCATION_COMMAND", "build")
    assert invocation_command(resolve_project("demo_project")) == "build"


def test_invocation_command_defaults_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DP_DBT_INVOCATION_COMMAND", raising=False)
    assert invocation_command(None) is None


def test_excluded_schemas_legacy_var_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DP_DBT_ORPHANS_EXCLUDE_SCHEMAS", "")
    assert excluded_schemas(None) == frozenset()


def test_excluded_schemas_legacy_var_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DP_DBT_ORPHANS_EXCLUDE_SCHEMAS", raising=False)
    assert excluded_schemas(None) == DEFAULT_EXCLUDED_SCHEMAS


def test_excluded_schemas_per_project_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEMO_PROJECT_DBT_ORPHANS_EXCLUDE_SCHEMAS", "")
    project = resolve_project("demo_project")
    assert excluded_schemas(project) == frozenset()


def test_excluded_schemas_project_empty_wins_over_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DP_DBT_ORPHANS_EXCLUDE_SCHEMAS", "something")
    monkeypatch.setenv("DEMO_PROJECT_DBT_ORPHANS_EXCLUDE_SCHEMAS", "")
    project = resolve_project("demo_project")
    assert excluded_schemas(project) == frozenset()


def test_excluded_schemas_project_absent_falls_back_to_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = resolve_project("demo_project")
    monkeypatch.delenv("DEMO_PROJECT_DBT_ORPHANS_EXCLUDE_SCHEMAS", raising=False)
    monkeypatch.setenv("DP_DBT_ORPHANS_EXCLUDE_SCHEMAS", "from_legacy")
    assert excluded_schemas(project) == frozenset({"from_legacy"})


def test_invocation_command_project_absent_falls_back_to_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = resolve_project("demo_project")
    monkeypatch.delenv("DEMO_PROJECT_DBT_INVOCATION_COMMAND", raising=False)
    monkeypatch.setenv("DP_DBT_INVOCATION_COMMAND", "build")
    assert invocation_command(project) == "build"


def test_invocation_command_project_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Project variable explicitly empty should return None, not fall through to legacy
    monkeypatch.setenv("DP_DBT_INVOCATION_COMMAND", "legacy_value")
    monkeypatch.setenv("DEMO_PROJECT_DBT_INVOCATION_COMMAND", "")
    project = resolve_project("demo_project")
    assert invocation_command(project) is None
