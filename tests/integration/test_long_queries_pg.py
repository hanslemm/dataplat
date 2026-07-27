"""``dataplat.services.db.long_queries`` (Postgres paths) against a live server.

The Redshift half of this module is covered by the fake-cursor unit suite; the
Postgres half cannot be, because everything it does is observation of live
server state. ``pg_stat_activity`` only has something to report when another
backend is genuinely busy, ``pg_cancel_backend`` only proves anything if the
victim actually dies, and ``pg_stat_statements``'s column names — the reason
the history query discovers them at runtime — depend on the server version.

So these tests open a second connection, make it run a slow query, watch the
scan find it, and then kill it.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any

import pytest

from dataplat.core.errors import ValidationError
from dataplat.services.db.long_queries import (
    LongQueryRow,
    cancel_query_postgres,
    fetch_long_queries_postgres,
    fetch_query_history_postgres,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from psycopg import Cursor
    from psycopg.rows import TupleRow

pytestmark = pytest.mark.integration


# Deliberately ragged whitespace: the scan runs the query text through
# REGEXP_REPLACE(query, '\s+', ' ', 'g'), and the collapsed form is what the
# terminal is supposed to show. A mangled escape in that constant (matching a
# literal backslash-s instead of whitespace) would leave the newline and tab
# in place, which is only visible against a real server.
_SLOW_QUERY = "SELECT\n\tpg_sleep(30)   /*  dp_it_slow_backend  */"
_SLOW_QUERY_COLLAPSED = "SELECT pg_sleep(30) /* dp_it_slow_backend */"

# Long enough that an elapsed time of at least this many seconds is a real
# measurement rather than a rounding artefact, short enough not to pad the run.
_MIN_OBSERVED_SECONDS = 1


class _SlowBackend:
    """A second backend running a slow query, so the scan has something to see.

    The query runs on its own thread because the point is to observe it
    *while* it blocks. The connection is only touched from that thread while
    the query is in flight, so nothing is shared concurrently.
    """

    def __init__(self, dsn: str) -> None:
        import psycopg

        self._dsn = dsn
        self._conn = psycopg.connect(
            dsn,
            autocommit=True,
            application_name="dataplat-integration-tests-slow",
        )
        row = self._conn.execute("SELECT pg_backend_pid()").fetchone()
        assert row is not None
        self.pid = int(row[0])
        self.error: BaseException | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Fire the slow query without waiting for it."""

        def run() -> None:
            try:
                self._conn.execute(_SLOW_QUERY)
            except Exception as exc:
                # Being cancelled or terminated is the expected ending; the
                # test asserts on which of the two it was.
                self.error = exc

        self.error = None
        thread = threading.Thread(target=run, daemon=True, name=f"dp-slow-{self.pid}")
        self._thread = thread
        thread.start()

    def wait_for_exit(self, timeout: float = 15.0) -> BaseException | None:
        """Join the query thread and return the exception that ended it."""
        assert self._thread is not None
        self._thread.join(timeout)
        assert not self._thread.is_alive(), (
            f"backend {self.pid} was still running its query after {timeout}s"
        )
        return self.error

    def is_usable(self) -> bool:
        """True when the backend survived, i.e. it was cancelled, not killed."""
        import psycopg

        try:
            self._conn.execute("SELECT 1")
        except psycopg.Error:
            return False
        return True

    def close(self) -> None:
        """Kill any query still in flight, then drop the connection."""
        import psycopg

        with psycopg.connect(self._dsn, autocommit=True) as killer:
            killer.execute("SELECT pg_terminate_backend(%s)", (self.pid,))
        if self._thread is not None:
            self._thread.join(5.0)
        self._conn.close()


@pytest.fixture
def slow_backend(pg_dsn: str) -> Iterator[_SlowBackend]:
    """A second connection that can be made to run a slow query on demand.

    Teardown terminates it unconditionally: a leaked ``pg_sleep`` would sit in
    ``pg_stat_activity`` and could be picked up by a later test's scan.
    """
    backend = _SlowBackend(pg_dsn)
    try:
        yield backend
    finally:
        backend.close()


def _scalar(cursor: Cursor[TupleRow], query: str, *params: Any) -> Any:
    """Run a single-value query and return that value."""
    cursor.execute(query, params or None)
    row = cursor.fetchone()
    assert row is not None
    return row[0]


def _refresh_activity(cursor: Cursor[TupleRow]) -> None:
    """Discard this transaction's cached ``pg_stat_activity`` snapshot.

    PostgreSQL reads the backend status array once per transaction and reuses
    that copy for every later query, so a test that watches a backend appear or
    die *inside* one transaction keeps seeing the stale picture. Production is
    unaffected — the CLI opens a connection and scans immediately — but it does
    mean the scan is a snapshot of the transaction, not of the instant.
    """
    cursor.execute("SELECT pg_stat_clear_snapshot()")


def _find(
    cursor: Cursor[TupleRow], pid: int, *, min_seconds: int
) -> LongQueryRow | None:
    """Run the scan and pick out the row for ``pid``, if it is there.

    Other suites may be running against the same server, so every assertion
    keys off this pid instead of assuming the scan returns exactly one row.
    """
    _refresh_activity(cursor)
    rows = fetch_long_queries_postgres(cursor, min_seconds=min_seconds, limit=200)
    return next((row for row in rows if row.query_id == str(pid)), None)


def _wait_for_hit(
    cursor: Cursor[TupleRow], pid: int, *, min_seconds: int, timeout: float = 15.0
) -> LongQueryRow:
    """Poll the scan until it reports ``pid``, or fail with a clear message."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = _find(cursor, pid, min_seconds=min_seconds)
        if row is not None:
            return row
        time.sleep(0.2)
    pytest.fail(
        f"the scan never reported backend {pid} with min_seconds={min_seconds} "
        f"within {timeout}s"
    )


def _wait_until_gone(
    cursor: Cursor[TupleRow], pid: int, *, timeout: float = 15.0
) -> None:
    """Poll ``pg_stat_activity`` directly until ``pid`` has no backend left."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _refresh_activity(cursor)
        if not _scalar(
            cursor, "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid = %s)", pid
        ):
            return
        time.sleep(0.2)
    pytest.fail(f"backend {pid} was still connected after {timeout}s")


# --- pg_stat_activity ------------------------------------------------------


def test_scan_reports_a_slow_backend_with_a_plausible_elapsed_time(
    pg_cursor: Cursor[TupleRow], slow_backend: _SlowBackend
) -> None:
    """Every projected column is checked against the live backend."""
    expected_user = _scalar(pg_cursor, "SELECT current_user")
    expected_db = _scalar(pg_cursor, "SELECT current_database()")

    slow_backend.start()
    row = _wait_for_hit(pg_cursor, slow_backend.pid, min_seconds=_MIN_OBSERVED_SECONDS)

    assert row.query_id == str(slow_backend.pid)
    # dp db kill acts on session_id, so it has to be the PID too.
    assert row.session_id == str(slow_backend.pid)
    assert row.status == "active"
    assert row.user_name == expected_user
    assert row.db_name == expected_db
    assert row.start_time is not None and row.start_time.tzinfo is not None
    # A real measurement: at least the threshold, and nowhere near the age of
    # the connection or of the scanning transaction.
    assert _MIN_OBSERVED_SECONDS <= row.elapsed_s < 120
    assert row.query_text == _SLOW_QUERY_COLLAPSED


def test_scan_honours_the_min_seconds_threshold(
    pg_cursor: Cursor[TupleRow], slow_backend: _SlowBackend
) -> None:
    """The interval filter is real: the same backend drops out above threshold."""
    slow_backend.start()
    _wait_for_hit(pg_cursor, slow_backend.pid, min_seconds=_MIN_OBSERVED_SECONDS)

    assert _find(pg_cursor, slow_backend.pid, min_seconds=3600) is None


def test_scan_excludes_the_scanning_backend(
    pg_cursor: Cursor[TupleRow], slow_backend: _SlowBackend
) -> None:
    """``pid <> pg_backend_pid()`` keeps the scan out of its own report.

    Checked with ``min_seconds=0``, where the scanning backend is guaranteed to
    qualify on age — so a missing exclusion could not hide behind the filter.
    """
    own_pid = int(_scalar(pg_cursor, "SELECT pg_backend_pid()"))
    slow_backend.start()
    _wait_for_hit(pg_cursor, slow_backend.pid, min_seconds=0)

    _refresh_activity(pg_cursor)
    rows = fetch_long_queries_postgres(pg_cursor, min_seconds=0, limit=200)
    ids = {row.query_id for row in rows}

    assert str(slow_backend.pid) in ids
    assert str(own_pid) not in ids


def test_scan_measures_wall_clock_not_transaction_start(
    pg_cursor: Cursor[TupleRow], slow_backend: _SlowBackend
) -> None:
    """A scan on an already-open transaction still sees newer queries.

    Regression test for a real defect: the SQL used ``now()``, which is the
    *transaction* timestamp, so a query that started after the scanning
    transaction opened had a negative age and was filtered out — the snapshot
    came back empty with nothing wrong in sight. Services here take an open
    cursor by design, so the scan must not depend on how old that cursor's
    transaction is.
    """
    # Pin the transaction timestamp, then start the query strictly after it.
    txn_start = _scalar(pg_cursor, "SELECT now()")
    slow_backend.start()

    row = _wait_for_hit(pg_cursor, slow_backend.pid, min_seconds=_MIN_OBSERVED_SECONDS)

    assert row.start_time > txn_start
    assert row.elapsed_s >= _MIN_OBSERVED_SECONDS


# --- cancel / terminate ----------------------------------------------------


def test_cancel_stops_the_statement_and_leaves_the_backend_alive(
    pg_cursor: Cursor[TupleRow], slow_backend: _SlowBackend
) -> None:
    """``--cancel`` must end the query without dropping the session."""
    import psycopg

    slow_backend.start()
    _wait_for_hit(pg_cursor, slow_backend.pid, min_seconds=0)

    assert cancel_query_postgres(pg_cursor, slow_backend.pid, terminate=False) is True

    error = slow_backend.wait_for_exit()
    assert isinstance(error, psycopg.errors.QueryCanceled)
    # The connection survived, which is the whole difference from terminate.
    assert slow_backend.is_usable()
    _refresh_activity(pg_cursor)
    assert _scalar(
        pg_cursor,
        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid = %s)",
        slow_backend.pid,
    )
    # ... but it is no longer running anything, so the scan drops it.
    assert _find(pg_cursor, slow_backend.pid, min_seconds=0) is None


def test_terminate_kills_the_backend(
    pg_cursor: Cursor[TupleRow], slow_backend: _SlowBackend
) -> None:
    """The default kill path really disconnects the session."""
    import psycopg

    slow_backend.start()
    _wait_for_hit(pg_cursor, slow_backend.pid, min_seconds=0)

    assert cancel_query_postgres(pg_cursor, slow_backend.pid, terminate=True) is True

    error = slow_backend.wait_for_exit()
    assert isinstance(error, psycopg.errors.AdminShutdown)
    assert not slow_backend.is_usable()
    _wait_until_gone(pg_cursor, slow_backend.pid)


def test_cancelling_an_unknown_pid_reports_false(pg_cursor: Cursor[TupleRow]) -> None:
    """The server's verdict is passed through, so the CLI can say "already gone".

    PostgreSQL answers a stale PID with a warning and ``false`` rather than an
    error, which is exactly why the return value is worth trusting.
    """
    unused_pid = 999_999
    assert not _scalar(
        pg_cursor,
        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid = %s)",
        unused_pid,
    )

    assert cancel_query_postgres(pg_cursor, unused_pid, terminate=False) is False
    assert cancel_query_postgres(pg_cursor, unused_pid, terminate=True) is False


# --- pg_stat_statements history -------------------------------------------

_HISTORY_MARKER = "dp_it_history_marker"


def _run_slow_statement(cursor: Cursor[TupleRow], seconds: float = 0.5) -> None:
    """Execute a recognisable statement slow enough to survive the filters."""
    cursor.execute(f"SELECT 1 AS {_HISTORY_MARKER}, pg_sleep({seconds})")


def test_history_returns_real_rows_from_pg_stat_statements(
    pg_cursor: Cursor[TupleRow],
) -> None:
    """The version-dependent column discovery is exercised for real.

    On this server the columns are the post-12 names (``total_exec_time`` and
    friends) and the values are milliseconds; the assertions on the marker
    statement's timings are what prove both — a run that picked, say, a
    plan-time column would report ~0 seconds for a 0.5s sleep.
    """
    _run_slow_statement(pg_cursor)

    rows = fetch_query_history_postgres(pg_cursor, min_seconds=0, limit=1000)
    assert rows

    marker = next((row for row in rows if _HISTORY_MARKER in row.query_text), None)
    assert marker is not None, "the marker statement was not tracked"
    assert marker.calls >= 1
    assert marker.max_s >= 0.4
    assert marker.total_s >= marker.max_s - 0.01
    assert marker.mean_s <= marker.max_s + 0.01
    # Descending by max duration, so the caller's "worst offenders" ordering
    # holds against real data.
    maxima = [row.max_s for row in rows]
    assert maxima == sorted(maxima, reverse=True)


def test_history_min_seconds_filters_against_real_durations(
    pg_cursor: Cursor[TupleRow],
) -> None:
    """The threshold is compared in seconds, not milliseconds."""
    _run_slow_statement(pg_cursor)

    assert fetch_query_history_postgres(pg_cursor, min_seconds=86_400, limit=10) == []


def _install_pgss_shim(cursor: Cursor[TupleRow], schema: str, select: str) -> None:
    """Create ``schema.pg_stat_statements`` and put it first on the search_path.

    Shadowing the extension's view is how the version-specific branches can be
    driven on one server. ``SET LOCAL`` keeps it inside this test's
    transaction, so the real extension stays intact for everyone else.
    """
    from psycopg import sql

    identifier = sql.Identifier(schema)
    cursor.execute(sql.SQL("CREATE SCHEMA {}").format(identifier))
    cursor.execute(
        sql.SQL("CREATE VIEW {schema}.pg_stat_statements AS {select}").format(
            schema=identifier, select=sql.SQL(select)
        )
    )
    cursor.execute(sql.SQL("SET LOCAL search_path = {}, public").format(identifier))


def test_history_supports_the_pre_13_total_time_columns(
    pg_cursor: Cursor[TupleRow],
) -> None:
    """The legacy branch has to build SQL that a server will actually run.

    ``total_time``/``mean_time``/``max_time`` are the names PostgreSQL 12 and
    older use. No modern server exposes them, so the only way to prove that
    branch is not dead-on-arrival is to shadow the view with one that does.
    """
    _run_slow_statement(pg_cursor)
    _install_pgss_shim(
        pg_cursor,
        "dp_it_pgss_legacy",
        """
        SELECT
            calls,
            total_exec_time AS total_time,
            mean_exec_time AS mean_time,
            max_exec_time AS max_time,
            query
        FROM public.pg_stat_statements
        """,
    )

    rows = fetch_query_history_postgres(pg_cursor, min_seconds=0, limit=1000)

    assert rows
    marker = next((row for row in rows if _HISTORY_MARKER in row.query_text), None)
    assert marker is not None
    # Same millisecond-to-second conversion as the modern branch.
    assert marker.max_s >= 0.4


def test_history_rejects_an_incompatible_pg_stat_statements(
    pg_cursor: Cursor[TupleRow],
) -> None:
    """Missing timing columns raise ValidationError instead of bad SQL."""
    _install_pgss_shim(
        pg_cursor,
        "dp_it_pgss_broken",
        "SELECT 1::bigint AS calls, 'x'::text AS query",
    )

    with pytest.raises(ValidationError, match="pg_stat_statements"):
        fetch_query_history_postgres(pg_cursor, min_seconds=0, limit=10)


def test_history_without_the_extension_raises_undefined_table(
    pg_cursor: Cursor[TupleRow],
) -> None:
    """A server without pg_stat_statements degrades to a clean driver error.

    ``dp db long-queries --history`` runs inside ``db_session``, which catches
    ``psycopg.Error`` and exits with a message, so this is the failure mode the
    CLI's graceful degradation depends on. Pinned here because the alternative
    — a stray ``AttributeError`` on ``cursor.description`` — would surface as a
    traceback instead.
    """
    import psycopg

    conn = pg_cursor.connection
    with pytest.raises(psycopg.errors.UndefinedTable) as excinfo, conn.transaction():
        # An empty user search_path is indistinguishable, to the query, from a
        # server where the extension was never created.
        pg_cursor.execute("SET LOCAL search_path = pg_catalog")
        fetch_query_history_postgres(pg_cursor, min_seconds=0, limit=10)

    assert "pg_stat_statements" in str(excinfo.value)
