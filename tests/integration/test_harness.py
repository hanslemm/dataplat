"""Tests for the integration harness itself.

Four other test suites are built on these fixtures, so the fixtures need their
own proof: that the DSN resolves, that the connection is live, that rollback
genuinely isolates one test from the next, that ``sample_schema`` really
contains every object kind it advertises, and that ``temp_role`` cleans up.

Some checks are deliberately order-dependent pairs -- one test writes, the
following test asserts the write is gone. Those pairs must stay adjacent and in
their current order; running one half alone fails with an explicit message.

``psycopg`` is imported inside the test bodies that need it, never at module
level: it ships in the optional ``db`` extra, and a module-level import would
turn its absence into a collection error instead of the skip the harness
promises.
"""

from __future__ import annotations

import itertools
import os
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import pytest

from tests.integration.conftest import (
    DEFAULT_DSN,
    SAMPLE_INDEXES,
    SAMPLE_POLICY,
    SAMPLE_RELATIONS,
    SAMPLE_TRIGGER,
    SAMPLE_TRIGGER_FUNCTION,
    TempRoleFactory,
    _redact,
    _truthy,
    _unavailable,
    _unique_ident,
)

if TYPE_CHECKING:
    from psycopg import Connection, Cursor
    from psycopg.rows import TupleRow

pytestmark = pytest.mark.integration


def _scalar(cursor: Cursor[TupleRow], query: str, *params: Any) -> Any:
    """Run a single-value query and return that value (None when no row)."""
    cursor.execute(query, params or None)
    row = cursor.fetchone()
    return None if row is None else row[0]


# --- DSN resolution and the skip/require rule ------------------------------
# The helpers below are pure, so these tests run on a machine with no database
# at all. That is deliberate: the branch deciding "skip" vs "error" is the one
# that can silently switch CI off, so it must be tested without needing the
# very thing it gates.


def test_pg_dsn_resolves_to_env_var_or_default(pg_dsn: str) -> None:
    expected = os.environ.get("DP_TEST_PG_DSN") or DEFAULT_DSN
    assert pg_dsn == expected
    assert pg_dsn.startswith("postgres")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, False),
        ("", False),
        ("  ", False),
        ("0", False),
        ("false", False),
        ("FALSE", False),
        ("no", False),
        ("off", False),
        ("1", True),
        ("true", True),
        ("yes", True),
        ("anything", True),
    ],
)
def test_truthy_only_treats_explicit_negatives_as_false(
    raw: str | None, expected: bool
) -> None:
    assert _truthy(raw) is expected


# Both outcomes are caught and then discriminated with isinstance, rather than
# naming one of them in pytest.raises. If the rule ever regressed to the lenient
# direction, a narrow `pytest.raises(pytest.fail.Exception)` would let the
# Skipped propagate and pytest would record the test as *skipped* -- green, and
# blind to precisely the regression it exists to catch.
_OUTCOMES = (pytest.fail.Exception, pytest.skip.Exception)


def test_unavailable_skips_when_not_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DP_TEST_PG_REQUIRED", raising=False)
    with pytest.raises(_OUTCOMES) as excinfo:
        _unavailable("connection refused", DEFAULT_DSN)
    assert isinstance(excinfo.value, pytest.skip.Exception)
    assert "connection refused" in str(excinfo.value)


def test_unavailable_errors_when_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DP_TEST_PG_REQUIRED", "1")
    # pytest.fail's exception -- not skip's -- is what makes an unreachable
    # database an ERROR in CI rather than a silently green run.
    with pytest.raises(_OUTCOMES) as excinfo:
        _unavailable("connection refused", DEFAULT_DSN)
    assert isinstance(excinfo.value, pytest.fail.Exception)
    assert not isinstance(excinfo.value, pytest.skip.Exception)


def test_unavailable_message_explains_how_to_get_a_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DP_TEST_PG_REQUIRED", raising=False)
    with pytest.raises(_OUTCOMES) as excinfo:
        _unavailable("boom", DEFAULT_DSN)
    message = str(excinfo.value)
    assert "DP_TEST_PG_DSN" in message
    assert "DP_TEST_PG_REQUIRED" in message
    assert "docker run" in message
    assert "dp-pg-test" in message


def test_unavailable_message_redacts_the_password() -> None:
    redacted = _redact("postgresql://postgres:sup3rs3cret@127.0.0.1:55432/dp")
    assert "sup3rs3cret" not in redacted
    assert "postgres:***@127.0.0.1:55432" in redacted


def test_unique_ident_keeps_uniqueness_under_63_byte_truncation() -> None:
    counter = itertools.count()
    long_label = "a_very_long_test_name" * 6
    first = _unique_ident("s", counter, long_label)
    second = _unique_ident("s", counter, long_label)
    # Postgres silently truncates identifiers past 63 bytes, which would fuse
    # these two names into one if the counter sat at the tail instead of the
    # front.
    assert first != second
    assert len(first) <= 63
    assert len(second) <= 63
    assert str(os.getpid()) in first


# --- Connection and rollback isolation ------------------------------------


def test_pg_conn_is_live_and_not_autocommit(pg_conn: Connection[TupleRow]) -> None:
    assert pg_conn.closed is False
    # Rollback-based isolation only works while autocommit stays off; row
    # factory must stay tuples because the services unpack rows positionally.
    assert pg_conn.autocommit is False
    with pg_conn.cursor() as cursor:
        cursor.execute("SELECT 1, 'two'")
        assert cursor.fetchone() == (1, "two")
    pg_conn.rollback()


def test_pg_cursor_talks_to_the_expected_server(pg_cursor: Cursor[TupleRow]) -> None:
    assert _scalar(pg_cursor, "SELECT current_database()") == "dataplat_test"
    assert _scalar(pg_cursor, "SELECT current_setting('server_version_num')::int") >= (
        160000
    )
    # The role suites need CREATE ROLE, so fail loudly here rather than in a
    # dozen confusing permission errors later.
    can_create_roles = _scalar(
        pg_cursor,
        """
        SELECT rolsuper OR rolcreaterole
        FROM pg_roles WHERE rolname = current_user
        """,
    )
    assert can_create_roles is True


def test_pg_cursor_can_read_pg_stat_statements(pg_cursor: Cursor[TupleRow]) -> None:
    # long-queries history reads this extension; if it is not installed the
    # suite should say so here instead of inside that agent's tests.
    assert _scalar(pg_cursor, "SELECT count(*) >= 0 FROM pg_stat_statements") is True


_LEAK_PROBE_SCHEMA = f"dp_it_leak_probe_{os.getpid()}"


def test_pg_cursor_write_that_the_next_test_must_not_see(
    pg_cursor: Cursor[TupleRow],
) -> None:
    """Half one of the isolation proof: create objects and do not clean up."""
    pg_cursor.execute(f"CREATE SCHEMA {_LEAK_PROBE_SCHEMA}")
    pg_cursor.execute(f"CREATE TABLE {_LEAK_PROBE_SCHEMA}.leaked (id int)")
    present = _scalar(
        pg_cursor,
        "SELECT count(*) FROM pg_namespace WHERE nspname = %s",
        _LEAK_PROBE_SCHEMA,
    )
    assert present == 1


def test_pg_cursor_rolled_back_the_previous_test(pg_cursor: Cursor[TupleRow]) -> None:
    """Half two: the previous test's schema must be gone, not merely unseen."""
    present = _scalar(
        pg_cursor,
        "SELECT count(*) FROM pg_namespace WHERE nspname = %s",
        _LEAK_PROBE_SCHEMA,
    )
    assert present == 0


def test_pg_cursor_tolerates_a_statement_that_aborts_the_transaction(
    pg_cursor: Cursor[TupleRow],
) -> None:
    """A service function running invalid SQL is the expected outcome here.

    The fixture therefore has to survive an aborted transaction and still hand
    the *next* test a usable one.
    """
    import psycopg

    with pytest.raises(psycopg.Error):
        pg_cursor.execute("SELECT no_such_column_anywhere")


def test_pg_cursor_is_usable_after_the_aborted_transaction(
    pg_cursor: Cursor[TupleRow],
) -> None:
    assert _scalar(pg_cursor, "SELECT 1") == 1


def test_pg_autocommit_cursor_holds_no_transaction(
    pg_autocommit_cursor: Cursor[TupleRow],
) -> None:
    from psycopg.pq import TransactionStatus

    connection = pg_autocommit_cursor.connection
    assert connection.autocommit is True
    pg_autocommit_cursor.execute("SELECT 1")
    # IDLE rather than INTRANS is the proof: statements that refuse to run in a
    # transaction block, e.g. CREATE INDEX CONCURRENTLY, will work here.
    assert connection.info.transaction_status is TransactionStatus.IDLE


def test_pg_autocommit_cursor_is_a_separate_connection(
    pg_cursor: Cursor[TupleRow], pg_autocommit_cursor: Cursor[TupleRow]
) -> None:
    assert pg_cursor.connection is not pg_autocommit_cursor.connection
    assert _scalar(pg_cursor, "SELECT pg_backend_pid()") != _scalar(
        pg_autocommit_cursor, "SELECT pg_backend_pid()"
    )


# --- sample_schema inventory ----------------------------------------------


def test_sample_schema_name_is_unique_per_test(sample_schema: str) -> None:
    assert sample_schema.startswith("dp_it_s")
    assert str(os.getpid()) in sample_schema
    assert len(sample_schema) <= 63


def test_sample_schema_exists_with_a_comment(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    comment = _scalar(
        pg_cursor,
        """
        SELECT obj_description(n.oid, 'pg_namespace')
        FROM pg_namespace n WHERE n.nspname = %s
        """,
        sample_schema,
    )
    assert comment == "dataplat integration fixture"


def test_sample_schema_contains_exactly_the_promised_relations(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    pg_cursor.execute(
        """
        SELECT c.relname, c.relkind
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s
          AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
        """,
        (sample_schema,),
    )
    found: dict[str, str] = dict(pg_cursor.fetchall())
    # Identity columns create their own sequences; those are incidental, so
    # compare only the objects the fixture names.
    named = {name: kind for name, kind in found.items() if not name.endswith("_id_seq")}
    assert named == SAMPLE_RELATIONS


def test_sample_schema_has_primary_key_foreign_key_check_and_unique(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    pg_cursor.execute(
        """
        SELECT c.contype, c.conname, pg_get_constraintdef(c.oid, true)
        FROM pg_constraint c
        JOIN pg_class rel ON rel.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = rel.relnamespace
        WHERE n.nspname = %s AND rel.relname = 'customers'
        """,
        (sample_schema,),
    )
    by_type = {contype: (name, ddl) for contype, name, ddl in pg_cursor.fetchall()}
    assert set(by_type) >= {"p", "f", "c"}, by_type
    assert "PRIMARY KEY (id)" in by_type["p"][1]
    fk_definition = by_type["f"][1]
    assert "REFERENCES" in fk_definition
    assert "ON UPDATE RESTRICT" in fk_definition
    assert "ON DELETE CASCADE" in fk_definition
    assert by_type["c"][0] == "customers_status_check"

    # UNIQUE lives on the foreign key's target table.
    unique = _scalar(
        pg_cursor,
        """
        SELECT c.conname
        FROM pg_constraint c
        JOIN pg_class rel ON rel.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = rel.relnamespace
        WHERE n.nspname = %s AND rel.relname = 'orgs' AND c.contype = 'u'
        """,
        sample_schema,
    )
    assert unique == "orgs_name_key"


def test_sample_schema_has_not_null_default_and_identity_columns(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    pg_cursor.execute(
        """
        SELECT a.attname, a.attnotnull, a.attidentity,
               pg_get_expr(ad.adbin, ad.adrelid)
        FROM pg_attribute a
        LEFT JOIN pg_attrdef ad
               ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relname = 'customers'
          AND a.attnum > 0 AND NOT a.attisdropped
        """,
        (sample_schema,),
    )
    columns = {
        name: (notnull, identity, default)
        for name, notnull, identity, default in pg_cursor.fetchall()
    }
    assert columns["email"][0] is True, "email must be NOT NULL"
    assert columns["updated_at"][0] is False, "updated_at must be nullable"
    assert columns["id"][1] == "d", "id must be GENERATED BY DEFAULT AS IDENTITY"
    assert columns["status"][2] == "'active'::text"
    assert columns["created_at"][2] == "now()"


def test_sample_schema_has_table_column_and_view_comments(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    table_comment = _scalar(
        pg_cursor,
        "SELECT obj_description(%s::regclass, 'pg_class')",
        f"{sample_schema}.customers",
    )
    assert table_comment == "Customer master (integration fixture)."

    column_comment = _scalar(
        pg_cursor,
        """
        SELECT col_description(a.attrelid, a.attnum)
        FROM pg_attribute a
        WHERE a.attrelid = %s::regclass AND a.attname = 'email'
        """,
        f"{sample_schema}.customers",
    )
    assert column_comment == "Primary contact address."

    view_comment = _scalar(
        pg_cursor,
        "SELECT obj_description(%s::regclass, 'pg_class')",
        f"{sample_schema}.active_customers",
    )
    assert view_comment == "Non-churned customers."


def test_sample_schema_has_a_secondary_index_and_a_partial_index(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    pg_cursor.execute(
        """
        SELECT ic.relname, i.indisprimary,
               pg_get_expr(i.indpred, i.indrelid, true) AS predicate
        FROM pg_index i
        JOIN pg_class ic ON ic.oid = i.indexrelid
        JOIN pg_class rel ON rel.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = rel.relnamespace
        WHERE n.nspname = %s AND rel.relname = 'customers'
        """,
        (sample_schema,),
    )
    indexes = {
        name: (primary, predicate) for name, primary, predicate in pg_cursor.fetchall()
    }
    for expected in SAMPLE_INDEXES:
        assert expected in indexes, indexes
    assert indexes["customers_org_id_idx"][0] is False
    assert indexes["customers_org_id_idx"][1] is None
    # describe renders index predicates, so an index whose indpred is NULL
    # would leave that branch untested.
    assert indexes["customers_active_email_idx"][1] == "status = 'active'::text"


def test_sample_schema_view_and_matview_are_readable(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    definition = _scalar(
        pg_cursor,
        "SELECT pg_get_viewdef(%s::regclass, true)",
        f"{sample_schema}.active_customers",
    )
    assert "FROM" in definition

    populated = _scalar(
        pg_cursor,
        "SELECT relispopulated FROM pg_class WHERE oid = %s::regclass",
        f"{sample_schema}.customers_per_org",
    )
    assert populated is True

    matview_rows = _scalar(
        pg_cursor, f"SELECT count(*) FROM {sample_schema}.customers_per_org"
    )
    assert matview_rows == 3
    view_rows = _scalar(
        pg_cursor, f"SELECT count(*) FROM {sample_schema}.active_customers"
    )
    assert view_rows > 0


def test_sample_schema_has_a_range_partitioned_table_with_partitions(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    strategy = _scalar(
        pg_cursor,
        "SELECT partstrat FROM pg_partitioned_table WHERE partrelid = %s::regclass",
        f"{sample_schema}.events",
    )
    assert strategy == "r"

    pg_cursor.execute(
        """
        SELECT child.relname, pg_get_expr(child.relpartbound, child.oid, true)
        FROM pg_inherits inh
        JOIN pg_class child ON child.oid = inh.inhrelid
        WHERE inh.inhparent = %s::regclass
        ORDER BY child.relname
        """,
        (f"{sample_schema}.events",),
    )
    children: dict[str, str] = dict(pg_cursor.fetchall())
    assert set(children) == {"events_2024", "events_2025"}
    assert "FROM ('2024-01-01') TO ('2025-01-01')" in children["events_2024"]

    event_rows = _scalar(pg_cursor, f"SELECT count(*) FROM {sample_schema}.events")
    assert event_rows == 30


def test_sample_schema_has_a_standalone_sequence(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    start_value = _scalar(
        pg_cursor,
        """
        SELECT start_value FROM pg_sequences
        WHERE schemaname = %s AND sequencename = 'invoice_number_seq'
        """,
        sample_schema,
    )
    assert start_value == 1000


def test_sample_schema_has_a_trigger_and_its_function(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    pg_cursor.execute(
        """
        SELECT t.tgname, pg_get_triggerdef(t.oid, true)
        FROM pg_trigger t
        WHERE t.tgrelid = %s::regclass AND NOT t.tgisinternal
        """,
        (f"{sample_schema}.customers",),
    )
    triggers: dict[str, str] = dict(pg_cursor.fetchall())
    assert list(triggers) == [SAMPLE_TRIGGER]
    assert "BEFORE UPDATE" in triggers[SAMPLE_TRIGGER]

    language = _scalar(
        pg_cursor,
        """
        SELECT l.lanname
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        JOIN pg_language l ON l.oid = p.prolang
        WHERE n.nspname = %s AND p.proname = %s
        """,
        sample_schema,
        SAMPLE_TRIGGER_FUNCTION,
    )
    assert language == "plpgsql"


def test_sample_schema_trigger_actually_fires(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    # A trigger whose body is broken still satisfies the catalog assertions
    # above, so make it run.
    pg_cursor.execute(
        f"UPDATE {sample_schema}.customers SET status = 'trial' WHERE id = 1"
    )
    touched = _scalar(
        pg_cursor,
        f"SELECT updated_at IS NOT NULL FROM {sample_schema}.customers WHERE id = 1",
    )
    assert touched is True


def test_sample_schema_has_rls_enabled_with_a_policy(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    rls_enabled = _scalar(
        pg_cursor,
        "SELECT relrowsecurity FROM pg_class WHERE oid = %s::regclass",
        f"{sample_schema}.secrets",
    )
    assert rls_enabled is True

    pg_cursor.execute(
        """
        SELECT pol.polname, pol.polcmd,
               pg_get_expr(pol.polqual, pol.polrelid, true),
               pg_get_expr(pol.polwithcheck, pol.polrelid, true)
        FROM pg_policy pol
        WHERE pol.polrelid = %s::regclass
        """,
        (f"{sample_schema}.secrets",),
    )
    policies = pg_cursor.fetchall()
    assert len(policies) == 1
    name, command, using, with_check = policies[0]
    assert name == SAMPLE_POLICY
    assert command == "*"  # '*' is ALL
    assert using is not None
    assert with_check is not None


def test_sample_schema_tables_are_analyzed_and_sized(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    # top-tables ranks on reltuples and pg_total_relation_size; both are
    # meaningless (-1 / 0) on a schema that was never populated or ANALYZEd.
    pg_cursor.execute(
        """
        SELECT c.reltuples, pg_total_relation_size(c.oid)
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relname = 'customers'
        """,
        (sample_schema,),
    )
    row = pg_cursor.fetchone()
    assert row is not None
    reltuples, size_bytes = row
    assert reltuples == 40
    assert size_bytes > 0


def test_sample_schema_has_a_default_privilege_entry(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    count = _scalar(
        pg_cursor,
        """
        SELECT count(*) FROM pg_default_acl d
        JOIN pg_namespace n ON n.oid = d.defaclnamespace
        WHERE n.nspname = %s
        """,
        sample_schema,
    )
    assert count == 1


def test_sample_schema_is_visible_to_information_schema(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    # The orphans service reads information_schema.tables and pg_matviews
    # rather than pg_class, so confirm the uncommitted fixture reaches both.
    expected_tables = sum(1 for kind in SAMPLE_RELATIONS.values() if kind in "rpv")
    tables = _scalar(
        pg_cursor,
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = %s",
        sample_schema,
    )
    assert tables == expected_tables

    matviews = _scalar(
        pg_cursor,
        "SELECT count(*) FROM pg_matviews WHERE schemaname = %s",
        sample_schema,
    )
    assert matviews == 1


_SCHEMA_FROM_PREVIOUS_TEST: list[str] = []


def test_sample_schema_records_its_name_for_the_next_test(sample_schema: str) -> None:
    _SCHEMA_FROM_PREVIOUS_TEST.append(sample_schema)


def test_sample_schema_was_dropped_after_the_previous_test(
    pg_cursor: Cursor[TupleRow],
) -> None:
    assert _SCHEMA_FROM_PREVIOUS_TEST, "the preceding test must run first"
    present = _scalar(
        pg_cursor,
        "SELECT count(*) FROM pg_namespace WHERE nspname = %s",
        _SCHEMA_FROM_PREVIOUS_TEST[0],
    )
    assert present == 0


# --- temp_role -------------------------------------------------------------


def test_temp_role_creates_a_nologin_role_by_default(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory
) -> None:
    name = temp_role("reader")
    assert name.startswith("dp_it_r")
    assert len(name) <= 63
    pg_cursor.execute(
        "SELECT rolcanlogin, rolcreatedb FROM pg_roles WHERE rolname = %s", (name,)
    )
    assert pg_cursor.fetchone() == (False, False)


def test_temp_role_honours_login_password_and_options(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory
) -> None:
    name = temp_role("writer", login=True, password="s3cret", options=["createdb"])
    pg_cursor.execute(
        """
        SELECT rolcanlogin, rolcreatedb, rolpassword IS NOT NULL
        FROM pg_authid WHERE rolname = %s
        """,
        (name,),
    )
    assert pg_cursor.fetchone() == (True, True, True)


def test_temp_role_rejects_an_option_that_is_not_a_bare_keyword(
    temp_role: TempRoleFactory,
) -> None:
    with pytest.raises(ValueError, match="invalid role option"):
        temp_role("evil", options=["CREATEDB; DROP DATABASE dataplat_test"])


def test_temp_role_names_do_not_collide(temp_role: TempRoleFactory) -> None:
    assert temp_role("dup") != temp_role("dup")


def test_temp_role_can_take_ownership_of_objects(
    pg_cursor: Cursor[TupleRow], temp_role: TempRoleFactory, sample_schema: str
) -> None:
    # ALTER ... OWNER TO is transactional too, so a role that acquired objects
    # still vanishes on rollback -- that is what lets the role suites lean on
    # pg_cursor instead of hand-rolled cleanup.
    name = temp_role("owner")
    pg_cursor.execute(f"ALTER TABLE {sample_schema}.customers OWNER TO {name}")
    owner = _scalar(
        pg_cursor,
        "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = %s::regclass",
        f"{sample_schema}.customers",
    )
    assert owner == name


# Any ACL entry, anywhere in this database, that mentions a harness-generated
# role -- as grantee or as grantor -- plus any entry pointing at a role oid that
# no longer exists. The second half matters because pg_get_userbyid() renders a
# dropped oid as "unknown (OID=...)", which the name patterns would miss.
# Grantee oid 0 is PUBLIC, which is no role and so is exempt from that check;
# a grantor is always a real role.
_LEAKED_ACL_SQL = r"""
SELECT count(*) FROM (
    SELECT (aclexplode(n.nspacl)).* FROM pg_namespace n
    UNION ALL
    SELECT (aclexplode(c.relacl)).* FROM pg_class c
) AS acl (grantor, grantee, privilege_type, is_grantable)
WHERE pg_get_userbyid(grantee) LIKE 'dp\_it\_%'
   OR pg_get_userbyid(grantor) LIKE 'dp\_it\_%'
   OR (grantee <> 0
       AND NOT EXISTS (SELECT 1 FROM pg_roles r WHERE r.oid = grantee))
   OR NOT EXISTS (SELECT 1 FROM pg_roles r WHERE r.oid = grantor)
"""


@pytest.fixture
def temp_roles_left_no_trace(pg_cursor: Cursor[TupleRow]) -> Iterator[None]:
    """Assert, after ``temp_role`` tore down, that its DROPs actually worked.

    Fixture ordering is the whole mechanism: pytest finalises in reverse setup
    order, so a test that requests this fixture *before* ``temp_role`` gets this
    check run after that fixture's DROP statements but before ``pg_cursor``
    rolls the transaction back. That is the only window in which the teardown's
    own effect is observable -- assert after the rollback and every leak looks
    cleaned up, because the rollback cleans up regardless.

    Requesting it in the wrong order fails loudly rather than silently passing:
    the check would then run while the roles still exist.
    """
    yield
    leaked_roles = _scalar(
        pg_cursor,
        r"SELECT count(*) FROM pg_roles WHERE rolname LIKE 'dp\_it\_r%'",
    )
    leaked_acl = _scalar(pg_cursor, _LEAKED_ACL_SQL)
    assert (leaked_roles, leaked_acl) == (0, 0)


# Deliberately not `sample_schema`: that fixture's own teardown drops the schema,
# and it would take the ACL entries under test with it, leaving the sweep in
# `temp_roles_left_no_trace` nothing to find.
_DELEGATED_GRANT_SCHEMA = f"dp_it_delegated_{os.getpid()}"


def test_temp_role_teardown_unwinds_a_delegated_grant(
    pg_cursor: Cursor[TupleRow],
    temp_roles_left_no_trace: None,
    temp_role: TempRoleFactory,
) -> None:
    """A grant one temp role made to another must not defeat the teardown.

    ``DROP OWNED BY r CASCADE`` is a ``REVOKE ALL ... FROM r CASCADE`` attributed
    to this session's grantor, so it cannot remove an entry a *different* role
    granted. While the fixture dropped each role right after draining that one
    role, the DROP ROLE here failed with DependentObjectsStillExist, and the
    only way to test a delegated grant was to REVOKE it by hand first.

    The test deliberately leaves the grant in place; the assertions live in
    ``temp_roles_left_no_trace``, which runs after the teardown.
    """
    delegate = temp_role("delegate")
    onward = temp_role("onward")
    pg_cursor.execute(f"CREATE SCHEMA {_DELEGATED_GRANT_SCHEMA}")
    pg_cursor.execute(
        f"GRANT USAGE ON SCHEMA {_DELEGATED_GRANT_SCHEMA} "
        f"TO {delegate} WITH GRANT OPTION"
    )
    pg_cursor.execute(f"SET ROLE {delegate}")
    pg_cursor.execute(f"GRANT USAGE ON SCHEMA {_DELEGATED_GRANT_SCHEMA} TO {onward}")
    # DROP ROLE cannot run while this session still *is* one of those roles.
    pg_cursor.execute("RESET ROLE")

    # Guard against a vacuous test: the entry the teardown has to unwind is the
    # one whose grantor is the delegate rather than the schema owner.
    grantor = _scalar(
        pg_cursor,
        """
        SELECT pg_get_userbyid((acl).grantor)
        FROM pg_namespace n
        CROSS JOIN LATERAL aclexplode(n.nspacl) AS acl
        WHERE n.nspname = %s AND pg_get_userbyid((acl).grantee) = %s
        """,
        _DELEGATED_GRANT_SCHEMA,
        onward,
    )
    assert grantor == delegate


_ROLES_FROM_PREVIOUS_TEST: list[str] = []


def test_temp_role_records_its_names_for_the_next_test(
    temp_role: TempRoleFactory,
) -> None:
    _ROLES_FROM_PREVIOUS_TEST.extend([temp_role("cleanup_a"), temp_role("cleanup_b")])


def test_temp_role_cleaned_up_after_the_previous_test(
    pg_cursor: Cursor[TupleRow],
) -> None:
    assert len(_ROLES_FROM_PREVIOUS_TEST) == 2, "the preceding test must run first"
    # Roles are cluster-wide, so a leak is visible to every database on the
    # server, not only this one.
    leaked = _scalar(
        pg_cursor,
        "SELECT count(*) FROM pg_roles WHERE rolname = ANY(%s)",
        _ROLES_FROM_PREVIOUS_TEST,
    )
    assert leaked == 0


def test_nothing_the_harness_generates_outlived_its_test(
    pg_cursor: Cursor[TupleRow],
) -> None:
    """Catch-all sweep for leaked ``dp_it_*`` schemas and roles.

    Safe against a parallel run on the same server: another session's
    uncommitted schemas and roles are invisible from this transaction.
    """
    schemas = _scalar(
        pg_cursor,
        r"SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'dp\_it\_%'",
    )
    roles = _scalar(
        pg_cursor,
        r"SELECT count(*) FROM pg_roles WHERE rolname LIKE 'dp\_it\_%'",
    )
    assert (schemas, roles) == (0, 0)
