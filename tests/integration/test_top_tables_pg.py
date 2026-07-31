"""``dataplat.services.db.top_tables`` against a live PostgreSQL server.

Two things in this module are impossible to validate with a fake cursor. The
first is the catalog read itself: ``pg_class``/``pg_total_relation_size``,
``reltuples``'s negative "never analyzed" sentinel, and the relkind filter
only mean something when a real server answers. The second is the
``LIKE ... ESCAPE '#'`` clause built by ``_build_schema_where``: a fake can
confirm the string contains a backslash, but only PostgreSQL can say whether a
schema whose name literally contains ``_``, ``%`` or ``\\`` still matches
exactly one prefix.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from dataplat.services.db._like import LIKE_ESCAPE_CLAUSE
from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.top_tables import (
    _build_schema_where,
    drop_statement,
    fetch_top_tables,
)
from tests.integration.conftest import SAMPLE_RELATIONS

if TYPE_CHECKING:
    from collections.abc import Iterator

    from psycopg import Cursor
    from psycopg.rows import TupleRow

pytestmark = pytest.mark.integration


# top_tables ranks storage, so it reads relkinds that occupy disk: ordinary
# tables, partitioned parents and matviews. Views and sequences are out.
_RANKED_KINDS = frozenset({"r", "p", "m"})
_EXPECTED_RANKED = frozenset(
    name for name, kind in SAMPLE_RELATIONS.items() if kind in _RANKED_KINDS
)


def _total_relation_size(cursor: Cursor[TupleRow], schema: str, name: str) -> int:
    """Size of one relation, read independently of the code under test."""
    # format() is variadic "any", so the placeholders need an explicit type or
    # the server cannot infer one for the bound parameters.
    cursor.execute(
        "SELECT pg_total_relation_size("
        "format('%%I.%%I', %s::text, %s::text)::regclass)",
        (schema, name),
    )
    row = cursor.fetchone()
    assert row is not None
    return int(row[0])


def _matched_total(cursor: Cursor[TupleRow], schema: str) -> tuple[int, int]:
    """``(bytes, count)`` for one schema, computed without the service SQL."""
    cursor.execute(
        """
        SELECT COALESCE(SUM(pg_total_relation_size(c.oid)), 0)::bigint, COUNT(*)
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s
          AND c.relkind IN ('r', 'p', 'm')
        """,
        (schema,),
    )
    row = cursor.fetchone()
    assert row is not None
    return int(row[0]), int(row[1])


def _relation_exists(cursor: Cursor[TupleRow], schema: str, name: str) -> bool:
    """True while ``schema.name`` is still in ``pg_class``."""
    cursor.execute(
        """
        SELECT 1
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relname = %s
        """,
        (schema, name),
    )
    return cursor.fetchone() is not None


# --- the ranking itself ----------------------------------------------------


def test_ranking_reports_real_sizes_kinds_and_owners(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    """Every projected column is checked against the server, not a stub row.

    Sizes and the matched totals are recomputed here with an independent
    query, so a wrong column (``pg_relation_size`` instead of
    ``pg_total_relation_size``, say) shows up as a mismatch rather than as a
    plausible-looking number.
    """
    result = fetch_top_tables(pg_cursor, SqlEngine.postgresql, [sample_schema], 50)

    assert {row.name for row in result.rows} == set(_EXPECTED_RANKED)
    # Storage-free relkinds must not be ranked at all.
    assert "active_customers" not in {row.name for row in result.rows}  # view
    assert "invoice_number_seq" not in {row.name for row in result.rows}  # sequence

    by_name = {row.name: row for row in result.rows}
    for name, row in by_name.items():
        assert row.schema == sample_schema
        assert row.kind == SAMPLE_RELATIONS[name]
        assert row.owner is not None
        assert row.size_bytes == _total_relation_size(pg_cursor, sample_schema, name)

    expected_bytes, expected_count = _matched_total(pg_cursor, sample_schema)
    assert result.matched_bytes == expected_bytes
    assert result.matched_count == expected_count == len(_EXPECTED_RANKED)
    # pg_database_size is the reporting denominator, so it has to be real and
    # at least as large as the slice we matched.
    assert result.disk_bytes >= result.matched_bytes > 0

    sizes = [row.size_bytes for row in result.rows]
    assert sizes == sorted(sizes, reverse=True)

    # A partitioned parent stores nothing itself; its children carry the bytes.
    # That is what keeps matched_bytes from double-counting a partition set.
    assert by_name["events"].size_bytes == 0


def test_ranking_reports_analyzed_estimates_and_nulls_the_sentinel(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    """``reltuples`` is a float sentinel, not a row count, until ANALYZE runs.

    ``sample_schema`` ANALYZEs its tables and leaves the matview alone, which
    is exactly the pair needed to prove both halves of
    ``CASE WHEN reltuples < 0 THEN NULL``: analyzed relations report their real
    row count, the never-analyzed one reports NULL instead of -1.
    """
    result = fetch_top_tables(pg_cursor, SqlEngine.postgresql, [sample_schema], 50)
    by_name = {row.name: row for row in result.rows}

    assert by_name["customers"].row_estimate == 40
    assert by_name["orgs"].row_estimate == 3
    assert by_name["secrets"].row_estimate == 2
    assert by_name["customers_per_org"].row_estimate is None


def test_limit_caps_rows_without_changing_the_totals(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    """Top-N is a display cap; the totals stay the whole matched universe."""
    full = fetch_top_tables(pg_cursor, SqlEngine.postgresql, [sample_schema], 50)
    capped = fetch_top_tables(pg_cursor, SqlEngine.postgresql, [sample_schema], 2)

    assert len(capped.rows) == 2
    assert capped.rows == full.rows[:2]
    assert capped.matched_bytes == full.matched_bytes
    assert capped.matched_count == full.matched_count == len(_EXPECTED_RANKED)


def test_no_matching_schema_still_reports_the_real_disk_size(
    pg_cursor: Cursor[TupleRow],
) -> None:
    """Zero matches must not zero out the denominator.

    The totals query runs even with nothing matched, so this exercises the
    ``COALESCE(SUM(...), 0)`` branch and ``pg_database_size`` against a real
    server rather than a queued fake row.
    """
    result = fetch_top_tables(
        pg_cursor, SqlEngine.postgresql, ["dp_it_no_such_prefix_"], 10
    )

    assert result.rows == []
    assert result.matched_bytes == 0
    assert result.matched_count == 0
    assert result.disk_bytes > 0


# --- the LIKE / ESCAPE clause ---------------------------------------------

# Each entry is a LIKE metacharacter that must be matched literally, plus a
# readable label for the test id.
_METACHARS: tuple[tuple[str, str], ...] = (
    ("_", "underscore"),
    ("%", "percent"),
    ("\\", "backslash"),
)


@pytest.fixture
def metachar_schemas(pg_cursor: Cursor[TupleRow]) -> Iterator[str]:
    """Create one schema per LIKE metacharacter plus a shared decoy; yield base.

    Layout, for base ``B``: ``B_hit``, ``B%hit``, ``B\\hit`` and ``BXhit``. Any
    prefix ``B<meta>`` must select exactly its own schema — an unescaped
    ``_`` would also match all three others, an unescaped ``%`` would match
    everything, and an unescaped ``\\`` (the ESCAPE character) would make the
    pattern quote the following character instead of matching a backslash.
    """
    from psycopg import sql

    # The pid keeps the base unique against a parallel run; the objects live in
    # this test's transaction and vanish on its rollback.
    base = f"dp_it_esc{os.getpid()}"
    names = [f"{base}{meta}hit" for meta, _ in _METACHARS] + [f"{base}Xhit"]
    for name in names:
        pg_cursor.execute(
            sql.SQL("CREATE SCHEMA {schema}").format(schema=sql.Identifier(name))
        )
        pg_cursor.execute(
            sql.SQL("CREATE TABLE {schema}.probe (id int)").format(
                schema=sql.Identifier(name)
            )
        )
        pg_cursor.execute(
            sql.SQL("INSERT INTO {schema}.probe SELECT generate_series(1, 10)").format(
                schema=sql.Identifier(name)
            )
        )
    yield base


@pytest.mark.parametrize(("meta", "label"), _METACHARS, ids=[m[1] for m in _METACHARS])
def test_schema_prefix_matches_a_like_metacharacter_literally(
    pg_cursor: Cursor[TupleRow], metachar_schemas: str, meta: str, label: str
) -> None:
    """A prefix ending in ``meta`` selects only the schema containing it."""
    result = fetch_top_tables(
        pg_cursor, SqlEngine.postgresql, [f"{metachar_schemas}{meta}"], 50
    )

    assert {row.schema for row in result.rows} == {f"{metachar_schemas}{meta}hit"}
    assert result.matched_count == 1
    assert result.rows[0].size_bytes is not None and result.rows[0].size_bytes > 0


def test_several_prefixes_are_ored_together(
    pg_cursor: Cursor[TupleRow], metachar_schemas: str
) -> None:
    """Multiple prefixes union their matches, each still escaped separately."""
    result = fetch_top_tables(
        pg_cursor,
        SqlEngine.postgresql,
        [f"{metachar_schemas}_", f"{metachar_schemas}%"],
        50,
    )

    assert {row.schema for row in result.rows} == {
        f"{metachar_schemas}_hit",
        f"{metachar_schemas}%hit",
    }
    assert result.matched_count == 2


# --- the generated DROP ----------------------------------------------------


def test_generated_drop_removes_a_real_table_and_a_real_matview(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    """The suggested statement is executable, and it drops the right thing."""
    result = fetch_top_tables(pg_cursor, SqlEngine.postgresql, [sample_schema], 50)
    by_name = {row.name: row for row in result.rows}

    matview = by_name["customers_per_org"]
    assert drop_statement(matview).startswith("DROP MATERIALIZED VIEW IF EXISTS")
    pg_cursor.execute(drop_statement(matview))
    assert not _relation_exists(pg_cursor, sample_schema, "customers_per_org")

    table = by_name["secrets"]
    assert drop_statement(table).startswith("DROP TABLE IF EXISTS")
    pg_cursor.execute(drop_statement(table))
    assert not _relation_exists(pg_cursor, sample_schema, "secrets")


def test_dropping_a_matview_as_a_table_is_refused_by_the_server(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    """Proof the ``kind == 'm'`` branch matters rather than being cosmetic.

    ``IF EXISTS`` does not soften a wrong object type: PostgreSQL rejects
    ``DROP TABLE`` on a materialized view outright.
    """
    import psycopg

    from dataplat.services.db.top_tables import TopTableRow

    mislabelled = TopTableRow(sample_schema, "customers_per_org", "r", None, None, 0)
    conn = pg_cursor.connection
    with pytest.raises(psycopg.errors.WrongObjectType), conn.transaction():
        pg_cursor.execute(drop_statement(mislabelled))

    assert _relation_exists(pg_cursor, sample_schema, "customers_per_org")


def test_generated_drop_is_idempotent_and_quotes_hostile_names(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    """A name with an embedded double quote survives the round trip.

    Quoting is only really tested when the server parses the result: an
    unescaped quote would end the identifier and turn the rest into syntax.
    """
    from psycopg import sql

    hostile = 'we"ird %_table'
    pg_cursor.execute(
        sql.SQL("CREATE TABLE {schema}.{name} (id int)").format(
            schema=sql.Identifier(sample_schema), name=sql.Identifier(hostile)
        )
    )

    result = fetch_top_tables(pg_cursor, SqlEngine.postgresql, [sample_schema], 50)
    row = next(r for r in result.rows if r.name == hostile)

    statement = drop_statement(row)
    pg_cursor.execute(statement)
    assert not _relation_exists(pg_cursor, sample_schema, hostile)
    # IF EXISTS is what makes a suggested statement safe to paste twice.
    pg_cursor.execute(statement)


@pytest.mark.integration
def test_like_escape_survives_redshift_string_literal_parsing(pg_dsn: str) -> None:
    """The closest thing to Redshift coverage this suite can have.

    Redshift runs with ``standard_conforming_strings`` off, and that single
    setting is what made ``ESCAPE '\\'`` a syntax error there while every test
    here passed: PostgreSQL has it on and parses the same text fine. A session
    started with the Redshift setting reproduces the failure exactly, so the
    engine nobody can reach in CI gets one real guard.

    Proven before the fix::

        ERROR:  unterminated quoted string at or near "'\\'"
    """
    psycopg = pytest.importorskip("psycopg")

    # options= applies before the first statement is parsed. Setting it with a
    # SET inside the same batch is too late -- the whole batch is parsed first,
    # which is why an earlier attempt at this test wrongly passed.
    with (
        psycopg.connect(pg_dsn, options="-c standard_conforming_strings=off") as conn,
        conn.cursor() as cursor,
    ):
        cursor.execute("SHOW standard_conforming_strings")
        row = cursor.fetchone()
        assert row is not None and row[0] == "off", "precondition: setting is off"

        clause, params = _build_schema_where("n.nspname", ["dev_"])
        # The production clause must carry the escape and must parse here.
        assert LIKE_ESCAPE_CLAUSE in clause
        cursor.execute(
            f"SELECT 1 WHERE 'dev_x' LIKE %s {LIKE_ESCAPE_CLAUSE}", (params[0],)
        )
        assert cursor.fetchone() is not None, "the escaped pattern stopped matching"

        # And the underscore must still be literal, not a wildcard.
        cursor.execute(
            f"SELECT 1 WHERE 'devXx' LIKE %s {LIKE_ESCAPE_CLAUSE}", (params[0],)
        )
        assert cursor.fetchone() is None, "the underscore behaved as a wildcard"


@pytest.mark.integration
def test_backslash_escape_would_fail_under_the_redshift_setting(pg_dsn: str) -> None:
    """Why the escape character is ``#``: the old clause is a syntax error.

    Pins the reason rather than the choice, so nobody "simplifies" it back.
    """
    psycopg = pytest.importorskip("psycopg")

    with (
        psycopg.connect(pg_dsn, options="-c standard_conforming_strings=off") as conn,
        conn.cursor() as cursor,
    ):
        with pytest.raises(psycopg.errors.SyntaxError) as excinfo:
            cursor.execute(r"SELECT 1 WHERE 'dev_x' LIKE 'dev\_%' ESCAPE '\'")
        assert "unterminated quoted string" in str(excinfo.value)
