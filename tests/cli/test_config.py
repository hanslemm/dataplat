from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from dataplat.cli import config as config_cli
from dataplat.core.envrc import EnvrcLocation, EnvrcSource

runner = CliRunner()


def _isolate_config_link(monkeypatch, tmp_path: Path) -> Path:
    link = tmp_path / "config" / ".envrc"
    monkeypatch.setattr(config_cli, "CONFIG_ENVRC", link)
    from dataplat.core import envrc as envrc_module

    monkeypatch.setattr(envrc_module, "CONFIG_ENVRC", link)
    monkeypatch.setattr(envrc_module, "CONFIG_DIR", link.parent)
    return link


def _pin_envrc(
    monkeypatch,
    path: Path | None,
    source: EnvrcSource = EnvrcSource.global_link,
) -> None:
    """Force the lookup down one provenance branch.

    Without this the commands would consult the developer's real global link
    and working directory.
    """
    location = None if path is None else EnvrcLocation(path, source)
    monkeypatch.setattr(config_cli, "locate_envrc", lambda: location)


def _configure_everything(monkeypatch) -> None:
    """Set every var doctor's offline checks want, so it has zero failures."""
    for prefix in ("DEMO_PG", "DEMO_RS"):
        for spec in config_cli._target_specs(prefix):
            if "ENGINE" not in spec.name:  # keep the engines from conftest
                monkeypatch.setenv(spec.name, "x")
    for var in (
        "AIRBYTE_BASE_URL",
        "AIRBYTE_CLIENT_ID",
        "AIRBYTE_CLIENT_SECRET",
        "SUPERSET_BASE_URL",
        "SUPERSET_ADMIN_USERNAME",
        "SUPERSET_ADMIN_PASSWORD",
        "GHA_APP_ID",
        "GHA_APP_PRIVATE_KEY",
    ):
        monkeypatch.setenv(var, "x")
    monkeypatch.setattr(config_cli.shutil, "which", lambda _: "/usr/bin/docker")


def test_init_links_envrc(monkeypatch, tmp_path: Path) -> None:
    link = _isolate_config_link(monkeypatch, tmp_path)
    source = tmp_path / ".envrc"
    source.write_text("export A=1")

    result = runner.invoke(config_cli.app, ["init", "--envrc", str(source)])

    assert result.exit_code == 0, result.output
    assert link.is_symlink()
    assert link.resolve() == source.resolve()


def test_init_missing_file_errors(monkeypatch, tmp_path: Path) -> None:
    _isolate_config_link(monkeypatch, tmp_path)

    result = runner.invoke(config_cli.app, ["init", "--envrc", str(tmp_path / "nope")])

    assert result.exit_code == 1
    assert "not found" in result.output


def test_show_reports_missing_and_set_vars(monkeypatch, tmp_path: Path) -> None:
    _isolate_config_link(monkeypatch, tmp_path)
    _pin_envrc(monkeypatch, None)
    monkeypatch.setenv("DEMO_PG_HOST", "db.example.com")
    monkeypatch.setenv("DEMO_PG_PASSWORD", "supersecret")
    monkeypatch.delenv("DEMO_RS_HOST", raising=False)

    result = runner.invoke(config_cli.app, ["show"])

    assert result.exit_code == 0, result.output
    assert "none found" in result.output
    assert "db.example.com" in result.output
    assert "supersecret" not in result.output  # secrets never printed
    assert "unset" in result.output
    # target sections come from DP_TARGETS
    assert "target: demo_pg" in result.output
    assert "target: demo_rs" in result.output


def test_doctor_offline_reports_failures(monkeypatch, tmp_path: Path) -> None:
    _isolate_config_link(monkeypatch, tmp_path)
    # demo_pg is declared in DP_TARGETS but has no connection vars set.
    for spec in config_cli._target_specs("DEMO_PG"):
        monkeypatch.delenv(spec.name, raising=False)
    _pin_envrc(monkeypatch, None)

    result = runner.invoke(config_cli.app, ["doctor"])

    assert result.exit_code == 1
    assert "check(s) failed" in result.output
    assert "target demo_pg" in result.output


def test_doctor_offline_passes_when_configured(monkeypatch, tmp_path: Path) -> None:
    _isolate_config_link(monkeypatch, tmp_path)
    envrc = tmp_path / ".envrc"
    envrc.write_text("export A=1")
    _pin_envrc(monkeypatch, envrc)
    _configure_everything(monkeypatch)

    result = runner.invoke(config_cli.app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "All checks passed" in result.output
    assert "warning(s)" not in result.output


def test_sync_reports_ok_when_everything_installed(monkeypatch) -> None:
    from dataplat.core import deps

    monkeypatch.setattr(deps, "missing_modules", lambda area: [])

    result = runner.invoke(config_cli.app, ["sync"])

    assert result.exit_code == 0, result.output
    assert "Every enabled area has its dependencies installed." in result.output


def test_sync_check_fails_when_enabled_area_missing_deps(monkeypatch) -> None:
    from dataplat.core import deps

    # db is enabled via DP_TARGETS (conftest); pretend psycopg is absent.
    monkeypatch.setattr(
        deps,
        "missing_modules",
        lambda area: ["psycopg"] if area == "db" else [],
    )

    result = runner.invoke(config_cli.app, ["sync", "--check"])

    assert result.exit_code == 1
    assert "missing: psycopg" in result.output


def test_sync_installs_needed_extras(monkeypatch) -> None:
    from dataplat.cli import _missing
    from dataplat.core import deps

    monkeypatch.setenv("AIRBYTE_BASE_URL", "https://airbyte.example.com")
    monkeypatch.setattr(
        deps,
        "missing_modules",
        lambda area: {"db": ["psycopg"], "ingest": ["httpx"]}.get(area, []),
    )
    installed: list[tuple[list[str], bool]] = []

    def fake_install(extras: list[str], *, yes: bool) -> bool:
        installed.append((extras, yes))
        return True

    monkeypatch.setattr(_missing, "run_install", fake_install)

    result = runner.invoke(config_cli.app, ["sync", "--yes"])

    assert result.exit_code == 0, result.output
    assert installed == [(["db", "ingest"], True)]
    assert "Dependencies installed" in result.output


def test_show_names_active_envrc_and_provenance(monkeypatch, tmp_path: Path) -> None:
    _isolate_config_link(monkeypatch, tmp_path)
    active = tmp_path / "trusted" / ".envrc"
    _pin_envrc(monkeypatch, active, EnvrcSource.global_link)

    result = runner.invoke(config_cli.app, ["show"])

    assert result.exit_code == 0, result.output
    assert str(active) in result.output
    assert "(global link)" in result.output
    assert "current directory" not in result.output


def test_show_warns_when_envrc_came_from_cwd(monkeypatch, tmp_path: Path) -> None:
    _isolate_config_link(monkeypatch, tmp_path)
    active = tmp_path / "some-clone" / ".envrc"
    _pin_envrc(monkeypatch, active, EnvrcSource.cwd)

    result = runner.invoke(config_cli.app, ["show"])

    assert result.exit_code == 0, result.output
    assert str(active) in result.output
    assert "(current directory)" in result.output
    assert config_cli.CWD_ENVRC_DETAIL in result.output
    # Both escape hatches must stay visible in the hint.
    assert "dp config init" in result.output
    assert "DP_ENVRC_ALLOW_CWD=0" in result.output


def test_show_renders_markup_in_env_value_verbatim(monkeypatch, tmp_path: Path) -> None:
    _isolate_config_link(monkeypatch, tmp_path)
    _pin_envrc(monkeypatch, None)
    # `[/x]` used to raise MarkupError mid-table and kill the command;
    # `[bold]` used to be swallowed, misreporting the value.
    hostile = "h[/x]o[bold]st"
    monkeypatch.setenv("DEMO_PG_HOST", hostile)

    result = runner.invoke(config_cli.app, ["show"])

    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert hostile in result.output


def test_doctor_names_envrc_provenance(monkeypatch, tmp_path: Path) -> None:
    _isolate_config_link(monkeypatch, tmp_path)
    active = tmp_path / "trusted" / ".envrc"
    _pin_envrc(monkeypatch, active, EnvrcSource.override)
    _configure_everything(monkeypatch)

    result = runner.invoke(config_cli.app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert f"{active} (DP_ENVRC_PATH)" in result.output


def test_doctor_warns_about_cwd_envrc_but_still_exits_zero(
    monkeypatch, tmp_path: Path
) -> None:
    _isolate_config_link(monkeypatch, tmp_path)
    _pin_envrc(monkeypatch, tmp_path / "some-clone" / ".envrc", EnvrcSource.cwd)
    _configure_everything(monkeypatch)

    result = runner.invoke(config_cli.app, ["doctor"])

    # A pre-existing user must not start seeing exit 1 over an advisory.
    assert result.exit_code == 0, result.output
    assert "envrc trust" in result.output
    assert config_cli.CWD_ENVRC_DETAIL in result.output
    assert "DP_ENVRC_ALLOW_CWD=0" in result.output
    assert "All checks passed" in result.output
    assert "1 warning(s)" in result.output


def test_doctor_renders_markup_in_detail_verbatim(monkeypatch, tmp_path: Path) -> None:
    _isolate_config_link(monkeypatch, tmp_path)
    hostile = tmp_path / "re[/x]po[bold]" / ".envrc"
    _pin_envrc(monkeypatch, hostile, EnvrcSource.global_link)
    _configure_everything(monkeypatch)

    result = runner.invoke(config_cli.app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert str(hostile) in result.output


def _pin_envrc_file(monkeypatch, tmp_path: Path, content: str) -> Path:
    """Write a real .envrc and make it the active one for both commands."""
    envrc = tmp_path / "active" / ".envrc"
    envrc.parent.mkdir(parents=True, exist_ok=True)
    envrc.write_text(content)
    _pin_envrc(monkeypatch, envrc)
    return envrc


def test_doctor_warns_about_unexpanded_shell_vars_without_failing(
    monkeypatch, tmp_path: Path
) -> None:
    """`export PGHOST=$DB_HOST` loads the literal, and the connection error that
    follows looks like a bad host or password. Doctor names it instead."""
    _isolate_config_link(monkeypatch, tmp_path)
    _pin_envrc_file(
        monkeypatch,
        tmp_path,
        'export DEMO_PG_HOST=$DB_HOST\nexport DEMO_PG_PASSWORD="p$ssw0rd"\n',
    )
    _configure_everything(monkeypatch)
    monkeypatch.setenv("DEMO_PG_HOST", "$DB_HOST")
    monkeypatch.setenv("DEMO_PG_PASSWORD", "p$ssw0rd")

    result = runner.invoke(config_cli.app, ["doctor"])

    # A warning must never flip a setup that used to exit 0.
    assert result.exit_code == 0, result.output
    assert "shell expansion" in result.output
    assert "DEMO_PG_HOST" in result.output
    assert "DEMO_PG_PASSWORD" in result.output
    assert config_cli.UNEXPANDED_HINT.split(".")[0] in result.output
    assert "All checks passed" in result.output
    assert "1 warning(s)" in result.output
    # The referenced name is extracted from the value, so it is never printed:
    # here it is a fragment of the password.
    assert "ssw0rd" not in result.output
    assert "DB_HOST" not in result.output.replace("DEMO_PG_HOST", "")


def test_doctor_stays_quiet_when_nothing_needs_expanding(
    monkeypatch, tmp_path: Path
) -> None:
    """Including the single-quoted case the shell would not expand either."""
    _isolate_config_link(monkeypatch, tmp_path)
    _pin_envrc_file(monkeypatch, tmp_path, "export DEMO_PG_HOST='db$1.example.com'\n")
    _configure_everything(monkeypatch)
    monkeypatch.setenv("DEMO_PG_HOST", "db$1.example.com")

    result = runner.invoke(config_cli.app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "shell expansion" not in result.output
    assert "warning(s)" not in result.output


def test_show_marks_unexpanded_values_and_explains_the_marker(
    monkeypatch, tmp_path: Path
) -> None:
    _isolate_config_link(monkeypatch, tmp_path)
    _pin_envrc_file(
        monkeypatch,
        tmp_path,
        'export DEMO_PG_HOST=$DB_HOST\nexport DEMO_PG_PASSWORD="$DB_PASS"\n',
    )
    monkeypatch.setenv("DEMO_PG_HOST", "$DB_HOST")
    monkeypatch.setenv("DEMO_PG_PASSWORD", "$DB_PASS")

    result = runner.invoke(config_cli.app, ["show"])

    assert result.exit_code == 0, result.output
    # The literal is shown *and* flagged: on its own it looks like a working line.
    assert "$DB_HOST" in result.output
    assert config_cli.UNEXPANDED_MARK in result.output
    assert config_cli.UNEXPANDED_HINT.split(".")[0] in result.output
    # A secret keeps its value hidden but still gets the marker.
    assert "$DB_PASS" not in result.output
    assert result.output.count(config_cli.UNEXPANDED_MARK) == 3  # 2 rows + footnote


def test_show_does_not_mark_a_value_the_shell_already_supplied(
    monkeypatch, tmp_path: Path
) -> None:
    """setdefault-based loading means the file's line never took effect."""
    _isolate_config_link(monkeypatch, tmp_path)
    _pin_envrc_file(monkeypatch, tmp_path, "export DEMO_PG_HOST=$DB_HOST\n")
    monkeypatch.setenv("DEMO_PG_HOST", "db.example.com")

    result = runner.invoke(config_cli.app, ["show"])

    assert result.exit_code == 0, result.output
    assert config_cli.UNEXPANDED_MARK not in result.output


def test_show_reports_a_broken_dp_targets_instead_of_crashing(
    monkeypatch, tmp_path: Path
) -> None:
    """`dp config show` is what you run *because* the config is broken.

    A reserved target name used to end it in a ConfigError traceback, hiding the
    "active .envrc" header that had already printed.
    """
    _isolate_config_link(monkeypatch, tmp_path)
    active = tmp_path / "trusted" / ".envrc"
    _pin_envrc(monkeypatch, active)
    monkeypatch.setenv("DP_TARGETS", "all")

    result = runner.invoke(config_cli.app, ["show"])

    # A clean exit, not an escaped ConfigError: it used to reach the top level.
    assert isinstance(result.exception, SystemExit), result.exception
    # ExitCode.CONFIG, taken from the exception itself — not the old bare 1.
    assert result.exit_code == 3, result.output
    assert "reserved target name" in result.output
    assert str(active) in result.output  # the header survives


def test_render_checks_counts_failures_but_not_warnings(capsys) -> None:
    failures = config_cli._render_checks(
        [
            config_cli._check("fine", True, detail="all set"),
            config_cli._warn("advisory", "worth knowing", "Do this."),
            config_cli._check(
                "broken", False, detail="ERROR: [/x] [bold]", hint="Fix."
            ),
        ]
    )
    out = capsys.readouterr().out

    assert failures == 1
    assert "! advisory" in out
    assert "Do this." in out  # warnings show their hint too
    assert "ERROR: [/x] [bold]" in out  # driver text renders verbatim
