"""Fixtures backing the integration suite against a live Redshift cluster.

Why this exists: ``dataplat/services/db`` targets PostgreSQL and Redshift, and
only PostgreSQL has a server to answer for it. Redshift is a managed service
with no container, so every Redshift-affecting change has so far been justified
by the evidence rules in CONTRIBUTING.md rather than by an executed statement.
This harness makes "run it against a real cluster" a matter of exporting two
environment variables.

The safety problem that shapes the design
=========================================

The PostgreSQL harness owns a throwaway container: it creates schemas and
cluster-wide roles freely and leans on transaction rollback to erase them. None
of that transfers. A Redshift cluster may be production, its roles and users are
cluster-wide, and its transactional-DDL semantics are not PostgreSQL's -- so
rollback cannot be *assumed* to undo anything. Therefore:

* the read-only tier is *incapable* of mutating, not merely intended not to --
  see :class:`ReadOnlyCursor` and :func:`assert_read_only`;
* the mutating tier refuses to exist unless a human has declared the cluster
  disposable, and it runs on its own connection;
* nothing here discovers a cluster implicitly. No ``DP_TEST_RS_*`` variable
  means "no cluster", never "fall back to ``PGHOST``".

Environment
===========

``DP_TEST_RS_TARGET``
    A dataplat target name, resolved through the tool's own configuration
    (``ConnCliParams`` -> ``resolve_target`` -> ``resolve_connection_params``).
    Preferred over a raw DSN: it dogfoods the resolution the CLI itself uses, so
    a bug in that path shows up here instead of in a user's terminal. Wins if
    both variables are set.

``DP_TEST_RS_DSN``
    A raw libpq URL, for when target resolution is in the way (a colleague's
    cluster, a one-off port-forward). Must name a host explicitly.

``DP_TEST_RS_REQUIRED``
    Truthy => an unreachable cluster is an ERROR, not a skip. Set it in CI, so a
    cluster that quietly stops answering cannot masquerade as a green run.

``DP_TEST_RS_DISPOSABLE``
    Explicitly affirmative (``1``/``true``/``yes``/``on``) => and only then may
    ``redshift_ddl`` tests run.

``DP_TEST_RS_SCHEMA``
    Optional. A schema the read-only tier may inspect. Discovered from the
    catalog when unset.

Fixtures
========

``rs_target_source`` (session)
    :class:`RsTargetSource` -- resolved connection info plus which variable
    produced it. Applies the skip/require rule. Never renders the password.
``rs_conn`` (session)
    ``psycopg.Connection``, ``autocommit=False``, in READ ONLY transactions
    where the server allows it.
``rs_cursor`` (function)
    :class:`ReadOnlyCursor` -- guarded; raises :class:`ReadOnlyViolation`
    before a non-read reaches the server.
``rs_ddl_cursor`` (function)
    Unguarded ``psycopg.Cursor`` on a second, read-write connection. Skips
    unless ``DP_TEST_RS_DISPOSABLE``.
``rs_probe_schema`` (session)
    A schema name the read-only tier may inspect.
``rs_probe_relation`` (session)
    :class:`RsProbeRelation` -- a ``(schema, name)`` pair to inspect.
``conformance`` (session)
    :class:`ConformanceLog` -- ``conformance.record(question, answer, detail)``,
    rendered as a summary table when the run ends. The point of this suite is
    learning what the dialect actually does, not going green.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, LiteralString, NamedTuple, NoReturn

import pytest

from dataplat.core.errors import DataplatError

if TYPE_CHECKING:
    from psycopg import Connection, Cursor
    from psycopg.abc import Params, Query
    from psycopg.rows import TupleRow

    from dataplat.services.db.connection import SqlEngine

# psycopg ships in the optional "db" extra, so a bare `uv sync` leaves it out.
# Import failure flows through the same skip/require gate as an unreachable
# cluster: swallowing it into an unconditional skip is exactly the lenient
# direction that lets CI stop testing anything. Mirrors the PostgreSQL harness.
try:
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    _PSYCOPG_MISSING: str | None = None
except ModuleNotFoundError as exc:  # pragma: no cover - depends on extras
    _PSYCOPG_MISSING = str(exc)


TARGET_ENV_VAR = "DP_TEST_RS_TARGET"
DSN_ENV_VAR = "DP_TEST_RS_DSN"
REQUIRED_ENV_VAR = "DP_TEST_RS_REQUIRED"
DISPOSABLE_ENV_VAR = "DP_TEST_RS_DISPOSABLE"
SCHEMA_ENV_VAR = "DP_TEST_RS_SCHEMA"

# Seconds. Long enough for a Redshift cluster behind a VPN to answer, short
# enough that a wrong hostname does not stall collection for a minute.
CONNECT_TIMEOUT = 10

_SETUP_HINT = f"""\
Point the suite at a cluster with either:

    export DP_TARGETS=warehouse WAREHOUSE_ENGINE=redshift \\
        WAREHOUSE_HOST=... WAREHOUSE_USER=... WAREHOUSE_DATABASE=... \\
        WAREHOUSE_PASSWORD=...
    export {TARGET_ENV_VAR}=warehouse

or, as an escape hatch:

    export {DSN_ENV_VAR}='postgresql://user:pw@cluster.eu-central-1.redshift\
.amazonaws.com:5439/dev?sslmode=require'

The scheme is postgresql://, not redshift://. Redshift speaks the PostgreSQL
wire protocol, and libpq only recognises postgresql:// and postgres:// — it
rejects anything else outright, before a connection is attempted.

The read-only tier is safe against a warehouse in use. Mutating tests
additionally need {DISPOSABLE_ENV_VAR}=1."""


# --- boolean environment variables -----------------------------------------
# Two readers, failing safe in OPPOSITE directions. That asymmetry is the whole
# point, so they are separate functions rather than one with a flag.


def truthy(raw: str | None) -> bool:
    """Lenient reader: anything but an explicit negative is True.

    Used for ``DP_TEST_RS_REQUIRED``, where the risky mistake is *not* being
    strict: a typo'd ``DP_TEST_RS_REQUIRED=ture`` that fell back to False would
    turn CI's hard failure back into a silent skip. Same semantics as the
    PostgreSQL harness's ``_truthy``.
    """
    if raw is None:
        return False
    return raw.strip().lower() not in {"", "0", "false", "no", "off"}


AFFIRMATIVE = ("1", "true", "yes", "on")


def explicit_yes(raw: str | None) -> bool:
    """Strict reader: only an explicit affirmative is True.

    Used for ``DP_TEST_RS_DISPOSABLE``, where the risky mistake is the opposite
    one: a typo must never be read as permission to mutate a cluster. Anything
    that is not in :data:`AFFIRMATIVE` is "no".
    """
    if raw is None:
        return False
    return raw.strip().lower() in AFFIRMATIVE


# --- password hygiene -------------------------------------------------------

# Two spellings reach us: the libpq URL a human exports, and the keyword form
# psycopg's make_conninfo emits. Both are redacted, because every message in
# this module may end up in a CI log.
_URL_PASSWORD_RE = re.compile(r"(?P<scheme>://[^:/?#@]+):[^@/?#]*@")
# `[a-z_]*password` so sslpassword= and PGPASSWORD= are covered too.
_KEYWORD_PASSWORD_RE = re.compile(
    r"(?P<key>\b[a-z_]*password\s*=\s*)(?P<value>'(?:[^']|'')*'|\S*)",
    re.IGNORECASE,
)


def redact(text: str) -> str:
    """Strip passwords out of anything about to be shown to a human.

    Applied to conninfo strings *and* to driver error messages, which quote the
    connection string back at you often enough to matter.
    """
    text = _URL_PASSWORD_RE.sub(r"\g<scheme>:***@", text)
    return _KEYWORD_PASSWORD_RE.sub(r"\g<key>***", text)


# --- the read-only guard ----------------------------------------------------


class ReadOnlyViolation(AssertionError):
    """A test tried to send a non-read through the read-only cursor.

    Deliberately an ``AssertionError`` and deliberately *not* a
    ``psycopg.Error``: this is a harness violation, not a database failure, and
    code under test that catches ``psycopg.Error`` must not be able to swallow
    it.
    """


# Everything else is refused. This allowlist is stricter than
# ``dataplat.cli.db._classify_sql``, which exists to decide whether to *prompt*
# a human -- it allows TABLE and VALUES, tolerates a semicolon-separated batch,
# and strips comments without tracking string literals. None of that is
# acceptable when the target may be production.
ALLOWED_FIRST_KEYWORDS = frozenset({"select", "with", "explain", "show"})

# Layer two. Every one of these can only appear at the start of a statement, so
# finding one *inside* an allowlisted statement means either a data-modifying
# CTE (`WITH x AS (DELETE ... RETURNING *) SELECT ...`), a locking clause
# (`... FOR UPDATE`), a table-creating SELECT (`SELECT ... INTO t`), or a
# smuggled second statement my splitter failed to see. All four are refusals.
#
# False positives are the intended failure direction: `SELECT x AS comment`
# trips this and has to be renamed. `end` is NOT in the list -- `CASE ... END`
# is unavoidable in catalog queries -- and neither are the cursor verbs
# (DECLARE/FETCH/MOVE/CLOSE), which cannot modify data and whose keywords show
# up in legitimate reads (`FETCH FIRST 10 ROWS ONLY`).
FORBIDDEN_KEYWORDS = frozenset(
    {
        # DML
        "insert",
        "update",
        "delete",
        "merge",
        "truncate",
        "copy",
        "unload",
        "into",
        # DDL
        "create",
        "alter",
        "drop",
        "rename",
        "comment",
        "refresh",
        # privileges and ownership
        "grant",
        "revoke",
        "reassign",
        # maintenance
        "vacuum",
        "analyze",
        "analyse",
        "reindex",
        "checkpoint",
        # session and transaction control
        "begin",
        "start",
        "commit",
        "rollback",
        "abort",
        "savepoint",
        "release",
        "set",
        "reset",
        "discard",
        "lock",
        # code execution
        "call",
        "do",
        "execute",
        "prepare",
        "deallocate",
        # asynchronous notification
        "listen",
        "notify",
        "unlisten",
    }
)

# Function calls a lexical guard would otherwise wave through, because they sit
# inside a perfectly ordinary SELECT. `dp db kill` terminates backends for a
# living; the read-only tier must not be able to do that to a warehouse in use.
# This list is not, and cannot be, complete -- see the honesty note in
# `assert_read_only`.
FORBIDDEN_FUNCTIONS = frozenset(
    {
        "nextval",
        "setval",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "pg_switch_wal",
        "pg_switch_xlog",
        "pg_create_restore_point",
        "pg_advisory_lock",
        "pg_advisory_xact_lock",
        "pg_drop_replication_slot",
        "pg_create_physical_replication_slot",
        "pg_create_logical_replication_slot",
        "dblink_exec",
        "lo_import",
        "lo_export",
        "lo_unlink",
    }
)

# Identifiers, keywords and numbers. `$` and `_` are identifier characters, so
# `last_analyze` and `stl_load_errors` stay single tokens and cannot be mistaken
# for the keywords they contain.
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


def _deny(reason: str, statement: str) -> NoReturn:
    """Refuse a statement, quoting enough of it to identify the caller."""
    excerpt = " ".join(statement.split())
    if len(excerpt) > 200:
        excerpt = f"{excerpt[:200]}..."
    raise ReadOnlyViolation(
        f"read-only cursor refused this statement: {reason}\n"
        f"  statement: {excerpt}\n"
        "This tier may be pointed at a warehouse in use, so only SELECT, "
        "WITH ... SELECT, EXPLAIN and SHOW are allowed. Use rs_ddl_cursor "
        f"(and {DISPOSABLE_ENV_VAR}=1) if the statement genuinely must mutate."
    )


def _split_statements(text: str) -> list[str]:
    """Split on top-level ``;``, replacing comments with a space.

    Hand-written rather than regex-based because a regex that does not track
    quoting can be made to *hide* a statement, which is the one failure
    direction that matters here::

        SELECT '--' ; DROP TABLE t

    Naive ``--``-to-end-of-line stripping deletes ``' ; DROP TABLE t``, leaving
    an innocent-looking ``SELECT '`` and a server that happily runs two
    statements. So string literals, quoted identifiers and (nesting) block
    comments are all scanned properly.

    Two deliberate asymmetries, both erring towards refusal:

    * A backslash never escapes anything. With ``standard_conforming_strings``
      on it does not, and if the server disagrees the worst case is that a
      literal looks unterminated to us and the statement is refused. Believing
      in backslash escapes has the opposite, dangerous failure: it would let
      ``'\\'; DROP TABLE t --'`` swallow a real statement.
    * Dollar quoting is not recognised. ``$$ ; DROP TABLE t $$`` therefore
      reads as a top-level semicolon and is refused, which is fine; treating it
      as a literal on an engine that does not support it would not be.

    Raises :class:`ReadOnlyViolation` on an unterminated literal or comment.
    """
    segments: list[list[str]] = [[]]
    i, n = 0, len(text)
    while i < n:
        pair = text[i : i + 2]
        char = text[i]
        if pair == "--":
            newline = text.find("\n", i)
            i = n if newline == -1 else newline + 1
            segments[-1].append(" ")
        elif pair == "/*":
            depth = 1
            i += 2
            while i < n and depth:
                inner = text[i : i + 2]
                if inner == "/*":
                    depth += 1
                    i += 2
                elif inner == "*/":
                    depth -= 1
                    i += 2
                else:
                    i += 1
            if depth:
                _deny("unterminated block comment", text)
            segments[-1].append(" ")
        elif char in "'\"":
            i += 1
            while True:
                if i >= n:
                    kind = "string literal" if char == "'" else "quoted identifier"
                    _deny(f"unterminated {kind}", text)
                if text[i] == char:
                    # A doubled quote is an escaped quote in both engines; a
                    # single one ends the literal.
                    if text[i : i + 2] == char * 2:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            # Quoted regions are replaced wholesale: their contents can never be
            # a keyword, and dropping them stops `SELECT 'DROP TABLE t'` from
            # looking like a mutation while `SELECT '--'` stops hiding one.
            segments[-1].append(" ")
        elif char == ";":
            segments.append([])
            i += 1
        else:
            segments[-1].append(char)
            i += 1
    return ["".join(segment) for segment in segments]


def _statement_text(query: object, context: object = None) -> str:
    """Coerce whatever psycopg accepts into text the guard can inspect.

    Deny by default: a query object this cannot turn into a string is refused
    rather than passed through unexamined.
    """
    if isinstance(query, str):
        return query
    if isinstance(query, bytes | bytearray | memoryview):
        return bytes(query).decode("utf-8", errors="replace")
    as_string = getattr(query, "as_string", None)
    if as_string is None:
        _deny(f"unrecognised query object of type {type(query).__name__}", repr(query))
    try:
        return str(as_string(context))
    except Exception:
        # psycopg composables can render without a connection, but a custom
        # object claiming the same API may not. Unexaminable means refused.
        _deny(f"could not render a {type(query).__name__} for inspection", repr(query))


def assert_read_only(query: object, context: object = None) -> str:
    """Return the statement's text, or raise :class:`ReadOnlyViolation`.

    Deny by default. A statement is allowed only when *all* of the following
    hold, in this order:

    1. it renders to text (comments removed, quoting respected);
    2. it is exactly one statement -- at most one trailing ``;``;
    3. its first keyword is in :data:`ALLOWED_FIRST_KEYWORDS`;
    4. ``WITH`` and ``EXPLAIN`` actually reach a ``SELECT``;
    5. no token is in :data:`FORBIDDEN_KEYWORDS` or
       :data:`FORBIDDEN_FUNCTIONS`.

    What this does NOT catch, stated plainly rather than implied: a
    side-effecting *function* that is not on the list. ``SELECT
    some_udf_that_writes()`` is lexically a read and will be allowed.
    :data:`FORBIDDEN_FUNCTIONS` names the built-ins whose side effects would be
    most damaging, and the server-side READ ONLY transaction is the second
    layer, but neither is a substitute for not writing such a test. There is no
    lexical guard that closes this hole.
    """
    text = _statement_text(query, context)
    segments = _split_statements(text)
    if len(segments) > 1 and any(segment.strip() for segment in segments[1:]):
        # `SELECT 1; DROP TABLE t` is two statements, and psycopg sends a
        # parameter-less query through the simple protocol, which runs both.
        _deny("more than one statement in a single execute()", text)

    statement = segments[0].strip()
    if not statement:
        _deny("empty statement (after removing comments)", text)

    tokens = [token.lower() for token in _TOKEN_RE.findall(statement)]
    if not tokens:
        _deny("no SQL keyword found", text)

    first = tokens[0]
    if first not in ALLOWED_FIRST_KEYWORDS:
        _deny(
            f"{first.upper()} is not one of "
            f"{', '.join(sorted(ALLOWED_FIRST_KEYWORDS)).upper()}",
            text,
        )
    if first in {"with", "explain"} and "select" not in tokens:
        # A WITH that resolves to anything but a SELECT, and EXPLAIN of a
        # non-SELECT, are both outside the allowlist.
        _deny(f"{first.upper()} does not resolve to a SELECT", text)

    forbidden = sorted(
        {
            token
            for token in tokens
            if token in FORBIDDEN_KEYWORDS
            or token in FORBIDDEN_FUNCTIONS
            or token.startswith("pg_stat_reset")
        }
    )
    if forbidden:
        _deny(f"forbidden token(s): {', '.join(forbidden)}", text)

    return statement


class ReadOnlyCursor:
    """A cursor that inspects statements before the server sees them.

    Wraps rather than subclasses ``psycopg.Cursor`` on purpose: subclassing
    would inherit every write path (``executemany``, ``copy``, ``stream``) and
    make each one an opt-out. Here they are opt-in, and the two that exist only
    to write are refused outright.

    ``connection`` is forwarded, because real tests need it. That is a hole --
    ``cursor.connection.execute(...)`` bypasses this class entirely -- and it is
    the hole the server-side READ ONLY transaction covers.
    """

    def __init__(self, cursor: Cursor[TupleRow]) -> None:
        self._cursor = cursor

    def execute(
        self, query: Query, params: Params | None = None, **kwargs: Any
    ) -> ReadOnlyCursor:
        """Guard, then delegate. Returns self so chaining still works."""
        # assert_read_only returns the vetted statement text. Sending that,
        # rather than the original object, also narrows psycopg's Query alias
        # (which admits a Template that Cursor.execute does not).
        statement = assert_read_only(query, self._cursor)
        self._cursor.execute(statement, params, **kwargs)
        return self

    def executemany(self, *args: Any, **kwargs: Any) -> NoReturn:
        """Always refused: executemany exists to repeat a write."""
        _deny("executemany() is never a read", str(args[:1]))

    def copy(self, *args: Any, **kwargs: Any) -> NoReturn:
        """Always refused: COPY moves data in, and its FROM/TO is not SQL."""
        _deny("COPY is never a read", str(args[:1]))

    def stream(self, query: Query, params: Params | None = None, **kwargs: Any) -> Any:
        """Server-side cursor iteration -- a read, so guarded and forwarded."""
        assert_read_only(query, self._cursor)
        return self._cursor.stream(query, params, **kwargs)

    def fetchone(self) -> TupleRow | None:
        return self._cursor.fetchone()

    def fetchmany(self, size: int = 0) -> list[TupleRow]:
        return self._cursor.fetchmany(size)

    def fetchall(self) -> list[TupleRow]:
        return self._cursor.fetchall()

    def __iter__(self) -> Iterator[TupleRow]:
        return iter(self._cursor)

    def __enter__(self) -> ReadOnlyCursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._cursor.close()

    def __repr__(self) -> str:
        return f"<ReadOnlyCursor wrapping {self._cursor!r}>"

    def __getattr__(self, name: str) -> Any:
        """Forward the read-only surface (description, rowcount, connection...).

        Private names are never forwarded, which also stops ``_cursor`` itself
        from recursing here before ``__init__`` has run.
        """
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._cursor, name)


# --- connection resolution --------------------------------------------------


@dataclass(frozen=True)
class RsTargetSource:
    """Where the cluster came from, and how to connect to it.

    ``conninfo`` carries the password, so it is excluded from ``repr`` and the
    class renders itself as the password-free ``label``. An accidental
    ``assert rs_target_source`` failure must not paste credentials into CI logs.
    """

    origin: str
    """The environment variable that produced this -- ``DP_TEST_RS_TARGET`` or
    ``DP_TEST_RS_DSN``, with the target name when relevant."""

    label: str
    """``user@host:port/dbname``. Safe to print."""

    engine: SqlEngine | None
    """The engine dataplat believes this is, or None when only a raw DSN was
    given and nothing has declared it."""

    conninfo: str = field(repr=False)
    """libpq keyword string, password included. Never log this."""

    def __str__(self) -> str:
        engine = self.engine.value if self.engine is not None else "engine undeclared"
        return f"{self.label} [{engine}] (from {self.origin})"

    def connect(self, **kwargs: Any) -> Connection[TupleRow]:
        """Open another connection to the same cluster."""
        kwargs.setdefault("connect_timeout", CONNECT_TIMEOUT)
        return psycopg.connect(self.conninfo, **kwargs)


def unavailable(reason: str) -> NoReturn:
    """Skip, or ERROR when ``DP_TEST_RS_REQUIRED`` is set.

    The load-bearing branch, same asymmetry as the PostgreSQL harness: getting
    it wrong in the lenient direction means a cluster that stopped answering
    reports as a green run that executed none of the SQL this suite exists to
    validate.
    """
    message = (
        f"Redshift for the integration suite is unavailable: {redact(reason)}\n\n"
        f"{_SETUP_HINT}\n\n"
        f"Set {REQUIRED_ENV_VAR}=1 to turn this skip into a hard failure."
    )
    if truthy(os.environ.get(REQUIRED_ENV_VAR)):
        # pytest.fail inside a fixture is reported as an ERROR at setup, which
        # is what "never silently decoration" requires.
        pytest.fail(message, pytrace=False)
    pytest.skip(message)


def _label_from(parts: dict[str, Any]) -> str:
    """Password-free ``user@host:port/dbname`` for messages and logs."""
    user = parts.get("user") or "?"
    host = parts.get("host") or parts.get("hostaddr") or "?"
    port = parts.get("port") or "?"
    dbname = parts.get("dbname") or "?"
    return f"{user}@{host}:{port}/{dbname}"


def _resolve_from_target(name: str) -> RsTargetSource:
    """Resolve a dataplat target name through the tool's own configuration."""
    from dataplat.cli.db._common import ConnCliParams
    from dataplat.services.db.targets import resolve_target

    try:
        target = resolve_target(name)
    except DataplatError as exc:
        unavailable(f"{TARGET_ENV_VAR}={name!r} does not resolve: {exc}")

    # resolve_connection_params() falls back to PGHOST/DB_HOST when the
    # target's own <PREFIX>_HOST is unset. That fallback is right for a CLI
    # driven by a human who can see what they typed, and wrong here: a
    # half-configured target would silently aim a test suite at whatever
    # happens to be in PGHOST. Refuse to guess.
    host_var = f"{target.env_prefix}_HOST"
    if not os.environ.get(host_var):
        unavailable(
            f"{TARGET_ENV_VAR}={name!r} resolves to target {target.name!r}, but "
            f"{host_var} is not set. Refusing to fall back to PGHOST/DB_HOST: "
            "set it explicitly so it is obvious which server this suite talks to."
        )

    try:
        params = ConnCliParams(target=name).resolve()
    except DataplatError as exc:
        unavailable(f"{TARGET_ENV_VAR}={name!r} resolves to target {name!r}: {exc}")

    # str() every value: make_conninfo types its kwargs as strings, and port
    # arrives from the resolver as an int.
    kwargs = {
        key: str(value)
        for key, value in params.as_psycopg_kwargs().items()
        if value is not None
    }
    return RsTargetSource(
        origin=f"{TARGET_ENV_VAR}={name}",
        label=_label_from(dict(kwargs)),
        engine=params.engine,
        conninfo=make_conninfo(**kwargs),
    )


def _resolve_from_dsn(dsn: str) -> RsTargetSource:
    """Normalise a raw libpq URL (or keyword string) into a source."""
    try:
        parts: dict[str, Any] = dict(conninfo_to_dict(dsn))
    except psycopg.Error as exc:
        unavailable(f"{DSN_ENV_VAR} is not a valid connection string: {exc}")

    # libpq would fill a missing host in from PGHOST at connect time. Same
    # reasoning as the target path: an under-specified DSN must not inherit a
    # host from the ambient environment.
    if not (parts.get("host") or parts.get("hostaddr")):
        unavailable(
            f"{DSN_ENV_VAR} does not name a host, so libpq would fall back to "
            "PGHOST. Spell the host out."
        )

    return RsTargetSource(
        origin=DSN_ENV_VAR,
        label=_label_from(parts),
        # A raw DSN says nothing about the dialect. Claiming "redshift" here
        # would be a confident falsehood; the conformance suite can ask the
        # server what it is.
        engine=None,
        conninfo=make_conninfo(**parts),
    )


def resolve_rs_source() -> RsTargetSource:
    """Resolve and probe the cluster, or skip/ERROR per the required rule."""
    if _PSYCOPG_MISSING is not None:
        unavailable(
            f"psycopg is not installed ({_PSYCOPG_MISSING}). "
            "Install the optional extra: uv sync --all-extras"
        )

    target = (os.environ.get(TARGET_ENV_VAR) or "").strip()
    dsn = (os.environ.get(DSN_ENV_VAR) or "").strip()
    if not target and not dsn:
        unavailable(
            f"neither {TARGET_ENV_VAR} nor {DSN_ENV_VAR} is set, so no cluster "
            "has been named"
        )

    # Target wins: it is the documented preference, and it is the path the CLI
    # itself takes. `origin` records which one was used either way.
    source = _resolve_from_target(target) if target else _resolve_from_dsn(dsn)

    # Probe once per session, so an unreachable cluster is reported here rather
    # than as a confusing failure deep inside somebody's test. autocommit keeps
    # us from leaving an idle-in-transaction backend on a production cluster.
    try:
        with source.connect(autocommit=True) as probe:
            probe.execute("SELECT 1")
    except psycopg.Error as exc:
        unavailable(f"{source} is not reachable: {str(exc).strip()}")

    return source


def require_disposable() -> None:
    """Skip unless the cluster has been declared disposable.

    Never an ERROR, even under ``DP_TEST_RS_REQUIRED``: "the cluster must be
    reachable" and "the cluster may be destroyed" are different claims, and only
    a human makes the second one. A read-only run must be impossible to turn
    into a mutating run by accident.
    """
    raw = os.environ.get(DISPOSABLE_ENV_VAR)
    if explicit_yes(raw):
        return
    affirmatives = ", ".join(AFFIRMATIVE)
    if raw is None or not raw.strip():
        detail = f"{DISPOSABLE_ENV_VAR} is not set"
    else:
        # Naming the rejected value matters: `DISPOSABLE=ture` looks set.
        detail = (
            f"{DISPOSABLE_ENV_VAR}={raw!r} is not an explicit yes "
            f"({affirmatives}), and anything else is read as no"
        )
    pytest.skip(
        f"needs a DISPOSABLE Redshift and {detail}. This test MUTATES the "
        f"cluster it runs against. Set {DISPOSABLE_ENV_VAR}=1 only for a "
        "cluster you are willing to lose."
    )


# --- server-side read-only transactions ------------------------------------


@dataclass(frozen=True)
class ReadOnlyTransaction:
    """What the server said when asked for a READ ONLY transaction.

    Three states rather than two, because "it did not refuse" and "it enforces
    it" are different facts and only one of them was actually observed. Saying
    "confirmed" when the server would not answer would be the kind of confident
    falsehood CONTRIBUTING.md forbids.
    """

    accepted: bool
    """``BEGIN ... READ ONLY`` did not raise."""

    confirmed: bool | None
    """Server reports ``transaction_read_only = on``; None if it would not
    say."""

    detail: str

    @property
    def answer(self) -> str:
        """One-word summary for the conformance table."""
        if not self.accepted:
            return "no"
        if self.confirmed is None:
            return "accepted, unconfirmed"
        return "yes" if self.confirmed else "accepted but not enforced"


def enable_read_only(conn: Connection[TupleRow]) -> ReadOnlyTransaction:
    """Ask psycopg to start every transaction on ``conn`` as READ ONLY.

    Setting ``Connection.read_only`` only stores the intent; psycopg emits it
    with the next ``BEGIN``. So a transaction is opened here on purpose: a
    server that rejects the syntax must be discovered during session setup, not
    halfway through somebody's test. PostgreSQL and Redshift both document
    ``READ ONLY`` on ``BEGIN``, but "documented" and "deployed" drift, and this
    degrades to a recorded fact rather than a failed run.
    """
    try:
        conn.read_only = True
    except psycopg.Error as exc:
        return ReadOnlyTransaction(False, None, f"driver refused read_only: {exc}")

    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1")
    except psycopg.Error as exc:
        conn.rollback()
        conn.read_only = None
        return ReadOnlyTransaction(
            False, None, f"server refused BEGIN ... READ ONLY: {str(exc).strip()}"
        )

    # PostgreSQL exposes the flag as a GUC. Whether Redshift does is exactly the
    # sort of thing this suite exists to find out, so a failure here is a
    # missing answer, not a broken harness.
    try:
        with conn.cursor() as cursor:
            cursor.execute("SHOW transaction_read_only")
            row = cursor.fetchone()
    except psycopg.Error as exc:
        conn.rollback()
        return ReadOnlyTransaction(
            True, None, f"SHOW transaction_read_only unavailable: {str(exc).strip()}"
        )

    raw = "" if row is None else str(row[0]).strip().lower()
    if raw not in {"on", "off", "true", "false", "1", "0"}:
        return ReadOnlyTransaction(
            True, None, f"SHOW transaction_read_only returned {raw!r}"
        )
    confirmed = raw in {"on", "true", "1"}
    return ReadOnlyTransaction(True, confirmed, f"transaction_read_only = {raw}")


# --- conformance collector --------------------------------------------------


class ConformanceEntry(NamedTuple):
    """One question this suite asked the cluster, and what came back."""

    question: str
    answer: str
    detail: str


class ConformanceLog:
    """Collects dialect facts and renders them at the end of the run.

    The point of the Redshift suite is learning what the engine actually does.
    A green run that recorded nothing has taught nobody anything, so the facts
    are printed whether the run passed or failed.
    """

    def __init__(self) -> None:
        self._entries: list[ConformanceEntry] = []

    def record(self, question: str, answer: object, detail: str = "") -> None:
        """Record an observed fact. Identical repeats are collapsed.

        Repeats are collapsed rather than counted because a session-scoped
        fixture answering the same question once per module is noise, while the
        same question coming back with a *different* answer is a fact worth
        seeing twice.
        """
        entry = ConformanceEntry(
            question=" ".join(question.split()),
            answer=" ".join(str(answer).split()),
            detail=" ".join(detail.split()),
        )
        if entry not in self._entries:
            self._entries.append(entry)

    @property
    def entries(self) -> Sequence[ConformanceEntry]:
        return tuple(self._entries)

    def render(self) -> str:
        """Plain-text table. No rich: this goes through pytest's reporter."""
        if not self._entries:
            return (
                "No conformance facts were recorded. Either no Redshift was "
                f"configured ({TARGET_ENV_VAR}/{DSN_ENV_VAR}) or no test called "
                "conformance.record()."
            )
        headers = ConformanceEntry("QUESTION", "ANSWER", "DETAIL")
        rows = [headers, *self._entries]
        question_width = max(len(row.question) for row in rows)
        answer_width = max(len(row.answer) for row in rows)
        lines = [
            f"{row.question.ljust(question_width)}  "
            f"{row.answer.ljust(answer_width)}  {row.detail}".rstrip()
            for row in rows
        ]
        lines.insert(1, "-" * len(max(lines, key=len)))
        return "\n".join(lines)


_CONFORMANCE_KEY = pytest.StashKey[ConformanceLog]()


def conformance_log(config: pytest.Config) -> ConformanceLog:
    """The run's single collector, created on first use.

    Lives on ``config`` rather than in a module global so the terminal-summary
    hook and the fixture are guaranteed to be looking at the same object.
    """
    log = config.stash.get(_CONFORMANCE_KEY, None)
    if log is None:
        log = ConformanceLog()
        config.stash[_CONFORMANCE_KEY] = log
    return log


def pytest_terminal_summary(
    terminalreporter: Any, exitstatus: int, config: pytest.Config
) -> None:
    """Print the conformance table once the run is over."""
    log = config.stash.get(_CONFORMANCE_KEY, None)
    if log is None or not log.entries:
        return
    terminalreporter.write_sep("=", "redshift dialect conformance")
    terminalreporter.write_line(log.render())


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Derive the redshift markers from the fixtures a test actually uses.

    ``pytestmark`` in each module is the readable declaration; this is the
    safety net that keeps ``-m 'not redshift_ddl'`` airtight when someone adds
    a mutating test and forgets the marker. Deliberately does NOT blanket-mark
    the directory: the harness self-tests need no cluster at all, and marking
    them ``redshift`` would make them look like dialect coverage.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for item in items:
        path = getattr(item, "path", None)
        if path is None or os.path.dirname(os.path.abspath(str(path))) != here:
            continue
        fixtures = set(getattr(item, "fixturenames", ()))
        if "rs_ddl_cursor" in fixtures:
            item.add_marker(pytest.mark.redshift_ddl)
        if fixtures & {"rs_conn", "rs_cursor", "rs_probe_schema", "rs_probe_relation"}:
            item.add_marker(pytest.mark.redshift)


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(scope="session")
def conformance(pytestconfig: pytest.Config) -> ConformanceLog:
    """Collector for dialect facts; rendered as a table when the run ends."""
    return conformance_log(pytestconfig)


@pytest.fixture(scope="session")
def rs_target_source(conformance: ConformanceLog) -> RsTargetSource:
    """Resolved connection info, plus which variable produced it.

    Skips when no cluster is configured or reachable; ERRORs instead under
    ``DP_TEST_RS_REQUIRED``.
    """
    source = resolve_rs_source()
    conformance.record("cluster under test", source.label, source.origin)
    if source.engine is None:
        conformance.record(
            "engine declared by configuration",
            "unknown",
            f"{DSN_ENV_VAR} declares no engine; ask the server instead",
        )
    else:
        conformance.record(
            "engine declared by configuration", source.engine.value, source.origin
        )
    return source


@pytest.fixture(scope="session")
def rs_conn(
    rs_target_source: RsTargetSource, conformance: ConformanceLog
) -> Iterator[Connection[TupleRow]]:
    """Session-wide connection with autocommit OFF, in READ ONLY transactions.

    Row factory stays at psycopg's default (plain tuples), because the services
    under test unpack rows positionally, e.g. ``IndexInfo(*row)``.

    This connection is never used for DDL, even on a disposable cluster:
    ``rs_ddl_cursor`` gets its own, so the read-only attribute here is set once
    and never toggled.
    """
    conn = rs_target_source.connect(
        autocommit=False, application_name="dataplat-redshift-tests"
    )
    try:
        read_only = enable_read_only(conn)
        conformance.record(
            "does BEGIN ... READ ONLY work?", read_only.answer, read_only.detail
        )
        yield conn
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


@pytest.fixture
def rs_cursor(rs_conn: Connection[TupleRow]) -> Iterator[ReadOnlyCursor]:
    """A cursor that cannot mutate: see :class:`ReadOnlyCursor`.

    Rolls back before and after, which on a read-only transaction costs nothing
    and guarantees the next test starts from a known transaction state -- a
    statement that aborted the transaction would otherwise poison it.
    """
    rs_conn.rollback()
    cursor = ReadOnlyCursor(rs_conn.cursor())
    try:
        yield cursor
    finally:
        cursor.close()
        rs_conn.rollback()


@pytest.fixture(scope="session")
def _rs_ddl_conn(request: pytest.FixtureRequest) -> Iterator[Connection[TupleRow]]:
    """Second connection, read-write, for the mutating tier.

    Order matters here. ``require_disposable()`` runs BEFORE the cluster is
    resolved -- hence ``getfixturevalue`` instead of a declared dependency --
    so a run without ``DP_TEST_RS_DISPOSABLE`` always skips with *that* reason,
    and can never be converted into an ERROR by ``DP_TEST_RS_REQUIRED``.

    autocommit is ON, unlike the PostgreSQL harness. Redshift's rules about what
    may run inside a transaction block are not PostgreSQL's, so rollback-based
    cleanup is not something this harness will promise: a test using
    ``rs_ddl_cursor`` owns cleanup of whatever it creates.
    """
    require_disposable()
    source: RsTargetSource = request.getfixturevalue("rs_target_source")
    conn = source.connect(
        autocommit=True, application_name="dataplat-redshift-tests-ddl"
    )
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def rs_ddl_cursor(_rs_ddl_conn: Connection[TupleRow]) -> Iterator[Cursor[TupleRow]]:
    """Unguarded cursor. Skips unless ``DP_TEST_RS_DISPOSABLE`` is set.

    The check is repeated (``_rs_ddl_conn`` made it too) so that reading this
    fixture alone tells the whole truth about when it runs.
    """
    require_disposable()
    cursor = _rs_ddl_conn.cursor()
    try:
        yield cursor
    finally:
        cursor.close()


# Schemas whose contents belong to the server, not to a user.
_PROBE_SCHEMA_SQL: LiteralString = r"""
    SELECT n.nspname, count(c.oid) AS relation_count
    FROM pg_namespace n
    LEFT JOIN pg_class c
        ON c.relnamespace = n.oid AND c.relkind IN ('r', 'v', 'm')
    WHERE n.nspname NOT LIKE 'pg\_%'
      AND n.nspname <> 'information_schema'
      AND has_schema_privilege(n.nspname, 'USAGE')
    GROUP BY n.nspname
    ORDER BY count(c.oid) DESC, n.nspname
    LIMIT 1
"""

_SCHEMA_USABLE_SQL: LiteralString = """
    SELECT bool_or(has_schema_privilege(n.nspname, 'USAGE'))
    FROM pg_namespace n
    WHERE n.nspname = %s
"""

_PROBE_RELATION_SQL: LiteralString = """
    SELECT c.relname, c.relkind
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = %s
      AND c.relkind IN ('r', 'v', 'm')
      AND has_table_privilege(c.oid, 'SELECT')
    ORDER BY c.relkind, c.relname
    LIMIT 1
"""


@pytest.fixture(scope="session")
def rs_probe_schema(rs_conn: Connection[TupleRow], conformance: ConformanceLog) -> str:
    """A schema the read-only tier may inspect.

    ``DP_TEST_RS_SCHEMA`` when set, otherwise discovered from the catalog --
    preferring the schema with the most relations, so the describe-style tests
    have something to look at. Skips with a specific reason when there is
    nothing usable, because "no schema" and "no privileges on the schema you
    named" want different fixes.

    Discovery runs through :class:`ReadOnlyCursor` like everything else: the
    harness holds itself to the guarantee it makes to its callers.
    """
    named = (os.environ.get(SCHEMA_ENV_VAR) or "").strip()
    with ReadOnlyCursor(rs_conn.cursor()) as cursor:
        if named:
            try:
                cursor.execute(_SCHEMA_USABLE_SQL, (named,))
                row = cursor.fetchone()
            except psycopg.Error as exc:
                rs_conn.rollback()
                pytest.skip(f"catalog probe for {SCHEMA_ENV_VAR}={named!r}: {exc}")
            usable = None if row is None else row[0]
            if usable is None:
                pytest.skip(
                    f"{SCHEMA_ENV_VAR}={named!r}: no schema of that name is "
                    "visible in pg_namespace"
                )
            if not usable:
                pytest.skip(
                    f"{SCHEMA_ENV_VAR}={named!r}: no USAGE privilege for "
                    "the connected user"
                )
            conformance.record("probe schema", named, f"from {SCHEMA_ENV_VAR}")
            return named

        try:
            cursor.execute(_PROBE_SCHEMA_SQL)
            row = cursor.fetchone()
        except psycopg.Error as exc:
            rs_conn.rollback()
            pytest.skip(f"catalog probe for a usable schema failed: {exc}")
        if row is None:
            pytest.skip(
                "no non-system schema with USAGE was found; set "
                f"{SCHEMA_ENV_VAR} to name one explicitly"
            )
        schema, relation_count = str(row[0]), row[1]
        conformance.record(
            "probe schema", schema, f"discovered, {relation_count} relation(s)"
        )
        return schema


class RsProbeRelation(NamedTuple):
    """A ``(schema, name)`` pair, unpackable as a plain 2-tuple."""

    schema: str
    name: str


@pytest.fixture(scope="session")
def rs_probe_relation(
    rs_conn: Connection[TupleRow],
    rs_probe_schema: str,
    conformance: ConformanceLog,
) -> RsProbeRelation:
    """A relation inside ``rs_probe_schema`` that the user may SELECT from.

    Tables before views (``relkind`` sorts ``r`` < ``v``), then by name, so two
    runs against the same cluster pick the same relation and a failure is
    reproducible.
    """
    with ReadOnlyCursor(rs_conn.cursor()) as cursor:
        try:
            cursor.execute(_PROBE_RELATION_SQL, (rs_probe_schema,))
            row = cursor.fetchone()
        except psycopg.Error as exc:
            # has_table_privilege(oid, text) is PostgreSQL 8-era and should
            # exist on Redshift, but this suite is here to find out, not to
            # assume. A surprise degrades to a clear skip.
            rs_conn.rollback()
            conformance.record(
                "does has_table_privilege(oid, 'SELECT') work?",
                "no",
                str(exc).strip(),
            )
            pytest.skip(f"catalog probe for a readable relation failed: {exc}")
        if row is None:
            pytest.skip(
                f"schema {rs_probe_schema!r} holds no table or view the "
                f"connected user may SELECT; set {SCHEMA_ENV_VAR} to one that "
                "does"
            )
    name, relkind = str(row[0]), str(row[1])
    conformance.record(
        "probe relation", f"{rs_probe_schema}.{name}", f"relkind={relkind}"
    )
    return RsProbeRelation(schema=rs_probe_schema, name=name)
