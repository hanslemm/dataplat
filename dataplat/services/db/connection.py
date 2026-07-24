"""Database connection parameter resolution."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

from dataplat.core.errors import ConfigError


class SqlEngine(str, Enum):
    """Supported SQL engines."""

    postgresql = "postgresql"
    redshift = "redshift"


@dataclass(frozen=True)
class DbConnectionParams:
    """Validated DB connection parameters for psycopg."""

    user: str
    host: str
    dbname: str
    port: int
    password: str | None = None
    sslmode: str | None = None
    client_encoding: str | None = None
    # Resolved engine (CLI flag > env > postgresql). Included so callers can
    # drive engine-specific SQL without re-running the resolution themselves.
    engine: SqlEngine = SqlEngine.postgresql

    def as_psycopg_kwargs(self) -> dict[str, str | int | None]:
        """Return kwargs compatible with psycopg.connect."""
        return {
            "user": self.user,
            "password": self.password,
            "host": self.host,
            "dbname": self.dbname,
            "port": self.port,
            "sslmode": self.sslmode,
            "client_encoding": self.client_encoding,
        }


def normalize_prefix(prefix: str | None) -> str:
    """Normalize env var prefix."""
    if not prefix:
        return ""
    return prefix.strip().upper().rstrip("_")


def env_get(prefix: str, key: str) -> str | None:
    """Fetch a prefixed environment variable."""
    if not prefix:
        return None
    return os.getenv(f"{prefix}_{key}")


def parse_int(value: str | None, label: str) -> int | None:
    """Parse an integer environment value."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{label} must be an integer") from exc


def resolve_connection_params(
    *,
    engine: SqlEngine | None,
    env_prefix: str | None,
    user: str | None,
    password: str | None,
    host: str | None,
    port: int | None,
    database: str | None,
    sslmode: str | None,
) -> DbConnectionParams:
    """Resolve DB settings from flags and environment.

    Precedence for the engine: explicit ``engine`` argument (from the CLI
    flag) > ``<PREFIX>_ENGINE`` env var > PostgreSQL default. ``None`` on
    the argument means "not specified on the command line"; callers should
    default their Typer option to ``None`` so the env var can take effect.
    """
    prefix = normalize_prefix(env_prefix)

    if engine is None:
        env_engine = env_get(prefix, "ENGINE") if prefix else None
        if env_engine:
            try:
                engine = SqlEngine(env_engine.lower())
            except ValueError as exc:
                raise ConfigError(
                    f"{prefix}_ENGINE must be 'postgresql' or 'redshift'"
                ) from exc
        else:
            engine = SqlEngine.postgresql

    resolved_user = (
        user or env_get(prefix, "USER") or os.getenv("PGUSER") or os.getenv("DB_USER")
    )
    resolved_password = (
        password
        or env_get(prefix, "PASSWORD")
        or os.getenv("PGPASSWORD")
        or os.getenv("DB_PASSWORD")
    )
    resolved_host = (
        host or env_get(prefix, "HOST") or os.getenv("PGHOST") or os.getenv("DB_HOST")
    )
    resolved_database = (
        database
        or env_get(prefix, "DATABASE")
        or env_get(prefix, "DB")
        or env_get(prefix, "NAME")
        or os.getenv("PGDATABASE")
        or os.getenv("DB_NAME")
    )
    resolved_port = (
        port
        or parse_int(env_get(prefix, "PORT"), f"{prefix}_PORT")
        or parse_int(os.getenv("PGPORT"), "PGPORT")
        or (5439 if engine == SqlEngine.redshift else 5432)
    )
    resolved_sslmode = (
        sslmode
        or env_get(prefix, "SSLMODE")
        or os.getenv("PGSSLMODE")
        or os.getenv("DB_SSLMODE")
    )
    resolved_client_encoding = env_get(prefix, "CLIENT_ENCODING") or os.getenv(
        "PGCLIENTENCODING"
    )
    if engine == SqlEngine.redshift and not resolved_client_encoding:
        resolved_client_encoding = "UTF8"

    missing: list[str] = []
    if not resolved_user:
        missing.append("user")
    if not resolved_host:
        missing.append("host")
    if not resolved_database:
        missing.append("database")

    if missing:
        env_hint = f"{prefix}_*" if prefix else "PG* or DB_*"
        raise ConfigError(
            "Missing required connection settings: "
            f"{', '.join(missing)}. Provide flags or {env_hint} env vars."
        )

    assert resolved_user is not None
    assert resolved_host is not None
    assert resolved_database is not None

    return DbConnectionParams(
        user=resolved_user,
        password=resolved_password,
        host=resolved_host,
        dbname=resolved_database,
        port=resolved_port,
        sslmode=resolved_sslmode,
        client_encoding=resolved_client_encoding,
        engine=engine,
    )
