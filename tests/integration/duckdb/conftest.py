"""Fixtures backing the integration suite against a real DuckDB database.

Why this exists is the same reason ``tests/integration/conftest.py`` exists:
every other test in the repo drives a hand-written fake cursor, which proves a
code path *called* ``execute`` but never that the SQL it built is valid. DuckDB
is the second real-SQL target in this project, so it is also the first time the
shared fetchers are asked whether they were engine-independent or merely
PostgreSQL-shaped.

Why there is no availability machinery
======================================

The PostgreSQL tier has ``DP_TEST_PG_DSN``/``DP_TEST_PG_REQUIRED``; the Redshift
tier has five ``DP_TEST_RS_*`` variables, a skip/require branch and a
disposability gate. **This tier deliberately has none of that, and its absence
is not an oversight.** Every one of those knobs answers a question that cannot
be asked here:

- *"Is the server reachable?"* — there is no server. DuckDB is a library that
  opens a file inside this process, so there is no host to be down, no port to
  be firewalled and no container to boot.
- *"Which database do we point at?"* — the fixtures create one, in pytest's
  ``tmp_path``. Nothing is discovered from the environment, so nothing can be
  misconfigured.
- *"May we mutate it?"* — the database is a throwaway file this test created and
  nothing else can see. There is no production DuckDB to protect, hence no
  read-only guard like :class:`~tests.integration.redshift.conftest.
  ReadOnlyCursor` and no ``DISPOSABLE`` affirmation.
- *"Do we skip or fail when it is unavailable?"* — the whole point of
  ``DP_TEST_*_REQUIRED`` is that a silently-skipping tier reports green while
  executing none of the SQL it exists to validate. Here there is nothing to be
  unavailable, so **these tests always run**: a skip path would be the only way
  to reintroduce the failure mode those variables were invented to prevent.

The one thing that *can* be missing is the ``duckdb`` package itself, which is
the optional ``duckdb`` extra. That is a broken development environment rather
than absent infrastructure — the documented setup is ``uv sync --group dev
--all-extras`` (CONTRIBUTING.md), and CI passes ``--all-extras`` — so it is a
setup **ERROR** on every test here, never a skip. The import goes through
:func:`~dataplat.services.db.connection.load_duckdb`, so the message a
contributor sees is the tool's own: "A duckdb target needs the duckdb package,
which is not installed: it is the 'duckdb' extra (dataplat[duckdb]).
Development checkout — run: uv sync --group dev --all-extras".

Why isolation is a fresh file per test, not BEGIN/ROLLBACK
=========================================================

DuckDB *does* roll back DDL, so the PostgreSQL harness's one-transaction-per-test
trick would work mechanically. It is not used, because two facts probed on
duckdb 1.5.5 would make the assertions vacuous:

1. **Catalog row counts read committed state.** A table created and filled
   inside an open transaction reports ``pg_class.reltuples = 0`` and
   ``duckdb_tables().estimated_size = 0`` for the whole transaction. Building
   the fixture in a transaction would silently zero every row-estimate and
   ranking assertion in ``dp db top-tables``.
2. **``pragma_database_size()`` reports ``0 bytes`` until ``CHECKPOINT``**, and
   ``CHECKPOINT`` cannot run inside a transaction that has written. So the
   whole-database size — the denominator for percentage-of-disk reporting —
   would also be zero.

Both are pinned by tests in ``test_duckdb_services.py`` so this decision can be
re-checked rather than re-derived. A fresh file per test costs a few
milliseconds and needs no cleanup logic at all: ``tmp_path`` removes it.

For the same reason the fixture does **not** run ``ANALYZE``: unlike PostgreSQL,
where ``reltuples`` is ``-1`` until something analyses the table, DuckDB's
estimate is the exact live row count and is correct immediately.

The database is a real file rather than ``:memory:`` because a file is what
users configure (``<PREFIX>_PATH=/data/warehouse.duckdb``), because file locking
is part of the behaviour under test, and because ``pragma_database_size()``
reports ``block_size = 0`` for an in-memory database — so a ``:memory:`` fixture
could not exercise the size path at all.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from dataplat.cli.db._common import DuckDbCursor, DuckDbSession, db_session
from dataplat.services.db.connection import DuckDbConnectionParams, load_duckdb

# The file name is part of the fixture's contract, because DuckDB derives the
# catalog name from the file stem: this database is `warehouse`, so
# `current_database()` answers 'warehouse'. That matters for a fixture author,
# not just for prose -- a schema whose name equals the database name becomes
# ambiguous, and `CREATE TABLE warehouse.t` then fails with "Ambiguous reference
# to catalog or schema" (probed on 1.5.5). None of the schemas below is called
# `warehouse` for exactly that reason.
DATABASE_FILENAME = "warehouse.duckdb"

# The schema the object-level tests describe. Named, not generated: the database
# is created per test and nothing else can see it, so the collision-proof
# identifiers the PostgreSQL harness needs (cluster-wide roles, a shared server)
# buy nothing here and only make failure messages harder to read.
SAMPLE_SCHEMA = "analytics"

# A second schema sharing SAMPLE_SCHEMA's prefix, so a `LIKE 'analytics%'` scan
# has more than one schema to find...
SAMPLE_STAGING_SCHEMA = "analytics_stg"

# ...and a third that must not match it, so a passing prefix test is evidence of
# filtering rather than of a query that returns everything.
SAMPLE_OTHER_SCHEMA = "reporting"

# The prefix that selects the first two schemas and rejects the third.
SAMPLE_SCHEMA_PREFIX = "analytics"


# Relation inventory the sample schema guarantees, keyed by qualified name with
# the ``pg_class.relkind`` DuckDB reports for it. Exported so tests can assert
# against the promise instead of re-hardcoding it. DuckDB fills relkind the same
# way PostgreSQL does for these five kinds, which is why `dp db describe` can
# resolve a target through pg_class on either engine.
SAMPLE_RELATIONS: dict[str, str] = {
    f"{SAMPLE_SCHEMA}.orgs": "r",  # PK, UNIQUE constraint, NOT NULL
    f"{SAMPLE_SCHEMA}.customers": "r",  # PK, FK, NOT NULL, CHECK, DEFAULT, comments
    f"{SAMPLE_SCHEMA}.active_customers": "v",  # view, with a comment
    f"{SAMPLE_SCHEMA}.customers_org_id_idx": "i",  # secondary index
    f"{SAMPLE_SCHEMA}.customers_email_key": "i",  # unique secondary index
    f"{SAMPLE_SCHEMA}.invoice_number_seq": "S",  # standalone sequence
    f"{SAMPLE_STAGING_SCHEMA}.customers_raw": "r",
    f"{SAMPLE_OTHER_SCHEMA}.unrelated": "r",
}

# Indexes beyond the ones the PRIMARY KEY and UNIQUE constraints create
# implicitly. DuckDB reports those implicit ones in duckdb_tables().index_count
# but not as named entries in pg_indexes, so only these two are assertable.
SAMPLE_INDEXES: tuple[str, ...] = ("customers_org_id_idx", "customers_email_key")

# Exact live row counts, which on DuckDB are also what `pg_class.reltuples` and
# `duckdb_tables().estimated_size` report -- see the module docstring on ANALYZE.
SAMPLE_ROW_COUNTS: dict[str, int] = {
    f"{SAMPLE_SCHEMA}.orgs": 3,
    f"{SAMPLE_SCHEMA}.customers": 40,
    f"{SAMPLE_STAGING_SCHEMA}.customers_raw": 7,
    f"{SAMPLE_OTHER_SCHEMA}.unrelated": 3,
}

# `status <> 'churned'` holds for every row whose id is not a multiple of 4.
SAMPLE_VIEW_ROWS = 30

SAMPLE_TABLE_COMMENT = "Customer master (integration fixture)."
SAMPLE_COLUMN_COMMENT = "Primary contact address."
SAMPLE_VIEW_COMMENT = "Non-churned customers."


# One statement per element, like the PostgreSQL harness: a failure then names
# the statement that broke instead of the whole script. Unlike that harness there
# is no `{s}` placeholder to format -- the schema names are constants, because a
# per-test database needs no per-test identifiers.
#
# Everything here was checked against duckdb 1.5.5. Two absences are deliberate
# rather than forgotten:
#
# * No `COMMENT ON SCHEMA`: DuckDB raises NotImplementedException ("Adding
#   comments to schemas is not implemented"), so a schema comment is a thing
#   `dp db describe <schema>` can never report on this engine.
# * No `ANALYZE`: the row estimates are already exact. See the module docstring.
_SAMPLE_DDL: tuple[str, ...] = (
    f"CREATE SCHEMA {SAMPLE_SCHEMA}",
    f"""
    CREATE TABLE {SAMPLE_SCHEMA}.orgs (
        id INTEGER PRIMARY KEY,
        name VARCHAR NOT NULL,
        CONSTRAINT orgs_name_key UNIQUE (name)
    )
    """,
    f"""
    CREATE TABLE {SAMPLE_SCHEMA}.customers (
        id BIGINT PRIMARY KEY,
        org_id INTEGER NOT NULL REFERENCES {SAMPLE_SCHEMA}.orgs (id),
        email VARCHAR NOT NULL,
        status VARCHAR NOT NULL DEFAULT 'active',
        lifetime_value DECIMAL(12, 2) DEFAULT 0,
        created_at TIMESTAMPTZ,
        CONSTRAINT customers_status_check
            CHECK (status IN ('active', 'churned', 'trial'))
    )
    """,
    f"COMMENT ON TABLE {SAMPLE_SCHEMA}.customers IS '{SAMPLE_TABLE_COMMENT}'",
    f"COMMENT ON COLUMN {SAMPLE_SCHEMA}.customers.email IS '{SAMPLE_COLUMN_COMMENT}'",
    f"CREATE INDEX customers_org_id_idx ON {SAMPLE_SCHEMA}.customers (org_id)",
    f"CREATE UNIQUE INDEX customers_email_key ON {SAMPLE_SCHEMA}.customers (email)",
    f"CREATE SEQUENCE {SAMPLE_SCHEMA}.invoice_number_seq START 1000",
    f"INSERT INTO {SAMPLE_SCHEMA}.orgs VALUES (1, 'acme'), (2, 'globex'), "
    "(3, 'initech')",
    # `range(1, 41)` is DuckDB's generate_series; the modulo arithmetic gives a
    # deterministic spread of orgs, statuses and values across 40 rows.
    f"""
    INSERT INTO {SAMPLE_SCHEMA}.customers
    SELECT
        g,
        (g % 3) + 1,
        'user' || g || '@example.com',
        CASE WHEN g % 4 = 0 THEN 'churned' ELSE 'active' END,
        g * 10.5,
        TIMESTAMPTZ '2025-01-01 00:00:00+00' + INTERVAL (g) DAY
    FROM range(1, 41) t(g)
    """,
    # After the INSERTs, so a test reading the view sees rows.
    f"""
    CREATE VIEW {SAMPLE_SCHEMA}.active_customers AS
    SELECT id, org_id, email, lifetime_value
    FROM {SAMPLE_SCHEMA}.customers
    WHERE status = 'active'
    """,
    f"COMMENT ON VIEW {SAMPLE_SCHEMA}.active_customers IS '{SAMPLE_VIEW_COMMENT}'",
    f"CREATE SCHEMA {SAMPLE_STAGING_SCHEMA}",
    f"CREATE TABLE {SAMPLE_STAGING_SCHEMA}.customers_raw (id BIGINT, payload VARCHAR)",
    f"INSERT INTO {SAMPLE_STAGING_SCHEMA}.customers_raw "
    "SELECT g, 'raw' || g FROM range(1, 8) t(g)",
    f"CREATE SCHEMA {SAMPLE_OTHER_SCHEMA}",
    f"CREATE TABLE {SAMPLE_OTHER_SCHEMA}.unrelated (id BIGINT)",
    f"INSERT INTO {SAMPLE_OTHER_SCHEMA}.unrelated SELECT * FROM range(1, 4)",
    # Load-bearing, not hygiene: until a CHECKPOINT flushes the write-ahead log
    # into the database file, `pragma_database_size()` reports `0 bytes` and zero
    # blocks, so every whole-database size assertion would pass vacuously.
    "CHECKPOINT",
)


def build_sample_schema(cursor: Any) -> None:
    """Run the sample DDL through ``cursor``.

    A function as well as a fixture because a few tests need the schema in a
    database *they* opened -- read-only mode, or a second connection -- and
    would otherwise have to duplicate the statement list.
    """
    for statement in _SAMPLE_DDL:
        cursor.execute(statement)


@pytest.fixture(scope="session")
def ddb_module() -> ModuleType:
    """The ``duckdb`` driver module, imported the way the tool imports it.

    Session-scoped and trivial, but it exists so a test that needs a driver
    class (``duckdb.InvalidInputException``) does not grow its own import and
    quietly stop matching what :func:`dataplat.services.db.connection.
    load_duckdb` resolves.
    """
    return load_duckdb()


@pytest.fixture
def ddb_path(tmp_path: Path) -> Path:
    """Path to a fresh, empty DuckDB database file.

    The file is created here rather than left to the code under test, because
    dataplat refuses to create one: ``ensure_duckdb_database_exists`` turns a
    missing path into a ConfigError so a typo cannot silently become an empty
    warehouse. So the harness does what a user's ``duckdb`` CLI would have done,
    then closes the connection -- leaving the file unlocked, which the read-only
    and second-connection tests depend on.
    """
    path = tmp_path / DATABASE_FILENAME
    seed = load_duckdb().connect(database=str(path))
    seed.close()
    return path


@pytest.fixture
def ddb_params(ddb_path: Path) -> DuckDbConnectionParams:
    """Resolved connection params for :func:`ddb_path`'s database.

    Constructed directly rather than through ``resolve_engine_params``: the
    resolver reads ``<PREFIX>_*`` environment, and a test that wants a specific
    file should not have to mutate the environment to get one. The resolver's own
    behaviour is covered by ``tests/services/db/test_connection.py``.
    """
    return DuckDbConnectionParams(path=str(ddb_path))


@pytest.fixture
def ddb_session(ddb_params: DuckDbConnectionParams) -> Iterator[DuckDbSession]:
    """An open :class:`~dataplat.cli.db._common.DuckDbSession`.

    Deliberately obtained from ``db_session`` -- the funnel every db command
    reaches its database through -- rather than from ``duckdb.connect``. That
    makes this tier cover the production seam too: the driver-error-to-exit-code
    mapping, the tracing hook, and the cursor facade's promise that
    ``cursor.description`` entries have a ``.name``. A raw connection would test
    DuckDB and skip dataplat.
    """
    with db_session(ddb_params) as session:
        yield session


@pytest.fixture
def ddb_cursor(ddb_session: DuckDbSession) -> Iterator[DuckDbCursor]:
    """A cursor over :func:`ddb_session`, as a command would hold one.

    Remember what :class:`~dataplat.cli.db._common.DuckDbCursor` promises and
    does not: every cursor from one session shares the connection, so it shares
    one result set. Fetch a statement's rows before executing the next.
    """
    with ddb_session.cursor() as cursor:
        yield cursor


@pytest.fixture
def ddb_sample_schema(ddb_cursor: DuckDbCursor) -> str:
    """Build the sample objects; return the schema the object tests describe.

    Contents (see :data:`SAMPLE_RELATIONS` and friends): two tables with a
    primary key, a foreign key, NOT NULL, CHECK and UNIQUE constraints and a
    column DEFAULT; table, column and view COMMENTs; a secondary index and a
    unique secondary index; a standalone sequence; a view over live rows; and
    two further schemas -- one sharing this one's prefix, one not -- so prefix
    filtering is testable. The database is CHECKPOINTed, so row estimates and
    sizes are non-zero.

    No teardown: the database is a file in ``tmp_path`` that only this test can
    see, so pytest removing the directory is the cleanup. Nothing is rolled back
    -- see the module docstring for why that matters.
    """
    build_sample_schema(ddb_cursor)
    return SAMPLE_SCHEMA


@pytest.fixture
def ddb_populated_path(ddb_path: Path) -> Path:
    """The sample database as a *closed* file, for tests that open it themselves.

    Needed because two of DuckDB's behaviours are only observable from outside an
    open session: ``read_only=True`` refusing writes, and a second connection
    whose configuration differs from an existing one being refused outright.
    """
    params = DuckDbConnectionParams(path=str(ddb_path))
    with db_session(params) as session, session.cursor() as cursor:
        build_sample_schema(cursor)
    return ddb_path
