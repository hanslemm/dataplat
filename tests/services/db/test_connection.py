from __future__ import annotations

import pytest

from dataplat.core.errors import ConfigError
from dataplat.services.db.connection import SqlEngine, resolve_connection_params


def test_resolve_connection_params_prefers_cli_values(monkeypatch) -> None:
    monkeypatch.setenv("DEMO_PG_HOST", "env-host")
    monkeypatch.setenv("PGUSER", "env-user")

    params = resolve_connection_params(
        engine=SqlEngine.postgresql,
        env_prefix="DEMO_PG",
        user="cli-user",
        password="cli-pass",
        host="cli-host",
        port=5555,
        database="cli-db",
        sslmode="require",
    )

    assert params.user == "cli-user"
    assert params.host == "cli-host"
    assert params.dbname == "cli-db"
    assert params.port == 5555
    assert params.password == "cli-pass"
    assert params.sslmode == "require"


def test_resolve_connection_params_redshift_defaults(monkeypatch) -> None:
    monkeypatch.setenv("DEMO_RS_USER", "u")
    monkeypatch.setenv("DEMO_RS_HOST", "h")
    monkeypatch.setenv("DEMO_RS_DATABASE", "d")

    params = resolve_connection_params(
        engine=SqlEngine.redshift,
        env_prefix="DEMO_RS",
        user=None,
        password=None,
        host=None,
        port=None,
        database=None,
        sslmode=None,
    )

    assert params.port == 5439
    assert params.client_encoding == "UTF8"


def test_resolve_connection_params_cli_engine_overrides_env(monkeypatch) -> None:
    """--engine from the CLI must win over <PREFIX>_ENGINE in the environment."""
    monkeypatch.setenv("DEMO_PG_ENGINE", "postgresql")
    monkeypatch.setenv("DEMO_PG_USER", "u")
    monkeypatch.setenv("DEMO_PG_HOST", "h")
    monkeypatch.setenv("DEMO_PG_DATABASE", "d")
    monkeypatch.delenv("DEMO_PG_PORT", raising=False)
    monkeypatch.delenv("PGPORT", raising=False)

    params = resolve_connection_params(
        engine=SqlEngine.redshift,  # explicit CLI flag
        env_prefix="DEMO_PG",
        user=None,
        password=None,
        host=None,
        port=None,
        database=None,
        sslmode=None,
    )
    assert params.engine == SqlEngine.redshift
    # Redshift defaults should kick in — proves the CLI flag actually took effect.
    assert params.port == 5439
    assert params.client_encoding == "UTF8"


def test_resolve_connection_params_env_engine_applies_when_cli_absent(
    monkeypatch,
) -> None:
    """When no CLI flag is passed (engine=None), env var fills in."""
    monkeypatch.setenv("DEMO_PG_ENGINE", "redshift")
    monkeypatch.setenv("DEMO_PG_USER", "u")
    monkeypatch.setenv("DEMO_PG_HOST", "h")
    monkeypatch.setenv("DEMO_PG_DATABASE", "d")
    monkeypatch.delenv("DEMO_PG_PORT", raising=False)
    monkeypatch.delenv("PGPORT", raising=False)

    params = resolve_connection_params(
        engine=None,
        env_prefix="DEMO_PG",
        user=None,
        password=None,
        host=None,
        port=None,
        database=None,
        sslmode=None,
    )
    assert params.engine == SqlEngine.redshift
    assert params.port == 5439


def test_resolve_connection_params_defaults_to_postgres_when_nothing_set(
    monkeypatch,
) -> None:
    monkeypatch.delenv("DEMO_PG_ENGINE", raising=False)
    monkeypatch.setenv("DEMO_PG_USER", "u")
    monkeypatch.setenv("DEMO_PG_HOST", "h")
    monkeypatch.setenv("DEMO_PG_DATABASE", "d")
    monkeypatch.delenv("DEMO_PG_PORT", raising=False)
    monkeypatch.delenv("PGPORT", raising=False)

    params = resolve_connection_params(
        engine=None,
        env_prefix="DEMO_PG",
        user=None,
        password=None,
        host=None,
        port=None,
        database=None,
        sslmode=None,
    )
    assert params.engine == SqlEngine.postgresql
    assert params.port == 5432


def test_resolve_connection_params_invalid_port_raises(monkeypatch) -> None:
    monkeypatch.setenv("DEMO_PG_PORT", "not-int")
    monkeypatch.setenv("DEMO_PG_USER", "u")
    monkeypatch.setenv("DEMO_PG_HOST", "h")
    monkeypatch.setenv("DEMO_PG_DATABASE", "d")

    with pytest.raises(ConfigError):
        resolve_connection_params(
            engine=SqlEngine.postgresql,
            env_prefix="DEMO_PG",
            user=None,
            password=None,
            host=None,
            port=None,
            database=None,
            sslmode=None,
        )
