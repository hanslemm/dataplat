from __future__ import annotations

from typer.testing import CliRunner

import dataplat.main as main_module
from dataplat.cli import open as open_cli

runner = CliRunner()


def _disable_envrc(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "load_envrc", lambda: None)


def test_open_airbyte_strips_api_suffix(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    monkeypatch.setenv("AIRBYTE_BASE_URL", "https://airbyte.example.com/api/public/v1")
    opened: list[str] = []
    monkeypatch.setattr(open_cli.webbrowser, "open", lambda url: opened.append(url))

    result = runner.invoke(main_module.app, ["open", "airbyte"])

    assert result.exit_code == 0, result.output
    assert opened == ["https://airbyte.example.com"]


def test_open_superset_missing_env(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    monkeypatch.delenv("SUPERSET_BASE_URL", raising=False)

    result = runner.invoke(main_module.app, ["open", "superset"])

    assert result.exit_code == 1
    assert "SUPERSET_BASE_URL" in result.output


def test_open_rds_print_only(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    monkeypatch.setenv("DP_RDS_INSTANCE", "prod-db-1")
    opened: list[str] = []
    monkeypatch.setattr(open_cli.webbrowser, "open", lambda url: opened.append(url))

    result = runner.invoke(main_module.app, ["open", "rds", "--print-only"])

    assert result.exit_code == 0, result.output
    assert opened == []  # --print-only never launches a browser
    assert "console.aws.amazon.com/rds" in result.output
    assert "prod-db-1" in result.output


def test_open_secrets_specific_name(monkeypatch) -> None:
    _disable_envrc(monkeypatch)
    opened: list[str] = []
    monkeypatch.setattr(open_cli.webbrowser, "open", lambda url: opened.append(url))

    result = runner.invoke(
        main_module.app, ["open", "secrets", "my/secret", "--print-only"]
    )

    assert result.exit_code == 0, result.output
    assert "name=my%2Fsecret" in result.output
