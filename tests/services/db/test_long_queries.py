from __future__ import annotations

from datetime import UTC, datetime

import pytest

from dataplat.core.errors import ValidationError
from dataplat.services.db.long_queries import (
    FAILURE_STATUSES,
    LongQueryRow,
    build_long_queries_query,
    fetch_long_queries,
    fetch_query_history_postgres,
)

# A representative sys_query_history column set.
_COLUMNS = {
    "query_id",
    "user_name",
    "database_name",
    "status",
    "start_time",
    "end_time",
    "query_text",
}

_CUTOFF = datetime(2026, 5, 21, 6, 0, 0, tzinfo=UTC)


class _Column:
    """Stub of a psycopg result-set column description entry."""

    def __init__(self, name: str) -> None:
        self.name = name


class FakeCursor:
    """Cursor stub: serves a fixed column set then a fixed row set."""

    def __init__(self, *, columns: list[str], rows: list[tuple]) -> None:
        self._columns = columns
        self._rows = rows
        self.description: list[_Column] | None = None
        self.executed: list[tuple[str, tuple | None]] = []

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self.executed.append((sql, tuple(params) if params is not None else None))
        if "limit 0" in sql.lower():
            self.description = [_Column(c) for c in self._columns]

    def fetchall(self) -> list[tuple]:
        return self._rows


# --- build_long_queries_query --------------------------------------------


def test_running_only_is_a_live_snapshot_without_window() -> None:
    sql, params = build_long_queries_query(
        _COLUMNS, min_seconds=60, limit=20, cutoff=_CUTOFF, running_only=True
    )
    assert "FROM sys_query_history" in sql
    assert "IN ('running', 'queued')" in sql
    # No failure branch and no look-back window in the live view.
    assert "'failed'" not in sql
    assert params == (60, 20)


def test_triage_includes_look_back_window_and_failures() -> None:
    sql, params = build_long_queries_query(
        _COLUMNS, min_seconds=120, limit=10, cutoff=_CUTOFF, running_only=False
    )
    # Failures of any duration, within the window.
    assert "'failed', 'aborted', 'canceled', 'cancelled'" in sql
    # min_seconds, then a cutoff for the long branch and one for failures.
    assert params == (120, _CUTOFF, _CUTOFF, 10)


def test_triage_keeps_live_queries_outside_the_window() -> None:
    # A running query must qualify even if it started before the cutoff.
    sql, _ = build_long_queries_query(
        _COLUMNS, min_seconds=60, limit=20, cutoff=_CUTOFF, running_only=False
    )
    assert "IN ('running', 'queued') OR \"start_time\" >=" in sql


def test_triage_orders_running_queries_first() -> None:
    sql, _ = build_long_queries_query(
        _COLUMNS, min_seconds=60, limit=20, cutoff=_CUTOFF, running_only=False
    )
    assert "ORDER BY CASE WHEN" in sql
    assert "THEN 0 ELSE 1 END, elapsed_s DESC" in sql


def test_missing_start_column_raises() -> None:
    with pytest.raises(ValidationError, match="start_time"):
        build_long_queries_query(
            {"query_id", "status"},
            min_seconds=60,
            limit=20,
            cutoff=_CUTOFF,
            running_only=False,
        )


def test_without_status_column_falls_back_to_end_time() -> None:
    columns = {
        "query_id",
        "user_name",
        "database_name",
        "start_time",
        "end_time",
        "query_text",
    }
    sql, params = build_long_queries_query(
        columns, min_seconds=60, limit=20, cutoff=_CUTOFF, running_only=False
    )
    # Failures cannot be detected without a status column.
    assert "'failed'" not in sql
    # Running detection falls back to an open end_time.
    assert '"end_time" IS NULL' in sql
    # Only one cutoff param — the long branch — no failure branch.
    assert params == (60, _CUTOFF, 20)


def test_failure_statuses_cover_the_known_terminal_states() -> None:
    assert FAILURE_STATUSES == ("failed", "aborted", "canceled", "cancelled")


# --- fetch_long_queries ---------------------------------------------------


def test_fetch_long_queries_introspects_then_maps_rows() -> None:
    started = datetime(2026, 5, 21, 8, 31, 8, tzinfo=UTC)
    cur = FakeCursor(
        columns=list(_COLUMNS),
        rows=[
            (1841201539, "m_fender", "demo_rs", "failed", started, 2820, "SELECT 1"),
        ],
    )
    result = fetch_long_queries(
        cur, min_seconds=60, limit=20, cutoff=_CUTOFF, running_only=False
    )

    assert result == [
        LongQueryRow(
            query_id="1841201539",
            user_name="m_fender",
            db_name="demo_rs",
            status="failed",
            start_time=started,
            elapsed_s=2820,
            query_text="SELECT 1",
        )
    ]
    # Introspection query runs first, then the scan.
    assert len(cur.executed) == 2
    assert "limit 0" in cur.executed[0][0].lower()


def test_fetch_long_queries_raises_on_unexpected_schema() -> None:
    cur = FakeCursor(columns=["query_id", "status"], rows=[])
    with pytest.raises(ValidationError):
        fetch_long_queries(
            cur, min_seconds=60, limit=20, cutoff=_CUTOFF, running_only=False
        )


# --- fetch_query_history_postgres -----------------------------------------

# A modern pg_stat_statements column set, trimmed to what the history query
# actually picks from: enough for discovery to resolve every lookup.
_PGSS_COLUMNS = ["calls", "total_exec_time", "mean_exec_time", "max_exec_time", "query"]


class _FakeDriverError(Exception):
    """Stand-in for a psycopg error, carrying only what is matched on.

    The service layer never imports psycopg, so the not-preloaded branch keys
    off the ``sqlstate`` attribute every DB-API driver exposes rather than an
    exception class. This fake keeps that branch covered on a machine without
    Docker, where no server that reproduces the misconfiguration exists.
    """

    def __init__(self, message: str, sqlstate: str) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class FakePgssCursor(FakeCursor):
    """Serves the pg_stat_statements column probe, then fails the real scan.

    That asymmetry *is* the defect being pinned: PostgreSQL answers
    ``SELECT * FROM pg_stat_statements LIMIT 0`` from the view definition
    without invoking the extension's C function, so column discovery succeeds
    on a server where every real read of the view fails.
    """

    def __init__(self, *, error: Exception) -> None:
        super().__init__(columns=list(_PGSS_COLUMNS), rows=[])
        self._error = error

    def execute(self, sql: str, params: tuple | None = None) -> None:
        super().execute(sql, params)
        if "limit 0" not in sql.lower():
            raise self._error


def test_history_translates_a_non_preloaded_extension() -> None:
    """SQLSTATE 55000 becomes a ValidationError naming the actual remedy."""
    cur = FakePgssCursor(
        error=_FakeDriverError(
            "pg_stat_statements must be loaded via shared_preload_libraries",
            "55000",
        )
    )

    with pytest.raises(ValidationError) as excinfo:
        fetch_query_history_postgres(cur, min_seconds=0, limit=10)

    message = str(excinfo.value)
    assert "shared_preload_libraries" in message
    # A reload does not load a preloaded library, so the text must say restart.
    assert "restart" in message.lower()
    # Discovery still ran and still resolved: the probe cannot see this problem,
    # which is why the translation has to sit on the scan.
    assert len(cur.executed) == 2
    assert "limit 0" in cur.executed[0][0].lower()
    assert isinstance(excinfo.value.__cause__, _FakeDriverError)


def test_history_does_not_swallow_unrelated_driver_errors() -> None:
    """Only 55000 is translated; anything else keeps its type and reaches the CLI.

    ``dp db long-queries --history`` runs inside ``db_session``, which handles
    ``psycopg.Error`` itself, so a broadened catch here would replace real
    connection failures with a misleading "not preloaded" message.
    """
    boom = _FakeDriverError("server closed the connection unexpectedly", "08006")
    cur = FakePgssCursor(error=boom)

    with pytest.raises(_FakeDriverError) as excinfo:
        fetch_query_history_postgres(cur, min_seconds=0, limit=10)

    assert excinfo.value is boom


def test_history_maps_rows_when_the_extension_works() -> None:
    """The happy path is untouched by the not-preloaded guard."""
    cur = FakeCursor(
        columns=list(_PGSS_COLUMNS),
        rows=[(3, 1.5, 0.5, 0.9, "SELECT 1")],
    )

    rows = fetch_query_history_postgres(cur, min_seconds=0, limit=10)

    assert [(r.calls, r.total_s, r.mean_s, r.max_s, r.query_text) for r in rows] == [
        (3, 1.5, 0.5, 0.9, "SELECT 1")
    ]
