"""``dataplat.services.db.orphans`` against a live PostgreSQL server.

The unit suite drives a fake cursor, so it proves the module *builds* an
``ALTER ... RENAME TO`` and a ``DROP``; it cannot prove the server accepts
them, that ``ALTER MATERIALIZED VIEW`` is really required for a matview, or
that the catalog afterwards holds what the code claims it does. Everything
here executes the real statement and then asks the catalog what happened.

The service modules are imported inside the test bodies, not at module level:
``orphans`` imports ``psycopg``, which ships in the optional ``db`` extra, and
a module-level import would turn its absence into a collection error instead
of the skip/ERROR that ``pg_dsn`` promises.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from tests.integration.conftest import SAMPLE_RELATIONS

if TYPE_CHECKING:
    from collections.abc import Iterator

    from psycopg import Cursor
    from psycopg.rows import TupleRow

    from dataplat.services.db.orphans import ObjectKind

pytestmark = pytest.mark.integration


# Relations `fetch_existing_relations` must report for `sample_schema`:
# every relkind information_schema.tables and pg_matviews expose, minus the
# partition children (the module filters them out on purpose) and minus the
# sequence (no catalog the module reads lists sequences).
_PARTITION_CHILDREN = frozenset({"events_2024", "events_2025"})
_EXPECTED_EXISTING = (
    frozenset(
        name for name, kind in SAMPLE_RELATIONS.items() if kind in {"r", "p", "v", "m"}
    )
    - _PARTITION_CHILDREN
)

_NODE_PREFIX = "model.myproject."
_PROD_COMMAND = "dbt build --target prod"


def _catalog(cursor: Cursor[TupleRow], schema: str) -> dict[str, str]:
    """Return ``{relname: relkind}`` straight from ``pg_class`` for one schema.

    Assertions go through this rather than through the service's own readers:
    a rename that only *looked* like it worked because the reader shares the
    bug would otherwise pass.
    """
    cursor.execute(
        """
        SELECT c.relname, c.relkind
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s
        """,
        (schema,),
    )
    return dict(cursor.fetchall())


# The three kinds whose rename/drop syntax differs, with the sample_schema
# relation of each kind. `customers` also carries the dependent view, so the
# order matters when these are dropped.
_KINDS: tuple[tuple[ObjectKind, str], ...] = (
    ("table", "orgs"),
    ("view", "active_customers"),
    ("matview", "customers_per_org"),
)


@pytest.fixture
def dbt_artifacts(pg_cursor: Cursor[TupleRow]) -> Iterator[None]:
    """Create empty ``dbt_artifacts.invocations`` / ``model_executions``.

    The schema name is hard-coded in the service SQL, so unlike the other
    fixtures this one cannot uniquify it. That is safe: the tables live in the
    test's transaction and disappear on its rollback, and a concurrent session
    creating the same schema blocks on ``pg_namespace``'s unique index rather
    than failing.

    Only the columns the query reads are modelled, with the types the
    dbt_artifacts package produces on Postgres. ``invocation_args`` is
    deliberately ``text``: the service filters it with ``LIKE``, which has no
    ``jsonb`` overload.
    """
    pg_cursor.execute("CREATE SCHEMA dbt_artifacts")
    pg_cursor.execute(
        """
        CREATE TABLE dbt_artifacts.invocations (
            command_invocation_id text PRIMARY KEY,
            dbt_command text,
            invocation_args text,
            run_started_at timestamptz
        )
        """
    )
    pg_cursor.execute(
        """
        CREATE TABLE dbt_artifacts.model_executions (
            command_invocation_id text,
            node_id text,
            "schema" text,
            name text,
            status text
        )
        """
    )
    yield
    # pg_cursor's rollback is the authoritative cleanup; nothing to undo here.


def _record_invocation(
    cursor: Cursor[TupleRow],
    invocation_id: str,
    *,
    started: datetime,
    command: str = "build",
    invocation_command: str = _PROD_COMMAND,
) -> None:
    """Insert one ``dbt_artifacts.invocations`` row."""
    cursor.execute(
        """
        INSERT INTO dbt_artifacts.invocations
            (command_invocation_id, dbt_command, invocation_args, run_started_at)
        VALUES (%s, %s, %s, %s)
        """,
        (
            invocation_id,
            command,
            f'{{"invocation_command": "{invocation_command}", "profile": "x"}}',
            started,
        ),
    )


def _record_model(
    cursor: Cursor[TupleRow],
    invocation_id: str,
    *,
    schema: str,
    name: str,
    status: str = "success",
    node_prefix: str = _NODE_PREFIX,
) -> None:
    """Insert one ``dbt_artifacts.model_executions`` row."""
    cursor.execute(
        """
        INSERT INTO dbt_artifacts.model_executions
            (command_invocation_id, node_id, "schema", name, status)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (invocation_id, f"{node_prefix}{name}", schema, name, status),
    )


# --- discovery -------------------------------------------------------------


def test_fetch_existing_relations_reports_every_kind_but_partition_children(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    """Tables, views and matviews are merged; partition children are dropped."""
    from dataplat.services.db import orphans

    found = orphans.fetch_existing_relations(
        pg_cursor, [sample_schema], is_redshift=False
    )

    assert set(found) == {sample_schema}
    assert found[sample_schema] == set(_EXPECTED_EXISTING)
    # The parent stays: only rows in pg_inherits are excluded, and renaming a
    # partitioned parent is a legitimate orphan action.
    assert "events" in found[sample_schema]
    for child in _PARTITION_CHILDREN:
        assert child not in found[sample_schema]


def test_fetch_existing_relations_returns_empty_for_an_unknown_schema(
    pg_cursor: Cursor[TupleRow],
) -> None:
    """A schema that does not exist yields no rows rather than an error."""
    from dataplat.services.db import orphans

    assert (
        orphans.fetch_existing_relations(
            pg_cursor, ["dp_it_no_such_schema"], is_redshift=False
        )
        == {}
    )


def test_classify_object_agrees_with_the_catalog_for_every_kind(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    """The rename/drop keyword is chosen from this, so it must match pg_class."""
    from dataplat.services.db import orphans

    catalog = _catalog(pg_cursor, sample_schema)
    expected_relkind = {"table": "r", "view": "v", "matview": "m"}

    for kind, relname in _KINDS:
        classified = orphans.classify_object(
            pg_cursor, sample_schema, relname, is_redshift=False
        )
        assert classified == kind
        assert catalog[relname] == expected_relkind[kind]

    # A partitioned parent is relkind 'p' but ALTER/DROP TABLE is correct for
    # it, so "table" is the right answer, not None.
    assert (
        orphans.classify_object(pg_cursor, sample_schema, "events", is_redshift=False)
        == "table"
    )
    assert (
        orphans.classify_object(pg_cursor, sample_schema, "ghost", is_redshift=False)
        is None
    )


def test_classify_object_skips_matviews_when_told_it_is_redshift(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    """The Redshift branch must not query pg_matviews — proven by the miss.

    Redshift has no pg_matviews, so the flag skips that lookup. Running the
    flag against Postgres is the only way to see that the skip really is the
    difference: the same object resolves as a matview without it.
    """
    from dataplat.services.db import orphans

    assert (
        orphans.classify_object(
            pg_cursor, sample_schema, "customers_per_org", is_redshift=True
        )
        is None
    )


def test_fetch_live_model_relations_unions_every_matching_invocation(
    pg_cursor: Cursor[TupleRow], dbt_artifacts: None
) -> None:
    """Models from all matching builds in the window count as live.

    Also pins the filters that make the set trustworthy: non-``build``
    commands, invocations older than ``since``, other dbt projects and
    non-live statuses are excluded.
    """
    from dataplat.services.db import orphans

    now = datetime.now(UTC)
    _record_invocation(pg_cursor, "nightly", started=now - timedelta(hours=2))
    _record_invocation(
        pg_cursor,
        "partial",
        started=now - timedelta(minutes=30),
        invocation_command="dbt build --select tag:hourly",
    )
    _record_invocation(
        pg_cursor, "plain_run", started=now - timedelta(minutes=10), command="run"
    )
    _record_invocation(pg_cursor, "ancient", started=now - timedelta(days=40))

    _record_model(pg_cursor, "nightly", schema="analytics", name="dim_customers")
    _record_model(
        pg_cursor, "nightly", schema="analytics", name="dim_wip", status="skipped"
    )
    _record_model(
        pg_cursor,
        "nightly",
        schema="analytics",
        name="from_other_project",
        node_prefix="model.otherproject.",
    )
    # Errored models still exist in the warehouse, so they count as live.
    _record_model(
        pg_cursor, "partial", schema="analytics", name="fct_orders", status="error"
    )
    _record_model(pg_cursor, "partial", schema="marts", name="dim_orgs")
    _record_model(pg_cursor, "plain_run", schema="analytics", name="run_only")
    _record_model(pg_cursor, "ancient", schema="analytics", name="long_gone")

    live = orphans.fetch_live_model_relations(
        pg_cursor,
        invocation_command=None,
        node_prefix=_NODE_PREFIX,
        statuses=orphans.LIVE_STATUSES,
        since=now - timedelta(days=7),
    )

    assert live == {
        "analytics": {"dim_customers", "fct_orders"},
        "marts": {"dim_orgs"},
    }


def test_fetch_live_model_relations_filters_on_the_invocation_command(
    pg_cursor: Cursor[TupleRow], dbt_artifacts: None
) -> None:
    """The ``invocation_args`` LIKE filter needs a text column and real data."""
    from dataplat.services.db import orphans

    now = datetime.now(UTC)
    _record_invocation(pg_cursor, "nightly", started=now - timedelta(hours=2))
    _record_invocation(
        pg_cursor,
        "partial",
        started=now - timedelta(minutes=30),
        invocation_command="dbt build --select tag:hourly",
    )
    _record_model(pg_cursor, "nightly", schema="analytics", name="dim_customers")
    _record_model(pg_cursor, "partial", schema="analytics", name="fct_orders")

    live = orphans.fetch_live_model_relations(
        pg_cursor,
        invocation_command=_PROD_COMMAND,
        node_prefix=_NODE_PREFIX,
        statuses=orphans.LIVE_STATUSES,
        since=now - timedelta(days=7),
    )

    assert live == {"analytics": {"dim_customers"}}


def test_scan_flags_only_relations_no_build_produced(
    pg_cursor: Cursor[TupleRow], sample_schema: str, dbt_artifacts: None
) -> None:
    """The whole scan, end to end, on real catalog and real artifact rows."""
    from dataplat.services.db import orphans

    now = datetime.now(UTC)
    _record_invocation(pg_cursor, "nightly", started=now - timedelta(hours=1))
    for name in ("customers", "active_customers"):
        _record_model(pg_cursor, "nightly", schema=sample_schema, name=name)
    pg_cursor.execute(f'CREATE TABLE "{sample_schema}".stale_table_deprecated (id int)')

    live = orphans.fetch_live_model_relations(
        pg_cursor,
        invocation_command=None,
        node_prefix=_NODE_PREFIX,
        statuses=orphans.LIVE_STATUSES,
        since=now - timedelta(days=7),
    )
    existing = orphans.fetch_existing_relations(
        pg_cursor, [sample_schema], is_redshift=False
    )
    found = orphans.diff_orphans(
        live=live,
        existing=existing,
        excluded_schemas=orphans.excluded_schemas(),
        excluded_user_schemas=frozenset(),
        excluded_user_relations=frozenset({(sample_schema, "secrets")}),
    )

    # Built models, the user-excluded relation and the already-deprecated
    # table are all absent; everything else in the schema is an orphan.
    assert found == {
        sample_schema: sorted(
            _EXPECTED_EXISTING - {"customers", "active_customers", "secrets"}
        )
    }


# --- rename and revert -----------------------------------------------------


def test_rename_and_revert_move_every_kind_in_the_catalog(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    """The generated ALTER really renames, and the reverse really restores.

    Both directions are checked through pg_class, including relkind, so a
    rename that silently created something else would fail here.
    """
    from dataplat.services.db import orphans

    before = _catalog(pg_cursor, sample_schema)

    for kind, relname in _KINDS:
        deprecated = f"{relname}{orphans.DEPRECATED_SUFFIX}"
        orphans.rename_object(pg_cursor, sample_schema, relname, deprecated, kind)
        after = _catalog(pg_cursor, sample_schema)
        assert relname not in after
        assert after[deprecated] == before[relname]

        orphans.rename_object(pg_cursor, sample_schema, deprecated, relname, kind)
        reverted = _catalog(pg_cursor, sample_schema)
        assert deprecated not in reverted
        assert reverted[relname] == before[relname]

    assert _catalog(pg_cursor, sample_schema) == before


def test_renaming_a_matview_needs_the_matview_keyword(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    """Proof that the per-kind keyword table is load-bearing on Postgres.

    ``ALTER VIEW`` on a materialized view is rejected outright, so
    misclassifying one would break the rename rather than degrade quietly.
    """
    import psycopg

    from dataplat.services.db import orphans

    conn = pg_cursor.connection
    with pytest.raises(psycopg.errors.WrongObjectType), conn.transaction():
        pg_cursor.execute(
            orphans.build_rename_statement(
                sample_schema, "customers_per_org", "mv_deprecated", "view"
            )
        )

    assert "customers_per_org" in _catalog(pg_cursor, sample_schema)


def test_rename_onto_a_taken_name_fails_and_classify_is_the_guard(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    """A left-over ``_deprecated`` object from an earlier run blocks the rename.

    The CLI's protection is a ``classify_object`` probe of the target name, so
    both halves are pinned: the probe sees the squatter, and the rename that
    ignores it raises instead of clobbering anything.
    """
    import psycopg

    from dataplat.services.db import orphans

    taken = f"orgs{orphans.DEPRECATED_SUFFIX}"
    pg_cursor.execute(f'CREATE TABLE "{sample_schema}".{taken} (id int)')

    assert (
        orphans.classify_object(pg_cursor, sample_schema, taken, is_redshift=False)
        == "table"
    )

    conn = pg_cursor.connection
    with pytest.raises(psycopg.errors.DuplicateTable), conn.transaction():
        orphans.rename_object(pg_cursor, sample_schema, "orgs", taken, "table")

    # Both relations survive the failed rename.
    catalog = _catalog(pg_cursor, sample_schema)
    assert catalog["orgs"] == "r"
    assert catalog[taken] == "r"


def test_rename_of_a_relation_that_vanished_after_the_scan_raises(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    """The scan/act race: classify returns None, the raw rename errors."""
    import psycopg

    from dataplat.services.db import orphans

    pg_cursor.execute(f'DROP TABLE "{sample_schema}".secrets')

    assert (
        orphans.classify_object(pg_cursor, sample_schema, "secrets", is_redshift=False)
        is None
    )

    conn = pg_cursor.connection
    with pytest.raises(psycopg.errors.UndefinedTable), conn.transaction():
        orphans.rename_object(
            pg_cursor, sample_schema, "secrets", "secrets_deprecated", "table"
        )


# --- the deprecated inventory ----------------------------------------------


def _deprecated_in(
    cursor: Cursor[TupleRow], schema: str, *, excluded: frozenset[str] | None = None
) -> dict[str, str]:
    """Run the deprecated scan and keep only ``schema``'s rows as name -> kind.

    Other sessions cannot contribute rows (their objects are uncommitted), but
    filtering keeps the assertions independent of whatever else the database
    happens to hold.
    """
    from dataplat.services.db import orphans

    rows = orphans.fetch_deprecated_objects(
        cursor,
        is_redshift=False,
        excluded_schemas=excluded if excluded is not None else frozenset(),
    )
    return {name: kind for found_schema, name, kind in rows if found_schema == schema}


def test_fetch_deprecated_objects_finds_every_kind_with_the_right_keyword_hint(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    """Renamed objects come back with the kind purge needs to DROP them."""
    from dataplat.services.db import orphans

    expected: dict[str, str] = {}
    for kind, relname in _KINDS:
        deprecated = f"{relname}{orphans.DEPRECATED_SUFFIX}"
        orphans.rename_object(pg_cursor, sample_schema, relname, deprecated, kind)
        expected[deprecated] = kind

    assert _deprecated_in(pg_cursor, sample_schema) == expected
    # Excluding the schema removes all of them again.
    excluded = frozenset({sample_schema})
    assert _deprecated_in(pg_cursor, sample_schema, excluded=excluded) == {}


def test_fetch_deprecated_objects_ignores_partition_children(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    """A renamed partition child must not become a purge candidate.

    Dropping one would silently delete a live partition of `events`, so the
    NOT EXISTS against pg_inherits is checked on a real partitioned table.
    """
    from dataplat.services.db import orphans

    orphans.rename_object(
        pg_cursor, sample_schema, "events_2024", "events_2024_deprecated", "table"
    )

    assert "events_2024_deprecated" in _catalog(pg_cursor, sample_schema)
    assert _deprecated_in(pg_cursor, sample_schema) == {}


def test_fetch_deprecated_objects_requires_a_literal_underscore(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    """Only names carrying the real ``_deprecated`` suffix are candidates.

    The scan pattern used to be ``LIKE '%_deprecated'`` with no ESCAPE, and an
    unescaped underscore matches any single character — so ``legacydeprecated``
    was reported as a purge candidate, and ``purge --include-unknown`` would
    have dropped it.
    """
    pg_cursor.execute(f'CREATE TABLE "{sample_schema}".legacydeprecated (id int)')
    pg_cursor.execute(f'CREATE TABLE "{sample_schema}".orgs_deprecated (id int)')

    assert _deprecated_in(pg_cursor, sample_schema) == {"orgs_deprecated": "table"}


def test_deprecated_scan_ignores_percent_and_underscore_tricks(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    """Neither LIKE metacharacter in a relation name can widen the scan."""
    for relname in ("a%deprecated", "b_deprecatedx", "xdeprecated"):
        pg_cursor.execute(f'CREATE TABLE "{sample_schema}"."{relname}" (id int)')
    pg_cursor.execute(f'CREATE TABLE "{sample_schema}".real_deprecated (id int)')

    assert _deprecated_in(pg_cursor, sample_schema) == {"real_deprecated": "table"}


# --- purge -----------------------------------------------------------------


def test_drop_object_removes_every_kind_from_the_catalog(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    """The purge statement executes and the relation is really gone.

    Dependents first (view, then matview, then table): the generated DROP has
    no CASCADE, so this order is what a working purge needs.
    """
    from dataplat.services.db import orphans

    for kind, relname in (
        ("view", "active_customers"),
        ("matview", "customers_per_org"),
        ("table", "customers"),
    ):
        orphans.drop_object(pg_cursor, sample_schema, relname, kind)
        assert relname not in _catalog(pg_cursor, sample_schema)


def test_dropping_a_matview_needs_the_matview_keyword(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    """``DROP TABLE`` on a matview is refused, so the kind must be right."""
    import psycopg

    from dataplat.services.db import orphans

    conn = pg_cursor.connection
    with pytest.raises(psycopg.errors.WrongObjectType), conn.transaction():
        pg_cursor.execute(
            orphans.build_drop_statement(sample_schema, "customers_per_org", "table")
        )

    assert _catalog(pg_cursor, sample_schema)["customers_per_org"] == "m"


def test_drop_object_of_a_vanished_relation_aborts_the_purge(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    """No ``IF EXISTS``: a relation dropped between scan and purge errors.

    The CLI runs every drop of a purge inside one transaction, so this failure
    discards the drops that already succeeded. Pinned rather than papered
    over — the statement builder is shared with Redshift.
    """
    import psycopg

    from dataplat.services.db import orphans

    conn = pg_cursor.connection
    with pytest.raises(psycopg.errors.UndefinedTable), conn.transaction():
        orphans.drop_object(pg_cursor, sample_schema, "ghost_deprecated", "table")


def test_dropping_a_table_a_view_still_depends_on_is_refused(
    pg_cursor: Cursor[TupleRow], sample_schema: str
) -> None:
    """A deprecated table with a live dependent view cannot be purged.

    Same single-transaction consequence as the vanished case, which is why the
    real behaviour is worth pinning: purge order is not arbitrary.
    """
    import psycopg

    from dataplat.services.db import orphans

    conn = pg_cursor.connection
    with pytest.raises(psycopg.errors.DependentObjectsStillExist), conn.transaction():
        orphans.drop_object(pg_cursor, sample_schema, "customers", "table")

    assert "customers" in _catalog(pg_cursor, sample_schema)


def _connection_params(pg_dsn: str) -> Any:
    """Build ``DbConnectionParams`` for the test server from the harness DSN."""
    import psycopg

    from dataplat.services.db.connection import DbConnectionParams, SqlEngine

    parsed: dict[str, Any] = psycopg.conninfo.conninfo_to_dict(pg_dsn)
    return DbConnectionParams(
        user=str(parsed.get("user", "postgres")),
        host=str(parsed.get("host", "127.0.0.1")),
        dbname=str(parsed["dbname"]),
        port=int(parsed.get("port", 5432)),
        password=parsed.get("password"),
        engine=SqlEngine.postgresql,
    )


def test_open_transactional_connection_honours_dry_run(pg_dsn: str) -> None:
    """The session wrapper rolls back on dry-run and commits otherwise.

    This is the difference between "would rename" and "renamed" for every
    command in the module, and it can only be observed across connections —
    the fake-cursor tests cannot see a commit at all. Uses its own connections
    (that is the point), so it owns its cleanup.
    """
    import os

    from dataplat.services.db.orphans import open_transactional_connection

    params = _connection_params(pg_dsn)
    schema = f"dp_it_txn_{os.getpid()}"

    def exists() -> bool:
        with open_transactional_connection(params, dry_run=True) as probe:
            row = probe.execute(
                "SELECT to_regnamespace(%s) IS NOT NULL", (schema,)
            ).fetchone()
        return bool(row and row[0])

    try:
        with open_transactional_connection(params, dry_run=True) as conn:
            conn.execute(f'CREATE SCHEMA "{schema}"')
        assert not exists()

        with open_transactional_connection(params, dry_run=False) as conn:
            conn.execute(f'CREATE SCHEMA "{schema}"')
        assert exists()
    finally:
        with open_transactional_connection(params, dry_run=False) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def test_open_transactional_connection_rolls_back_on_failure(pg_dsn: str) -> None:
    """A statement error rolls the whole session back and re-raises."""
    import os

    import psycopg

    from dataplat.services.db.orphans import open_transactional_connection

    params = _connection_params(pg_dsn)
    schema = f"dp_it_txn_fail_{os.getpid()}"

    with (
        pytest.raises(psycopg.errors.UndefinedTable),
        open_transactional_connection(params, dry_run=False) as conn,
    ):
        conn.execute(f'CREATE SCHEMA "{schema}"')
        conn.execute("DROP TABLE dp_it_no_such_table")

    with open_transactional_connection(params, dry_run=True) as conn:
        row = conn.execute("SELECT to_regnamespace(%s)", (schema,)).fetchone()
    assert row == (None,)
