"""Shared Typer options and connection plumbing for db commands.

Every db command takes the same connection surface: a named ``--target``
plus low-level overrides. Declaring the options here keeps flags, shorts,
and help text identical across commands, and ``db_session`` centralizes the
resolve/connect/error-exit dance that used to be copy-pasted per command.

``--json``/``--yes`` are not db-specific, so they now come from
:mod:`dataplat.cli._options` and are only re-exported here.

Being the funnel makes this module the one place two cross-cutting concerns
have to be implemented: the exit code a typed error produces (see
:func:`dataplat.cli._exit.fail`) and ``--verbose`` SQL tracing (see
:mod:`dataplat.core.trace`). Both are here rather than at the call sites
because every db command reaches its connection and its cursor through
``db_session``.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, cast

import psycopg
import typer
from psycopg import sql
from psycopg.abc import AdaptContext, Params, Query, QueryNoTemplate
from rich.console import Console

from dataplat.cli._exit import fail

# Re-exported: db commands import --json/--yes from here, but the spelling is
# owned by dataplat.cli._options. The redundant alias marks the re-export.
from dataplat.cli._options import JsonOption as JsonOption
from dataplat.cli._options import YesOption as YesOption
from dataplat.cli._render import esc
from dataplat.core.errors import DataplatError, ExitCode
from dataplat.core.trace import CATEGORY_SQL, is_enabled, trace, trace_sql
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
    """Resolve connection params, printing a friendly error on failure.

    Every db command that takes a connection comes through here, so this is
    where the exit-code contract is applied for the two things that go wrong
    before a socket is opened: an unknown ``--target`` is a
    :class:`~dataplat.core.errors.ValidationError` and exits 2, a missing
    ``<PREFIX>_HOST`` is a :class:`~dataplat.core.errors.ConfigError` and exits
    3. They used to be indistinguishable at 1, which is the difference between
    a CI job that can retry and one that must stop.
    """
    try:
        return params.resolve()
    except DataplatError as exc:
        fail(exc, console=console)


def _statement_text(query: Query, context: AdaptContext | None) -> str:
    """Render ``query`` as the SQL text the server will receive.

    A :class:`~psycopg.sql.Composed` must be rendered, never ``str()``-ed: its
    repr is a Python data structure (``Literal('s3cr3t')``) that is neither what
    the server sees nor something :func:`dataplat.core.trace.redact` can read,
    so a password inside one would survive redaction. Rendering needs an
    adaptation context, which is what the cursor supplies; ``None`` falls back
    to psycopg's defaults.
    """
    if isinstance(query, sql.Composable):
        return query.as_string(context)
    if isinstance(query, bytes):
        # Statements are text; a driver-level bytes query is still readable.
        return query.decode("utf-8", "replace")
    return str(query)


class _TracingCursor(psycopg.Cursor[Any]):
    """A cursor that writes each statement to the tracer before sending it.

    Installed through ``psycopg.connect(cursor_factory=...)`` rather than by
    editing call sites: the db area has one cursor factory's worth of surface
    (``with db_session(...) as conn, conn.cursor() as cur``) and roughly thirty
    ``cur.execute`` calls, so the factory is the only version of this that
    cannot be forgotten by the next command. ``conn.execute(...)`` builds its
    own cursor from the same factory, so it is covered too.

    Tracing happens *before* the statement runs, deliberately. The trace exists
    for the run that never finished — a query still executing when the user
    gives up, or a session the server kills — and a trace written afterwards
    says nothing about either. The cost is that no duration is reported;
    ``dp db long-queries`` is the tool for that question, and a wrong answer to
    "what did we send" is the one this seam exists to prevent.
    """

    def execute(
        self,
        query: Query,
        params: Params | None = None,
        *,
        prepare: bool | None = None,
        binary: bool | None = None,
    ) -> _TracingCursor:
        if is_enabled():
            trace_sql(_statement_text(query, self), params=params)
        # The cast is about psycopg's overload set, not about the value: it
        # types `execute` once per half of the `Query` alias — a plain statement
        # and a 3.14 t-string `Template` — and mypy will not match the whole
        # alias against either half. Nothing here builds a t-string (the floor
        # is 3.12), and the object is forwarded exactly as it arrived.
        super().execute(
            cast("QueryNoTemplate", query), params, prepare=prepare, binary=binary
        )
        return self

    def executemany(
        self,
        query: Query,
        params_seq: Iterable[Params],
        *,
        returning: bool = False,
    ) -> None:
        # Unused by the db area today, but a cursor that traced only `execute`
        # would silently stop tracing the first time someone reached for this.
        # params_seq may be a one-shot iterable, so it is never inspected: the
        # count would consume the batch psycopg is about to send.
        if is_enabled():
            trace_sql(_statement_text(query, self), params=None)
        super().executemany(query, params_seq, returning=returning)


@contextmanager
def db_session(params: DbConnectionParams) -> Iterator[psycopg.Connection]:
    """Open a connection; translate psycopg errors into a clean exit.

    With tracing on, statements executed through this connection are written to
    stderr — never stdout, so ``--json`` and ``--format csv`` stay machine-clean
    (see :mod:`dataplat.core.trace`). The factory is installed only when
    tracing is enabled, so the default path is the plain psycopg cursor it has
    always been.
    """
    if is_enabled():
        # One line per session, so the statements that follow can be attributed
        # when a command loops over targets (`-t all`). No password: the whole
        # point of naming the fields is that these four are not secret.
        trace(
            CATEGORY_SQL,
            f"connect {params.user}@{params.host}:{params.port}/{params.dbname}"
            f" engine={params.engine.value}",
        )
    # None is psycopg's own default, so the untraced path is the plain cursor.
    factory = _TracingCursor if is_enabled() else None
    kwargs = params.as_psycopg_kwargs()
    try:
        with psycopg.connect(cursor_factory=factory, **kwargs) as conn:  # type: ignore[arg-type]
            yield conn
    except psycopg.Error as exc:
        # A driver message quotes the offending SQL and server hints verbatim,
        # so it can carry anything — never let it reach Rich as markup.
        #
        # Not routed through _exit.fail: psycopg.Error is not a DataplatError,
        # so there is no declared code to take, and "Database error" is a
        # different sentence from fail()'s "Error". The code is chosen here
        # instead, and only OperationalError earns SERVICE.
        #
        # That split is the whole point. OperationalError is the environment
        # failing — refused connection, timeout, server gone mid-query — which is
        # the one class where retrying can help, and the documented meaning of
        # exit 5. Everything else psycopg raises is the statement's own fault
        # (ProgrammingError for bad SQL, IntegrityError for a constraint) and a
        # retry would only fail identically, so those stay unclassified at 1.
        # Mapping every psycopg.Error to 5 would tell a wrapper to keep retrying
        # a syntax error.
        code = (
            ExitCode.SERVICE
            if isinstance(exc, psycopg.OperationalError)
            else ExitCode.FAILURE
        )
        console.print(f"[red]Database error: {esc(exc)}[/red]")
        raise typer.Exit(code=code) from exc
