"""Per-engine schema SQL: the parts a real server cannot be asked about.

The Postgres and DuckDB statements are executed for real in
``tests/integration/test_schema_pg.py`` and the DuckDB tier. What is left here is
the shape of the SQL (which engine reads which catalog) and the Redshift paths
there is no cluster to run — chiefly a quota view that is absent, which must
degrade to unknown rather than to zero.
"""

from __future__ import annotations

import pytest

from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.role_dialects import ParentKind
from dataplat.services.db.schema_dialects import (
    DuckDbSchemaDialect,
    PostgresSchemaDialect,
    RedshiftSchemaDialect,
    _held_identity_pin,
    schema_dialect_for,
)


class _Cursor:
    """Fake cursor returning queued rows, recording the SQL it was handed."""

    def __init__(
        self,
        rows: list[tuple] | None = None,
        quota: list[tuple] | None = None,
        *,
        quota_available: bool = True,
    ) -> None:
        self._rows = rows if rows is not None else []
        self._quota = quota if quota is not None else []
        self._quota_available = quota_available
        self._result: list[tuple] = []
        self.executed: list[str] = []
        self.params: list[object] = []

    def execute(self, sql_text, params=None) -> None:
        text = str(sql_text)
        self.executed.append(text)
        self.params.append(params)
        if text.startswith(("SAVEPOINT", "ROLLBACK", "RELEASE")):
            return
        if "svv_schema_quota_state" in text:
            if not self._quota_available:
                raise RuntimeError("relation svv_schema_quota_state does not exist")
            self._result = self._quota
        else:
            self._result = self._rows

    def fetchall(self) -> list[tuple]:
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None


_ROW = [("analytics", "alice", 3, 2, 1)]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def test_dialect_for_each_engine() -> None:
    assert isinstance(schema_dialect_for(SqlEngine.postgresql), PostgresSchemaDialect)
    assert isinstance(schema_dialect_for(SqlEngine.redshift), RedshiftSchemaDialect)
    assert isinstance(schema_dialect_for(SqlEngine.duckdb), DuckDbSchemaDialect)


# ---------------------------------------------------------------------------
# Which catalog each engine reads
# ---------------------------------------------------------------------------


def test_postgres_resolves_the_owner_through_pg_roles() -> None:
    cursor = _Cursor(_ROW)
    PostgresSchemaDialect().list_schemas(cursor)

    assert "pg_roles" in cursor.executed[0]
    assert "pg_user" not in cursor.executed[0]


def test_redshift_resolves_the_owner_through_pg_user() -> None:
    """pg_roles does not exist on Redshift; usesysid is the join key."""
    cursor = _Cursor(_ROW)
    RedshiftSchemaDialect().list_schemas(cursor)

    assert "pg_user" in cursor.executed[0]
    assert "usesysid" in cursor.executed[0]
    assert "pg_roles" not in cursor.executed[0]


def test_duckdb_joins_no_owner_catalog_at_all() -> None:
    """Measured, not assumed: DuckDB has no pg_roles, so the join would fail."""
    cursor = _Cursor(_ROW)
    DuckDbSchemaDialect().list_schemas(cursor)

    assert "pg_roles" not in cursor.executed[0]
    assert "pg_user" not in cursor.executed[0]


def test_duckdb_binds_question_marks() -> None:
    """This codebase does not translate placeholders — DuckDB SQL uses `?`."""
    cursor = _Cursor(_ROW)
    DuckDbSchemaDialect().list_schemas(cursor, like="dev%")

    assert "LIKE ?" in cursor.executed[0]
    assert "%s" not in cursor.executed[0]


@pytest.mark.parametrize("dialect", [PostgresSchemaDialect(), RedshiftSchemaDialect()])
def test_libpq_engines_bind_percent_s(dialect) -> None:
    cursor = _Cursor(_ROW)
    dialect.list_schemas(cursor, like="dev%")

    assert "LIKE %s" in cursor.executed[0]


# ---------------------------------------------------------------------------
# The WHERE clause
# ---------------------------------------------------------------------------


def test_system_schemas_are_hidden_by_default() -> None:
    cursor = _Cursor(_ROW)
    PostgresSchemaDialect().list_schemas(cursor)

    assert "information_schema" in cursor.executed[0]
    assert "WHERE" in cursor.executed[0]


def test_include_system_drops_the_predicate_entirely() -> None:
    cursor = _Cursor(_ROW)
    PostgresSchemaDialect().list_schemas(cursor, include_system=True)

    assert "WHERE" not in cursor.executed[0]
    assert cursor.params[0] == ()


def test_the_system_predicate_escapes_its_own_underscore() -> None:
    """`pg#_%` with ESCAPE '#', so a schema named `pgx` is not swept up.

    Proven against PostgreSQL 16 in the integration tier; asserted here so the
    clause cannot be simplified away without a failing test. The escape character
    is `#` rather than a backslash for the reason in _like.py — Redshift runs with
    standard_conforming_strings off, where a backslash escapes its own quote.
    """
    cursor = _Cursor(_ROW)
    PostgresSchemaDialect().list_schemas(cursor)

    assert "'pg#_%" in cursor.executed[0]
    assert "ESCAPE '#'" in cursor.executed[0]
    assert "\\" not in cursor.executed[0]


def test_the_system_wildcard_is_doubled_for_client_side_parsing() -> None:
    """psycopg parses %-placeholders whenever params is not None.

    _roster always passes a tuple — `()` at minimum — so a single `%` here would
    be read as a malformed placeholder rather than sent as a wildcard.
    """
    from dataplat.services.db.schema_dialects import _HIDE_SYSTEM

    assert "%%" in _HIDE_SYSTEM


def test_a_like_pattern_is_bound_not_interpolated() -> None:
    cursor = _Cursor(_ROW)
    PostgresSchemaDialect().list_schemas(cursor, like="dev#_%")

    assert cursor.params[0] == ("dev#_%",)
    assert "dev" not in cursor.executed[0]


def test_schema_exists_uses_the_engines_placeholder() -> None:
    for dialect, placeholder in (
        (PostgresSchemaDialect(), "%s"),
        (DuckDbSchemaDialect(), "?"),
    ):
        cursor = _Cursor([(1,)])
        assert dialect.schema_exists(cursor, "analytics") is True
        assert placeholder in cursor.executed[0]

    assert PostgresSchemaDialect().schema_exists(_Cursor([]), "ghost") is False


# ---------------------------------------------------------------------------
# Counts
# ---------------------------------------------------------------------------


def test_counts_are_coerced_and_null_becomes_zero() -> None:
    """A schema with no relations LEFT JOINs to NULL, not to 0."""
    cursor = _Cursor([("empty", "alice", None, None, None)])

    (row,) = PostgresSchemaDialect().list_schemas(cursor)

    assert (row.tables, row.views, row.other) == (0, 0, 0)


def test_relation_buckets_cover_everything_a_drop_destroys() -> None:
    """Sequences and composite types land in `other`, never uncounted.

    A drop pre-flight that reported a schema as empty because its only contents
    were sequences would be actively misleading.
    """
    from dataplat.services.db.schema_dialects import _RELATION_COUNTS

    for relkind in ("'r'", "'p'", "'f'", "'v'", "'m'", "'S'", "'c'"):
        assert relkind in _RELATION_COUNTS


# ---------------------------------------------------------------------------
# Redshift quotas — no cluster to run these against
# ---------------------------------------------------------------------------


def test_redshift_attaches_quota_state_to_each_row() -> None:
    cursor = _Cursor(_ROW, quota=[("analytics", 51200, 1024)])

    (row,) = RedshiftSchemaDialect().list_schemas(cursor)

    assert (row.quota_mb, row.used_mb) == (51200, 1024)


def test_an_unavailable_quota_view_leaves_quota_unknown_not_zero() -> None:
    """The whole reason quota_mb is None-able. Zero would read as "no limit"."""
    cursor = _Cursor(_ROW, quota_available=False)

    (row,) = RedshiftSchemaDialect().list_schemas(cursor)

    assert row.quota_mb is None
    assert row.used_mb is None
    # And the listing still succeeded, with the transaction rolled back cleanly.
    assert row.name == "analytics"
    assert any(q.startswith("ROLLBACK TO SAVEPOINT") for q in cursor.executed)


def test_a_schema_missing_from_the_quota_view_keeps_none() -> None:
    """Quotas are per-schema and optional; absence is not zero."""
    cursor = _Cursor(
        [("analytics", "alice", 1, 0, 0), ("staging", "alice", 0, 0, 0)],
        quota=[("analytics", 51200, 1024)],
    )

    rows = RedshiftSchemaDialect().list_schemas(cursor)

    assert rows[1].name == "staging"
    assert rows[1].quota_mb is None


def test_a_null_quota_row_is_carried_as_none() -> None:
    """svv_schema_quota_state reports NULL for a schema with no quota set."""
    cursor = _Cursor(_ROW, quota=[("analytics", None, 512)])

    (row,) = RedshiftSchemaDialect().list_schemas(cursor)

    assert row.quota_mb is None
    assert row.used_mb == 512


def test_postgres_never_asks_about_quotas() -> None:
    """Redshift is the only engine with schema quotas; the others must not probe."""
    for dialect in (PostgresSchemaDialect(), DuckDbSchemaDialect()):
        cursor = _Cursor(_ROW)
        (row,) = dialect.list_schemas(cursor)
        assert row.quota_mb is None
        assert not any("quota" in q for q in cursor.executed)


# ---------------------------------------------------------------------------
# _held_identity_pin — the predicate that keeps two principals apart
# ---------------------------------------------------------------------------


def test_the_pin_requires_name_and_type_together() -> None:
    """Name alone is the bug this exists to close.

    Redshift lets a group and an RBAC role share one name, and
    svv_schema_privileges matches on identity name only — so an unpinned
    predicate merges both principals' privileges into one answer and reports
    access nobody has.
    """
    clause, params = _held_identity_pin(["finance"], {"finance": ParentKind.group})

    assert "identity_name = %s AND identity_type = %s" in clause
    assert params == ("finance", "group")


@pytest.mark.parametrize(
    ("kind", "spelling"),
    [
        (ParentKind.user, "user"),
        (ParentKind.group, "group"),
        (ParentKind.role, "role"),
    ],
)
def test_each_kind_gets_its_catalog_spelling(kind: ParentKind, spelling: str) -> None:
    """The values are svv_schema_privileges' own, not ParentKind's names."""
    _, params = _held_identity_pin(["x"], {"x": kind})

    assert params == ("x", spelling)


def test_public_gets_its_own_arm_and_no_parameters() -> None:
    """Its rows carry identity_type = 'public', so a name+type pair would drop it."""
    clause, params = _held_identity_pin(["PUBLIC"], {"PUBLIC": ParentKind.role})

    assert "identity_type = 'public'" in clause
    # Not bound as a name: PUBLIC is a keyword, and binding it would look for a
    # principal actually called PUBLIC.
    assert params == ()


def test_the_public_arm_is_always_present() -> None:
    """Even when nobody asked for PUBLIC — a grant to PUBLIC is held by everyone.

    Leaving it out would report a schema as un-granted to a named user who in fact
    reaches it through PUBLIC.
    """
    clause, _ = _held_identity_pin(["alice"], {"alice": ParentKind.user})

    assert "identity_type = 'public'" in clause


def test_an_unknown_kind_matches_nothing_rather_than_falling_back() -> None:
    """Under-detecting costs one redundant idempotent GRANT. Matching on name
    alone is the incident. So a grantee whose kind was never resolved is simply
    left out of the predicate."""
    clause, params = _held_identity_pin(["ghost"], {})

    assert clause == "identity_type = 'public'"
    assert params == ()
    assert "identity_name" not in clause


def test_absent_is_treated_as_unknown() -> None:
    clause, params = _held_identity_pin(["ghost"], {"ghost": ParentKind.absent})

    assert "identity_name" not in clause
    assert params == ()


def test_several_grantees_each_get_their_own_pinned_arm() -> None:
    clause, params = _held_identity_pin(
        ["alice", "finance", "PUBLIC", "ghost"],
        {"alice": ParentKind.user, "finance": ParentKind.role},
    )

    # Two pinned arms plus the public one; ghost contributes nothing.
    assert clause.count("identity_name = %s") == 2
    assert params == ("alice", "user", "finance", "role")


def test_no_kinds_at_all_degrades_to_the_public_arm() -> None:
    """`kinds=None` is a legal call, and must not produce a name-only match."""
    clause, params = _held_identity_pin(["alice"], None)

    assert clause == "identity_type = 'public'"
    assert params == ()
