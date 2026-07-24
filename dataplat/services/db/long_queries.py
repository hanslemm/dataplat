"""Scan ``sys_query_history`` for long-running and failed Redshift queries.

The default *triage* view answers "what is hurting the cluster, now or
recently?" — currently-running queries, recently-finished slow queries, and
recent failures. ``running_only`` narrows it to a live snapshot.

Column naming in ``sys_query_history`` differs across Redshift variants, so
the relevant columns are discovered from the result-set description rather
than assumed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from dataplat.core.errors import ValidationError

# Statuses that mean a query is still consuming cluster resources.
RUNNING_STATUSES: tuple[str, ...] = ("running", "queued")

# Statuses for a query that did not finish cleanly. Matched case-insensitively.
FAILURE_STATUSES: tuple[str, ...] = ("failed", "aborted", "canceled", "cancelled")


@dataclass(frozen=True)
class LongQueryRow:
    """One query surfaced by a long-queries scan.

    ``session_id`` is the id ``dp db kill`` acts on: the backend PID on
    Postgres, the session/process id on Redshift.
    """

    query_id: str
    user_name: str
    db_name: str
    status: str
    start_time: Any
    elapsed_s: int
    query_text: str
    session_id: str = ""


def _quote(name: str) -> str:
    """Quote a SQL identifier, escaping embedded double quotes."""
    return '"' + name.replace('"', '""') + '"'


def _pick(column_names: set[str], *candidates: str) -> str | None:
    """Return the first candidate that exists in ``column_names``, else None."""
    for candidate in candidates:
        if candidate in column_names:
            return candidate
    return None


def _status_in(status_expr: str, statuses: tuple[str, ...]) -> str:
    """SQL: case-insensitive membership test against literal statuses.

    The status values are hard-coded constants, never user input, so they
    are inlined as literals rather than bound parameters.
    """
    literals = ", ".join(f"'{s}'" for s in statuses)
    return f"LOWER({status_expr}) IN ({literals})"


def build_long_queries_query(
    column_names: set[str],
    *,
    min_seconds: int,
    limit: int,
    cutoff: datetime,
    running_only: bool,
) -> tuple[str, tuple[Any, ...]]:
    """Build the ``sys_query_history`` scan SQL and its positional params.

    With ``running_only`` the scan returns only queries running or queued
    right now that have been alive at least ``min_seconds`` — a live
    snapshot with no time bound.

    Otherwise (the default triage view) the scan also returns recently
    finished slow queries and recent failures: a query qualifies when it is
    running/queued OR ran at least ``min_seconds`` and started on or after
    ``cutoff``, OR it failed and started on or after ``cutoff`` (failures
    show regardless of duration). Results are ordered with live queries
    first, then by elapsed time descending.

    ``column_names`` is the lower-cased column set of ``sys_query_history``.
    Raises :class:`ValidationError` if no start-time column is present.
    """
    start_col = _pick(column_names, "start_time", "starttime")
    if not start_col:
        raise ValidationError(
            "sys_query_history is missing a start_time/starttime column."
        )

    query_id_col = _pick(column_names, "query_id", "queryid", "query")
    user_col = _pick(
        column_names, "user_name", "username", "user", "user_id", "userid"
    )
    db_col = _pick(column_names, "database_name", "database", "dbname")
    status_col = _pick(column_names, "status", "state")
    end_col = _pick(column_names, "end_time", "endtime")
    text_col = _pick(column_names, "query_text", "query", "text")

    start_ref = _quote(start_col)
    end_ref = _quote(end_col) if end_col else None
    elapsed_expr = (
        f"DATEDIFF(second, {start_ref}, COALESCE({end_ref}, GETDATE()))"
        if end_ref
        else f"DATEDIFF(second, {start_ref}, GETDATE())"
    )

    query_id_expr = (
        f"CAST({_quote(query_id_col)} AS VARCHAR)"
        if query_id_col
        else "''::VARCHAR"
    )
    user_expr = (
        f"CAST({_quote(user_col)} AS VARCHAR)" if user_col else "''::VARCHAR"
    )
    db_expr = f"CAST({_quote(db_col)} AS VARCHAR)" if db_col else "''::VARCHAR"

    status_raw = (
        f"CAST({_quote(status_col)} AS VARCHAR)" if status_col else None
    )
    if status_raw is not None:
        status_expr = status_raw
    elif end_ref:
        status_expr = (
            f"CASE WHEN {end_ref} IS NULL THEN 'running' ELSE 'completed' END"
        )
    else:
        status_expr = "'unknown'"

    text_expr = (
        "LEFT(REGEXP_REPLACE(CAST("
        f"{_quote(text_col)} AS VARCHAR), '\\\\s+', ' '), 180)"
        if text_col
        else "''::VARCHAR"
    )

    session_col = _pick(column_names, "session_id", "pid", "process")
    session_expr = (
        f"CAST({_quote(session_col)} AS VARCHAR)" if session_col else "''::VARCHAR"
    )

    # SQL boolean that is true while a query still occupies the cluster.
    if status_raw is not None:
        is_running: str | None = _status_in(status_raw, RUNNING_STATUSES)
    elif end_ref:
        is_running = f"{end_ref} IS NULL"
    else:
        is_running = None

    params: list[Any] = []
    if running_only:
        where = f"{elapsed_expr} >= %s"
        params.append(min_seconds)
        if is_running is not None:
            where = f"{where} AND ({is_running})"
    else:
        live_or_recent = (
            f"({is_running} OR {start_ref} >= %s)"
            if is_running is not None
            else f"{start_ref} >= %s"
        )
        branches = [f"({elapsed_expr} >= %s AND {live_or_recent})"]
        params.extend([min_seconds, cutoff])
        if status_raw is not None:
            branches.append(
                f"({start_ref} >= %s "
                f"AND {_status_in(status_raw, FAILURE_STATUSES)})"
            )
            params.append(cutoff)
        where = " OR ".join(branches)

    order_rank = (
        f"CASE WHEN {is_running} THEN 0 ELSE 1 END"
        if is_running is not None
        else "1"
    )

    sql = f"""
        SELECT
            {query_id_expr} AS query_id,
            {user_expr} AS user_name,
            {db_expr} AS db_name,
            {status_expr} AS status,
            {start_ref} AS start_time,
            {elapsed_expr} AS elapsed_s,
            {text_expr} AS query_text,
            {session_expr} AS session_id
        FROM sys_query_history
        WHERE {where}
        ORDER BY {order_rank}, elapsed_s DESC
        LIMIT %s
    """
    params.append(limit)
    return sql, tuple(params)


def fetch_long_queries(
    cursor: Any,
    *,
    min_seconds: int,
    limit: int,
    cutoff: datetime,
    running_only: bool,
) -> list[LongQueryRow]:
    """Run the long-queries scan on an open cursor and return typed rows.

    ``cutoff`` is the look-back boundary for the triage view; it is unused
    when ``running_only`` is set.
    """
    cursor.execute("SELECT * FROM sys_query_history LIMIT 0")
    column_names = {
        desc.name.lower() for desc in (cursor.description or [])
    }
    sql, params = build_long_queries_query(
        column_names,
        min_seconds=min_seconds,
        limit=limit,
        cutoff=cutoff,
        running_only=running_only,
    )
    cursor.execute(sql, params)
    return [
        LongQueryRow(
            query_id=str(row[0]),
            user_name=str(row[1]),
            db_name=str(row[2]),
            status=str(row[3]),
            start_time=row[4],
            elapsed_s=int(row[5]) if row[5] is not None else 0,
            query_text=str(row[6]) if row[6] is not None else "",
            session_id=str(row[7]) if len(row) > 7 and row[7] is not None else "",
        )
        for row in cursor.fetchall()
    ]


# --- Postgres (pg_stat_activity) -------------------------------------------

_PG_ACTIVITY_SQL = """
    SELECT
        CAST(pid AS VARCHAR) AS query_id,
        COALESCE(usename, '') AS user_name,
        COALESCE(datname, '') AS db_name,
        COALESCE(state, '') AS status,
        query_start AS start_time,
        CAST(EXTRACT(EPOCH FROM (now() - query_start)) AS INT) AS elapsed_s,
        LEFT(REGEXP_REPLACE(query, '\\s+', ' ', 'g'), 180) AS query_text,
        CAST(pid AS VARCHAR) AS session_id
    FROM pg_stat_activity
    WHERE state IS NOT NULL
      AND state <> 'idle'
      AND pid <> pg_backend_pid()
      AND query_start IS NOT NULL
      AND now() - query_start >= make_interval(secs => %s)
    ORDER BY elapsed_s DESC
    LIMIT %s
"""


def fetch_long_queries_postgres(
    cursor: Any,
    *,
    min_seconds: int,
    limit: int,
) -> list[LongQueryRow]:
    """Live snapshot of non-idle backends running at least ``min_seconds``.

    Postgres keeps no built-in finished-query history, so unlike the
    Redshift triage view this is always a snapshot of what is running now.
    """
    cursor.execute(_PG_ACTIVITY_SQL, (min_seconds, limit))
    return [
        LongQueryRow(
            query_id=str(row[0]),
            user_name=str(row[1]),
            db_name=str(row[2]),
            status=str(row[3]),
            start_time=row[4],
            elapsed_s=int(row[5]) if row[5] is not None else 0,
            query_text=str(row[6]) if row[6] is not None else "",
            session_id=str(row[7]) if row[7] is not None else "",
        )
        for row in cursor.fetchall()
    ]


@dataclass(frozen=True)
class QueryHistoryRow:
    """One aggregated statement from ``pg_stat_statements``."""

    calls: int
    total_s: float
    mean_s: float
    max_s: float
    query_text: str


def fetch_query_history_postgres(
    cursor: Any,
    *,
    min_seconds: int,
    limit: int,
) -> list[QueryHistoryRow]:
    """Aggregate slow statements from ``pg_stat_statements``.

    Column names changed across pg_stat_statements versions
    (``total_time`` -> ``total_exec_time``), so they are discovered from the
    result-set description. Raises :class:`ValidationError` when the
    extension's columns are missing/incompatible.
    """
    cursor.execute("SELECT * FROM pg_stat_statements LIMIT 0")
    column_names = {desc.name.lower() for desc in (cursor.description or [])}

    total_col = _pick(column_names, "total_exec_time", "total_time")
    mean_col = _pick(column_names, "mean_exec_time", "mean_time")
    max_col = _pick(column_names, "max_exec_time", "max_time")
    query_col = _pick(column_names, "query")
    calls_col = _pick(column_names, "calls")

    if not all([total_col, mean_col, max_col, query_col, calls_col]):
        raise ValidationError(
            "pg_stat_statements columns are missing or incompatible."
        )
    assert total_col and mean_col and max_col and query_col and calls_col

    sql = f"""
        SELECT
            {_quote(calls_col)}::bigint AS calls,
            ({_quote(total_col)} / 1000.0) AS total_s,
            ({_quote(mean_col)} / 1000.0) AS mean_s,
            ({_quote(max_col)} / 1000.0) AS max_s,
            LEFT(
                REGEXP_REPLACE({_quote(query_col)}, '\\s+', ' ', 'g'),
                180
            ) AS query_text
        FROM pg_stat_statements
        WHERE ({_quote(max_col)} / 1000.0) >= %s
        ORDER BY {_quote(max_col)} DESC
        LIMIT %s
    """
    cursor.execute(sql, (min_seconds, limit))
    return [
        QueryHistoryRow(
            calls=int(row[0]),
            total_s=float(row[1]),
            mean_s=float(row[2]),
            max_s=float(row[3]),
            query_text=str(row[4]) if row[4] is not None else "",
        )
        for row in cursor.fetchall()
    ]


# --- kill / cancel -----------------------------------------------------------


def cancel_query_postgres(cursor: Any, pid: int, *, terminate: bool) -> bool:
    """Cancel (or terminate) a Postgres backend. Returns the server's verdict."""
    fn = "pg_terminate_backend" if terminate else "pg_cancel_backend"
    cursor.execute(f"SELECT {fn}(%s)", (pid,))
    row = cursor.fetchone()
    return bool(row and row[0])


def cancel_query_redshift(cursor: Any, pid: int) -> None:
    """Cancel a Redshift query by session/process id.

    ``CANCEL`` takes no bound parameters; ``pid`` is validated as an int
    by the caller, so inlining it is safe.
    """
    cursor.execute(f"CANCEL {int(pid)}")
