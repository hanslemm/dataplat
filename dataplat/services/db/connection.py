"""Database connection parameter resolution.

Two engine families live here, and the difference is the reason this module has
two parameter shapes instead of one with optional fields:

- **libpq engines** (PostgreSQL, Redshift) are servers reached over the
  PostgreSQL wire protocol with psycopg: host, port, user, password, TLS.
- **DuckDB** is a database *file* opened inside this process. It has no host,
  no port, no password, no TLS and no users at all — ``current_user`` is the
  constant ``'duckdb'``.

:class:`DbConnectionParams` and :class:`DuckDbConnectionParams` therefore share
no field but ``engine``. Keeping them apart is deliberate: a command that
reaches for ``params.host`` on a DuckDB target is a *type* error rather than an
empty string that quietly connects to the wrong thing, and the resolver refuses
a target that mixes the two shapes instead of ignoring the half it cannot use.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import ModuleType

from dataplat.core.deps import ENGINE_DEPS, engine_install_hint, install_spec
from dataplat.core.errors import ConfigError, ValidationError


class SqlEngine(str, Enum):
    """Supported SQL engines."""

    postgresql = "postgresql"
    redshift = "redshift"
    duckdb = "duckdb"


# The engines psycopg can open. Stated once, because "is this reachable over
# libpq?" is the question every host/port/user code path is really asking, and
# spelling it `engine is not SqlEngine.duckdb` at each site silently includes
# whatever non-libpq engine is added next.
LIBPQ_ENGINES: frozenset[SqlEngine] = frozenset(
    {SqlEngine.postgresql, SqlEngine.redshift}
)

# DuckDB's own spelling for "no file, keep it in RAM". Exact match only: an
# ordinary path is normalized (``~`` expanded), which would mangle this one.
MEMORY_PATH = ":memory:"


@dataclass(frozen=True)
class DbConnectionParams:
    """Validated DB connection parameters for psycopg.

    The libpq shape, for PostgreSQL and Redshift. DuckDB targets resolve to
    :class:`DuckDbConnectionParams` instead — see the module docstring.
    """

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


@dataclass(frozen=True)
class DuckDbConnectionParams:
    """Validated connection parameters for an in-process DuckDB database.

    ``path`` is a filesystem path or :data:`MEMORY_PATH`. There is no
    ``as_psycopg_kwargs``, on purpose: nothing here can be handed to psycopg,
    and a method that raised would only move the mistake from mypy to runtime.
    """

    path: str
    # Opens the file read-only. DuckDB enforces this itself: a write statement
    # against a read-only database fails with InvalidInputException (probed on
    # 1.5.5), so this is a real guard rather than a hint.
    read_only: bool = False
    engine: SqlEngine = SqlEngine.duckdb

    @property
    def in_memory(self) -> bool:
        """Whether this target is the ephemeral ``:memory:`` database."""
        return self.path == MEMORY_PATH


# What a resolver returns when the engine is not known in advance. Callers
# annotated with this must handle both shapes — that is the point of the union.
ConnectionParams = DbConnectionParams | DuckDbConnectionParams


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


def _env_truthy(raw: str | None) -> bool:
    """Read a boolean environment value.

    Same rule as :func:`dataplat.core.trace._truthy` on purpose — one project
    spelling for a boolean env var — including its lenient direction: a typo'd
    ``<PREFIX>_READ_ONLY=ture`` opens the database read-only, so writes are
    refused loudly instead of allowed silently. That is the safe direction for
    this flag as it is for tracing.
    """
    if raw is None:
        return False
    return raw.strip().lower() not in {"", "0", "false", "no", "off"}


def _engine_setting(prefix: str) -> str:
    """How to name the engine setting in an error, with or without a prefix."""
    return f"{prefix}_ENGINE" if prefix else "--engine"


def resolve_engine(engine: SqlEngine | None, env_prefix: str | None) -> SqlEngine:
    """Pick the engine: CLI flag > ``<PREFIX>_ENGINE`` > PostgreSQL.

    Public and separate from the two resolvers because a command that refuses an
    engine outright needs to know which one it is *before* connection settings
    are resolved — asking the capability matrix then produces the specific
    reason rather than a generic "this path speaks libpq" one. Nothing else
    about the target is read, so it works for a target with no settings at all.
    """
    if engine is not None:
        return engine
    prefix = normalize_prefix(env_prefix)
    raw = env_get(prefix, "ENGINE") if prefix else None
    if not raw:
        return SqlEngine.postgresql
    try:
        return SqlEngine(raw.lower())
    except ValueError as exc:
        valid = ", ".join(e.value for e in SqlEngine)
        raise ConfigError(f"{prefix}_ENGINE must be one of: {valid}.") from exc


def load_duckdb() -> ModuleType:
    """Import the duckdb driver, or explain how to install it.

    The only ``import duckdb`` in the package, and it is inside a function for
    two reasons: a psycopg-only user must not carry an embedded database engine
    they never open, and ``dp --version`` must not import a driver at all.
    Callers needing the module (to catch ``duckdb.Error``, or for a
    driver-specific API) go through here so the missing-package message is
    written once.
    """
    spec = ENGINE_DEPS[SqlEngine.duckdb.value]
    try:
        import duckdb
    except ImportError as exc:
        raise ConfigError(
            f"A {spec.engine} target needs the {spec.module} package, which is "
            f"not installed: it is the '{spec.extra}' extra "
            f"({install_spec([spec.extra])}). {engine_install_hint(spec)}"
        ) from exc
    return duckdb


def ensure_duckdb_database_exists(params: DuckDbConnectionParams) -> None:
    """Refuse a configured path that is not a DuckDB database file.

    ``duckdb.connect()`` *creates* a missing file. For a tool whose db commands
    are read-mostly that is the wrong default: a mistyped ``<PREFIX>_PATH``
    would become an empty database, and ``dp db describe`` would then report,
    truthfully, that it contains nothing — sending the reader to look for their
    tables in the wrong place. Naming the path that is not there is the more
    useful answer, and it is a ConfigError because the configuration is what is
    wrong.
    """
    if params.in_memory:
        return
    path = Path(params.path)
    if path.is_dir():
        raise ConfigError(
            f"DuckDB database path is a directory: {params.path}. "
            "Point it at the database file itself."
        )
    if not path.exists():
        raise ConfigError(
            f"DuckDB database not found: {params.path}. Check the configured "
            f"path, or use {MEMORY_PATH} for an ephemeral in-memory database. "
            "(dataplat does not create a database file: an empty one would "
            "report an empty warehouse instead of a wrong path.)"
        )


# Settings that describe a server. On a DuckDB target every one of them is
# unanswerable, so they are refused rather than ignored: a target carrying
# HOST *and* PATH is a configuration someone got wrong in one of two ways, and
# guessing which half they meant is how a `dp db query` ends up running against
# the wrong database.
_LIBPQ_ONLY_ENV_KEYS = ("HOST", "PORT", "USER", "PASSWORD", "SSLMODE")


def _reject_libpq_settings(
    prefix: str,
    *,
    user: str | None,
    password: str | None,
    host: str | None,
    port: int | None,
    sslmode: str | None,
) -> None:
    """Refuse a DuckDB target that also carries libpq settings.

    Only *explicit* settings count: the ``<PREFIX>_*`` vars for this target and
    flags the user typed. The unprefixed PG*/DB_* fallbacks are ignored on
    purpose — they describe some PostgreSQL server in the developer's shell,
    and inheriting them here would make an unrelated ``PGHOST`` break every
    DuckDB target.
    """
    offenders = [
        name
        for name, value in (
            ("--host", host),
            ("--port", port),
            ("--user", user),
            ("--password", password),
            ("--sslmode", sslmode),
        )
        if value is not None
    ]
    offenders += [
        f"{prefix}_{key}" for key in _LIBPQ_ONLY_ENV_KEYS if env_get(prefix, key)
    ]
    if not offenders:
        return
    raise ConfigError(
        f"{', '.join(offenders)} cannot apply to a duckdb target: DuckDB opens "
        "a database file in this process, so it has no host, port, user, "
        f"password or TLS. Remove {offenders[0]}, or set "
        f"{_engine_setting(prefix)}=postgresql if this target is a server."
    )


def _resolve_duckdb_path(prefix: str, database: str | None) -> str:
    """Resolve the database file: ``--database`` > ``_PATH`` > ``_DATABASE``.

    ``<PREFIX>_DATABASE`` is a documented fallback rather than an accident:
    every other engine takes a database *name* from it, so that is what a user
    reaches for first, and refusing the variable they already set would be
    pedantry. ``--database``/``-d`` is the flag spelling for the same reason —
    the db commands have no ``--path``, and adding one to every command's
    signature would buy nothing over the flag that is already there.
    """
    for raw in (database, env_get(prefix, "PATH"), env_get(prefix, "DATABASE")):
        if raw and raw.strip():
            value = raw.strip()
            if value == MEMORY_PATH:
                return value
            # expanduser, but never resolve(): the configured spelling is what
            # the user will recognize in an error message, and DuckDB resolves
            # a relative path against the cwd exactly as they typed it.
            return str(Path(value).expanduser())
    if prefix:
        raise ConfigError(
            f"Missing the DuckDB database path. Set {prefix}_PATH (or "
            f"{prefix}_DATABASE) to a database file, or to {MEMORY_PATH} for "
            "an ephemeral in-memory database."
        )
    raise ConfigError(
        "Missing the DuckDB database path. Pass --database with the path to a "
        f"database file, or {MEMORY_PATH} for an ephemeral in-memory database."
    )


def _resolve_duckdb_params(
    prefix: str,
    *,
    user: str | None,
    password: str | None,
    host: str | None,
    port: int | None,
    database: str | None,
    sslmode: str | None,
) -> DuckDbConnectionParams:
    """Resolve a DuckDB target from flags and ``<PREFIX>_*`` environment."""
    _reject_libpq_settings(
        prefix, user=user, password=password, host=host, port=port, sslmode=sslmode
    )
    return DuckDbConnectionParams(
        path=_resolve_duckdb_path(prefix, database),
        read_only=_env_truthy(env_get(prefix, "READ_ONLY")),
    )


def _libpq_only_message(engine: SqlEngine) -> str:
    """Why a libpq-shaped code path cannot serve ``engine``."""
    # Named commands rather than "not supported": these are not gaps to be
    # filled in later, they are what an engine without a server, without users
    # and without other sessions can answer. Grep for this string if a command
    # gains DuckDB support — it is the only list of them outside
    # dataplat/services/db/capabilities.py.
    return (
        f"This command connects over the PostgreSQL wire protocol, and the "
        f"{engine.value} engine has no server to connect to: it opens a "
        "database file in this process. The commands that speak it are "
        "`dp db query`, `dp db describe` and `dp db top-tables`."
    )


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
    """Resolve libpq DB settings from flags and environment.

    Precedence for the engine: explicit ``engine`` argument (from the CLI
    flag) > ``<PREFIX>_ENGINE`` env var > PostgreSQL default. ``None`` on
    the argument means "not specified on the command line"; callers should
    default their Typer option to ``None`` so the env var can take effect.

    Refuses a non-libpq engine with a :class:`~dataplat.core.errors.
    ValidationError`, because the return type is the libpq shape and DuckDB has
    no host, user or port to put in it. That refusal is also the safety net for
    every command that predates DuckDB: they all funnel through here, so a
    DuckDB target exits 2 with an explanation instead of failing somewhere
    deeper with a missing-host ConfigError. Commands that *do* support DuckDB
    call :func:`resolve_engine_params` instead.
    """
    prefix = normalize_prefix(env_prefix)
    engine = resolve_engine(engine, prefix)
    if engine not in LIBPQ_ENGINES:
        raise ValidationError(_libpq_only_message(engine))

    # The mirror of _reject_libpq_settings: a server target carrying a DuckDB
    # path is the same mistake from the other side, and silently ignoring the
    # variable is how someone ends up convinced they are querying a file.
    if env_get(prefix, "PATH"):
        raise ConfigError(
            f"{prefix}_PATH is a DuckDB setting, and this target's engine is "
            f"{engine.value}. Set {prefix}_ENGINE=duckdb to open it as a "
            f"database file, or remove {prefix}_PATH."
        )

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


def resolve_engine_params(
    *,
    engine: SqlEngine | None,
    env_prefix: str | None,
    user: str | None,
    password: str | None,
    host: str | None,
    port: int | None,
    database: str | None,
    sslmode: str | None,
) -> ConnectionParams:
    """Resolve settings for whichever engine the target names.

    The engine-agnostic entry point: same arguments and same precedence as
    :func:`resolve_connection_params`, but it returns the shape the engine
    needs, so the caller has to handle both. Commands that cannot handle a
    DuckDB target should keep calling ``resolve_connection_params`` — its
    narrower return type is what turns "forgot about DuckDB" into a refusal
    instead of a wrong connection.
    """
    prefix = normalize_prefix(env_prefix)
    resolved = resolve_engine(engine, prefix)
    if resolved is SqlEngine.duckdb:
        return _resolve_duckdb_params(
            prefix,
            user=user,
            password=password,
            host=host,
            port=port,
            database=database,
            sslmode=sslmode,
        )
    # Pass the already-resolved engine so <PREFIX>_ENGINE is read once.
    return resolve_connection_params(
        engine=resolved,
        env_prefix=env_prefix,
        user=user,
        password=password,
        host=host,
        port=port,
        database=database,
        sslmode=sslmode,
    )
