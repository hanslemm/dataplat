from __future__ import annotations

import pytest
import typer
from typer.testing import CliRunner

from dataplat.cli import _missing

runner = CliRunner()


def _stub() -> typer.Typer:
    return _missing.build_missing_deps_app("db", "Database query commands")


def test_stub_help_mentions_extra() -> None:
    result = runner.invoke(_stub(), ["--help"])
    assert result.exit_code == 0
    assert "needs extra: db" in result.output


def test_stub_declines_and_exits_when_install_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_missing, "missing_modules", lambda area: ["psycopg"])
    monkeypatch.setattr(_missing, "run_install", lambda extras, yes: False)

    result = runner.invoke(_stub(), ["query", "SELECT 1", "--format", "json"])

    assert result.exit_code == 1
    assert "psycopg" in result.output
    assert "dataplat[db]" in result.output


def test_stub_reexecs_after_successful_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed: list[list[str]] = []
    reexeced: list[bool] = []

    monkeypatch.setattr(_missing, "missing_modules", lambda area: ["psycopg"])
    monkeypatch.setattr(
        _missing,
        "run_install",
        lambda extras, yes: installed.append(extras) or True,
    )

    def fake_reexec():
        reexeced.append(True)
        raise typer.Exit(code=0)

    monkeypatch.setattr(_missing, "reexec", fake_reexec)

    result = runner.invoke(_stub(), ["query", "SELECT 1"])

    assert result.exit_code == 0
    assert installed == [["db"]]
    assert reexeced == [True]


def test_run_install_prints_hint_when_no_installer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(_missing, "install_command", lambda extras: None)
    assert _missing.run_install(["db"], yes=True) is False


def test_run_install_declines_when_not_a_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _missing, "install_command", lambda extras: ["uv", "tool", "install", "x"]
    )
    monkeypatch.setattr(_missing.sys.stdin, "isatty", lambda: False)
    ran: list[list[str]] = []
    monkeypatch.setattr(_missing.subprocess, "run", lambda cmd: ran.append(cmd))

    assert _missing.run_install(["db"], yes=False) is False
    assert ran == []  # never installs silently


def test_run_install_runs_command_with_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Proc:
        returncode = 0

    ran: list[list[str]] = []
    monkeypatch.setattr(
        _missing, "install_command", lambda extras: ["uv", "tool", "install", "x"]
    )
    monkeypatch.setattr(
        _missing.subprocess, "run", lambda cmd: ran.append(cmd) or _Proc()
    )

    assert _missing.run_install(["db"], yes=True) is True
    assert ran == [["uv", "tool", "install", "x"]]


def test_run_install_reports_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Proc:
        returncode = 3

    monkeypatch.setattr(
        _missing, "install_command", lambda extras: ["uv", "tool", "install", "x"]
    )
    monkeypatch.setattr(_missing.subprocess, "run", lambda cmd: _Proc())

    assert _missing.run_install(["db"], yes=True) is False
