from __future__ import annotations

from typer.testing import CliRunner

import dataplat.main as main_module

runner = CliRunner()


def _disable_envrc(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "load_envrc", lambda: None)


def test_version_flag(monkeypatch) -> None:
    _disable_envrc(monkeypatch)

    result = runner.invoke(main_module.app, ["--version"])

    assert result.exit_code == 0
    assert "dp" in result.stdout
    assert any(ch.isdigit() for ch in result.stdout)


def test_new_command_groups_present(monkeypatch) -> None:
    _disable_envrc(monkeypatch)

    result = runner.invoke(main_module.app, ["--help"])

    assert result.exit_code == 0
    assert "config" in result.stdout
    assert "db" in result.stdout
    assert "cloud" in result.stdout
    assert "ingest" in result.stdout
    assert "bi" in result.stdout
    assert "ci" in result.stdout


def test_old_sql_command_removed(monkeypatch) -> None:
    _disable_envrc(monkeypatch)

    result = runner.invoke(main_module.app, ["sql", "--help"])

    assert result.exit_code != 0


def test_new_db_query_command_exists(monkeypatch) -> None:
    _disable_envrc(monkeypatch)

    result = runner.invoke(main_module.app, ["db", "query", "--help"])

    assert result.exit_code == 0


def test_airbyte_subcommand_groups_present(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    result = runner.invoke(main_module.app, ["ingest", "airbyte", "--help"])
    assert result.exit_code == 0
    for group in ("connections", "sources", "destinations", "definitions", "workspaces", "tags", "templates"):
        assert group in result.stdout


def test_airbyte_sources_commands_present(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    result = runner.invoke(main_module.app, ["ingest", "airbyte", "sources", "--help"])
    assert result.exit_code == 0
    for cmd in ("list", "get", "create", "update", "delete"):
        assert cmd in result.stdout


def test_airbyte_destinations_commands_present(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    result = runner.invoke(main_module.app, ["ingest", "airbyte", "destinations", "--help"])
    assert result.exit_code == 0
    for cmd in ("list", "get", "create", "update", "delete"):
        assert cmd in result.stdout


def test_airbyte_connections_commands_present(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    result = runner.invoke(main_module.app, ["ingest", "airbyte", "connections", "--help"])
    assert result.exit_code == 0
    for cmd in ("list", "update", "sync", "get", "create", "delete"):
        assert cmd in result.stdout


def test_db_dbt_orphans_group_present(monkeypatch) -> None:
    _disable_envrc(monkeypatch)

    result = runner.invoke(main_module.app, ["db", "dbt-orphans", "--help"])

    assert result.exit_code == 0
    assert "revert" in result.stdout


def test_db_dbt_orphans_root_help(monkeypatch) -> None:
    _disable_envrc(monkeypatch)

    result = runner.invoke(main_module.app, ["db", "dbt-orphans", "--help"])

    assert result.exit_code == 0
    for flag in (
        "--log",
        "--target",
        "--dry-run",
        "--no-dry-run",
        "--yes",
        "--exclude",
        "--exclude-file",
    ):
        assert flag in result.stdout


def test_db_dbt_orphans_revert_help(monkeypatch) -> None:
    _disable_envrc(monkeypatch)

    result = runner.invoke(
        main_module.app, ["db", "dbt-orphans", "revert", "--help"]
    )

    assert result.exit_code == 0
    for flag in ("--log", "--dry-run", "--no-dry-run", "--target"):
        assert flag in result.stdout


def test_db_dbt_orphans_rejects_multi_dot_exclusion(monkeypatch) -> None:
    _disable_envrc(monkeypatch)

    result = runner.invoke(
        main_module.app,
        ["db", "dbt-orphans", "--exclude", "a.b.c"],
    )

    assert result.exit_code == 1
    assert "Invalid exclusion" in result.stdout


def test_db_dbt_orphans_revert_missing_log(tmp_path, monkeypatch) -> None:
    _disable_envrc(monkeypatch)

    result = runner.invoke(
        main_module.app,
        [
            "db",
            "dbt-orphans",
            "revert",
            "--log",
            str(tmp_path / "missing.json"),
        ],
    )

    assert result.exit_code == 1
    assert "not found" in result.stdout


def test_db_dbt_orphans_revert_empty_log(tmp_path, monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    log_path = tmp_path / "log.json"
    log_path.write_text('{"renames": [], "dry_run": false}')

    result = runner.invoke(
        main_module.app,
        ["db", "dbt-orphans", "revert", "--log", str(log_path)],
    )

    assert result.exit_code == 0
    assert "nothing to revert" in result.stdout


def test_db_dbt_orphans_purge_help(monkeypatch) -> None:
    _disable_envrc(monkeypatch)

    result = runner.invoke(
        main_module.app, ["db", "dbt-orphans", "purge", "--help"]
    )

    assert result.exit_code == 0
    for flag in (
        "--log",
        "--target",
        "--dry-run",
        "--no-dry-run",
        "--yes",
        "--older-than",
        "--exclude",
        "--exclude-file",
    ):
        assert flag in result.stdout


def test_db_dbt_orphans_purge_rejects_multi_dot_exclusion(monkeypatch) -> None:
    _disable_envrc(monkeypatch)

    result = runner.invoke(
        main_module.app,
        ["db", "dbt-orphans", "purge", "--exclude", "a.b.c"],
    )

    assert result.exit_code == 1
    assert "Invalid exclusion" in result.stdout


def _isolate_log_dir(monkeypatch, tmp_path) -> None:
    from dataplat.cli.db import dbt_orphans as orphans_module

    monkeypatch.setattr(orphans_module, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(
        orphans_module, "LEGACY_LOG_DIR", tmp_path / "local"
    )


def test_db_dbt_orphans_revert_no_log_found(tmp_path, monkeypatch) -> None:
    """When no --log is passed and no timestamped log exists, revert errors."""
    _disable_envrc(monkeypatch)
    _isolate_log_dir(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(main_module.app, ["db", "dbt-orphans", "revert"])

    assert result.exit_code == 1
    assert "no dbt_orphans log found" in result.stdout


def test_db_dbt_orphans_revert_auto_picks_latest_log(
    tmp_path, monkeypatch
) -> None:
    """Revert without --log finds the newest timestamped log (legacy dir included)."""
    _disable_envrc(monkeypatch)
    _isolate_log_dir(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)

    local_dir = tmp_path / "local"
    local_dir.mkdir()
    (local_dir / "dbt_orphans-20260101T000000Z.log.json").write_text(
        '{"renames": [], "dry_run": false}'
    )
    (local_dir / "dbt_orphans-20260422T120000Z.log.json").write_text(
        '{"renames": [], "dry_run": false}'
    )

    result = runner.invoke(main_module.app, ["db", "dbt-orphans", "revert"])

    assert result.exit_code == 0
    assert "dbt_orphans-20260422T120000Z.log.json" in result.stdout
    assert "nothing to revert" in result.stdout


def test_db_dbt_orphans_revert_corrupt_log(tmp_path, monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    log_path = tmp_path / "log.json"
    log_path.write_text("{not json")

    result = runner.invoke(
        main_module.app,
        ["db", "dbt-orphans", "revert", "--log", str(log_path)],
    )

    assert result.exit_code == 1
    assert "could not read log" in result.stdout


def test_db_dbt_orphans_group_lists_purge(monkeypatch) -> None:
    _disable_envrc(monkeypatch)

    result = runner.invoke(main_module.app, ["db", "dbt-orphans", "--help"])

    assert result.exit_code == 0
    assert "purge" in result.stdout
