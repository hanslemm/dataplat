from __future__ import annotations

from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.top_tables import (
    TopTableRow,
    TopTablesResult,
    _build_schema_where,
    _like_escape,
    drop_statement,
    fetch_top_tables,
)


class FakeCursor:
    """Cursor stub — queues per-query row/rows results and records calls."""

    def __init__(
        self, *, one: list[tuple] | None = None, many: list[list[tuple]] | None = None
    ) -> None:
        self._one = list(one or [])
        self._many = list(many or [])
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql: str, params: tuple) -> None:
        self.executed.append((sql, tuple(params)))

    def fetchone(self):
        return self._one.pop(0) if self._one else None

    def fetchall(self):
        return self._many.pop(0) if self._many else []


def test_like_escape_preserves_literal_metacharacters() -> None:
    assert _like_escape("dev_") == "dev\\_"
    assert _like_escape("a%b") == "a\\%b"
    assert _like_escape("a\\b") == "a\\\\b"
    assert _like_escape("plain") == "plain"


def test_build_schema_where_joins_with_or_and_adds_percent() -> None:
    clause, params = _build_schema_where("n.nspname", ["dev_", "sandbox_"])
    assert clause == "n.nspname LIKE %s ESCAPE '\\' OR n.nspname LIKE %s ESCAPE '\\'"
    assert params == ["dev\\_%", "sandbox\\_%"]


def test_fetch_top_tables_postgres_runs_totals_then_rows() -> None:
    totals = (10_000_000, 42, 100_000_000)
    rows = [
        ("dev_alice", "big_fact", "r", "alice", 1_000_000, 6_000_000),
        ("dev_bob", "staging_raw", "m", "bob", 500, 4_000_000),
    ]
    cur = FakeCursor(one=[totals], many=[rows])
    result = fetch_top_tables(cur, SqlEngine.postgresql, ["dev_"], limit=5)

    assert result == TopTablesResult(
        rows=[
            TopTableRow("dev_alice", "big_fact", "r", "alice", 1_000_000, 6_000_000),
            TopTableRow("dev_bob", "staging_raw", "m", "bob", 500, 4_000_000),
        ],
        matched_bytes=10_000_000,
        matched_count=42,
        disk_bytes=100_000_000,
    )
    assert len(cur.executed) == 2
    totals_sql, totals_params = cur.executed[0]
    assert "pg_database_size(current_database())" in totals_sql
    assert "pg_total_relation_size" in totals_sql
    assert totals_params == ("dev\\_%",)
    rows_sql, rows_params = cur.executed[1]
    assert "ORDER BY size_bytes DESC" in rows_sql
    assert rows_params == ("dev\\_%", 5)


def test_fetch_top_tables_redshift_uses_svv_table_info() -> None:
    totals = (2048 * 1024 * 1024, 1, 10_000 * 1024 * 1024)
    rows = [("dev_x", "events", "r", None, 42, 2048 * 1024 * 1024)]
    cur = FakeCursor(one=[totals], many=[rows])
    result = fetch_top_tables(cur, SqlEngine.redshift, ["dev_", "sandbox_"], limit=10)
    assert result.rows == [
        TopTableRow("dev_x", "events", "r", None, 42, 2048 * 1024 * 1024)
    ]
    assert result.matched_bytes == 2048 * 1024 * 1024
    assert result.matched_count == 1
    assert result.disk_bytes == 10_000 * 1024 * 1024

    totals_sql, totals_params = cur.executed[0]
    assert "svv_table_info" in totals_sql
    # schema filter appears once (WHERE); disk_bytes subquery has no filter.
    assert totals_sql.count('"schema" LIKE %s') == 2
    assert totals_params == ("dev\\_%", "sandbox\\_%")

    rows_sql, rows_params = cur.executed[1]
    assert rows_params == ("dev\\_%", "sandbox\\_%", 10)


def test_fetch_top_tables_skips_rows_query_when_no_matches() -> None:
    cur = FakeCursor(one=[(0, 0, 100_000_000)], many=[])
    result = fetch_top_tables(cur, SqlEngine.postgresql, ["dev_"], limit=5)
    assert result == TopTablesResult(
        rows=[], matched_bytes=0, matched_count=0, disk_bytes=100_000_000
    )
    assert len(cur.executed) == 1  # totals only; no rows query


def test_fetch_top_tables_empty_inputs_do_not_query() -> None:
    cur = FakeCursor()
    assert (
        fetch_top_tables(cur, SqlEngine.postgresql, [], limit=10) == TopTablesResult()
    )
    assert (
        fetch_top_tables(cur, SqlEngine.postgresql, ["dev_"], limit=0)
        == TopTablesResult()
    )
    assert cur.executed == []


def test_drop_statement_quotes_identifiers_and_picks_matview() -> None:
    table = TopTableRow("dev_a", "big_fact", "r", "alice", 1, 2)
    matview = TopTableRow("dev_a", "mv_x", "m", "alice", 1, 2)
    weird = TopTableRow('dev"a', 'has"quote', "r", None, 1, 2)

    assert drop_statement(table) == 'DROP TABLE IF EXISTS "dev_a"."big_fact";'
    assert drop_statement(matview) == 'DROP MATERIALIZED VIEW IF EXISTS "dev_a"."mv_x";'
    assert drop_statement(weird) == 'DROP TABLE IF EXISTS "dev""a"."has""quote";'
