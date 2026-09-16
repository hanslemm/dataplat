from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from dataplat.core.errors import ValidationError
from dataplat.main import app

runner = CliRunner()


def test_dbt_area_is_mounted() -> None:
    result = runner.invoke(app, ["dbt", "--help"])
    assert result.exit_code == 0
    assert "dbt project" in result.output.lower()


def test_project_fans_out_over_its_declared_targets() -> None:
    from dataplat.cli.dbt._common import projects_and_targets

    pairs = projects_and_targets("demo_project", None)
    assert [p.name for p, _ in pairs] == ["demo_project"]
    assert [t.name for t in pairs[0][1]] == ["demo_pg", "demo_rs"]


def test_target_narrows_the_fan_out() -> None:
    from dataplat.cli.dbt._common import projects_and_targets

    pairs = projects_and_targets("demo_project", "demo_rs")
    assert [t.name for t in pairs[0][1]] == ["demo_rs"]


def test_target_outside_the_project_is_rejected() -> None:
    from dataplat.cli.dbt._common import projects_and_targets

    with pytest.raises(ValidationError, match="does not build into"):
        projects_and_targets("demo_other", "demo_pg")


def test_project_with_no_declared_targets_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataplat.cli.dbt._common import projects_and_targets

    monkeypatch.delenv("DEMO_OTHER_DBT_TARGETS", raising=False)
    with pytest.raises(ValidationError, match="DEMO_OTHER_DBT_TARGETS"):
        projects_and_targets("demo_other", None)


def test_doctor_flags_legacy_vars_without_projects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`dp dbt` needs named projects; doctor should nudge, not fail, when only
    the legacy single-project variables are set. This is rendered as a warning
    ``CheckResult`` from ``_offline_checks`` (see dataplat/cli/config.py) so it
    is tallied like every other check rather than printed out of band."""
    from tests.cli.test_config import (
        _configure_everything,
        _isolate_config_link,
        _pin_envrc,
    )

    _isolate_config_link(monkeypatch, tmp_path)
    envrc = tmp_path / ".envrc"
    envrc.write_text("export A=1")
    _pin_envrc(monkeypatch, envrc)
    _configure_everything(monkeypatch)
    monkeypatch.delenv("DP_DBT_PROJECTS", raising=False)
    monkeypatch.setenv("DP_DBT_PROJECT", "legacy")

    result = runner.invoke(app, ["config", "doctor"])

    # warn must never flip doctor's exit code -- that is a documented contract.
    assert result.exit_code == 0, result.output
    assert "All checks passed" in result.output
    assert "1 warning(s)" in result.output
    assert "DP_DBT_PROJECTS" in result.output
