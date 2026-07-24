from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from dataplat.cli import config as config_cli

runner = CliRunner()


def _isolate_config_link(monkeypatch, tmp_path: Path) -> Path:
    link = tmp_path / "config" / ".envrc"
    monkeypatch.setattr(config_cli, "CONFIG_ENVRC", link)
    from dataplat.core import envrc as envrc_module

    monkeypatch.setattr(envrc_module, "CONFIG_ENVRC", link)
    monkeypatch.setattr(envrc_module, "CONFIG_DIR", link.parent)
    return link


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

    result = runner.invoke(
        config_cli.app, ["init", "--envrc", str(tmp_path / "nope")]
    )

    assert result.exit_code == 1
    assert "not found" in result.output


def test_show_reports_missing_and_set_vars(monkeypatch, tmp_path: Path) -> None:
    _isolate_config_link(monkeypatch, tmp_path)
    monkeypatch.setenv("DEMO_PG_HOST", "db.example.com")
    monkeypatch.setenv("DEMO_PG_PASSWORD", "supersecret")
    monkeypatch.delenv("DEMO_RS_HOST", raising=False)

    result = runner.invoke(config_cli.app, ["show"])

    assert result.exit_code == 0, result.output
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
    monkeypatch.setattr(config_cli, "find_envrc", lambda: None)

    result = runner.invoke(config_cli.app, ["doctor"])

    assert result.exit_code == 1
    assert "check(s) failed" in result.output
    assert "target demo_pg" in result.output


def test_doctor_offline_passes_when_configured(monkeypatch, tmp_path: Path) -> None:
    _isolate_config_link(monkeypatch, tmp_path)
    envrc = tmp_path / ".envrc"
    envrc.write_text("export A=1")
    monkeypatch.setattr(config_cli, "find_envrc", lambda: envrc)
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

    result = runner.invoke(config_cli.app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "All checks passed" in result.output
