from __future__ import annotations

import pytest
import typer

from dataplat.cli.db._common import ConnCliParams, resolve_params_or_exit
from dataplat.services.db.connection import SqlEngine


def _set_target_env(monkeypatch, prefix: str) -> None:
    monkeypatch.setenv(f"{prefix}_HOST", "db.example.com")
    monkeypatch.setenv(f"{prefix}_USER", "alice")
    monkeypatch.setenv(f"{prefix}_DATABASE", "warehouse")


def test_target_sets_prefix_and_engine(monkeypatch) -> None:
    _set_target_env(monkeypatch, "DEMO_RS")

    params = ConnCliParams(target="demo_rs").resolve()

    assert params.engine == SqlEngine.redshift
    assert params.host == "db.example.com"
    assert params.port == 5439


def test_flags_override_target(monkeypatch) -> None:
    _set_target_env(monkeypatch, "DEMO_RS")

    params = ConnCliParams(
        target="demo_rs", engine=SqlEngine.postgresql, host="other"
    ).resolve()

    assert params.engine == SqlEngine.postgresql
    assert params.host == "other"


def test_default_prefix_is_demo_pg(monkeypatch) -> None:
    _set_target_env(monkeypatch, "DEMO_PG")

    params = ConnCliParams().resolve()

    assert params.host == "db.example.com"
    assert params.engine == SqlEngine.postgresql


def test_explicit_env_prefix_wins_over_target(monkeypatch) -> None:
    _set_target_env(monkeypatch, "CUSTOM")

    params = ConnCliParams(target="demo_rs", env_prefix="CUSTOM").resolve()

    assert params.host == "db.example.com"
    # engine still comes from the target when not otherwise specified
    assert params.engine == SqlEngine.redshift


def test_resolve_params_or_exit_unknown_target(monkeypatch, capsys) -> None:
    with pytest.raises(typer.Exit):
        resolve_params_or_exit(ConnCliParams(target="nope"))

    assert "Unknown target" in capsys.readouterr().out


def test_resolve_params_or_exit_missing_settings(monkeypatch, capsys) -> None:
    for var in ("DEMO_PG_HOST", "DEMO_PG_USER", "DEMO_PG_DATABASE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("PGHOST", raising=False)
    monkeypatch.delenv("PGUSER", raising=False)
    monkeypatch.delenv("PGDATABASE", raising=False)
    monkeypatch.delenv("DB_HOST", raising=False)
    monkeypatch.delenv("DB_USER", raising=False)
    monkeypatch.delenv("DB_NAME", raising=False)

    with pytest.raises(typer.Exit):
        resolve_params_or_exit(ConnCliParams())

    assert "Missing required connection settings" in capsys.readouterr().out
