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

Two engine families reach a database through that one funnel, so ``db_session``
dispatches: psycopg for PostgreSQL and Redshift, an in-process DuckDB
connection for ``duckdb``. Both cross-cutting concerns are implemented twice —
once per driver — and nowhere else. See :class:`DuckDbSession` for the shape the
DuckDB half yields and what it deliberately does not emulate.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Any, NamedTuple, cast, overload

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
    ConnectionParams,
    DbConnectionParams,
    DuckDbConnectionParams,
    SqlEngine,
    ensure_duckdb_database_exists,
    load_duckdb,
    resolve_connection_params,
    resolve_engine,
    resolve_engine_params,
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
    help="postgresql, redshift or duckdb. "
    "Overrides the target/<PREFIX>_ENGINE default.",
)
UserOption = typer.Option(None, "--user", "-u", help="Database username.")
PasswordOption = typer.Option(
    None,
    "--password",
    help="Database password (prefer <PREFIX>_PASSWORD env var).",
)
DatabaseOption = typer.Option(
    None,
    "--database",
    "-d",
    help="Database name (duckdb: the database file path, or :memory:).",
)
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

    def _engine_and_prefix(self) -> tuple[SqlEngine | None, str | None]:
        """Fill engine and env prefix in from the named target, if any.

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
        return engine, env_prefix

    def resolved_engine(self) -> SqlEngine:
        """The engine this invocation will use, and nothing else.

        For a command that refuses an engine outright: asking
        :func:`dataplat.services.db.capabilities.require_capability` with this
        *before* resolving produces the specific reason ("it has no users at
        all") instead of the generic libpq refusal :meth:`resolve` would raise a
        moment later. No connection setting is read, so it answers for a target
        that has none.
        """
        engine, env_prefix = self._engine_and_prefix()
        return resolve_engine(engine, env_prefix)

    def resolve(self) -> DbConnectionParams:
        """Resolve to concrete libpq connection params.

        Refuses a DuckDB target, because the type it returns has a host and a
        port in it. That is the safety net for the commands DuckDB cannot serve
        (:mod:`dataplat.services.db.capabilities` explains which and why): they
        keep calling this, so a DuckDB target exits 2 with a reason instead of
        connecting to something. Commands that support DuckDB call
        :meth:`resolve_any`.
        """
        engine, env_prefix = self._engine_and_prefix()
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

    def resolve_any(self) -> ConnectionParams:
        """Resolve to whichever param shape the target's engine needs.

        For the commands that work on every engine. The caller then holds a
        union and must say which shape it is handling — ``isinstance(params,
        DuckDbConnectionParams)``, or ``params.engine`` where only the dialect
        matters. ``db_session`` accepts either.
        """
        engine, env_prefix = self._engine_and_prefix()
        return resolve_engine_params(
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

    A DuckDB target is the third case: :meth:`ConnCliParams.resolve` refuses it
    as a :class:`~dataplat.core.errors.ValidationError`, so it exits 2 like any
    other combination of arguments that cannot work.
    """
    try:
        return params.resolve()
    except DataplatError as exc:
        fail(exc, console=console)


def resolve_any_params_or_exit(params: ConnCliParams) -> ConnectionParams:
    """Like :func:`resolve_params_or_exit`, for a command that supports DuckDB.

    Same errors and the same exit codes, wider return type: the caller receives
    whichever shape the engine needs, and has to handle both.
    """
    try:
        return params.resolve_any()
    except DataplatError as exc:
        fail(exc, console=console)


def engine_or_exit(params: ConnCliParams) -> SqlEngine:
    """The engine this invocation will use, or a clean exit.

    The first line of a command that cannot serve every engine::

        engine = engine_or_exit(conn_cli)
        require_capability(engine, Capability.roles, command="dp db role list")
        conn_params = resolve_params_or_exit(conn_cli)

    In that order, the user gets the reason the *engine* cannot answer instead of
    the generic refusal the libpq resolver would raise next — and the command
    keeps the narrow ``DbConnectionParams`` type, with no unreachable branch to
    satisfy a union it can never receive.
    """
    try:
        return params.resolved_engine()
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


DuckDbParams = Sequence[Any] | Mapping[str, Any] | None


class DuckDbColumn(NamedTuple):
    """One entry of ``cursor.description``, shaped like psycopg's ``Column``.

    DuckDB returns plain 7-tuples and this codebase reads ``desc.name`` (see
    ``dataplat/cli/db/__init__.py``). A NamedTuple *is* the DB-API tuple —
    indexing and unpacking are unchanged — so this adds the attribute access
    psycopg callers already use without taking anything away from a caller that
    treats it as a tuple.
    """

    name: str
    type_code: Any = None
    display_size: Any = None
    internal_size: Any = None
    precision: Any = None
    scale: Any = None
    null_ok: Any = None


class DuckDbCursor:
    """A cursor-shaped facade over one DuckDB connection.

    It exists for the same reason :class:`_TracingCursor` does — nothing may be
    executed without ``--verbose`` seeing it — by a different mechanism, because
    DuckDB has no ``cursor_factory`` to install one through.

    Unlike ``_TracingCursor`` it is installed whether or not tracing is on. The
    object commands hold has to be the same either way, and it does two more
    jobs regardless: ``description`` entries gain a ``.name``
    (:class:`DuckDbColumn`), and ``cursor()`` is kept from opening a second
    connection (see :class:`DuckDbSession`).

    What it deliberately does not do:

    - **Translate placeholders.** DuckDB binds ``?``; psycopg binds ``%s``.
      Rewriting would mean deciding which ``%`` in a statement are literals, and
      getting that wrong changes the SQL. Write DuckDB SQL for DuckDB — the
      house rule for Redshift already (CONTRIBUTING: keep the engine constants
      split).
    - **Give independent cursors.** Every cursor from one session shares the
      connection, so it shares one result set: fetch a statement's rows before
      executing the next. DuckDB's own ``cursor()`` would be independent, and
      that is exactly why it is not used.
    """

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    @property
    def raw(self) -> Any:
        """The underlying ``duckdb.DuckDBPyConnection``, for driver-only APIs."""
        return self._connection

    def execute(self, query: Query, params: DuckDbParams = None) -> DuckDbCursor:
        # Composed statements are rendered rather than rejected: psycopg's
        # sql.Identifier quoting produces standard SQL that DuckDB accepts, so a
        # shared helper that quotes an identifier keeps working. Placeholders
        # inside one are still not translated -- see the class docstring.
        statement = _statement_text(query, None)
        if is_enabled():
            trace_sql(statement, params=params)
        self._connection.execute(statement, params)
        return self

    def executemany(self, query: Query, params_seq: Iterable[DuckDbParams]) -> None:
        statement = _statement_text(query, None)
        # Materialized because the driver indexes the batch, not because the
        # trace wants a count: the batch size is deliberately not traced, so the
        # line reads the same as the psycopg branch's, where params_seq may be a
        # one-shot iterable that counting would consume.
        batch = list(params_seq)
        if is_enabled():
            trace_sql(statement, params=None)
        self._connection.executemany(statement, batch)

    @property
    def description(self) -> list[DuckDbColumn] | None:
        raw = self._connection.description
        if raw is None:
            return None
        # Sliced to the seven DB-API fields: DuckDB fills only the first two
        # (name, type_code) and reports None for the rest, and a future release
        # widening the tuple must not crash a render.
        return [DuckDbColumn(*entry[:7]) for entry in raw]

    @property
    def rowcount(self) -> int:
        """DuckDB reports -1 — the DB-API's "unknown" — for every statement.

        Left as it is rather than counted: a DML statement returns its row count
        as a one-row result set (``Count``), so the honest -1 is what a caller
        that ignores the result set should see.
        """
        return int(self._connection.rowcount)

    def fetchone(self) -> Any:
        return self._connection.fetchone()

    def fetchmany(self, size: int = 1) -> list[Any]:
        return list(self._connection.fetchmany(size))

    def fetchall(self) -> list[Any]:
        return list(self._connection.fetchall())

    def close(self) -> None:
        """No-op, deliberately: the connection outlives the cursor.

        Closing the shared connection here would end the session on the first
        ``with conn.cursor() as cur`` block that exited. ``db_session`` owns the
        connection's lifetime.
        """
        return None

    def __enter__(self) -> DuckDbCursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


class DuckDbSession:
    """What ``db_session`` yields for a DuckDB target.

    Shaped like a psycopg ``Connection`` wherever the db area touches one:
    ``cursor()`` as a context manager, ``execute()``, ``close()``.

    ``cursor()`` returns a facade over *this* connection and never calls
    DuckDB's own ``connection.cursor()``, which opens a second connection to the
    same database. That distinction is not cosmetic. Probed on duckdb 1.5.5: a
    ``CREATE TABLE`` inside an open transaction is invisible to a ``cursor()``
    connection — which is precisely how the PostgreSQL test harness isolates
    each test (BEGIN, run, ROLLBACK) — and DuckDB permits one writing
    transaction at a time, so the two would contend for the same file.
    """

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    @property
    def raw(self) -> Any:
        """The underlying ``duckdb.DuckDBPyConnection``, for driver-only APIs."""
        return self._connection

    def cursor(self) -> DuckDbCursor:
        return DuckDbCursor(self._connection)

    def execute(self, query: Query, params: DuckDbParams = None) -> DuckDbCursor:
        return self.cursor().execute(query, params)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        """Roll back the open transaction.

        Not smoothed over: DuckDB raises TransactionException when there is
        nothing to roll back, where psycopg would shrug. Emulating psycopg here
        would hide a command rolling back a transaction it never opened.
        """
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> DuckDbSession:
        return self

    def __exit__(self, *exc_info: object) -> None:
        # Matches both drivers: psycopg's `with connect(...)` and DuckDB's own
        # `with duckdb.connect(...)` each close on exit.
        self.close()


@contextmanager
def _duckdb_session(params: DuckDbConnectionParams) -> Iterator[DuckDbSession]:
    """Open the DuckDB half of :func:`db_session`.

    The exit-code contract mirrors the psycopg branch, and DuckDB's DB-API
    hierarchy makes that a one-line mapping rather than a judgement call:
    a database another process holds the lock on, or one whose file cannot be
    read, raises IOException — a subclass of ``duckdb.OperationalError``, which
    is exactly the retryable "the environment failed" class that earns exit 5.
    Everything else is the statement's own fault (CatalogException,
    ParserException and BinderException are all ``duckdb.ProgrammingError``, as
    is the InvalidInputException a write against a read-only database raises)
    and stays unclassified at 1, so a wrapper does not retry a typo forever.
    All three class relationships were probed on duckdb 1.5.5.
    """
    try:
        # Both raise DataplatError, and neither is a driver failure: a missing
        # package and a path that is not there are local configuration, so they
        # take the ConfigError exit code through fail() rather than the
        # database-error path below.
        duckdb = load_duckdb()
        ensure_duckdb_database_exists(params)
    except DataplatError as exc:
        fail(exc, console=console)

    if is_enabled():
        mode = " read-only" if params.read_only else ""
        trace(CATEGORY_SQL, f"connect {params.path} engine=duckdb{mode}")
    try:
        connection = duckdb.connect(database=params.path, read_only=params.read_only)
        try:
            yield DuckDbSession(connection)
        finally:
            # Closing an in-memory database discards it, which is the correct
            # end for a session that created it.
            connection.close()
    except duckdb.Error as exc:
        # Same reasoning as the psycopg branch: never let a driver message
        # reach Rich as markup, and only the operational class earns SERVICE.
        code = (
            ExitCode.SERVICE
            if isinstance(exc, duckdb.OperationalError)
            else ExitCode.FAILURE
        )
        console.print(f"[red]Database error: {esc(exc)}[/red]")
        raise typer.Exit(code=code) from exc


@contextmanager
def _psycopg_session(params: DbConnectionParams) -> Iterator[psycopg.Connection]:
    """Open the libpq half of :func:`db_session`."""
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


# Overloads so a call site keeps the exact connection type its params imply:
# the ~30 psycopg call sites still see psycopg.Connection, a DuckDB one sees
# DuckDbSession, and only a caller holding the union (resolve_any) has to
# narrow. Without them every existing command would silently be typed Any.
@overload
def db_session(
    params: DbConnectionParams,
) -> AbstractContextManager[psycopg.Connection]: ...


@overload
def db_session(
    params: DuckDbConnectionParams,
) -> AbstractContextManager[DuckDbSession]: ...


@overload
def db_session(params: ConnectionParams) -> AbstractContextManager[Any]: ...


@contextmanager
def db_session(params: ConnectionParams) -> Iterator[Any]:
    """Open a connection to ``params``; translate driver errors into a clean exit.

    The funnel every db command reaches its database through, and the reason no
    command has to know which driver it is talking to. It dispatches on the
    resolved engine — psycopg for PostgreSQL and Redshift, an in-process
    connection for DuckDB — and both halves keep the same two promises:

    - the exit-code contract (an unreachable or locked database is
      :attr:`~dataplat.core.errors.ExitCode.SERVICE`, a bad statement stays
      :attr:`~dataplat.core.errors.ExitCode.FAILURE`);
    - with tracing on, every statement is written to stderr — never stdout, so
      ``--json`` and ``--format csv`` stay machine-clean.

    The context-manager shape is identical for both, so a command that only ever
    calls ``with db_session(params) as conn, conn.cursor() as cur`` needs no
    change to work on either. What differs is what the cursor accepts: DuckDB
    binds ``?`` rather than ``%s`` and speaks its own catalogs, so SQL is still
    per-engine. See :class:`DuckDbSession` and :class:`DuckDbCursor`.
    """
    # isinstance on the params type, not a check on params.engine: the shape is
    # what decides which driver can be handed the values, and the two cannot
    # disagree because the resolver builds them together.
    if isinstance(params, DuckDbConnectionParams):
        with _duckdb_session(params) as session:
            yield session
    else:
        with _psycopg_session(params) as conn:
            yield conn
