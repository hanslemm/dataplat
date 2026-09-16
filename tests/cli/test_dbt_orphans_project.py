from __future__ import annotations

from typer.testing import CliRunner

from dataplat.main import app

runner = CliRunner()


def test_orphans_is_mounted_under_dbt() -> None:
    result = runner.invoke(app, ["dbt", "orphans", "--help"])
    assert result.exit_code == 0
    assert "--project" in result.output


def test_orphans_rejects_a_target_outside_the_project() -> None:
    result = runner.invoke(app, ["dbt", "orphans", "-p", "demo_other", "-t", "demo_pg"])
    assert result.exit_code != 0
    assert "does not build into" in result.output


def test_orphans_defaults_to_dry_run() -> None:
    result = runner.invoke(app, ["dbt", "orphans", "--help"])
    assert "--no-dry-run" in result.output
