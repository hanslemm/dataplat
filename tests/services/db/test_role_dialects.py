from __future__ import annotations

import pytest

from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.role_dialects import (
    ParentKind,
    PostgresDialect,
    RedshiftDialect,
    dialect_for,
)


def _r(op) -> str:
    return op.statement.as_string(None)


def test_dialect_for_postgres() -> None:
    assert isinstance(dialect_for(SqlEngine.postgresql), PostgresDialect)


def test_postgres_create_login_is_create_role_login() -> None:
    op = PostgresDialect().create_login("alice", "hunter2_______________________")
    assert op.secret is True
    assert _r(op).startswith('CREATE ROLE "alice" LOGIN PASSWORD ')
    assert "hunter2" not in op.description


def test_postgres_membership_ignores_kind() -> None:
    d = PostgresDialect()
    for kind in (ParentKind.role, ParentKind.group):
        assert (
            _r(d.grant_membership("alice", "analyst", kind))
            == 'GRANT "analyst" TO "alice"'
        )


def test_postgres_shared_grants() -> None:
    d = PostgresDialect()
    assert _r(d.grant_schema_usage("a", "raw")) == 'GRANT USAGE ON SCHEMA "raw" TO "a"'
    assert (
        _r(d.grant_schema_create("a", "raw")) == 'GRANT CREATE ON SCHEMA "raw" TO "a"'
    )
    assert _r(d.grant_table_select("a", "raw")) == (
        'GRANT SELECT ON ALL TABLES IN SCHEMA "raw" TO "a"'
    )
    assert _r(d.grant_table_all("a", "raw")) == (
        'GRANT ALL ON ALL TABLES IN SCHEMA "raw" TO "a"'
    )
    assert _r(d.grant_sequence_usage("a", "raw")) == (
        'GRANT USAGE ON ALL SEQUENCES IN SCHEMA "raw" TO "a"'
    )
    assert _r(d.alter_default_table_select("a", "raw")) == (
        'ALTER DEFAULT PRIVILEGES IN SCHEMA "raw" GRANT SELECT ON TABLES TO "a"'
    )
    assert _r(d.alter_default_table_all("a", "raw")) == (
        'ALTER DEFAULT PRIVILEGES IN SCHEMA "raw" GRANT ALL ON TABLES TO "a"'
    )


def test_dialect_for_redshift() -> None:
    assert isinstance(dialect_for(SqlEngine.redshift), RedshiftDialect)


def test_redshift_create_login_is_create_user() -> None:
    op = RedshiftDialect().create_login("svc", "hunter2_______________________")
    assert op.secret is True
    assert _r(op).startswith('CREATE USER "svc" PASSWORD ')
    assert "hunter2" not in op.description


def test_redshift_membership_group_vs_role() -> None:
    d = RedshiftDialect()
    assert _r(d.grant_membership("svc", "reporting", ParentKind.group)) == (
        'ALTER GROUP "reporting" ADD USER "svc"'
    )
    assert _r(d.grant_membership("svc", "reporting", ParentKind.role)) == (
        'GRANT ROLE "reporting" TO "svc"'
    )


def test_redshift_sequence_usage_is_skipped() -> None:
    assert RedshiftDialect().grant_sequence_usage("svc", "public") is None


def test_redshift_inherits_shared_table_grants() -> None:
    d = RedshiftDialect()
    assert _r(d.grant_table_select("svc", "public")) == (
        'GRANT SELECT ON ALL TABLES IN SCHEMA "public" TO "svc"'
    )


def test_postgres_create_nologin() -> None:
    op = PostgresDialect().create_nologin("readers")
    assert op.secret is False
    assert _r(op) == 'CREATE ROLE "readers" NOLOGIN'


def test_redshift_create_nologin_is_rbac_role() -> None:
    op = RedshiftDialect().create_nologin("readers")
    assert op.secret is False
    assert _r(op) == 'CREATE ROLE "readers"'


def test_postgres_grant_role_to_ignores_kind_and_role_flag() -> None:
    d = PostgresDialect()
    for kind in (ParentKind.role, ParentKind.user):
        assert _r(d.grant_role_to("readers", "alice", kind)) == (
            'GRANT "readers" TO "alice"'
        )
        assert _r(d.grant_role_to("readers", "alice", kind, name_is_role=True)) == (
            'GRANT "readers" TO "alice"'
        )


def test_redshift_grant_role_to_user_and_role() -> None:
    d = RedshiftDialect()
    assert _r(
        d.grant_role_to("readers", "alice", ParentKind.user, name_is_role=True)
    ) == ('GRANT ROLE "readers" TO "alice"')
    assert _r(
        d.grant_role_to("readers", "rbac", ParentKind.role, name_is_role=True)
    ) == ('GRANT ROLE "readers" TO ROLE "rbac"')


def test_postgres_grants_ignore_as_role() -> None:
    d = PostgresDialect()
    assert _r(d.grant_schema_usage("a", "raw", as_role=True)) == (
        'GRANT USAGE ON SCHEMA "raw" TO "a"'
    )


def test_redshift_grants_as_role_use_role_keyword() -> None:
    d = RedshiftDialect()
    assert _r(d.grant_schema_usage("readers", "public", as_role=True)) == (
        'GRANT USAGE ON SCHEMA "public" TO ROLE "readers"'
    )
    assert _r(d.grant_table_select("readers", "public", as_role=True)) == (
        'GRANT SELECT ON ALL TABLES IN SCHEMA "public" TO ROLE "readers"'
    )
    assert _r(d.grant_table_select("svc", "public")) == (
        'GRANT SELECT ON ALL TABLES IN SCHEMA "public" TO "svc"'
    )


def test_redshift_default_privileges_skip_for_roles() -> None:
    d = RedshiftDialect()
    assert d.alter_default_table_select("readers", "public", as_role=True) is None
    assert d.alter_default_table_all("readers", "public", as_role=True) is None
    assert d.alter_default_table_select("svc", "public") is not None


def test_redshift_membership_role_member_uses_to_role() -> None:
    d = RedshiftDialect()
    assert _r(
        d.grant_membership("readers", "rbac", ParentKind.role, member_is_role=True)
    ) == ('GRANT ROLE "rbac" TO ROLE "readers"')


class _ScriptedCursor:
    """Fake cursor that returns queued results and swallows SAVEPOINT/ROLLBACK.

    ``script`` maps a substring of the SQL to the ``fetchone`` result for the
    next matching execute. Transaction-control statements are no-ops.
    """

    def __init__(self, script: dict[str, tuple | None]) -> None:
        self._script = script
        self._next: tuple | None = None
        self.executed: list[str] = []

    def execute(self, sql_text, params=None) -> None:
        text = str(sql_text)
        self.executed.append(text)
        if text.startswith(("SAVEPOINT", "ROLLBACK", "RELEASE")):
            return
        self._next = None
        for needle, result in self._script.items():
            if needle in text:
                self._next = result
                return

    def fetchone(self):
        return self._next


def test_redshift_role_exists_checks_user_then_group() -> None:
    d = RedshiftDialect()
    assert d.role_exists(_ScriptedCursor({"pg_user": (1,)}), "svc") is True
    assert d.role_exists(_ScriptedCursor({"pg_group": (1,)}), "grp") is True
    assert d.role_exists(_ScriptedCursor({}), "nobody") is False


def test_redshift_resolve_parent_kind_group() -> None:
    kind = RedshiftDialect().resolve_parent_kind(
        _ScriptedCursor({"pg_group": (1,)}),
        "reporting",
    )
    assert kind is ParentKind.group


def test_redshift_resolve_parent_kind_rbac_role() -> None:
    kind = RedshiftDialect().resolve_parent_kind(
        _ScriptedCursor({"svv_roles": (1,)}),
        "rbac_reader",
    )
    assert kind is ParentKind.role


def test_redshift_resolve_parent_kind_absent() -> None:
    kind = RedshiftDialect().resolve_parent_kind(_ScriptedCursor({}), "ghost")
    assert kind is ParentKind.absent


def test_redshift_role_exists_checks_rbac_roles() -> None:
    d = RedshiftDialect()
    assert d.role_exists(_ScriptedCursor({"svv_roles": (1,)}), "rbac") is True


def test_redshift_resolve_grantee_kind() -> None:
    d = RedshiftDialect()
    assert (
        d.resolve_grantee_kind(_ScriptedCursor({"pg_user": (1,)}), "alice")
        is ParentKind.user
    )
    assert (
        d.resolve_grantee_kind(_ScriptedCursor({"pg_group": (1,)}), "grp")
        is ParentKind.group
    )
    assert (
        d.resolve_grantee_kind(_ScriptedCursor({"svv_roles": (1,)}), "rbac")
        is ParentKind.role
    )
    assert d.resolve_grantee_kind(_ScriptedCursor({}), "ghost") is ParentKind.absent


def test_postgres_resolve_grantee_kind_is_role_without_io() -> None:
    cursor = _ScriptedCursor({})
    assert PostgresDialect().resolve_grantee_kind(cursor, "x") is ParentKind.role
    assert cursor.executed == []


def test_postgres_resolve_parent_kind_is_role_without_io() -> None:
    # Base default: no query, always role (preserves today's behavior).
    cursor = _ScriptedCursor({})
    kind = PostgresDialect().resolve_parent_kind(cursor, "whatever")
    assert kind is ParentKind.role
    assert cursor.executed == []


class _RaisingOnSavepointCursor(_ScriptedCursor):
    """Raises on the initial SAVEPOINT (simulated connection-level failure)."""

    def execute(self, sql_text, params=None) -> None:
        text = str(sql_text)
        if text.startswith("SAVEPOINT"):
            self.executed.append(text)
            raise RuntimeError("savepoint not supported")
        super().execute(sql_text, params)


class _RaisingOnProbeCursor(_ScriptedCursor):
    """Raises on the svv_roles probe (simulated missing view / no permission)."""

    def execute(self, sql_text, params=None) -> None:
        text = str(sql_text)
        if "svv_roles" in text:
            self.executed.append(text)
            raise RuntimeError("svv_roles not available")
        super().execute(sql_text, params)


def test_redshift_rbac_probe_bails_on_savepoint_failure() -> None:
    """A connection-level SAVEPOINT failure must return False without ever
    attempting the svv_roles probe query."""
    cursor = _RaisingOnSavepointCursor({})
    assert RedshiftDialect()._rbac_role_exists(cursor, "rbac_reader") is False
    assert not any("svv_roles" in q for q in cursor.executed)


def test_redshift_rbac_probe_rolls_back_savepoint_on_probe_failure() -> None:
    """A failed svv_roles probe must ROLLBACK TO SAVEPOINT to keep the tx
    usable, and the parent must be treated as absent (not an RBAC role)."""
    cursor = _RaisingOnProbeCursor({})
    kind = RedshiftDialect().resolve_parent_kind(cursor, "rbac_reader")
    assert kind is ParentKind.absent
    assert any(q.startswith("ROLLBACK TO SAVEPOINT") for q in cursor.executed)


class _RowsCursor:
    """Fake cursor returning a fixed ``fetchall`` list per execute."""

    def __init__(self, batches: list[list[tuple]]) -> None:
        self._batches = list(batches)
        self._current: list[tuple] = []

    def execute(self, sql_text, params=None) -> None:
        self._current = self._batches.pop(0) if self._batches else []

    def fetchall(self) -> list[tuple]:
        return self._current


def test_redshift_list_roles_unions_users_and_groups() -> None:
    # First execute -> users, second -> groups.
    cursor = _RowsCursor(
        [
            [("svc", True, False)],  # usename, usesuper, usecreatedb
            [("reporting", 3)],  # groname, member count
        ]
    )
    rows = RedshiftDialect().list_roles(cursor)
    by_name = {r.name: r for r in rows}
    assert by_name["svc"].can_login is True
    assert by_name["reporting"].can_login is False
    assert by_name["reporting"].members_count == 3


class _CatalogCursor:
    """Fake cursor for drop-side enumeration.

    Unlike ``_ScriptedCursor`` (fetchone-only) and ``_RowsCursor``
    (fetchall-only), ``enumerate_owned``/``groups_of`` interleave a
    ``fetchone`` (the ``usesysid`` lookup) with one or more ``fetchall``
    calls (schemas/relations/groups), so this fake routes both by SQL
    substring against separate maps.
    """

    def __init__(
        self,
        fetchone: dict[str, tuple | None] | None = None,
        fetchall: dict[str, list[tuple]] | None = None,
    ) -> None:
        self._fetchone = fetchone or {}
        self._fetchall = fetchall or {}
        self._next_one: tuple | None = None
        self._next_all: list[tuple] = []
        self.executed: list[str] = []

    def execute(self, sql_text, params=None) -> None:
        text = str(sql_text)
        self.executed.append(text)
        self._next_one = None
        self._next_all = []
        for needle, result in self._fetchone.items():
            if needle in text:
                self._next_one = result
                return
        for needle, rows in self._fetchall.items():
            if needle in text:
                self._next_all = rows
                return

    def fetchone(self):
        return self._next_one

    def fetchall(self) -> list[tuple]:
        return self._next_all


def test_redshift_enumerate_owned_returns_schemas_and_relations() -> None:
    cursor = _CatalogCursor(
        fetchone={"pg_user": (42,)},
        fetchall={
            # "FROM pg_namespace" (schemas) vs. "FROM pg_class" (relations,
            # which also JOINs pg_namespace) must route to distinct queues —
            # a bare "pg_namespace" substring would match both.
            "FROM pg_namespace": [("analytics",), ("reporting",)],
            "FROM pg_class": [
                ("public", "orders", "r"),
                ("public", "orders_v", "v"),
            ],
        },
    )
    owned = RedshiftDialect().enumerate_owned(cursor, "svc")
    assert owned.schemas == ["analytics", "reporting"]
    assert owned.relations == [
        ("public", "orders", "r"),
        ("public", "orders_v", "v"),
    ]


def test_redshift_enumerate_owned_raises_for_non_user() -> None:
    # pg_user lookup misses -> the name is a group or does not exist at all.
    cursor = _CatalogCursor()
    with pytest.raises(ValueError, match="not a Redshift user"):
        RedshiftDialect().enumerate_owned(cursor, "ghost")


def test_redshift_groups_of_returns_membership() -> None:
    cursor = _CatalogCursor(
        fetchone={"pg_user": (42,)},
        fetchall={"pg_group": [("reporting",), ("analysts",)]},
    )
    assert RedshiftDialect().groups_of(cursor, "svc") == ["reporting", "analysts"]


def test_redshift_groups_of_returns_empty_for_absent_user() -> None:
    cursor = _CatalogCursor()
    assert RedshiftDialect().groups_of(cursor, "ghost") == []
