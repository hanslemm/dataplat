"""Shared Typer options and connection plumbing for db commands.

Every db command takes the same connection surface: a named ``--target``
plus low-level overrides. Declaring the options here keeps flags, shorts,
and help text identical across commands, and ``db_session`` centralizes the
resolve/connect/error-exit dance that used to be copy-pasted per command.

``--json``/``--yes`` are not db-specific, so they now come from
:mod:`dataplat.cli._options` and are only re-exported here.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import psycopg
import typer
from rich.console import Console

# Re-exported: db commands import --json/--yes from here, but the spelling is
# owned by dataplat.cli._options. The redundant alias marks the re-export.
from dataplat.cli._options import JsonOption as JsonOption
from dataplat.cli._options import YesOption as YesOption
from dataplat.cli._render import esc
from dataplat.core.errors import DataplatError
from dataplat.services.db.connection import (
    DbConnectionParams,
    SqlEngine,
    resolve_connection_params,
)
from dataplat.services.db.targets import default_target_name, resolve_target

console = Console()

TargetOption = typer.Option(
    None,
    "--target",
    "-t",
    help="Named DB target from DP_TARGETS (default: DP_DEFAULT_TARGET). "
    "Sets engine and env prefix.",
)
EngineOption = typer.Option(
    None,
    "--engine",
    "-e",
    help="postgresql or redshift. Overrides the target/<PREFIX>_ENGINE default.",
)
UserOption = typer.Option(None, "--user", "-u", help="Database username.")
PasswordOption = typer.Option(
    None,
    "--password",
    help="Database password (prefer <PREFIX>_PASSWORD env var).",
)
DatabaseOption = typer.Option(None, "--database", "-d", help="Database name.")
HostOption = typer.Option(None, "--host", "-H", help="Database host.")
PortOption = typer.Option(None, "--port", help="Database port.")
SslmodeOption = typer.Option(
    None, "--sslmode", help="SSL mode (e.g., require, prefer, disable)."
)
EnvPrefixOption = typer.Option(
    None,
    "--env-prefix",
    help="Env var prefix for connection settings (default: the target's prefix).",
)


def limit_option(default: int, help_text: str) -> Any:
    """A per-command row cap with the shared --limit/-n spelling."""
    return typer.Option(default, "--limit", "-n", help=help_text)


@dataclass
class ConnCliParams:
    """Raw connection-related CLI values, before resolution."""

    target: str | None = None
    engine: SqlEngine | None = None
    user: str | None = None
    password: str | None = None
    database: str | None = None
    host: str | None = None
    port: int | None = None
    sslmode: str | None = None
    env_prefix: str | None = None

    def resolve(self) -> DbConnectionParams:
        """Resolve to concrete connection params.

        Precedence: explicit flags > target-derived defaults > env vars.
        """
        env_prefix = self.env_prefix
        engine = self.engine
        target_name = self.target
        if target_name is None and env_prefix is None:
            target_name = default_target_name()
        if target_name:
            target = resolve_target(target_name)
            if env_prefix is None:
                env_prefix = target.env_prefix
            if engine is None:
                engine = target.engine
        return resolve_connection_params(
            engine=engine,
            env_prefix=env_prefix,
            user=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
            database=self.database,
            sslmode=self.sslmode,
        )


def resolve_params_or_exit(params: ConnCliParams) -> DbConnectionParams:
    """Resolve connection params, printing a friendly error on failure."""
    try:
        return params.resolve()
    except DataplatError as exc:
        console.print(f"[red]Error: {esc(exc)}[/red]")
        raise typer.Exit(code=1) from exc


@contextmanager
def db_session(params: DbConnectionParams) -> Iterator[psycopg.Connection]:
    """Open a connection; translate psycopg errors into a clean exit."""
    try:
        with psycopg.connect(**params.as_psycopg_kwargs()) as conn:  # type: ignore[arg-type]
            yield conn
    except psycopg.Error as exc:
        # A driver message quotes the offending SQL and server hints verbatim,
        # so it can carry anything — never let it reach Rich as markup.
        console.print(f"[red]Database error: {esc(exc)}[/red]")
        raise typer.Exit(code=1) from exc
