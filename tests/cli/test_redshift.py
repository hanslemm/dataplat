from __future__ import annotations

from typer.testing import CliRunner

from dataplat.cli.cloud.aws.redshift import app as redshift_app

runner = CliRunner()


def test_long_queries_alias_removed() -> None:
    result = runner.invoke(redshift_app, ["long-queries"])
    assert result.exit_code == 2  # no such command


def test_metrics_command_exists() -> None:
    result = runner.invoke(redshift_app, ["metrics", "--help"])
    assert result.exit_code == 0
