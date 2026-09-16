"""`dp db dbt-orphans` still works but signals it moved to `dp dbt orphans`."""

from __future__ import annotations

from typer.testing import CliRunner

from dataplat.main import app

runner = CliRunner()


def test_old_path_still_resolves() -> None:
    result = runner.invoke(app, ["db", "dbt-orphans", "--help"])
    assert result.exit_code == 0


def test_old_path_warns_about_the_move() -> None:
    result = runner.invoke(app, ["db", "dbt-orphans", "--help"])
    assert "dp dbt orphans" in result.output


def test_old_path_own_help_is_marked_deprecated() -> None:
    # Someone reaching for the old path runs its own --help, not the parent's.
    # Matched against the "(deprecated)" tag Click/Typer render, not the bare
    # word: purge's own help text legitimately mentions the "_deprecated"
    # suffix orphans get renamed to, which must not trip this assertion.
    result = runner.invoke(app, ["db", "dbt-orphans", "--help"])
    assert "(deprecated)" in result.output.lower()


def test_db_group_help_lists_the_alias_as_deprecated() -> None:
    # `dp db --help` should signal the move in the subcommand listing too.
    result = runner.invoke(app, ["db", "--help"])
    assert "dp dbt orphans" in result.output
    assert "(deprecated)" in result.output.lower()


def test_new_path_help_does_not_claim_deprecation() -> None:
    # The sub-app is shared between both mounts (`dp db dbt-orphans` and
    # `dp dbt orphans`) — only the old mount may be marked deprecated. Checked
    # against the "(deprecated)" tag specifically, not the bare word: the
    # command's own help legitimately talks about renaming objects with a
    # "_deprecated" suffix, which must not trip this assertion.
    result = runner.invoke(app, ["dbt", "orphans", "--help"])
    assert result.exit_code == 0
    assert "(deprecated)" not in result.output.lower()
