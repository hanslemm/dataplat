from __future__ import annotations

import pytest

from dataplat.core.errors import ConfigError, ValidationError
from dataplat.services.dbt.projects import (
    default_project_name,
    load_projects,
    resolve_project,
    resolve_projects,
)


def test_demo_project_prefix_and_path() -> None:
    project = resolve_project("demo_project")
    assert project.env_prefix == "DEMO_PROJECT"
    assert project.path.name == "demo_project"
    assert project.profiles_dir == project.path


def test_project_name_defaults_from_dbt_project_yml() -> None:
    assert resolve_project("demo_project").project_name == "demo_project_from_yml"


def test_explicit_name_env_var_wins() -> None:
    assert resolve_project("demo_other").project_name == "explicit_name"


def test_declared_targets() -> None:
    assert resolve_project("demo_project").target_names == ("demo_pg", "demo_rs")
    assert resolve_project("demo_other").target_names == ("demo_rs",)


def test_resolve_project_case_insensitive() -> None:
    assert resolve_project("Demo_Project") == load_projects()["demo_project"]


def test_resolve_project_unknown_raises() -> None:
    with pytest.raises(ValidationError, match="Unknown dbt project"):
        resolve_project("nope")


def test_resolve_projects_all() -> None:
    assert [p.name for p in resolve_projects("all")] == ["demo_project", "demo_other"]


def test_default_project_name_is_first_when_unset() -> None:
    assert default_project_name() == "demo_project"


def test_missing_path_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DP_DBT_PROJECTS", "broken")
    monkeypatch.delenv("BROKEN_DBT_PATH", raising=False)
    with pytest.raises(ConfigError, match="BROKEN_DBT_PATH"):
        resolve_project("broken")


def test_path_without_dbt_project_yml_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("DP_DBT_PROJECTS", "broken")
    monkeypatch.setenv("BROKEN_DBT_PATH", str(tmp_path))
    with pytest.raises(ConfigError, match="dbt_project.yml"):
        resolve_project("broken")


def test_all_is_reserved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DP_DBT_PROJECTS", "all")
    with pytest.raises(ConfigError, match="reserved"):
        load_projects()
