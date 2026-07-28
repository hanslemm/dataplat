from __future__ import annotations

from pathlib import Path

import duckdb

from dataplat.services.db._like import like_escape
from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.top_tables import (
    SIZE_BASIS,
    TopTableRow,
    TopTablesResult,
    _build_schema_where,
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
    assert like_escape("dev_") == "dev\\_"
    assert like_escape("a%b") == "a\\%b"
    assert like_escape("a\\b") == "a\\\\b"
    assert like_escape("plain") == "plain"


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


# =========================================================================
# DuckDB, against a real database.
#
# It is in-process and file-backed, so there is nothing to fake and a fake
# would have hidden the fact that shaped this whole branch: duckdb_tables()
# .estimated_size is a row count, not a byte size. Every test below opens a
# real DuckDB.
# =========================================================================


def _duckdb_warehouse(path: str | Path = ":memory:") -> duckdb.DuckDBPyConnection:
    """A database with two dev_ schemas, one non-matching schema, and a view."""
    conn = duckdb.connect(database=str(path))
    conn.execute("CREATE SCHEMA dev_alice")
    conn.execute("CREATE SCHEMA dev_bob")
    conn.execute("CREATE SCHEMA prod")
    conn.execute("CREATE TABLE dev_alice.big_fact(id BIGINT, note VARCHAR)")
    conn.execute("INSERT INTO dev_alice.big_fact SELECT i, 'x' FROM range(5000) t(i)")
    conn.execute("CREATE TABLE dev_alice.small(id INTEGER)")
    conn.execute("INSERT INTO dev_alice.small VALUES (1), (2)")
    conn.execute("CREATE TABLE dev_bob.tmp(a INTEGER)")
    conn.execute("CREATE TABLE prod.keepme(a INTEGER)")
    conn.execute("CREATE VIEW dev_alice.v_fact AS SELECT * FROM dev_alice.big_fact")
    return conn


def test_duckdb_estimated_size_is_a_row_count_not_bytes() -> None:
    """The probe that decided the DuckDB branch, pinned as a test.

    ``duckdb_tables().estimated_size`` is the cardinality estimate: a table of
    200k long strings and a table of 200k bigints report the same number, and
    ``pg_class.relpages`` is 0 for every relation. So there is no per-table byte
    size to read on this engine, and reporting ``estimated_size`` as a size
    would be a confident falsehood in the units a reader cares about. If a
    future DuckDB adds one, this test fails and the branch can be revisited.
    """
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE wide(id BIGINT, pad VARCHAR)")
    conn.execute(
        "INSERT INTO wide SELECT i, repeat('abcdefghij', 100) FROM range(20000) t(i)"
    )
    conn.execute("CREATE TABLE narrow(id BIGINT)")
    conn.execute("INSERT INTO narrow SELECT i FROM range(20000) t(i)")

    sizes = dict(
        conn.execute(
            "SELECT table_name, estimated_size FROM duckdb_tables() ORDER BY 1"
        ).fetchall()
    )
    assert sizes == {"narrow": 20000, "wide": 20000}

    pages = conn.execute(
        "SELECT DISTINCT relpages FROM pg_catalog.pg_class WHERE relname IN "
        "('wide', 'narrow')"
    ).fetchall()
    assert pages == [(0,)]
    conn.close()


def test_fetch_top_tables_duckdb_ranks_by_rows_and_reports_no_size() -> None:
    conn = _duckdb_warehouse()
    result = fetch_top_tables(conn, SqlEngine.duckdb, ["dev_"], limit=10)

    # Ranked by estimated rows, because that is the only magnitude DuckDB has.
    assert [(r.schema, r.name, r.row_estimate) for r in result.rows] == [
        ("dev_alice", "big_fact", 5000),
        ("dev_alice", "small", 2),
        ("dev_bob", "tmp", 0),
    ]
    # Unknown, not zero: 0 B would claim the tables are empty.
    assert [r.size_bytes for r in result.rows] == [None, None, None]
    assert result.matched_bytes is None
    assert result.matched_count == 3
    # No owners and no materialized views on this engine, so kind is always 'r'
    # and drop_statement's DROP MATERIALIZED VIEW branch cannot fire.
    assert {r.kind for r in result.rows} == {"r"}
    assert {r.owner for r in result.rows} == {None}
    conn.close()


def test_fetch_top_tables_duckdb_excludes_views_and_other_schemas() -> None:
    """A view must not be ranked: the DROP this command generates is DROP TABLE."""
    conn = _duckdb_warehouse()
    result = fetch_top_tables(conn, SqlEngine.duckdb, ["dev_"], limit=10)

    names = {r.name for r in result.rows}
    assert "v_fact" not in names  # duckdb_tables() lists tables only
    assert "keepme" not in names  # prod does not match dev_
    conn.close()


def test_fetch_top_tables_duckdb_disk_bytes_is_the_file(tmp_path: Path) -> None:
    """disk_bytes comes from pragma_database_size, whose database_size is text.

    ``pragma_database_size()`` reports ``database_size`` as a human string
    ('512.0 KiB'), so the number has to be computed from
    ``block_size * total_blocks`` — and checked against ``stat``, not against
    another catalog read. It comes out slightly *under* the file, because
    DuckDB's fixed header is outside the counted blocks; the bound is one block,
    since a shortfall smaller than a block cannot be a block of data going
    unreported.
    """
    database = tmp_path / "w.duckdb"
    conn = _duckdb_warehouse(database)
    conn.execute("CHECKPOINT")
    result = fetch_top_tables(conn, SqlEngine.duckdb, ["dev_"], limit=10)
    block_size = conn.execute(
        "SELECT block_size FROM pragma_database_size()"
    ).fetchone()[0]
    conn.close()

    on_disk = database.stat().st_size
    assert 0 < result.disk_bytes <= on_disk
    assert on_disk - result.disk_bytes < block_size
    # Still no per-table size, even with a real file behind the database.
    assert result.matched_bytes is None


def test_fetch_top_tables_duckdb_memory_database_has_no_disk() -> None:
    conn = _duckdb_warehouse()
    result = fetch_top_tables(conn, SqlEngine.duckdb, ["dev_"], limit=10)
    # :memory: reports total_blocks = 0. Truthful, and the renderers must treat
    # 0 as "no denominator" rather than dividing by it.
    assert result.disk_bytes == 0
    conn.close()


def test_fetch_top_tables_duckdb_underscore_in_prefix_stays_literal() -> None:
    """ESCAPE '\\' works on DuckDB too — a `_` prefix must not match any char."""
    conn = _duckdb_warehouse()
    conn.execute("CREATE SCHEMA devXfake")
    conn.execute("CREATE TABLE devXfake.decoy(a INTEGER)")

    result = fetch_top_tables(conn, SqlEngine.duckdb, ["dev_"], limit=10)

    assert "decoy" not in {r.name for r in result.rows}
    assert result.matched_count == 3
    conn.close()


def test_fetch_top_tables_duckdb_ignores_an_attached_database(tmp_path: Path) -> None:
    """Only the current catalog is ranked, because the DROPs resolve against it.

    An ATTACHed database's tables share the schema namespace in
    ``duckdb_tables()`` output but not in a statement, so a
    ``DROP TABLE "dev_x"."t"`` generated from one would hit the wrong catalog —
    or nothing.
    """
    other = tmp_path / "other.duckdb"
    side = duckdb.connect(database=str(other))
    side.execute("CREATE SCHEMA dev_attached")
    side.execute("CREATE TABLE dev_attached.elsewhere(a INTEGER)")
    side.close()

    conn = _duckdb_warehouse()
    conn.execute(f"ATTACH '{other}' AS att")
    result = fetch_top_tables(conn, SqlEngine.duckdb, ["dev_"], limit=10)

    assert "elsewhere" not in {r.name for r in result.rows}
    assert result.matched_count == 3
    conn.close()


def test_fetch_top_tables_duckdb_no_match_keeps_bytes_unknown() -> None:
    conn = _duckdb_warehouse()
    result = fetch_top_tables(conn, SqlEngine.duckdb, ["nomatch_"], limit=10)

    assert result.rows == []
    assert result.matched_count == 0
    # Not 0: the engine did not report a size, and the empty-match shortcut must
    # not turn "unknown" into a number on the way out.
    assert result.matched_bytes is None
    conn.close()


def test_fetch_top_tables_duckdb_limit_caps_rows_not_totals() -> None:
    conn = _duckdb_warehouse()
    capped = fetch_top_tables(conn, SqlEngine.duckdb, ["dev_"], limit=1)

    assert [r.name for r in capped.rows] == ["big_fact"]
    assert capped.matched_count == 3
    conn.close()


def test_duckdb_binds_question_marks_and_would_reject_psycopg_style() -> None:
    """Why the marker is a dialect split rather than a shared constant."""
    clause, params = _build_schema_where("t.schema_name", ["dev_"], marker="?")
    assert clause == "t.schema_name LIKE ? ESCAPE '\\'"
    assert params == ["dev\\_%"]

    conn = duckdb.connect(":memory:")
    try:
        conn.execute("SELECT 1 WHERE 'a' LIKE %s", ["a"])
    except duckdb.Error as exc:
        assert "%" in str(exc)
    else:  # pragma: no cover - would mean DuckDB grew %s support
        raise AssertionError("DuckDB accepted a %s placeholder")
    conn.close()


def test_duckdb_drop_statements_are_valid_duckdb(tmp_path: Path) -> None:
    """The generated script, run against a real DuckDB, in one transaction."""
    database = tmp_path / "w.duckdb"
    conn = _duckdb_warehouse(database)
    conn.execute('CREATE TABLE dev_bob."has""quote"(a INTEGER)')
    result = fetch_top_tables(conn, SqlEngine.duckdb, ["dev_"], limit=10)

    conn.execute("BEGIN TRANSACTION")
    for row in result.rows:
        conn.execute(drop_statement(row))
    conn.execute("COMMIT")

    remaining = conn.execute(
        "SELECT table_name FROM duckdb_tables() "
        "WHERE database_name = current_database() ORDER BY 1"
    ).fetchall()
    assert remaining == [("keepme",)]

    # And the claim the generated script's header makes, verified here rather
    # than asserted in prose: DuckDB dropped dev_alice.big_fact while
    # dev_alice.v_fact still selected from it (enable_view_dependencies is off by
    # default in 1.5.5), leaving the view in the catalog and broken. The libpq
    # engines refuse that drop, which is why the two headers differ.
    assert conn.execute(
        "SELECT count(*) FROM duckdb_views() WHERE view_name = 'v_fact'"
    ).fetchone() == (1,)
    try:
        conn.execute("SELECT * FROM dev_alice.v_fact")
    except duckdb.Error as exc:
        assert "big_fact" in str(exc)
    else:  # pragma: no cover - would mean the view survived its table
        raise AssertionError("the view still resolves after its table was dropped")
    conn.close()


def test_size_basis_covers_every_engine() -> None:
    """A caller prints this; an engine missing from it would KeyError at render."""
    assert set(SIZE_BASIS) == set(SqlEngine)
    assert "estimated" in SIZE_BASIS[SqlEngine.duckdb]
    assert "pg_total_relation_size" in SIZE_BASIS[SqlEngine.postgresql]
