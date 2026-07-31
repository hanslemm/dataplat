"""dataplat's DuckDB paths, executed against a real DuckDB database.

Five things are proved here, and they are different in kind:

1. **The harness's own premises** (``TestHarness``). Every unusual choice in
   ``conftest.py`` -- a file per test instead of a transaction, no ``ANALYZE``, a
   ``CHECKPOINT`` at the end -- rests on a probed DuckDB behaviour. Those probes
   are tests, so the next reader can re-run the evidence instead of trusting a
   comment.

2. **The production seam** (``TestSession``). ``DuckDbSession`` and
   ``DuckDbCursor`` exist because DuckDB has no ``cursor_factory`` to hang
   tracing on. Their unit tests drive a fake driver, which cannot show that a
   ``psycopg.sql.Composed`` renders to SQL DuckDB accepts, that
   ``cursor.description`` really is a seven-field tuple, or that the
   driver-error-to-exit-code mapping matches the exception classes duckdb
   actually raises.

3. **The dialect facts behind the capability matrix** (``TestDialectFacts``).
   ``services/db/capabilities.py`` refuses ``dp db role *``, ``long-queries``,
   ``kill`` and ``dbt-orphans`` on DuckDB, and each refusal cites something the
   engine does not have. Asserting the declaration alongside the missing catalog
   keeps the two from drifting -- a future DuckDB that grows ``pg_roles`` should
   fail here, not silently keep refusing.

4. **``dp db describe``'s fetchers** (``TestDescribe``). Same reason
   ``test_describe_pg.py`` exists: a fake cursor proves a fetcher called
   ``execute``, never that the catalog SQL parses or that the values are true.
   Every assertion here compares a fetcher's output to a literal the fixture DDL
   guarantees, or to DuckDB's own answer to the same question.

5. **``dp db top-tables``** (``TestTopTables``), including the part a fake
   cannot reach: whether ``LIKE ... ESCAPE '#'`` really distinguishes
   ``analytics`` from ``analytics_stg`` on this engine, and whether the
   ``DROP`` statement the command prints is valid DuckDB SQL.

Everything runs unconditionally. There is no skip path and no environment gate;
``conftest.py`` explains at length why introducing one would be a regression.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import typer
from psycopg import sql
from psycopg.abc import Query

from dataplat.cli.db._common import (
    DuckDbColumn,
    DuckDbCursor,
    DuckDbSession,
    db_session,
)
from dataplat.core.errors import ExitCode, ValidationError
from dataplat.services.db._like import glob_to_like
from dataplat.services.db.capabilities import Capability, capabilities_for
from dataplat.services.db.connection import (
    MEMORY_PATH,
    DuckDbConnectionParams,
    SqlEngine,
)
from dataplat.services.db.describe import (
    ObjectKind,
    TargetNotFoundError,
    TargetRef,
    describe_schema,
    describe_table,
    describe_view,
    fetch_columns,
    fetch_constraints,
    fetch_dependencies,
    fetch_indexes,
    fetch_partitioning,
    fetch_policies,
    fetch_relation_header,
    fetch_relation_privileges,
    fetch_schema_contents,
    fetch_schema_default_privileges,
    fetch_schema_header,
    fetch_schema_privileges,
    fetch_triggers,
    fetch_view_definition,
    resolve_target,
    schema_not_applicable,
    table_not_applicable,
    view_not_applicable,
)
from dataplat.services.db.role_dialects import ParentKind
from dataplat.services.db.schema_admin import (
    CreateSchemaSpec,
    SchemaPrivilege,
    build_create_plan,
    build_drop_plan,
)
from dataplat.services.db.schema_dialects import DuckDbSchemaDialect
from dataplat.services.db.top_tables import (
    SIZE_BASIS,
    drop_statement,
    fetch_top_tables,
)
from tests.integration.duckdb.conftest import (
    DATABASE_FILENAME,
    SAMPLE_INDEXES,
    SAMPLE_OTHER_SCHEMA,
    SAMPLE_RELATIONS,
    SAMPLE_ROW_COUNTS,
    SAMPLE_SCHEMA,
    SAMPLE_SCHEMA_PREFIX,
    SAMPLE_STAGING_SCHEMA,
    SAMPLE_TABLE_COMMENT,
    SAMPLE_VIEW_COMMENT,
    SAMPLE_VIEW_ROWS,
    build_sample_schema,
)

# The engine under test, spelled once. `DDB` rather than `ENGINE` so a reader
# skimming an assertion sees which dialect it is about, as `PG` does in
# tests/integration/test_describe_pg.py.
DDB = SqlEngine.duckdb

# Declared for readability; tests/integration/conftest.py adds it anyway as the
# safety net that keeps `-m 'not integration'` airtight. Worth knowing: this tier
# needs no server, so CI runs it by path in the fast matrix job rather than
# through this marker -- see .github/workflows/ci.yml.
pytestmark = pytest.mark.integration


def scalar(cursor: Any, statement: Query, params: Any = None) -> Any:
    """First column of the first row, or None. DuckDB binds ``?``, not ``%s``.

    ``statement`` is psycopg's ``Query`` rather than ``str`` because
    :class:`DuckDbCursor` accepts a composable too, and one test exists to prove
    it.
    """
    cursor.execute(statement, params)
    row = cursor.fetchone()
    return None if row is None else row[0]


# Every database path this run has handed out, for
# TestHarness.test_each_test_gets_its_own_database_file.
_SEEN_PATHS: set[str] = set()


class TestHarness:
    """The probed behaviours conftest.py's design decisions rest on."""

    def test_the_database_is_a_real_file_on_disk(
        self, ddb_params: DuckDbConnectionParams, ddb_sample_schema: str
    ) -> None:
        path = Path(ddb_params.path)
        assert path.name == DATABASE_FILENAME
        assert path.is_file()
        # Non-empty because the fixture CHECKPOINTs: DuckDB keeps writes in the
        # WAL until then, so this is also evidence the checkpoint happened.
        assert path.stat().st_size > 0
        assert ddb_params.in_memory is False
        assert ddb_params.engine is SqlEngine.duckdb

    @pytest.mark.parametrize("run", [1, 2])
    def test_each_test_gets_its_own_database_file(
        self, run: int, ddb_params: DuckDbConnectionParams
    ) -> None:
        """Isolation is the fresh file, so no two tests may share one.

        Parametrized rather than written twice: a lone test cannot observe reuse,
        because there is nothing to reuse yet. ``run`` is unused in the assertion
        on purpose -- checking membership rather than ``len(...) == run`` keeps
        this honest under ``--lf`` or ``-k``, where only the second case may
        execute and a count would then be wrong about a suite that is fine.
        """
        assert ddb_params.path not in _SEEN_PATHS
        _SEEN_PATHS.add(ddb_params.path)

    def test_row_estimates_are_exact_without_analyze(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        """Why the fixture runs no ANALYZE.

        On PostgreSQL ``pg_class.reltuples`` is ``-1`` until something analyses
        the table, which is why that harness ends its DDL with an ANALYZE. DuckDB
        reports the exact live count immediately, from both the compatibility
        catalog and its own -- so an ANALYZE here would be cargo cult.
        """
        ddb_cursor.execute(
            """
            SELECT n.nspname || '.' || c.relname, c.reltuples
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'r'
            """
        )
        assert {name: int(count) for name, count in ddb_cursor.fetchall()} == (
            SAMPLE_ROW_COUNTS
        )

        ddb_cursor.execute(
            "SELECT schema_name || '.' || table_name, estimated_size "
            "FROM duckdb_tables()"
        )
        assert {name: int(size) for name, size in ddb_cursor.fetchall()} == (
            SAMPLE_ROW_COUNTS
        )

    def test_row_estimates_are_zero_inside_an_open_transaction(
        self, ddb_cursor: DuckDbCursor
    ) -> None:
        """Why isolation is a file per test and not BEGIN/ROLLBACK.

        The PostgreSQL harness builds its fixture inside the test's transaction
        and rolls it back. DuckDB would roll it back too -- the ROLLBACK below
        proves that much -- but for the whole life of the transaction the catalog
        reports the *committed* row count, which for a table created inside it is
        zero. A transactional fixture would therefore hand every ranking and
        row-estimate assertion a table that looks empty.
        """
        ddb_cursor.execute("BEGIN TRANSACTION")
        ddb_cursor.execute(f"CREATE SCHEMA {SAMPLE_SCHEMA}")
        ddb_cursor.execute(f"CREATE TABLE {SAMPLE_SCHEMA}.t (id INTEGER)")
        ddb_cursor.execute(f"INSERT INTO {SAMPLE_SCHEMA}.t SELECT * FROM range(1, 12)")

        # The rows are really there...
        assert scalar(ddb_cursor, f"SELECT count(*) FROM {SAMPLE_SCHEMA}.t") == 11
        # ...and both catalogs still report the committed count, which is zero.
        # The whole row is compared rather than its first column: a *missing*
        # row would read as None, which `== 0` would not catch, and the test
        # would then be asserting nothing at all.
        ddb_cursor.execute("SELECT reltuples FROM pg_class WHERE relname = 't'")
        assert ddb_cursor.fetchone() == (0.0,)
        ddb_cursor.execute(
            "SELECT estimated_size FROM duckdb_tables() WHERE table_name = 't'"
        )
        assert ddb_cursor.fetchone() == (0,)

        ddb_cursor.execute("ROLLBACK")
        assert scalar(ddb_cursor, "SELECT count(*) FROM duckdb_tables()") == 0

    def test_database_size_is_zero_until_checkpoint(
        self, ddb_cursor: DuckDbCursor
    ) -> None:
        """Why the sample DDL ends with CHECKPOINT.

        ``pragma_database_size()`` measures the database *file*, and DuckDB keeps
        writes in the write-ahead log until a checkpoint moves them. Without the
        final CHECKPOINT the whole-database size -- the denominator for
        percentage-of-disk reporting in ``dp db top-tables`` -- is zero, and every
        assertion about it passes for the wrong reason.
        """
        ddb_cursor.execute(f"CREATE SCHEMA {SAMPLE_SCHEMA}")
        ddb_cursor.execute(
            f"CREATE TABLE {SAMPLE_SCHEMA}.t AS SELECT * FROM range(5000)"
        )

        assert self._total_bytes(ddb_cursor) == 0
        ddb_cursor.execute("CHECKPOINT")
        assert self._total_bytes(ddb_cursor) > 0

    def test_an_in_memory_database_reports_no_blocks(self) -> None:
        """Why the fixture is file-backed even though ``:memory:`` is supported.

        An in-memory database has no file, so ``pragma_database_size()`` reports a
        block size of zero however much data it holds. A ``:memory:`` fixture
        could not exercise the size path at all.
        """
        params = DuckDbConnectionParams(path=MEMORY_PATH)
        with db_session(params) as session, session.cursor() as cursor:
            cursor.execute("CREATE TABLE t AS SELECT * FROM range(5000)")
            assert scalar(cursor, "SELECT count(*) FROM t") == 5000
            cursor.execute("CHECKPOINT")
            assert self._total_bytes(cursor) == 0

    @staticmethod
    def _total_bytes(cursor: Any) -> int:
        """Whole-database size in bytes.

        Computed from ``block_size * total_blocks`` because
        ``pragma_database_size().database_size`` is a *VARCHAR* holding a
        human-readable string ('2.5 MiB') -- a real trap for anyone reaching for
        the obviously-named column to build a byte total.
        """
        cursor.execute("SELECT block_size, total_blocks FROM pragma_database_size()")
        block_size, total_blocks = cursor.fetchone()
        return int(block_size) * int(total_blocks)

    def test_read_only_mode_refuses_writes_and_exits_unclassified(
        self, ddb_populated_path: Path
    ) -> None:
        """``<PREFIX>_READ_ONLY=1`` is a real guard, and a write is exit 1.

        Two claims in one test because they are one behaviour: DuckDB raises
        InvalidInputException, which is a ``ProgrammingError`` and therefore the
        statement's own fault -- so ``db_session`` must not report it as the
        retryable SERVICE class.
        """
        params = DuckDbConnectionParams(path=str(ddb_populated_path), read_only=True)
        with db_session(params) as session, session.cursor() as cursor:
            # Reads are unaffected.
            assert (
                scalar(cursor, f"SELECT count(*) FROM {SAMPLE_SCHEMA}.customers")
                == SAMPLE_ROW_COUNTS[f"{SAMPLE_SCHEMA}.customers"]
            )

        with pytest.raises(typer.Exit) as excinfo, db_session(params) as session:
            session.execute(f"DELETE FROM {SAMPLE_SCHEMA}.customers")
        assert excinfo.value.exit_code == ExitCode.FAILURE

    def test_a_conflicting_second_connection_is_a_service_error(
        self, ddb_session: DuckDbSession, ddb_path: Path, ddb_module: ModuleType
    ) -> None:
        """Opening the same file with a different configuration is refused.

        DuckDB shares one database instance per file within a process, so a
        second connection asking for ``read_only=True`` while this session holds
        it read-write raises ConnectionException. That is an ``OperationalError``
        -- the environment failing, not the statement -- so it must earn exit 5,
        the code a wrapper is allowed to retry. It is also why
        ``ddb_populated_path`` closes its session before yielding the path.
        """
        with pytest.raises(ddb_module.ConnectionException):
            ddb_module.connect(database=str(ddb_path), read_only=True)

        conflicting = DuckDbConnectionParams(path=str(ddb_path), read_only=True)
        with pytest.raises(typer.Exit) as excinfo, db_session(conflicting):
            pass
        assert excinfo.value.exit_code == ExitCode.SERVICE


class TestSession:
    """``db_session``'s DuckDB half, against the driver rather than a fake."""

    def test_db_session_opens_the_configured_file(
        self, ddb_session: DuckDbSession, ddb_params: DuckDbConnectionParams
    ) -> None:
        assert isinstance(ddb_session, DuckDbSession)
        # DuckDB names the catalog after the file stem, so this is the connection
        # answering which file it opened.
        assert (
            scalar(ddb_session.cursor(), "SELECT current_database()")
            == Path(ddb_params.path).stem
        )
        assert scalar(ddb_session.cursor(), "SELECT current_user") == "duckdb"

    def test_cursors_share_the_session_connection(
        self, ddb_session: DuckDbSession, ddb_module: ModuleType
    ) -> None:
        """The reason ``DuckDbSession.cursor()`` does not call DuckDB's.

        DuckDB's own ``connection.cursor()`` opens a *second* connection, which
        cannot see uncommitted DDL. Both halves are asserted, because only the
        contrast explains the design: the facade sees the table, the driver's
        cursor raises CatalogException for it.
        """
        ddb_session.execute("BEGIN TRANSACTION")
        ddb_session.execute("CREATE TABLE staged (id INTEGER)")
        ddb_session.execute("INSERT INTO staged VALUES (1), (2)")

        with ddb_session.cursor() as second:
            assert scalar(second, "SELECT count(*) FROM staged") == 2

        driver_cursor = ddb_session.raw.cursor()
        try:
            with pytest.raises(ddb_module.CatalogException, match="staged"):
                driver_cursor.execute("SELECT count(*) FROM staged")
        finally:
            driver_cursor.close()

        ddb_session.rollback()
        assert scalar(ddb_session.cursor(), "SELECT count(*) FROM duckdb_tables()") == 0

    def test_description_entries_are_named_seven_field_tuples(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        """``desc.name`` is what ``dp db query`` renders its header from.

        DuckDB returns plain tuples, so the attribute exists only because
        :class:`DuckDbColumn` adds it. The tuple shape is asserted too: the
        facade slices ``entry[:7]``, and a caller unpacking a row of
        ``description`` must still get the DB-API's seven fields.
        """
        ddb_cursor.execute(
            f"SELECT id, email, lifetime_value FROM {SAMPLE_SCHEMA}.customers LIMIT 1"
        )
        description = ddb_cursor.description
        assert description is not None
        assert [column.name for column in description] == [
            "id",
            "email",
            "lifetime_value",
        ]
        for column in description:
            assert isinstance(column, DuckDbColumn)
            assert len(column) == 7
        # DuckDB fills type_code and leaves the rest None, so a renderer must not
        # depend on the trailing five.
        assert description[0].type_code is not None
        assert description[0].null_ok is None

    def test_description_is_none_before_any_statement(
        self, ddb_session: DuckDbSession
    ) -> None:
        assert ddb_session.cursor().description is None

    def test_rowcount_is_unknown_for_every_statement(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        """DuckDB reports the DB-API's -1, including for DML.

        Pinned because the honest -1 is a deliberate non-emulation: the affected
        count comes back as a one-row result set instead, which the second
        assertion shows.
        """
        ddb_cursor.execute(f"SELECT * FROM {SAMPLE_SCHEMA}.orgs")
        assert ddb_cursor.rowcount == -1

        ddb_cursor.execute(f"DELETE FROM {SAMPLE_SCHEMA}.customers WHERE id > 10")
        assert ddb_cursor.rowcount == -1
        assert ddb_cursor.fetchone() == (30,)

    def test_executemany_binds_a_batch(self, ddb_cursor: DuckDbCursor) -> None:
        ddb_cursor.execute("CREATE TABLE batched (id INTEGER, label VARCHAR)")
        ddb_cursor.executemany(
            "INSERT INTO batched VALUES (?, ?)",
            [(1, "one"), (2, "two"), (3, "three")],
        )
        ddb_cursor.execute("SELECT id, label FROM batched ORDER BY id")
        assert ddb_cursor.fetchall() == [(1, "one"), (2, "two"), (3, "three")]

    def test_fetchmany_and_fetchall_return_lists(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        ddb_cursor.execute(f"SELECT id FROM {SAMPLE_SCHEMA}.orgs ORDER BY id")
        first = ddb_cursor.fetchmany(2)
        assert first == [(1,), (2,)]
        assert ddb_cursor.fetchall() == [(3,)]

    def test_a_composed_statement_renders_to_sql_duckdb_accepts(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        """``DuckDbCursor`` renders psycopg composables instead of refusing them.

        That is only defensible if the SQL psycopg emits is portable, which this
        asserts for the one construct the codebase actually shares: an identifier
        quoted by ``sql.Identifier``. Placeholders are *not* translated, so the
        parameter marker in the composed statement is DuckDB's ``?``.
        """
        statement = sql.SQL("SELECT count(*) FROM {schema}.{table} WHERE status = ?")
        composed = statement.format(
            schema=sql.Identifier(SAMPLE_SCHEMA), table=sql.Identifier("customers")
        )
        assert scalar(ddb_cursor, composed, ["active"]) == SAMPLE_VIEW_ROWS

    def test_a_psycopg_placeholder_is_not_translated(
        self, ddb_cursor: DuckDbCursor, ddb_module: ModuleType
    ) -> None:
        """The documented non-goal, asserted so nobody "fixes" it by accident.

        A shared query written with ``%s`` does not silently work on DuckDB; it
        fails in the parser. This is why every DuckDB statement in
        ``services/db`` needs its own constant rather than reusing the
        PostgreSQL one.
        """
        with pytest.raises(ddb_module.ParserException):
            ddb_cursor.execute("SELECT 1 WHERE 1 = %s", [1])

    def test_cursor_close_does_not_end_the_session(
        self, ddb_session: DuckDbSession
    ) -> None:
        """``with conn.cursor() as cur`` must not close the connection.

        The facade's ``close()`` is a documented no-op; if it forwarded, the first
        ``with`` block in any command would end the session.
        """
        with ddb_session.cursor() as cursor:
            cursor.execute("CREATE TABLE t (id INTEGER)")
        assert scalar(ddb_session.cursor(), "SELECT count(*) FROM t") == 0

    def test_rollback_without_a_transaction_raises(
        self, ddb_session: DuckDbSession, ddb_module: ModuleType
    ) -> None:
        """Not smoothed over into psycopg's shrug -- see ``DuckDbSession``.

        Worth knowing for the exit-code contract: TransactionException is an
        ``OperationalError``, so a command that rolls back a transaction it never
        opened would exit 5. Surfacing it is the point.
        """
        with pytest.raises(ddb_module.TransactionException):
            ddb_session.rollback()
        assert issubclass(ddb_module.TransactionException, ddb_module.OperationalError)

    def test_a_bad_statement_exits_unclassified_not_service(
        self, ddb_params: DuckDbConnectionParams
    ) -> None:
        """The split ``db_session`` promises, with DuckDB's exception classes.

        CatalogException is a ``ProgrammingError``: retrying a typo forever is
        exactly what mapping every driver error to SERVICE would tell a wrapper
        to do.
        """
        with pytest.raises(typer.Exit) as excinfo, db_session(ddb_params) as session:
            session.execute("SELECT * FROM no_such_table")
        assert excinfo.value.exit_code == ExitCode.FAILURE


class TestDialectFacts:
    """What the capability matrix claims about DuckDB, asked of DuckDB.

    Each test pairs the declaration in ``services/db/capabilities.py`` with the
    catalog it cites. Neither half alone is enough: the declaration without the
    probe is a memory of a doc, and the probe without the declaration is a fact
    nothing consumes.
    """

    caps = capabilities_for(SqlEngine.duckdb)

    @pytest.mark.parametrize(
        ("capability", "statement"),
        [
            (Capability.roles, "SELECT 1 FROM pg_roles"),
            (Capability.roles, "SELECT 1 FROM pg_user"),
            (Capability.role_password_store, "SELECT 1 FROM pg_authid"),
            (Capability.concurrent_sessions, "SELECT 1 FROM pg_stat_activity"),
            (Capability.concurrent_sessions, "SELECT 1 FROM pg_stat_statements"),
            (Capability.matview_catalog, "SELECT 1 FROM pg_matviews"),
            (Capability.acl_introspection, "SELECT aclexplode(NULL)"),
            (Capability.acl_introspection, "SELECT pg_get_userbyid(1)"),
            (
                Capability.relation_size_functions,
                "SELECT pg_total_relation_size(1)",
            ),
            (
                Capability.relation_size_functions,
                "SELECT pg_database_size(current_database())",
            ),
        ],
    )
    def test_a_declared_absence_is_really_absent(
        self,
        capability: Capability,
        statement: str,
        ddb_cursor: DuckDbCursor,
        ddb_module: ModuleType,
    ) -> None:
        assert self.caps.support(capability).available is False
        with pytest.raises(ddb_module.CatalogException):
            ddb_cursor.execute(statement)

    def test_the_catalogs_dataplat_does_read_are_present(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        """The other half of the matrix: what DuckDB *does* answer.

        Grouped into one test on purpose -- individually they are one-liners, and
        together they are the list a reader needs when deciding whether a new
        query can be shared with the PostgreSQL path.
        """
        assert scalar(ddb_cursor, "SELECT version()").startswith("v")
        assert scalar(ddb_cursor, "SELECT current_user") == "duckdb"
        assert scalar(
            ddb_cursor, "SELECT has_schema_privilege(?, 'USAGE')", [SAMPLE_SCHEMA]
        )

        counts = {
            "pg_class": "SELECT count(*) FROM pg_class WHERE relkind = 'r'",
            "pg_namespace": (
                f"SELECT count(*) FROM pg_namespace WHERE nspname = '{SAMPLE_SCHEMA}'"
            ),
            "pg_attribute": (
                "SELECT count(*) FROM pg_attribute a JOIN pg_class c "
                "ON c.oid = a.attrelid WHERE c.relname = 'customers'"
            ),
            "pg_constraint": "SELECT count(*) FROM pg_constraint",
            "pg_index": "SELECT count(*) FROM pg_index",
            "pg_indexes": (
                f"SELECT count(*) FROM pg_indexes WHERE schemaname = '{SAMPLE_SCHEMA}'"
            ),
            "pg_views": (
                f"SELECT count(*) FROM pg_views WHERE schemaname = '{SAMPLE_SCHEMA}'"
            ),
            "information_schema.columns": (
                "SELECT count(*) FROM information_schema.columns "
                f"WHERE table_schema = '{SAMPLE_SCHEMA}'"
            ),
            "duckdb_tables": "SELECT count(*) FROM duckdb_tables()",
            "duckdb_columns": "SELECT count(*) FROM duckdb_columns()",
            "duckdb_constraints": "SELECT count(*) FROM duckdb_constraints()",
            "duckdb_indexes": "SELECT count(*) FROM duckdb_indexes()",
            "duckdb_views": "SELECT count(*) FROM duckdb_views() WHERE NOT internal",
        }
        empty = [
            name
            for name, statement in counts.items()
            if int(scalar(ddb_cursor, statement)) == 0
        ]
        assert empty == []

    def test_pg_get_viewdef_takes_an_oid_not_a_name(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str, ddb_module: ModuleType
    ) -> None:
        """Why the view definition comes from ``duckdb_views().sql``.

        ``pg_get_viewdef`` exists but only in its oid form, so the
        ``pg_get_viewdef('schema.view')`` spelling the Redshift path uses cannot
        be shared with DuckDB.
        """
        with pytest.raises(ddb_module.ConversionException):
            ddb_cursor.execute(
                f"SELECT pg_get_viewdef('{SAMPLE_SCHEMA}.active_customers')"
            )

        definition = scalar(
            ddb_cursor,
            "SELECT sql FROM duckdb_views() WHERE schema_name = ? AND view_name = ?",
            [SAMPLE_SCHEMA, "active_customers"],
        )
        assert "SELECT" in definition
        assert "customers" in definition

    def test_comments_on_a_schema_are_not_implemented(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str, ddb_module: ModuleType
    ) -> None:
        """So ``dp db describe <schema>`` can never print one on this engine.

        Table, column and view comments all work -- only the schema-level one is
        missing -- which is why the fixture carries the first three and not this.
        """
        with pytest.raises(ddb_module.NotImplementedException):
            ddb_cursor.execute(f"COMMENT ON SCHEMA {SAMPLE_SCHEMA} IS 'nope'")

    def test_rename_is_blocked_by_an_index_or_a_foreign_key_not_by_a_view(
        self, ddb_cursor: DuckDbCursor, ddb_module: ModuleType
    ) -> None:
        """The mechanism behind the ``rename_with_dependents`` refusal.

        Probed on duckdb 1.5.5, and it is **not** what the reason string in
        ``services/db/capabilities.py`` currently says. A dependent *view* does
        not block the rename at all: the ALTER succeeds and leaves the view
        pointing at a name that no longer exists, so the next read of it raises
        CatalogException. What does raise DependencyException is a secondary
        index on the table, or a foreign key referencing it.

        The refusal stands either way -- and is arguably better justified, since
        silently breaking a dbt project's views is worse than refusing to rename
        -- but the *reason* dataplat prints names the one dependency DuckDB
        tolerates. See the report accompanying this suite.
        """
        assert self.caps.support(Capability.rename_with_dependents).available is False

        ddb_cursor.execute("CREATE SCHEMA quarantine")
        ddb_cursor.execute("CREATE TABLE quarantine.t (id INTEGER, v VARCHAR)")
        ddb_cursor.execute("INSERT INTO quarantine.t VALUES (1, 'a')")
        ddb_cursor.execute("CREATE VIEW quarantine.v AS SELECT id FROM quarantine.t")

        # A dependent view does not stop it.
        ddb_cursor.execute("ALTER TABLE quarantine.t RENAME TO t_deprecated")
        # ...and the view is now broken, silently, with no CASCADE to have asked
        # for and nothing in the ALTER's result to say so.
        with pytest.raises(ddb_module.CatalogException):
            ddb_cursor.execute("SELECT * FROM quarantine.v")

        # A secondary index does stop it.
        ddb_cursor.execute("CREATE INDEX t_v_idx ON quarantine.t_deprecated (v)")
        with pytest.raises(ddb_module.DependencyException):
            ddb_cursor.execute("ALTER TABLE quarantine.t_deprecated RENAME TO t2")

        # ...as does being the parent of a foreign key.
        ddb_cursor.execute("CREATE TABLE quarantine.p (id INTEGER PRIMARY KEY)")
        ddb_cursor.execute(
            "CREATE TABLE quarantine.c (p_id INTEGER REFERENCES quarantine.p (id))"
        )
        with pytest.raises(ddb_module.DependencyException):
            ddb_cursor.execute("ALTER TABLE quarantine.p RENAME TO p2")

        # DependencyException is not an OperationalError, so a refused rename
        # exits 1 rather than telling a wrapper to retry.
        assert not issubclass(
            ddb_module.DependencyException, ddb_module.OperationalError
        )

    def test_the_sample_index_and_view_inventory_matches_the_catalog(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        """Guards the fixture's promise, which several tests below assert against.

        Named indexes come from ``pg_indexes``; the implicit PRIMARY KEY and
        UNIQUE indexes are not listed there, which is why
        :data:`SAMPLE_INDEXES` holds only the two explicit ones.
        """
        ddb_cursor.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = ? ORDER BY indexname",
            [SAMPLE_SCHEMA],
        )
        assert [row[0] for row in ddb_cursor.fetchall()] == sorted(SAMPLE_INDEXES)

        assert (
            scalar(
                ddb_cursor,
                f"SELECT count(*) FROM {SAMPLE_SCHEMA}.active_customers",
            )
            == SAMPLE_VIEW_ROWS
        )


@pytest.fixture
def customers_ref(ddb_cursor: DuckDbCursor, ddb_sample_schema: str) -> TargetRef:
    """``analytics.customers`` resolved the way the command resolves it."""
    return resolve_target(ddb_cursor, DDB, f"{SAMPLE_SCHEMA}.customers")


@pytest.fixture
def orgs_ref(ddb_cursor: DuckDbCursor, ddb_sample_schema: str) -> TargetRef:
    """``analytics.orgs`` -- the table carrying the UNIQUE constraint."""
    return resolve_target(ddb_cursor, DDB, f"{SAMPLE_SCHEMA}.orgs")


@pytest.fixture
def view_ref(ddb_cursor: DuckDbCursor, ddb_sample_schema: str) -> TargetRef:
    """``analytics.active_customers``."""
    return resolve_target(ddb_cursor, DDB, f"{SAMPLE_SCHEMA}.active_customers")


class TestDescribe:
    """``services/db/describe`` against DuckDB's catalogs.

    The oid is the hinge of this whole module: ``resolve_target`` reads
    ``pg_class.oid``, and every ``_*_SQL_DUCKDB`` constant then looks that value
    up in a ``duckdb_*()`` catalog. If those two numbering schemes ever diverge,
    the fetchers below stop returning rows -- which is why several tests assert on
    content rather than merely on a non-empty list.
    """

    def test_resolve_target_finds_a_schema_a_table_and_a_view(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        schema_ref = resolve_target(ddb_cursor, DDB, SAMPLE_SCHEMA)
        assert schema_ref == TargetRef(
            kind=ObjectKind.schema, schema=SAMPLE_SCHEMA, name=None, oid=None
        )

        table_ref = resolve_target(ddb_cursor, DDB, f"{SAMPLE_SCHEMA}.customers")
        assert table_ref.kind is ObjectKind.table
        assert (table_ref.schema, table_ref.name) == (SAMPLE_SCHEMA, "customers")
        assert isinstance(table_ref.oid, int)

        view_ref = resolve_target(ddb_cursor, DDB, f"{SAMPLE_SCHEMA}.active_customers")
        assert view_ref.kind is ObjectKind.view
        assert isinstance(view_ref.oid, int)
        assert view_ref.oid != table_ref.oid

    @pytest.mark.parametrize(
        "qualified",
        [
            name
            for name, relkind in SAMPLE_RELATIONS.items()
            if relkind not in {"r", "v"}
        ],
    )
    def test_resolve_target_refuses_a_kind_the_report_cannot_render(
        self, qualified: str, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        """An index or a sequence resolves to a clear refusal, not a crash.

        Driven from the fixture inventory rather than a literal list, so a new
        relation kind in the sample schema has to be classified here too.
        """
        with pytest.raises(TargetNotFoundError, match="unsupported kind"):
            resolve_target(ddb_cursor, DDB, qualified)

    def test_resolve_target_reports_a_miss_with_a_next_step(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        with pytest.raises(TargetNotFoundError, match="dp db describe"):
            resolve_target(ddb_cursor, DDB, f"{SAMPLE_SCHEMA}.no_such_table")
        with pytest.raises(TargetNotFoundError, match="not found"):
            resolve_target(ddb_cursor, DDB, "no_such_schema")

    def test_fetch_columns_reads_types_defaults_keys_and_comments(
        self, ddb_cursor: DuckDbCursor, customers_ref: TargetRef
    ) -> None:
        """The section most likely to be quietly wrong, so it is asserted whole.

        ``data_type`` is DuckDB's own spelling, not ``format_type``'s flattened
        one; ``default`` comes from ``duckdb_columns()`` because ``pg_attrdef`` is
        empty there; the foreign key is resolved through ``duckdb_constraints()``
        because ``pg_constraint.confrelid`` is always 0. Each of those three is a
        wrong-but-plausible answer the PostgreSQL query would have produced.
        """
        assert customers_ref.oid is not None
        columns = fetch_columns(ddb_cursor, customers_ref.oid, DDB)

        assert [(c.ordinal, c.name) for c in columns] == [
            (1, "id"),
            (2, "org_id"),
            (3, "email"),
            (4, "status"),
            (5, "lifetime_value"),
            (6, "created_at"),
        ]
        assert [c.data_type for c in columns] == [
            "BIGINT",
            "INTEGER",
            "VARCHAR",
            "VARCHAR",
            "DECIMAL(12,2)",
            "TIMESTAMP WITH TIME ZONE",
        ]
        assert [c.nullable for c in columns] == [
            False,
            False,
            False,
            False,
            True,
            True,
        ]

        by_name = {c.name: c for c in columns}
        assert by_name["status"].default == "'active'"
        assert by_name["lifetime_value"].default == "0"
        assert by_name["created_at"].default is None

        assert by_name["id"].is_primary_key is True
        assert by_name["org_id"].is_primary_key is False

        assert by_name["org_id"].fk_target_table == f"{SAMPLE_SCHEMA}.orgs"
        assert by_name["org_id"].fk_target_column == "id"
        assert by_name["email"].fk_target_table is None

        assert by_name["email"].comment == "Primary contact address."
        assert by_name["id"].comment is None
        # Redshift-only field; never populated here.
        assert all(c.encoding is None for c in columns)

    def test_fetch_columns_covers_a_view(
        self, ddb_cursor: DuckDbCursor, view_ref: TargetRef
    ) -> None:
        """A view's columns come from the same catalog, with no keys attached."""
        assert view_ref.oid is not None
        columns = fetch_columns(ddb_cursor, view_ref.oid, DDB)
        assert [c.name for c in columns] == ["id", "org_id", "email", "lifetime_value"]
        assert not any(c.is_primary_key for c in columns)
        assert not any(c.fk_target_table for c in columns)
        # DuckDB reports every view column nullable, which is honest: a view has
        # no NOT NULL of its own.
        assert all(c.nullable for c in columns)

    def test_fetch_constraints_reads_names_columns_and_targets(
        self, ddb_cursor: DuckDbCursor, customers_ref: TargetRef
    ) -> None:
        """Names, not constraint *text*, which is what ``pg_constraint`` holds.

        ``pg_constraint.conname`` on DuckDB is the definition ('PRIMARY KEY(id)')
        and ``pg_get_constraintdef()`` returns NULL, so a query that looked
        plausible would have reported a name nobody could reference.
        """
        assert customers_ref.oid is not None
        bundle = fetch_constraints(ddb_cursor, customers_ref.oid, DDB)

        assert bundle.primary_key is not None
        assert bundle.primary_key.columns == ["id"]
        assert "PRIMARY KEY" not in bundle.primary_key.name

        assert len(bundle.foreign_keys) == 1
        fk = bundle.foreign_keys[0]
        assert fk.columns == ["org_id"]
        assert fk.referenced_table == f"{SAMPLE_SCHEMA}.orgs"
        assert fk.referenced_columns == ["id"]
        # DuckDB rejects referential actions at parse time, so NO ACTION is not a
        # fallback -- it is the only thing a DuckDB foreign key can do.
        assert (fk.on_update, fk.on_delete) == ("NO ACTION", "NO ACTION")
        assert fk.deferrable is False

        assert [c.name for c in bundle.check_constraints] == ["customers_status_check"]
        assert "status" in bundle.check_constraints[0].definition

        # NOT NULL is deliberately excluded, even though duckdb_constraints()
        # reports it: the Columns section above already carries it, and listing it
        # twice for one engine only would make the same table look different per
        # dialect.
        definitions = [c.definition for c in bundle.check_constraints]
        assert not any(d.startswith("NOT NULL") for d in definitions)

    def test_fetch_constraints_reads_a_unique_constraint(
        self, ddb_cursor: DuckDbCursor, orgs_ref: TargetRef
    ) -> None:
        """The UNIQUE branch, which ``customers`` has no example of."""
        assert orgs_ref.oid is not None
        bundle = fetch_constraints(ddb_cursor, orgs_ref.oid, DDB)
        assert [c.name for c in bundle.unique_constraints] == ["orgs_name_key"]
        assert bundle.primary_key is not None
        assert bundle.primary_key.columns == ["id"]
        assert bundle.foreign_keys == []

    def test_fetch_indexes_lists_the_explicit_indexes_only(
        self, ddb_cursor: DuckDbCursor, customers_ref: TargetRef
    ) -> None:
        """And the implicit PK/UNIQUE indexes are absent, on purpose.

        ``duckdb_tables().index_count`` counts the indexes DuckDB builds for a
        PRIMARY KEY and a UNIQUE constraint, but ``duckdb_indexes()`` does not
        list them -- so ``is_primary`` is false throughout and the Constraints
        section is where those appear. That asymmetry is asserted rather than
        assumed, because a future DuckDB listing them would change the report.
        """
        assert customers_ref.oid is not None
        indexes = fetch_indexes(ddb_cursor, customers_ref.oid, DDB)

        assert [i.name for i in indexes] == [
            "customers_email_key",
            "customers_org_id_idx",
        ]
        assert set(SAMPLE_INDEXES) == {i.name for i in indexes}
        by_name = {i.name: i for i in indexes}
        assert by_name["customers_email_key"].unique is True
        assert by_name["customers_org_id_idx"].unique is False
        assert not any(i.primary for i in indexes)
        assert by_name["customers_org_id_idx"].columns == ["org_id"]
        # 'art' is DuckDB's only index type, and there are no partial indexes or
        # per-index sizes to report.
        assert {i.method for i in indexes} == {"art"}
        assert all(i.size_bytes is None and i.predicate is None for i in indexes)

    def test_fetch_indexes_parses_a_multi_column_expression_list(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        """``duckdb_indexes().expressions`` is a string, cast back to a list.

        Two columns and an expression that itself contains a comma, because
        splitting the rendered list on ``', '`` would tear ``concat(a, b)`` in
        half -- and that is the shortcut a reader might think is equivalent.
        """
        ddb_cursor.execute(
            f"CREATE INDEX customers_pair_idx "
            f"ON {SAMPLE_SCHEMA}.customers (org_id, status)"
        )
        ddb_cursor.execute(
            f"CREATE INDEX customers_expr_idx "
            f"ON {SAMPLE_SCHEMA}.customers (concat(email, status))"
        )
        ref = resolve_target(ddb_cursor, DDB, f"{SAMPLE_SCHEMA}.customers")
        assert ref.oid is not None
        by_name = {i.name: i for i in fetch_indexes(ddb_cursor, ref.oid, DDB)}

        assert by_name["customers_pair_idx"].columns == ["org_id", "status"]
        assert len(by_name["customers_expr_idx"].columns) == 1
        assert "concat" in by_name["customers_expr_idx"].columns[0]

    def test_fetch_relation_header_reports_rows_and_withholds_sizes(
        self, ddb_cursor: DuckDbCursor, customers_ref: TargetRef
    ) -> None:
        """The four size columns are NULL, and that is a withdrawn claim.

        ``duckdb_tables().estimated_size`` is a row count; feeding it to a size
        field would be wrong by three orders of magnitude. ``owner`` is empty
        rather than 'duckdb' so the report does not imply a user model.
        """
        assert customers_ref.oid is not None
        header = fetch_relation_header(ddb_cursor, customers_ref.oid, DDB)

        assert (header.schema, header.name) == (SAMPLE_SCHEMA, "customers")
        assert header.comment == SAMPLE_TABLE_COMMENT
        assert header.row_estimate == SAMPLE_ROW_COUNTS[f"{SAMPLE_SCHEMA}.customers"]
        assert header.owner == ""
        assert header.tablespace is None
        assert (
            header.total_size,
            header.table_size,
            header.index_size,
            header.toast_size,
        ) == (None, None, None, None)

    def test_fetch_relation_header_covers_the_view_arm_of_the_union(
        self, ddb_cursor: DuckDbCursor, view_ref: TargetRef
    ) -> None:
        """``duckdb_tables()`` does not list views, hence the UNION.

        ``row_estimate`` is None rather than 0 for a view: DuckDB's
        ``pg_class.reltuples`` is 0.0 for every view, which would render as
        "0 rows" about a view returning thirty.
        """
        assert view_ref.oid is not None
        header = fetch_relation_header(ddb_cursor, view_ref.oid, DDB)
        assert (header.schema, header.name) == (SAMPLE_SCHEMA, "active_customers")
        assert header.comment == SAMPLE_VIEW_COMMENT
        assert header.row_estimate is None

    def test_fetch_relation_header_rejects_an_unknown_oid(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        with pytest.raises(TargetNotFoundError, match="not found"):
            fetch_relation_header(ddb_cursor, 999_999_999, DDB)

    def test_fetch_view_definition_returns_the_engine_s_own_text(
        self, ddb_cursor: DuckDbCursor, view_ref: TargetRef
    ) -> None:
        """Verbatim, including the ``CREATE VIEW`` prefix PostgreSQL omits.

        Trimming that prefix would be the start of a report lying about what is
        stored, so the difference is asserted rather than normalised away.
        """
        assert view_ref.oid is not None
        definition = fetch_view_definition(ddb_cursor, view_ref.oid, DDB)
        assert definition.sql.startswith("CREATE VIEW")
        assert f"{SAMPLE_SCHEMA}.customers" in definition.sql
        assert definition.is_updatable is False
        assert definition.check_option is None

    def test_fetch_view_definition_rejects_a_table_oid(
        self, ddb_cursor: DuckDbCursor, customers_ref: TargetRef
    ) -> None:
        """A table's oid is not in ``duckdb_views()``, so this must not invent one."""
        assert customers_ref.oid is not None
        with pytest.raises(TargetNotFoundError, match="not found"):
            fetch_view_definition(ddb_cursor, customers_ref.oid, DDB)

    def test_the_sections_duckdb_cannot_answer_are_empty_without_a_query(
        self, ddb_cursor: DuckDbCursor, customers_ref: TargetRef, view_ref: TargetRef
    ) -> None:
        """Five fetchers short-circuit, and each emptiness has a stated reason.

        Grouped because the assertion is the same in every case and the value is
        in the list being complete: any of these reaching a cursor would raise,
        since all five queries are PostgreSQL-only and use ``%s``.
        """
        assert customers_ref.oid is not None
        assert view_ref.oid is not None
        assert (
            fetch_relation_privileges(ddb_cursor, SAMPLE_SCHEMA, "customers", DDB) == []
        )
        assert fetch_triggers(ddb_cursor, customers_ref.oid, DDB) == []
        assert fetch_policies(ddb_cursor, customers_ref.oid, DDB) == ([], False)
        assert fetch_partitioning(ddb_cursor, customers_ref.oid, DDB).parent is None
        assert fetch_partitioning(ddb_cursor, customers_ref.oid, DDB).children == []
        for direction in ("upstream", "downstream"):
            assert fetch_dependencies(ddb_cursor, view_ref.oid, direction, DDB) == []
        assert fetch_schema_privileges(ddb_cursor, SAMPLE_SCHEMA, DDB) == []
        assert fetch_schema_default_privileges(ddb_cursor, SAMPLE_SCHEMA, DDB) == []

    def test_an_empty_section_is_paired_with_a_reason(self) -> None:
        """Otherwise "Privileges: none" reads as "nobody has access".

        The ``NotApplicable`` lists are what turn an emptiness into a sentence,
        so they must be non-empty on DuckDB and empty on PostgreSQL -- a
        PostgreSQL report gaining "not applicable" notes would be a bug in the
        other direction.
        """
        for not_applicable in (
            table_not_applicable,
            view_not_applicable,
            schema_not_applicable,
        ):
            entries = not_applicable(DDB)
            assert entries, not_applicable.__name__
            assert not_applicable(SqlEngine.postgresql) == []
            for entry in entries:
                assert entry.section
                assert entry.reason
                # Phrased as the tail of a sentence, so no trailing period and no
                # leading capital that would read as a new sentence.
                assert not entry.reason.endswith(".")

        sections = {entry.section for entry in table_not_applicable(DDB)}
        assert {"Size", "Privileges", "Triggers", "Partitioning"} <= sections

    def test_fetch_schema_header_reads_duckdb_s_own_catalog(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        """``pg_namespace`` is present but useless here: no owner, no comment.

        ``comment`` is None because DuckDB refuses ``COMMENT ON SCHEMA``, which
        ``test_comments_on_a_schema_are_not_implemented`` proves -- so this is a
        real read of a column that is always NULL today, not a hardcoded None.
        """
        header = fetch_schema_header(ddb_cursor, SAMPLE_SCHEMA, DDB)
        assert header.name == SAMPLE_SCHEMA
        assert header.owner == ""
        assert header.comment is None

    def test_fetch_schema_header_rejects_a_missing_schema(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        with pytest.raises(TargetNotFoundError, match="not found"):
            fetch_schema_header(ddb_cursor, "no_such_schema", DDB)

    def test_fetch_schema_header_picks_the_user_database_s_main(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        """``duckdb_schemas()`` lists every attached catalog's schemas.

        'main' exists in the user database, in 'system' and in 'temp', so without
        the ``current_database()`` filter this returns three rows and the header
        describes whichever came first. One row is the assertion.
        """
        ddb_cursor.execute(
            "SELECT count(*) FROM duckdb_schemas() WHERE schema_name = 'main'"
        )
        assert int(ddb_cursor.fetchone()[0]) > 1
        assert fetch_schema_header(ddb_cursor, "main", DDB).name == "main"

    def test_fetch_schema_contents_labels_every_kind_the_fixture_holds(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        """Tables, the view and the sequence, with row counts but no sizes."""
        contents = fetch_schema_contents(ddb_cursor, SAMPLE_SCHEMA, DDB)
        by_name = {item.name: item for item in contents}

        assert by_name["customers"].kind == "table"
        assert by_name["orgs"].kind == "table"
        assert by_name["active_customers"].kind == "view"
        assert by_name["invoice_number_seq"].kind == "sequence"
        # The two indexes are not listed: the report shows them under the table
        # they belong to, as the PostgreSQL path does.
        assert set(by_name) == {
            "customers",
            "orgs",
            "active_customers",
            "invoice_number_seq",
        }

        assert (
            by_name["customers"].row_estimate
            == (SAMPLE_ROW_COUNTS[f"{SAMPLE_SCHEMA}.customers"])
        )
        assert (
            by_name["orgs"].row_estimate == (SAMPLE_ROW_COUNTS[f"{SAMPLE_SCHEMA}.orgs"])
        )
        # A view has no row count and nothing here has a size.
        assert by_name["active_customers"].row_estimate is None
        assert all(item.size_bytes is None for item in contents)
        assert all(item.owner == "" for item in contents)

    def test_describe_table_composes_every_section(
        self, ddb_cursor: DuckDbCursor, customers_ref: TargetRef
    ) -> None:
        """End to end, because a fetcher that works alone can still be miscalled.

        Every section of ``dp db describe analytics.customers`` in one statement
        sequence -- which is the only place a fetcher invoked *without* its engine
        argument shows up, since the default is a libpq engine and its ``%s``
        would reach DuckDB's parser.
        """
        description = describe_table(ddb_cursor, customers_ref, DDB)

        assert description.header.comment == SAMPLE_TABLE_COMMENT
        assert [c.name for c in description.columns][:2] == ["id", "org_id"]
        assert description.constraints.primary_key is not None
        assert len(description.indexes) == 2
        assert description.privileges == []
        assert description.triggers == []
        assert (description.policies, description.policies_enabled) == ([], False)
        assert description.partitioning.children == []
        # Redshift-only sections stay None on every other engine.
        assert description.redshift_distribution is None
        assert description.redshift_stats is None
        # A table is not a matview, so there is no definition to carry.
        assert description.definition is None

    def test_describe_view_composes_every_section(
        self, ddb_cursor: DuckDbCursor, view_ref: TargetRef
    ) -> None:
        description = describe_view(ddb_cursor, view_ref, DDB)

        assert description.header.comment == SAMPLE_VIEW_COMMENT
        assert [c.name for c in description.columns] == [
            "id",
            "org_id",
            "email",
            "lifetime_value",
        ]
        assert description.definition.sql.startswith("CREATE VIEW")
        # DuckDB has no pg_rewrite, so lineage is empty and the report says why
        # through view_not_applicable() rather than showing an empty graph.
        assert description.upstream == []
        assert description.downstream == []
        assert description.privileges == []
        assert description.triggers == []

    def test_describe_schema_composes_every_section(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        ref = resolve_target(ddb_cursor, DDB, SAMPLE_SCHEMA)
        description = describe_schema(ddb_cursor, ref, DDB)

        assert description.header.name == SAMPLE_SCHEMA
        assert description.privileges == []
        assert description.default_privileges == []
        assert {item.name for item in description.contents} == {
            "customers",
            "orgs",
            "active_customers",
            "invoice_number_seq",
        }


class TestTopTables:
    """``dp db top-tables`` on an engine that cannot size a table.

    The interesting assertions here are the negative ones. DuckDB reports no
    per-relation byte size, so ``size_bytes`` and ``matched_bytes`` must be
    ``None`` rather than 0 -- "0 B" about a 40-row table is a claim the engine
    never made, and a script summing two engines' output has to be able to tell.
    """

    def test_the_ranking_is_by_rows_and_every_size_is_unknown(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        result = fetch_top_tables(ddb_cursor, DDB, [SAMPLE_SCHEMA_PREFIX], 10)

        assert [(row.schema, row.name, row.row_estimate) for row in result.rows] == [
            (SAMPLE_SCHEMA, "customers", 40),
            (SAMPLE_STAGING_SCHEMA, "customers_raw", 7),
            (SAMPLE_SCHEMA, "orgs", 3),
        ]
        assert all(row.size_bytes is None for row in result.rows)
        # None, not 0: see the class docstring.
        assert result.matched_bytes is None
        assert result.matched_count == 3
        # Every DuckDB row is an ordinary table, which is what makes
        # drop_statement's matview branch unreachable here.
        assert {row.kind for row in result.rows} == {"r"}
        assert all(row.owner is None for row in result.rows)

    def test_the_view_and_the_sequence_are_not_ranked(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        """``duckdb_tables()`` lists neither, so the relkind filter is implicit."""
        result = fetch_top_tables(ddb_cursor, DDB, [SAMPLE_SCHEMA_PREFIX], 10)
        assert "active_customers" not in {row.name for row in result.rows}
        assert "invoice_number_seq" not in {row.name for row in result.rows}

    def test_disk_bytes_is_the_file_minus_its_header(
        self, ddb_cursor: DuckDbCursor, ddb_params: DuckDbConnectionParams
    ) -> None:
        """Checked against ``os.stat``, not against another catalog read.

        This is the only assertion in the module DuckDB cannot make agree with
        itself, and it found something worth knowing: ``block_size *
        total_blocks`` is *slightly smaller* than the file, because DuckDB's
        fixed header sits outside the counted blocks (12,288 bytes on 1.5.5 --
        constant, measured at 100, 100k and 2M rows, so it is a header and not a
        rounding artefact).

        The bound is one block rather than that literal: a shortfall smaller than
        a single block cannot be a block of *data* going unreported, which is the
        thing that would matter, while pinning 12,288 would turn a DuckDB upgrade
        into a red build over a figure nothing depends on.
        """
        build_sample_schema(ddb_cursor)
        result = fetch_top_tables(ddb_cursor, DDB, [SAMPLE_SCHEMA_PREFIX], 10)
        file_bytes = Path(ddb_params.path).stat().st_size
        block_size = int(
            scalar(ddb_cursor, "SELECT block_size FROM pragma_database_size()")
        )

        assert 0 < result.disk_bytes <= file_bytes
        assert file_bytes - result.disk_bytes < block_size
        # Emphatically not the sum of the matched tables: there is no such sum.
        assert result.matched_bytes is None

    def test_the_escape_clause_distinguishes_a_literal_underscore(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        """The one thing a fake cursor cannot answer for this module.

        ``analytics_`` must match ``analytics_stg`` and *not* ``analytics``: an
        unescaped ``_`` is a single-character wildcard, so without
        ``ESCAPE '#'`` this prefix would match both and the caller would be told
        a schema it did not ask about is in the report.
        """
        prefix = f"{SAMPLE_SCHEMA}_"
        assert SAMPLE_STAGING_SCHEMA.startswith(prefix)

        result = fetch_top_tables(ddb_cursor, DDB, [prefix], 10)
        assert {row.schema for row in result.rows} == {SAMPLE_STAGING_SCHEMA}
        assert result.matched_count == 1

    def test_a_prefix_matching_nothing_still_reports_the_disk(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        """The early-return path, which must not invent a 0-byte total."""
        result = fetch_top_tables(ddb_cursor, DDB, ["no_such_prefix"], 10)
        assert result.rows == []
        assert result.matched_count == 0
        assert result.matched_bytes is None
        assert result.disk_bytes > 0

    def test_multiple_prefixes_are_ored_and_the_limit_caps_only_the_rows(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        """``matched_count`` spans every match; ``rows`` is the top N of them."""
        result = fetch_top_tables(
            ddb_cursor, DDB, [SAMPLE_SCHEMA_PREFIX, SAMPLE_OTHER_SCHEMA], 2
        )
        assert result.matched_count == 4
        assert len(result.rows) == 2
        assert [row.name for row in result.rows] == ["customers", "customers_raw"]

    def test_an_empty_prefix_list_or_a_zero_limit_asks_nothing(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        for result in (
            fetch_top_tables(ddb_cursor, DDB, [], 10),
            fetch_top_tables(ddb_cursor, DDB, [SAMPLE_SCHEMA_PREFIX], 0),
        ):
            assert result.rows == []
            assert result.matched_count == 0
            assert result.disk_bytes == 0

    def test_the_generated_drop_statement_runs_on_duckdb(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        """The command prints these for a human to run, so they must be valid.

        ``analytics_stg.customers_raw`` is dropped rather than ``customers``
        because the latter is a foreign-key parent -- and this asserts the DROP
        parses and takes effect, which is the only way to know the quoting is
        right.
        """
        result = fetch_top_tables(ddb_cursor, DDB, [SAMPLE_STAGING_SCHEMA], 10)
        (row,) = result.rows
        statement = drop_statement(row)
        assert statement == (
            f'DROP TABLE IF EXISTS "{SAMPLE_STAGING_SCHEMA}"."customers_raw";'
        )

        ddb_cursor.execute(statement)
        assert (
            scalar(
                ddb_cursor,
                "SELECT count(*) FROM duckdb_tables() WHERE table_name = ?",
                ["customers_raw"],
            )
            == 0
        )

    def test_the_drop_statement_quotes_an_identifier_containing_a_quote(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        """The doubled-quote escape, executed rather than string-compared.

        A table named ``has"quote`` is legal in all three engines and is the case
        where naive quoting produces a statement that either fails or -- worse --
        parses as something else.
        """
        ddb_cursor.execute(f'CREATE TABLE {SAMPLE_SCHEMA}."has""quote" (id INTEGER)')
        result = fetch_top_tables(ddb_cursor, DDB, [SAMPLE_SCHEMA_PREFIX], 10)
        (row,) = [r for r in result.rows if '"' in r.name]
        assert row.name == 'has"quote'

        ddb_cursor.execute(drop_statement(row))
        assert (
            scalar(
                ddb_cursor,
                "SELECT count(*) FROM duckdb_tables() WHERE table_name = ?",
                ['has"quote'],
            )
            == 0
        )

    def test_an_attached_database_s_tables_are_not_ranked(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str, tmp_path: Path
    ) -> None:
        """``duckdb_tables()`` spans every attached catalog, which would be wrong.

        A DROP generated for a table in another catalog would not resolve, and the
        disk figure belongs to one file -- so the ``current_database()`` filter is
        load-bearing rather than defensive. ATTACH is the only way to observe it.
        """
        other = tmp_path / "attached.duckdb"
        ddb_cursor.execute(f"ATTACH '{other}' AS attached")
        ddb_cursor.execute(f"CREATE SCHEMA attached.{SAMPLE_SCHEMA}")
        ddb_cursor.execute(
            f"CREATE TABLE attached.{SAMPLE_SCHEMA}.decoy AS SELECT * FROM range(999)"
        )

        # The decoy really is visible in the catalog this query reads...
        assert (
            scalar(
                ddb_cursor,
                "SELECT count(*) FROM duckdb_tables() WHERE table_name = 'decoy'",
            )
            == 1
        )
        # ...and it is still not in the ranking, despite outranking every row.
        result = fetch_top_tables(ddb_cursor, DDB, [SAMPLE_SCHEMA_PREFIX], 10)
        assert "decoy" not in {row.name for row in result.rows}
        assert result.matched_count == 3

    def test_every_engine_states_what_its_numbers_mean(self) -> None:
        """``SIZE_BASIS`` is what stops two engines' output being summed.

        Asserted for all three rather than just DuckDB: the entry exists so a
        reader can tell the figures apart, which only works if none is missing.
        """
        assert set(SIZE_BASIS) == set(SqlEngine)
        assert "estimated_size" in SIZE_BASIS[DDB]
        assert "pragma_database_size" in SIZE_BASIS[DDB]


def test_the_sample_schema_builds_on_an_in_memory_database_too(
    ddb_session: DuckDbSession,
) -> None:
    """``build_sample_schema`` depends on no fixture state, only on a cursor.

    Run against ``:memory:`` while a file-backed session is also open, which is
    legal because they are different databases -- the same-file constraint is
    pinned from the other side by
    ``test_a_conflicting_second_connection_is_a_service_error``.
    """
    params = DuckDbConnectionParams(path=MEMORY_PATH)
    with db_session(params) as memory, memory.cursor() as cursor:
        build_sample_schema(cursor)
        assert (
            scalar(cursor, f"SELECT count(*) FROM {SAMPLE_SCHEMA}.customers")
            == SAMPLE_ROW_COUNTS[f"{SAMPLE_SCHEMA}.customers"]
        )
    # The file-backed session is untouched by the in-memory one.
    assert scalar(ddb_session.cursor(), "SELECT count(*) FROM duckdb_tables()") == 0


class TestSchemaList:
    """``dp db schema list`` on DuckDB, which needed its own dialect.

    Not a refusal like the role commands: DuckDB genuinely has schemas, and
    listing them is a real answer. What it does not have is ``pg_roles`` — so the
    Postgres statement fails on it outright rather than degrading, and the reason
    for a third dialect is a probe rather than a preference.
    """

    def test_duckdb_has_no_pg_roles_to_join(self, ddb_cursor: DuckDbCursor) -> None:
        """The measurement the DuckDB dialect exists because of.

        Paired with the dialect's own statement below: if a future DuckDB grows
        ``pg_roles``, this fails and the owner join becomes worth reconsidering,
        rather than the workaround outliving its reason.
        """
        import duckdb

        with pytest.raises(duckdb.CatalogException, match="pg_roles"):
            ddb_cursor.execute("SELECT rolname FROM pg_roles LIMIT 1")

    def test_the_duckdb_statement_joins_no_owner_catalog(self) -> None:
        from dataplat.services.db.schema_dialects import _LIST_DUCKDB

        assert "pg_roles" not in _LIST_DUCKDB
        assert "pg_user" not in _LIST_DUCKDB

    def test_the_sample_schemas_are_listed_with_their_counts(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        """Counts compared against the fixture's own declared contents."""
        rows = {r.name: r for r in DuckDbSchemaDialect().list_schemas(ddb_cursor)}

        assert set(rows) >= {
            SAMPLE_SCHEMA,
            SAMPLE_STAGING_SCHEMA,
            SAMPLE_OTHER_SCHEMA,
        }
        expected_tables = sum(
            1
            for name, relkind in SAMPLE_RELATIONS.items()
            if relkind == "r" and name.startswith(f"{SAMPLE_SCHEMA}.")
        )
        expected_views = sum(
            1
            for name, relkind in SAMPLE_RELATIONS.items()
            if relkind == "v" and name.startswith(f"{SAMPLE_SCHEMA}.")
        )
        assert rows[SAMPLE_SCHEMA].tables == expected_tables
        assert rows[SAMPLE_SCHEMA].views == expected_views

    def test_the_standalone_sequence_is_counted_as_other(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        """Nothing a drop would destroy may go uncounted — sequences included."""
        rows = {r.name: r for r in DuckDbSchemaDialect().list_schemas(ddb_cursor)}

        assert rows[SAMPLE_SCHEMA].other >= 1

    def test_owner_is_the_one_implicit_user(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        """Every connection is the same user, so ownership cannot differ by row.

        Reported as `duckdb` rather than blank or `?`: it is a true statement
        about this engine, not missing information.
        """
        rows = DuckDbSchemaDialect().list_schemas(ddb_cursor)

        assert {r.owner for r in rows} == {"duckdb"}

    def test_the_default_schema_is_visible(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        """`main` is DuckDB's default schema, not a system one.

        It is the analogue of Postgres's `public`, which the listing also shows.
        Hiding it was a real bug: a database whose tables all live in `main` —
        the common case for a file nobody bothered to organise — listed as empty.
        """
        names = {r.name for r in DuckDbSchemaDialect().list_schemas(ddb_cursor)}

        assert "main" in names

    def test_duckdbs_catalog_schemas_never_reach_pg_namespace(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        """Which is why --include-system has nothing extra to show here.

        DuckDB flags `information_schema` and `pg_catalog` as `internal` in
        `duckdb_schemas()` and omits them from `pg_namespace` altogether — unlike
        Postgres, where they are ordinary rows that a predicate has to exclude.
        Asserted from both sides so the asymmetry is recorded rather than
        rediscovered.
        """
        dialect = DuckDbSchemaDialect()

        ddb_cursor.execute("SELECT nspname FROM pg_namespace")
        exposed = {name for (name,) in ddb_cursor.fetchall()}
        assert "pg_catalog" not in exposed
        assert "information_schema" not in exposed

        ddb_cursor.execute(
            "SELECT DISTINCT schema_name FROM duckdb_schemas() WHERE internal"
        )
        internal = {name for (name,) in ddb_cursor.fetchall()}
        assert {"pg_catalog", "information_schema"} <= internal

        with_system = {
            r.name for r in dialect.list_schemas(ddb_cursor, include_system=True)
        }
        without = {r.name for r in dialect.list_schemas(ddb_cursor)}
        assert with_system == without

    def test_a_like_pattern_filters_and_the_underscore_stays_a_wildcard(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        """`analytics_%` matches `analytics_stg` but not `analytics` itself.

        The same distinction top-tables needed ESCAPE '#' for, from the other
        side: here the wildcard is wanted, so the pattern is passed through
        unescaped and `_` does its LIKE job.
        """
        rows = DuckDbSchemaDialect().list_schemas(ddb_cursor, like="analytics_%")

        names = {r.name for r in rows}
        assert SAMPLE_STAGING_SCHEMA in names
        assert SAMPLE_SCHEMA not in names

    def test_a_glob_pattern_reaches_both(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        rows = DuckDbSchemaDialect().list_schemas(
            ddb_cursor, like=glob_to_like("analytics*")
        )

        names = {r.name for r in rows}
        assert {SAMPLE_SCHEMA, SAMPLE_STAGING_SCHEMA} <= names

    def test_quotas_are_never_invented(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        """DuckDB has no schema quotas, so the values stay unknown, not zero."""
        rows = DuckDbSchemaDialect().list_schemas(ddb_cursor)

        assert all(r.quota_mb is None and r.used_mb is None for r in rows)

    def test_schema_exists_agrees_with_the_catalog(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        dialect = DuckDbSchemaDialect()

        assert dialect.schema_exists(ddb_cursor, SAMPLE_SCHEMA) is True
        assert dialect.schema_exists(ddb_cursor, "dp_absent_schema") is False


class TestSchemaDdl:
    """What DuckDB can and cannot do to a schema, asked of DuckDB.

    The capability matrix refuses ``schema grant/revoke/alter`` there and allows
    ``create``/``drop``. Both halves are probed: a gate that refuses too much is
    as wrong as one that refuses too little, and only the engine can settle it.
    """

    def test_create_and_drop_round_trip(self, ddb_cursor: DuckDbCursor) -> None:
        dialect = DuckDbSchemaDialect()
        create = build_create_plan([CreateSchemaSpec("made")], dialect)
        for op in create.ops:
            ddb_cursor.execute(op.statement)
        assert dialect.schema_exists(ddb_cursor, "made") is True

        for op in build_drop_plan(["made"]).ops:
            ddb_cursor.execute(op.statement)
        assert dialect.schema_exists(ddb_cursor, "made") is False

    def test_if_not_exists_is_idempotent(self, ddb_cursor: DuckDbCursor) -> None:
        plan = build_create_plan(
            [CreateSchemaSpec("twice", if_not_exists=True)], DuckDbSchemaDialect()
        )
        for _ in range(2):
            for op in plan.ops:
                ddb_cursor.execute(op.statement)

        assert DuckDbSchemaDialect().schema_exists(ddb_cursor, "twice") is True

    def test_restrict_refuses_a_non_empty_schema(
        self, ddb_cursor: DuckDbCursor, ddb_module: ModuleType
    ) -> None:
        """Same semantics as Postgres, and worth proving rather than assuming."""
        ddb_cursor.execute("CREATE SCHEMA full_one")
        ddb_cursor.execute("CREATE TABLE full_one.t(i INTEGER)")

        with pytest.raises(ddb_module.Error):
            for op in build_drop_plan(["full_one"]).ops:
                ddb_cursor.execute(op.statement)

    def test_cascade_destroys_the_contents(self, ddb_cursor: DuckDbCursor) -> None:
        ddb_cursor.execute("CREATE SCHEMA doomed")
        ddb_cursor.execute("CREATE TABLE doomed.t(i INTEGER)")

        for op in build_drop_plan(["doomed"], cascade=True).ops:
            ddb_cursor.execute(op.statement)

        assert DuckDbSchemaDialect().schema_exists(ddb_cursor, "doomed") is False

    def test_authorization_does_not_parse_here(
        self, ddb_cursor: DuckDbCursor, ddb_module: ModuleType
    ) -> None:
        """Why the dialect drops --owner and the CLI refuses it.

        Built by hand rather than through the plan builder, precisely because the
        builder will not emit it — this is the statement that would run if it did.
        """
        with pytest.raises(ddb_module.ParserException):
            ddb_cursor.execute("CREATE SCHEMA authorized AUTHORIZATION bob")

    def test_grant_does_not_parse_here(
        self, ddb_cursor: DuckDbCursor, ddb_module: ModuleType
    ) -> None:
        """The measured fact behind the schema_privileges refusal."""
        ddb_cursor.execute("CREATE SCHEMA grantable")

        with pytest.raises(ddb_module.ParserException):
            ddb_cursor.execute("GRANT USAGE ON SCHEMA grantable TO bob")

    def test_alter_schema_is_not_implemented_here(
        self, ddb_cursor: DuckDbCursor, ddb_module: ModuleType
    ) -> None:
        """The measured fact behind the schema_alter refusal.

        A NotImplementedException rather than a parser error, which is why that
        capability's reason says "does not implement" — this one may well change
        in a future DuckDB, unlike having no users at all.
        """
        ddb_cursor.execute("CREATE SCHEMA renamable")

        with pytest.raises(ddb_module.NotImplementedException):
            ddb_cursor.execute("ALTER SCHEMA renamable RENAME TO renamed")

    def test_the_capability_declarations_match_those_probes(self) -> None:
        """Pair the engine facts above with what capabilities.py claims.

        Neither half alone is enough: the declaration without the probe is a
        memory of a doc, and the probe without the declaration is a fact nothing
        consumes.
        """
        caps = capabilities_for(DDB)

        assert not caps.support(Capability.schema_privileges)
        assert not caps.support(Capability.schema_alter)
        # And the two that are allowed, so the gate cannot quietly widen.
        assert "GRANT" in caps.support(Capability.schema_privileges).reason
        assert "ALTER SCHEMA" in caps.support(Capability.schema_alter).reason

    def test_the_dialect_refuses_before_any_sql_is_built(self) -> None:
        """The service-layer half of the CLI's gate.

        A library caller bypassing the CLI must not be able to construct a
        statement this engine cannot parse.
        """
        dialect = DuckDbSchemaDialect()

        with pytest.raises(ValidationError, match="GRANT"):
            dialect.privilege_op(SchemaPrivilege.usage, "s", "bob", ParentKind.user)
        with pytest.raises(ValidationError, match="ALTER SCHEMA"):
            dialect.rename_schema("s", "t")
        with pytest.raises(ValidationError, match="owner"):
            dialect.alter_owner("s", "bob")

    def test_nothing_can_be_held_where_nothing_can_be_granted(
        self, ddb_cursor: DuckDbCursor
    ) -> None:
        assert (
            DuckDbSchemaDialect().held_schema_privileges(ddb_cursor, ["main"], ["bob"])
            == set()
        )

    def test_a_glob_prefix_does_not_reach_a_neighbouring_schema(
        self, ddb_cursor: DuckDbCursor, ddb_sample_schema: str
    ) -> None:
        """DuckDB honours ESCAPE too — asserted, not assumed from Postgres.

        The fixture gives us `analytics` and `analytics_stg`. An unescaped
        `analytics_*` matches both; escaped, it matches only the one whose name
        really continues after a literal underscore.
        """
        dialect = DuckDbSchemaDialect()

        escaped = {
            r.name
            for r in dialect.list_schemas(ddb_cursor, like=glob_to_like("analytics_*"))
        }
        assert escaped == {SAMPLE_STAGING_SCHEMA}

        unescaped = {
            r.name for r in dialect.list_schemas(ddb_cursor, like="analytics_%")
        }
        assert unescaped == {SAMPLE_STAGING_SCHEMA}
