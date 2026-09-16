from __future__ import annotations

import pytest
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


def test_orphans_refuses_projects_that_share_a_target() -> None:
    """demo_project and demo_other both declare demo_rs (see
    tests/conftest.py). Each project's live dbt model set is scoped to
    itself, so scanning the same physical target once per project would see
    the other project's live tables as its own orphans -- proposing (and,
    without --dry-run, renaming) one project's production tables as
    another's. Refuse the fan-out outright rather than attempt it.
    """
    result = runner.invoke(app, ["dbt", "orphans", "-p", "all"])
    assert result.exit_code != 0
    assert "demo_rs" in result.output
    assert "demo_project" in result.output
    assert "demo_other" in result.output


def test_orphans_refuses_differently_named_targets_on_one_warehouse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The name-based overlap check above only catches two projects
    declaring the *same* target name. It cannot catch two projects that each
    declare their own, differently-named target pointing at the same
    physical warehouse -- separate credentials per project sharing one
    cluster is a normal setup, not a misconfiguration, so the name check
    alone lets it straight through. Caught instead by comparing connection
    identity (host, port, database name) -- see _connection_identity.
    """
    for prefix in ("DEMO_PG", "DEMO_PG2"):
        monkeypatch.setenv(f"{prefix}_HOST", "shared.example.invalid")
        monkeypatch.setenv(f"{prefix}_PORT", "5432")
        monkeypatch.setenv(f"{prefix}_USER", "svc")
        monkeypatch.setenv(f"{prefix}_PASSWORD", "x")
        monkeypatch.setenv(f"{prefix}_DATABASE", "analytics")
    monkeypatch.setenv("DEMO_PROJECT_DBT_TARGETS", "demo_pg")
    monkeypatch.setenv("DEMO_OTHER_DBT_TARGETS", "demo_pg2")

    result = runner.invoke(app, ["dbt", "orphans", "-p", "all"])

    assert result.exit_code != 0
    assert "demo_pg" in result.output
    assert "demo_pg2" in result.output
    assert "demo_project" in result.output
    assert "demo_other" in result.output
