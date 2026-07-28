from __future__ import annotations

import sys
from pathlib import Path

import pytest

from dataplat.core.errors import ConfigError, ExitCode, ValidationError
from dataplat.services.db.connection import (
    MEMORY_PATH,
    DbConnectionParams,
    DuckDbConnectionParams,
    SqlEngine,
    ensure_duckdb_database_exists,
    load_duckdb,
    resolve_connection_params,
    resolve_engine_params,
)


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


# =========================================================================
# DuckDB: a third engine with a different *shape*, not a smaller PostgreSQL.
#
# It is a file opened inside this process, so it resolves to
# DuckDbConnectionParams (path + read_only) and never to the libpq shape. The
# tests below are the contract for that split: which settings feed the path,
# which settings are refused because they describe a server, and what a
# libpq-only code path does when it is handed a DuckDB target.
# =========================================================================


def _duckdb_env(monkeypatch, prefix: str = "DEMO_DDB") -> None:
    """A duckdb target with nothing but an engine set."""
    monkeypatch.setenv(f"{prefix}_ENGINE", "duckdb")
    for key in ("HOST", "PORT", "USER", "PASSWORD", "SSLMODE", "PATH", "DATABASE"):
        monkeypatch.delenv(f"{prefix}_{key}", raising=False)


def _resolve_ddb(prefix: str = "DEMO_DDB", **overrides) -> DuckDbConnectionParams:
    kwargs: dict = {
        "engine": None,
        "env_prefix": prefix,
        "user": None,
        "password": None,
        "host": None,
        "port": None,
        "database": None,
        "sslmode": None,
    }
    kwargs.update(overrides)
    params = resolve_engine_params(**kwargs)
    assert isinstance(params, DuckDbConnectionParams)
    return params


def test_duckdb_engine_resolves_to_the_duckdb_shape(monkeypatch) -> None:
    _duckdb_env(monkeypatch)
    monkeypatch.setenv("DEMO_DDB_PATH", "/data/warehouse.duckdb")

    params = _resolve_ddb()

    assert params.engine == SqlEngine.duckdb
    assert params.path == "/data/warehouse.duckdb"
    assert params.read_only is False
    assert params.in_memory is False


def test_duckdb_database_is_the_documented_path_fallback(monkeypatch) -> None:
    """Users reach for _DATABASE first, because every other engine takes it."""
    _duckdb_env(monkeypatch)
    monkeypatch.setenv("DEMO_DDB_DATABASE", "/data/from_database.duckdb")

    assert _resolve_ddb().path == "/data/from_database.duckdb"


def test_duckdb_path_wins_over_database(monkeypatch) -> None:
    _duckdb_env(monkeypatch)
    monkeypatch.setenv("DEMO_DDB_PATH", "/data/from_path.duckdb")
    monkeypatch.setenv("DEMO_DDB_DATABASE", "/data/from_database.duckdb")

    assert _resolve_ddb().path == "/data/from_path.duckdb"


def test_duckdb_database_flag_beats_the_environment(monkeypatch) -> None:
    """House precedence: an explicit flag wins over anything configured."""
    _duckdb_env(monkeypatch)
    monkeypatch.setenv("DEMO_DDB_PATH", "/data/from_path.duckdb")

    assert _resolve_ddb(database="/data/from_flag.duckdb").path == (
        "/data/from_flag.duckdb"
    )


def test_duckdb_memory_path_is_kept_verbatim(monkeypatch) -> None:
    _duckdb_env(monkeypatch)
    monkeypatch.setenv("DEMO_DDB_PATH", MEMORY_PATH)

    params = _resolve_ddb()

    assert params.path == MEMORY_PATH
    assert params.in_memory is True


def test_duckdb_memory_survives_surrounding_whitespace(monkeypatch) -> None:
    """An env var with a stray space must not become a file called ' :memory: '."""
    _duckdb_env(monkeypatch)
    monkeypatch.setenv("DEMO_DDB_PATH", " :memory: ")

    assert _resolve_ddb().in_memory is True


def test_duckdb_path_expands_a_home_directory(monkeypatch) -> None:
    _duckdb_env(monkeypatch)
    monkeypatch.setenv("DEMO_DDB_PATH", "~/warehouse.duckdb")

    assert _resolve_ddb().path == str(Path.home() / "warehouse.duckdb")


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
def test_duckdb_read_only_is_opt_in(monkeypatch, raw: str) -> None:
    _duckdb_env(monkeypatch)
    monkeypatch.setenv("DEMO_DDB_PATH", MEMORY_PATH)
    monkeypatch.setenv("DEMO_DDB_READ_ONLY", raw)

    assert _resolve_ddb().read_only is True


@pytest.mark.parametrize("raw", ["", "0", "false", "no", "off"])
def test_duckdb_read_only_negatives(monkeypatch, raw: str) -> None:
    _duckdb_env(monkeypatch)
    monkeypatch.setenv("DEMO_DDB_PATH", MEMORY_PATH)
    monkeypatch.setenv("DEMO_DDB_READ_ONLY", raw)

    assert _resolve_ddb().read_only is False


def test_duckdb_read_only_typo_fails_towards_read_only(monkeypatch) -> None:
    """Same lenient rule as the tracer's, and the safe direction for this flag."""
    _duckdb_env(monkeypatch)
    monkeypatch.setenv("DEMO_DDB_PATH", MEMORY_PATH)
    monkeypatch.setenv("DEMO_DDB_READ_ONLY", "ture")

    assert _resolve_ddb().read_only is True


def test_duckdb_without_a_path_names_the_variable_to_set(monkeypatch) -> None:
    _duckdb_env(monkeypatch)

    with pytest.raises(ConfigError) as excinfo:
        _resolve_ddb()

    message = str(excinfo.value)
    assert "DEMO_DDB_PATH" in message
    assert MEMORY_PATH in message


def test_duckdb_without_a_prefix_names_the_flag(monkeypatch) -> None:
    """With no target there is no <PREFIX>_PATH to name, so name --database."""
    with pytest.raises(ConfigError) as excinfo:
        resolve_engine_params(
            engine=SqlEngine.duckdb,
            env_prefix=None,
            user=None,
            password=None,
            host=None,
            port=None,
            database=None,
            sslmode=None,
        )

    assert "--database" in str(excinfo.value)


def test_duckdb_target_with_a_host_is_a_config_error(monkeypatch) -> None:
    """The two shapes must not be confusable: HOST + duckdb is a mistake."""
    _duckdb_env(monkeypatch)
    monkeypatch.setenv("DEMO_DDB_PATH", MEMORY_PATH)
    monkeypatch.setenv("DEMO_DDB_HOST", "db.example.com")

    with pytest.raises(ConfigError) as excinfo:
        _resolve_ddb()

    message = str(excinfo.value)
    assert "DEMO_DDB_HOST" in message
    # And it says which way out is which, since either half could be the typo.
    assert "DEMO_DDB_ENGINE=postgresql" in message


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("PORT", "5432"),
        ("USER", "alice"),
        ("PASSWORD", "s3cr3t"),
        ("SSLMODE", "require"),
    ],
)
def test_duckdb_target_refuses_every_server_setting(
    monkeypatch, key: str, value: str
) -> None:
    _duckdb_env(monkeypatch)
    monkeypatch.setenv("DEMO_DDB_PATH", MEMORY_PATH)
    monkeypatch.setenv(f"DEMO_DDB_{key}", value)

    with pytest.raises(ConfigError, match=f"DEMO_DDB_{key}"):
        _resolve_ddb()


def test_duckdb_refusal_never_leaks_the_password_it_refused(monkeypatch) -> None:
    _duckdb_env(monkeypatch)
    monkeypatch.setenv("DEMO_DDB_PATH", MEMORY_PATH)
    monkeypatch.setenv("DEMO_DDB_PASSWORD", "s3cr3t")

    with pytest.raises(ConfigError) as excinfo:
        _resolve_ddb()

    assert "s3cr3t" not in str(excinfo.value)


def test_duckdb_target_refuses_a_libpq_flag(monkeypatch) -> None:
    _duckdb_env(monkeypatch)
    monkeypatch.setenv("DEMO_DDB_PATH", MEMORY_PATH)

    with pytest.raises(ConfigError, match="--host"):
        _resolve_ddb(host="db.example.com")


def test_duckdb_ignores_the_unprefixed_pg_fallbacks(monkeypatch) -> None:
    """A PGHOST in the developer's shell must not break every DuckDB target.

    The prefixed vars are this target's configuration and are refused; PG*/DB_*
    describe some server elsewhere and are ignored on purpose.
    """
    _duckdb_env(monkeypatch)
    monkeypatch.setenv("DEMO_DDB_PATH", MEMORY_PATH)
    monkeypatch.setenv("PGHOST", "somewhere.example.com")
    monkeypatch.setenv("PGUSER", "alice")
    monkeypatch.setenv("PGDATABASE", "not_this_one")

    params = _resolve_ddb()

    assert params.path == MEMORY_PATH


def test_postgres_target_with_a_duckdb_path_is_a_config_error(monkeypatch) -> None:
    """The mirror image: a server target carrying a database file path."""
    monkeypatch.setenv("DEMO_PG_ENGINE", "postgresql")
    monkeypatch.setenv("DEMO_PG_USER", "u")
    monkeypatch.setenv("DEMO_PG_HOST", "h")
    monkeypatch.setenv("DEMO_PG_DATABASE", "d")
    monkeypatch.setenv("DEMO_PG_PATH", "/data/warehouse.duckdb")

    with pytest.raises(ConfigError) as excinfo:
        resolve_connection_params(
            engine=None,
            env_prefix="DEMO_PG",
            user=None,
            password=None,
            host=None,
            port=None,
            database=None,
            sslmode=None,
        )

    message = str(excinfo.value)
    assert "DEMO_PG_PATH" in message
    assert "DEMO_PG_ENGINE=duckdb" in message


def test_libpq_resolver_refuses_duckdb_and_says_why(monkeypatch) -> None:
    """The safety net under every command that predates DuckDB.

    They all resolve through resolve_connection_params, whose return type has a
    host in it. Refusing there means a DuckDB target exits 2 with an
    explanation instead of dying on a missing-host ConfigError somewhere deeper.
    """
    _duckdb_env(monkeypatch)
    monkeypatch.setenv("DEMO_DDB_PATH", MEMORY_PATH)

    with pytest.raises(ValidationError) as excinfo:
        resolve_connection_params(
            engine=None,
            env_prefix="DEMO_DDB",
            user=None,
            password=None,
            host=None,
            port=None,
            database=None,
            sslmode=None,
        )

    message = str(excinfo.value)
    assert excinfo.value.exit_code == ExitCode.INVALID_INPUT
    assert "duckdb" in message
    # Names what does work, and never claims something is missing from dataplat.
    assert "dp db query" in message
    assert "not implemented" not in message.lower()


def test_engine_resolver_still_returns_the_libpq_shape_for_a_server(
    monkeypatch,
) -> None:
    """resolve_engine_params is additive: nothing changes for the other engines."""
    monkeypatch.setenv("DEMO_RS_USER", "u")
    monkeypatch.setenv("DEMO_RS_HOST", "h")
    monkeypatch.setenv("DEMO_RS_DATABASE", "d")
    monkeypatch.delenv("DEMO_RS_PORT", raising=False)

    params = resolve_engine_params(
        engine=SqlEngine.redshift,
        env_prefix="DEMO_RS",
        user=None,
        password=None,
        host=None,
        port=None,
        database=None,
        sslmode=None,
    )

    assert isinstance(params, DbConnectionParams)
    assert params.engine == SqlEngine.redshift
    assert params.port == 5439
    assert params.client_encoding == "UTF8"


def test_unknown_engine_error_lists_every_engine(monkeypatch) -> None:
    monkeypatch.setenv("DEMO_DDB_ENGINE", "sqlite")

    with pytest.raises(ConfigError) as excinfo:
        _resolve_ddb()

    message = str(excinfo.value)
    for engine in SqlEngine:
        assert engine.value in message


# --- the database file itself ------------------------------------------------


def test_missing_database_file_is_refused_not_created(tmp_path: Path) -> None:
    """duckdb.connect() would create it, and then report an empty warehouse."""
    target = tmp_path / "absent.duckdb"

    with pytest.raises(ConfigError) as excinfo:
        ensure_duckdb_database_exists(DuckDbConnectionParams(path=str(target)))

    assert str(target) in str(excinfo.value)
    assert not target.exists()


def test_a_directory_is_refused_with_its_own_message(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="is a directory"):
        ensure_duckdb_database_exists(DuckDbConnectionParams(path=str(tmp_path)))


def test_an_existing_database_file_passes(tmp_path: Path) -> None:
    duckdb = load_duckdb()
    target = tmp_path / "present.duckdb"
    duckdb.connect(database=str(target)).close()

    ensure_duckdb_database_exists(DuckDbConnectionParams(path=str(target)))


def test_memory_is_never_checked_against_the_filesystem() -> None:
    ensure_duckdb_database_exists(DuckDbConnectionParams(path=MEMORY_PATH))


# --- the driver --------------------------------------------------------------


def test_load_duckdb_returns_the_real_module() -> None:
    duckdb = load_duckdb()

    assert duckdb.__name__ == "duckdb"
    with duckdb.connect(database=MEMORY_PATH) as conn:
        assert conn.execute("SELECT 42").fetchall() == [(42,)]


def test_load_duckdb_without_the_package_names_the_extra(monkeypatch) -> None:
    """A psycopg-only user gets a command to run, not an ImportError traceback.

    None in sys.modules is what makes `import duckdb` raise ImportError while
    the package is in fact installed.
    """
    monkeypatch.setitem(sys.modules, "duckdb", None)

    with pytest.raises(ConfigError) as excinfo:
        load_duckdb()

    message = str(excinfo.value)
    assert "dataplat[duckdb]" in message
    assert excinfo.value.exit_code == ExitCode.CONFIG
