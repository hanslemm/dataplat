from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from dataplat.core.errors import ExitCode
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


def test_orphans_accepts_target_all_under_a_configured_project(tmp_path: Path) -> None:
    """``-t all`` predates named projects (the option used to default to it,
    with its own help text documenting that meaning) and must keep working
    once a project is configured, not just on the legacy no-project path --
    otherwise a documented invocation breaks during the exact migration
    ``dp dbt`` exists to sell.

    Both invocations here fail downstream (the tracked demo_project fixture
    has no compiled manifest -- see tests/cli/test_dbt_orphans_manifest.py),
    which is the point: comparing ``-t all`` against omitting ``-t``
    entirely proves ``all`` reached the exact same per-target work as the
    default, not merely that project/target *resolution* let it through.
    ``--log`` is pointed at tmp_path so a downstream failure's audit log
    does not land in the developer's real ``~/.config/dataplat``.
    """
    log_a = str(tmp_path / "a.json")
    log_b = str(tmp_path / "b.json")
    with_all = runner.invoke(
        app, ["dbt", "orphans", "-p", "demo_project", "-t", "all", "--log", log_a]
    )
    without_t = runner.invoke(
        app, ["dbt", "orphans", "-p", "demo_project", "--log", log_b]
    )
    assert "does not build into" not in with_all.output
    assert with_all.exit_code == without_t.exit_code == ExitCode.CONFIG


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
