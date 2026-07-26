from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import typer
from typer.main import get_group
from typer.testing import CliRunner

import dataplat.main as main_module
from dataplat.cli._lazy import AreaPlaceholderGroup, LazyRootGroup, area_placeholder
from dataplat.core import registry
from dataplat.core.deps import AreaDeps
from dataplat.core.registry import AreaMount, all_areas

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[2]

# Optional dependencies of the areas: importing any of them to print a version
# number is the regression this file guards.
HEAVY_MODULES = ("psycopg", "textual", "httpx", "boto3", "plotext", "croniter")

# A plugin-supplied area, absent from both AREAS and the built-in registry, and
# with an app whose own name differs from the name it is mounted under.
third_party_app = typer.Typer(name="not-the-mount-name", help="Widget tools")


@third_party_app.command("ping")
def _third_party_ping() -> None:
    print("widget pong")


THIRD_PARTY = AreaMount(
    name="widget",
    help_text="Widget tools",
    target="tests.cli.test_cli_smoke:third_party_app",
    deps=AreaDeps(
        area="widget",
        extra="widget",
        modules=("json",),  # importable, so the area itself mounts
        enabled_by=("WIDGET_URL",),
    ),
)


def _run_dp(*argv: str) -> tuple[str, list[str]]:
    """Run ``dp argv`` in a fresh interpreter; return its output and the heavy
    modules it left in ``sys.modules``.

    A subprocess is the only honest measurement here: by the time pytest runs,
    this process has already imported every area itself.
    """
    code = textwrap.dedent(f"""
        import json, sys
        sys.argv = ["dp", *{list(argv)!r}]
        import dataplat.main
        if sys.argv[1:]:
            try:
                dataplat.main.app()
            except SystemExit:
                pass
        loaded = [m for m in {list(HEAVY_MODULES)!r} if m in sys.modules]
        sys.stderr.write(json.dumps(loaded))
    """)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={**os.environ, "COLUMNS": "200"},
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout, json.loads(proc.stderr)


def _root_group() -> LazyRootGroup:
    group = get_group(main_module.app)
    assert isinstance(group, LazyRootGroup)
    return group


# --- lazy mounting -----------------------------------------------------------


def test_importing_main_imports_no_optional_dependency() -> None:
    _, loaded = _run_dp()
    assert loaded == []


def test_version_run_imports_no_optional_dependency() -> None:
    out, loaded = _run_dp("--version")
    assert out.startswith("dp ")
    assert loaded == []


def test_root_help_is_served_without_importing_any_area() -> None:
    # The registry supplies every area's name and help text, so listing the
    # commands must not cost an import.
    out, loaded = _run_dp("--help")
    for mount in all_areas():
        assert mount.name in out
        assert mount.help_text in out
    assert loaded == []


def test_status_run_imports_no_area_dependency() -> None:
    _, loaded = _run_dp("status", "--help")
    assert loaded == []


def test_dispatching_an_area_imports_exactly_that_area() -> None:
    # The mirror image of the tests above: the lazy mount must really load the
    # area, and only the one being run.
    _, loaded = _run_dp("db", "query", "--help")
    assert "psycopg" in loaded


def _complete(args: list[str]) -> tuple[int, list[str]]:
    """Run shell completion for ``dp <args> <TAB>`` in a fresh interpreter.

    Returns the number of completions offered and the heavy modules left
    imported. Completion goes through click's own walker, which is the only
    faithful way to exercise the resilient-parsing path.
    """
    code = textwrap.dedent(f"""
        import json, sys
        import typer
        from click.shell_completion import ShellComplete
        from dataplat.main import app
        group = typer.main.get_group(app)
        shell = ShellComplete(group, {{}}, "dp", "_DP_COMPLETE")
        offered = list(shell.get_completions({args!r}, ""))
        loaded = [m for m in {list(HEAVY_MODULES)!r} if m in sys.modules]
        sys.stderr.write(json.dumps([len(offered), loaded]))
    """)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={**os.environ, "COLUMNS": "200"},
    )
    assert proc.returncode == 0, proc.stderr
    count, loaded = json.loads(proc.stderr)
    return count, loaded


def test_top_level_completion_imports_no_area() -> None:
    """`dp <TAB>` is answered from the placeholders alone."""
    count, loaded = _complete([])
    assert count >= len(all_areas())
    assert loaded == []


def test_completing_inside_an_area_imports_only_that_area() -> None:
    """`dp db <TAB>` must import db — and that is not a leak.

    The completions owed to the shell are the area's own subcommands, so there
    is nothing to offer without importing it. Pinned here so that nobody
    "optimizes" the resilient-parsing path and silently leaves completion
    inside every area returning nothing.
    """
    count, loaded = _complete(["db"])
    assert count > 0
    assert "psycopg" in loaded
    assert "boto3" not in loaded
    assert "textual" not in loaded and "boto3" not in loaded


def test_areas_mount_as_placeholders_in_registry_order() -> None:
    group = _root_group()
    with group.make_context("dp", [], resilient_parsing=True) as ctx:
        assert group.list_commands(ctx) == [
            "config",
            "db",
            "ingest",
            "bi",
            "cloud",
            "ci",
            "status",
            "open",
        ]
    for mount in all_areas():
        assert isinstance(group.commands[mount.name], AreaPlaceholderGroup)


def test_resolving_an_area_caches_the_real_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads: list[str] = []
    real_load_app = registry.load_app
    monkeypatch.setattr(
        "dataplat.cli._lazy.load_app",
        lambda mount: loads.append(mount.name) or real_load_app(mount),
    )
    group = _root_group()

    with group.make_context("dp", [], resilient_parsing=True) as ctx:
        first = group.resolve_command(ctx, ["db", "query"])[1]
        second = group.resolve_command(ctx, ["db", "query"])[1]

    assert loads == ["db"]  # one process, one import
    assert first is second
    assert group.commands["db"] is first


def test_unknown_command_still_suggests_a_lazy_area() -> None:
    result = runner.invoke(main_module.app, ["dbb"])

    assert result.exit_code == 2
    assert "Did you mean 'db'?" in result.output


def test_a_third_party_area_mounts_under_its_registry_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The registry — not the AREAS global and not the area's own app name — is
    # what decides how an area is mounted and whether its deps are satisfied.
    monkeypatch.setattr(registry, "BUILTIN_AREAS", (THIRD_PARTY,))
    app = typer.Typer(name="dp", cls=LazyRootGroup, no_args_is_help=True)
    app.add_typer(area_placeholder(THIRD_PARTY), name=THIRD_PARTY.name)

    listing = runner.invoke(app, ["--help"])
    assert listing.exit_code == 0
    assert "widget" in listing.output
    assert "Widget tools" in listing.output

    # The mount name wins over the area app's own, the way add_typer's name=
    # used to: nothing downstream should ever see "not-the-mount-name".
    group = get_group(app)
    with group.make_context("dp", [], resilient_parsing=True) as ctx:
        resolved = group.resolve_command(ctx, ["widget", "ping"])[1]
    assert resolved is not None and resolved.name == "widget"

    result = runner.invoke(app, ["widget", "ping"])
    assert result.exit_code == 0, result.output
    assert "widget pong" in result.output


def test_bootstrap_no_longer_reloads_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Module scope loads .envrc before any area import; the callback repeating
    # it was a no-op, since loading is setdefault-based.
    calls: list[int] = []
    monkeypatch.setattr(main_module, "load_envrc", lambda: calls.append(1))

    result = runner.invoke(main_module.app, ["status", "--help"])

    assert result.exit_code == 0
    assert calls == []


# --- CLI surface -------------------------------------------------------------


def test_version_flag() -> None:
    result = runner.invoke(main_module.app, ["--version"])

    assert result.exit_code == 0
    assert "dp" in result.stdout
    assert any(ch.isdigit() for ch in result.stdout)


def test_new_command_groups_present() -> None:
    result = runner.invoke(main_module.app, ["--help"])

    assert result.exit_code == 0
    assert "config" in result.stdout
    assert "db" in result.stdout
    assert "cloud" in result.stdout
    assert "ingest" in result.stdout
    assert "bi" in result.stdout
    assert "ci" in result.stdout


def test_every_area_dispatches_to_its_real_app() -> None:
    for mount in all_areas():
        result = runner.invoke(main_module.app, [mount.name, "--help"])
        assert result.exit_code == 0, mount.name
        assert f"dp {mount.name}" in result.stdout


def test_old_sql_command_removed() -> None:
    result = runner.invoke(main_module.app, ["sql", "--help"])

    assert result.exit_code != 0


def test_new_db_query_command_exists() -> None:
    result = runner.invoke(main_module.app, ["db", "query", "--help"])

    assert result.exit_code == 0


def test_airbyte_subcommand_groups_present() -> None:
    result = runner.invoke(main_module.app, ["ingest", "airbyte", "--help"])
    assert result.exit_code == 0
    for group in (
        "connections",
        "sources",
        "destinations",
        "definitions",
        "workspaces",
        "tags",
        "templates",
    ):
        assert group in result.stdout


def test_airbyte_sources_commands_present() -> None:
    result = runner.invoke(main_module.app, ["ingest", "airbyte", "sources", "--help"])
    assert result.exit_code == 0
    for cmd in ("list", "get", "create", "update", "delete"):
        assert cmd in result.stdout


def test_airbyte_destinations_commands_present() -> None:
    result = runner.invoke(
        main_module.app, ["ingest", "airbyte", "destinations", "--help"]
    )
    assert result.exit_code == 0
    for cmd in ("list", "get", "create", "update", "delete"):
        assert cmd in result.stdout


def test_airbyte_connections_commands_present() -> None:
    result = runner.invoke(
        main_module.app, ["ingest", "airbyte", "connections", "--help"]
    )
    assert result.exit_code == 0
    for cmd in ("list", "update", "sync", "get", "create", "delete"):
        assert cmd in result.stdout


def test_db_dbt_orphans_group_present() -> None:
    result = runner.invoke(main_module.app, ["db", "dbt-orphans", "--help"])

    assert result.exit_code == 0
    assert "revert" in result.stdout


def test_db_dbt_orphans_root_help() -> None:
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


def test_db_dbt_orphans_revert_help() -> None:
    result = runner.invoke(main_module.app, ["db", "dbt-orphans", "revert", "--help"])

    assert result.exit_code == 0
    for flag in ("--log", "--dry-run", "--no-dry-run", "--target"):
        assert flag in result.stdout


def test_db_dbt_orphans_rejects_multi_dot_exclusion() -> None:
    result = runner.invoke(
        main_module.app,
        ["db", "dbt-orphans", "--exclude", "a.b.c"],
    )

    assert result.exit_code == 1
    assert "Invalid exclusion" in result.stdout


def test_db_dbt_orphans_revert_missing_log(tmp_path) -> None:
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


def test_db_dbt_orphans_revert_empty_log(tmp_path) -> None:
    log_path = tmp_path / "log.json"
    log_path.write_text('{"renames": [], "dry_run": false}')

    result = runner.invoke(
        main_module.app,
        ["db", "dbt-orphans", "revert", "--log", str(log_path)],
    )

    assert result.exit_code == 0
    assert "nothing to revert" in result.stdout


def test_db_dbt_orphans_purge_help() -> None:
    result = runner.invoke(main_module.app, ["db", "dbt-orphans", "purge", "--help"])

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


def test_db_dbt_orphans_purge_rejects_multi_dot_exclusion() -> None:
    result = runner.invoke(
        main_module.app,
        ["db", "dbt-orphans", "purge", "--exclude", "a.b.c"],
    )

    assert result.exit_code == 1
    assert "Invalid exclusion" in result.stdout


def _isolate_log_dir(monkeypatch, tmp_path) -> None:
    from dataplat.cli.db import dbt_orphans as orphans_module

    monkeypatch.setattr(orphans_module, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(orphans_module, "LEGACY_LOG_DIR", tmp_path / "local")


def test_db_dbt_orphans_revert_no_log_found(tmp_path, monkeypatch) -> None:
    """When no --log is passed and no timestamped log exists, revert errors."""
    _isolate_log_dir(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(main_module.app, ["db", "dbt-orphans", "revert"])

    assert result.exit_code == 1
    assert "no dbt_orphans log found" in result.stdout


def test_db_dbt_orphans_revert_auto_picks_latest_log(tmp_path, monkeypatch) -> None:
    """Revert without --log finds the newest timestamped log (legacy dir included)."""
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


def test_db_dbt_orphans_revert_corrupt_log(tmp_path) -> None:
    log_path = tmp_path / "log.json"
    log_path.write_text("{not json")

    result = runner.invoke(
        main_module.app,
        ["db", "dbt-orphans", "revert", "--log", str(log_path)],
    )

    assert result.exit_code == 1
    assert "could not read log" in result.stdout


def test_db_dbt_orphans_group_lists_purge() -> None:
    result = runner.invoke(main_module.app, ["db", "dbt-orphans", "--help"])

    assert result.exit_code == 0
    assert "purge" in result.stdout
