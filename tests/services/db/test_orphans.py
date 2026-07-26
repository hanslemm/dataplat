from __future__ import annotations

from datetime import UTC

import pytest

from dataplat.core.errors import ConfigError
from dataplat.services.db.connection import SqlEngine
from dataplat.services.db.orphans import (
    DBT_ARTIFACTS_SCHEMA,
    DEPRECATED_SUFFIX,
    LIVE_STATUSES,
    build_rename_statement,
    classify_object,
    excluded_schemas,
    invocation_command,
    node_prefix,
    resolve_orphans_connection_params,
)


class FakeCursor:
    """Minimal cursor stub recording queries and replaying fetchone results."""

    def __init__(self, fetch_results: list[object]) -> None:
        self._results = list(fetch_results)
        self.queries: list[tuple[str, tuple]] = []

    def execute(self, query, params=None) -> None:
        self.queries.append((str(query), tuple(params or ())))

    def fetchone(self):
        if not self._results:
            return None
        return self._results.pop(0)


def _clear_orphan_env(monkeypatch) -> None:
    for name in (
        "DEMO_PG_HOST",
        "DEMO_PG_USER",
        "DEMO_PG_PASSWORD",
        "DEMO_PG_DATABASE",
        "DEMO_PG_DB",
        "DEMO_PG_NAME",
        "DEMO_PG_PORT",
        "DEMO_PG_SSLMODE",
        "DEMO_PG_ENGINE",
        "DEMO_RS_HOST",
        "DEMO_RS_USER",
        "DEMO_RS_PASSWORD",
        "DEMO_RS_DATABASE",
        "DEMO_RS_DB",
        "DEMO_RS_NAME",
        "DEMO_RS_PORT",
        "DEMO_RS_SSLMODE",
        "DEMO_RS_ENGINE",
        "PGUSER",
        "PGPASSWORD",
        "PGHOST",
        "PGDATABASE",
        "PGPORT",
        "PGSSLMODE",
        "PGCLIENTENCODING",
        "DB_USER",
        "DB_PASSWORD",
        "DB_HOST",
        "DB_NAME",
        "DB_SSLMODE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_constants() -> None:
    assert DEPRECATED_SUFFIX == "_deprecated"
    assert frozenset({"success", "error"}) == LIVE_STATUSES
    assert DBT_ARTIFACTS_SCHEMA == "dbt_artifacts"


def test_excluded_schemas_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DP_DBT_ORPHANS_EXCLUDE_SCHEMAS", raising=False)
    assert excluded_schemas() == frozenset({"raw", "_raw", "dbt_artifacts"})


def test_excluded_schemas_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DP_DBT_ORPHANS_EXCLUDE_SCHEMAS", "a, b ,")
    assert excluded_schemas() == frozenset({"a", "b"})


def test_node_prefix_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DP_DBT_PROJECT", "acme")
    assert node_prefix() == "model.acme."


def test_node_prefix_requires_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DP_DBT_PROJECT", raising=False)
    with pytest.raises(ConfigError, match="DP_DBT_PROJECT"):
        node_prefix()


def test_invocation_command_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DP_DBT_INVOCATION_COMMAND", raising=False)
    assert invocation_command() is None
    monkeypatch.setenv("DP_DBT_INVOCATION_COMMAND", "dbt build")
    assert invocation_command() == "dbt build"


def test_classify_object_returns_view_for_view() -> None:
    cur = FakeCursor([("VIEW",)])
    assert classify_object(cur, "public", "my_view", is_redshift=False) == "view"
    assert len(cur.queries) == 1


def test_classify_object_returns_table_for_base_table() -> None:
    cur = FakeCursor([("BASE TABLE",)])
    assert classify_object(cur, "public", "t", is_redshift=False) == "table"


def test_classify_object_returns_matview_on_postgres() -> None:
    cur = FakeCursor([None, (1,)])
    assert classify_object(cur, "public", "mv", is_redshift=False) == "matview"
    assert len(cur.queries) == 2


def test_classify_object_skips_pg_matviews_on_redshift() -> None:
    cur = FakeCursor([None])
    assert classify_object(cur, "public", "missing", is_redshift=True) is None
    assert len(cur.queries) == 1


def test_classify_object_returns_none_when_absent() -> None:
    cur = FakeCursor([None, None])
    assert classify_object(cur, "public", "missing", is_redshift=False) is None


def test_build_rename_statement_table() -> None:
    stmt = build_rename_statement("public", "old", "new", "table")
    assert stmt.as_string(None) == 'ALTER TABLE "public"."old" RENAME TO "new"'


def test_build_rename_statement_view() -> None:
    stmt = build_rename_statement("public", "old_v", "new_v", "view")
    assert stmt.as_string(None) == 'ALTER VIEW "public"."old_v" RENAME TO "new_v"'


def test_build_rename_statement_matview() -> None:
    stmt = build_rename_statement("public", "old_mv", "new_mv", "matview")
    assert (
        stmt.as_string(None)
        == 'ALTER MATERIALIZED VIEW "public"."old_mv" RENAME TO "new_mv"'
    )


def test_build_rename_statement_quotes_unsafe_identifier() -> None:
    stmt = build_rename_statement('schema"with"quotes', "t", "t_deprecated", "table")
    assert '"schema""with""quotes"' in stmt.as_string(None)


def test_build_rename_statement_redshift_view_uses_alter_table() -> None:
    stmt = build_rename_statement(
        "staging", "stg_foo", "stg_foo_deprecated", "view", is_redshift=True
    )
    assert (
        stmt.as_string(None)
        == 'ALTER TABLE "staging"."stg_foo" RENAME TO "stg_foo_deprecated"'
    )


def test_build_rename_statement_redshift_table_uses_alter_table() -> None:
    stmt = build_rename_statement(
        "analytics", "t", "t_deprecated", "table", is_redshift=True
    )
    assert (
        stmt.as_string(None) == 'ALTER TABLE "analytics"."t" RENAME TO "t_deprecated"'
    )


def test_resolve_params_postgres_uses_demo_pg_prefix(monkeypatch) -> None:
    _clear_orphan_env(monkeypatch)
    monkeypatch.setenv("DEMO_PG_HOST", "pg-host")
    monkeypatch.setenv("DEMO_PG_USER", "pg-user")
    monkeypatch.setenv("DEMO_PG_PASSWORD", "pg-pass")
    monkeypatch.setenv("DEMO_PG_DATABASE", "demo_pg")

    params = resolve_orphans_connection_params(
        SqlEngine.postgresql, env_prefix="DEMO_PG"
    )
    assert params is not None
    assert params.host == "pg-host"
    assert params.user == "pg-user"
    assert params.password == "pg-pass"
    assert params.dbname == "demo_pg"
    assert params.port == 5432
    assert params.client_encoding is None


def test_resolve_params_postgres_respects_overrides(monkeypatch) -> None:
    _clear_orphan_env(monkeypatch)
    monkeypatch.setenv("DEMO_PG_HOST", "h")
    monkeypatch.setenv("DEMO_PG_USER", "u")
    monkeypatch.setenv("DEMO_PG_PASSWORD", "p")
    monkeypatch.setenv("DEMO_PG_DATABASE", "custom_db")
    monkeypatch.setenv("DEMO_PG_PORT", "6543")

    params = resolve_orphans_connection_params(
        SqlEngine.postgresql, env_prefix="DEMO_PG"
    )
    assert params is not None
    assert params.dbname == "custom_db"
    assert params.port == 6543


def test_resolve_params_redshift_uses_demo_rs_prefix(monkeypatch) -> None:
    _clear_orphan_env(monkeypatch)
    monkeypatch.setenv("DEMO_RS_HOST", "rs-host")
    monkeypatch.setenv("DEMO_RS_USER", "rs-user")
    monkeypatch.setenv("DEMO_RS_PASSWORD", "rs-pass")
    monkeypatch.setenv("DEMO_RS_DATABASE", "demo_rs")

    params = resolve_orphans_connection_params(SqlEngine.redshift, env_prefix="DEMO_RS")
    assert params is not None
    assert params.host == "rs-host"
    assert params.user == "rs-user"
    assert params.dbname == "demo_rs"
    assert params.port == 5439
    assert params.client_encoding == "UTF8"


def test_resolve_params_returns_none_when_creds_missing(monkeypatch) -> None:
    _clear_orphan_env(monkeypatch)
    assert (
        resolve_orphans_connection_params(SqlEngine.postgresql, env_prefix="DEMO_PG")
        is None
    )
    assert (
        resolve_orphans_connection_params(SqlEngine.redshift, env_prefix="DEMO_RS")
        is None
    )


def test_resolve_params_returns_none_when_password_missing(monkeypatch) -> None:
    _clear_orphan_env(monkeypatch)
    monkeypatch.setenv("DEMO_PG_HOST", "h")
    monkeypatch.setenv("DEMO_PG_USER", "u")
    monkeypatch.setenv("DEMO_PG_DATABASE", "d")
    assert (
        resolve_orphans_connection_params(SqlEngine.postgresql, env_prefix="DEMO_PG")
        is None
    )


def test_resolve_params_returns_none_when_database_missing(monkeypatch) -> None:
    _clear_orphan_env(monkeypatch)
    monkeypatch.setenv("DEMO_PG_HOST", "h")
    monkeypatch.setenv("DEMO_PG_USER", "u")
    monkeypatch.setenv("DEMO_PG_PASSWORD", "p")
    assert (
        resolve_orphans_connection_params(SqlEngine.postgresql, env_prefix="DEMO_PG")
        is None
    )


def test_resolve_params_raises_on_invalid_port(monkeypatch) -> None:
    _clear_orphan_env(monkeypatch)
    monkeypatch.setenv("DEMO_PG_HOST", "h")
    monkeypatch.setenv("DEMO_PG_USER", "u")
    monkeypatch.setenv("DEMO_PG_PASSWORD", "p")
    monkeypatch.setenv("DEMO_PG_DATABASE", "d")
    monkeypatch.setenv("DEMO_PG_PORT", "not-a-port")

    with pytest.raises(ConfigError):
        resolve_orphans_connection_params(SqlEngine.postgresql, env_prefix="DEMO_PG")


class FakeCursorWithAll:
    """Cursor stub that replays a full result set via fetchall."""

    def __init__(self, rows: list[tuple]) -> None:
        self._rows = list(rows)
        self.queries: list[tuple[str, tuple]] = []

    def execute(self, query, params=None) -> None:
        self.queries.append((str(query), tuple(params or ())))

    def fetchall(self) -> list[tuple]:
        return list(self._rows)


class MultiResultCursor:
    """Cursor stub that returns a different fetchall result per call."""

    def __init__(self, result_sets: list[list[tuple]]) -> None:
        self._result_sets = list(result_sets)
        self.queries: list[tuple[str, tuple]] = []

    def execute(self, query, params=None) -> None:
        self.queries.append((str(query), tuple(params or ())))

    def fetchall(self) -> list[tuple]:
        return list(self._result_sets.pop(0)) if self._result_sets else []


def test_fetch_live_model_relations_unions_across_window() -> None:
    from datetime import datetime, timedelta

    from dataplat.services.db.orphans import fetch_live_model_relations

    cur = FakeCursorWithAll(
        [
            ("public", "stg_users"),
            ("public", "fct_orders"),
            ("analytics", "dim_customer"),
        ]
    )
    since = datetime.now(UTC) - timedelta(days=7)
    result = fetch_live_model_relations(
        cur,
        invocation_command="dbt start /foo",
        node_prefix="model.acme.",
        statuses=frozenset({"success", "error"}),
        since=since,
    )
    assert result == {
        "public": {"stg_users", "fct_orders"},
        "analytics": {"dim_customer"},
    }
    assert len(cur.queries) == 1
    query_text, params = cur.queries[0]
    assert "FROM dbt_artifacts.model_executions" in query_text
    assert "JOIN dbt_artifacts.invocations" in query_text
    assert "i.dbt_command = 'build'" in query_text
    assert "i.run_started_at >= %s" in query_text
    assert params[0] == '%"invocation_command": "dbt start /foo"%'
    assert params[1] == since
    assert params[2] == "model.acme.%"
    assert set(params[3]) == {"success", "error"}


def test_fetch_live_model_relations_empty() -> None:
    from datetime import datetime

    from dataplat.services.db.orphans import fetch_live_model_relations

    cur = FakeCursorWithAll([])
    result = fetch_live_model_relations(
        cur,
        invocation_command="x",
        node_prefix="model.acme.",
        statuses=frozenset({"success"}),
        since=datetime.now(UTC),
    )
    assert result == {}


def test_fetch_existing_relations_postgres_merges_tables_and_matviews() -> None:
    from dataplat.services.db.orphans import fetch_existing_relations

    cur = MultiResultCursor(
        [
            [
                ("public", "users", "BASE TABLE"),
                ("public", "users_view", "VIEW"),
                ("analytics", "reports", "BASE TABLE"),
            ],
            [("public", "users_matview")],
        ]
    )
    result = fetch_existing_relations(cur, ["public", "analytics"], is_redshift=False)
    assert result == {
        "public": {"users", "users_view", "users_matview"},
        "analytics": {"reports"},
    }
    assert len(cur.queries) == 2
    assert "information_schema.tables" in cur.queries[0][0]
    assert "pg_inherits" in cur.queries[0][0]
    assert "pg_matviews" in cur.queries[1][0]


def test_fetch_existing_relations_redshift_skips_pg_matviews() -> None:
    from dataplat.services.db.orphans import fetch_existing_relations

    cur = MultiResultCursor([[("public", "users", "BASE TABLE")]])
    result = fetch_existing_relations(cur, ["public"], is_redshift=True)
    assert result == {"public": {"users"}}
    assert len(cur.queries) == 1
    assert "pg_matviews" not in cur.queries[0][0]
    assert "pg_inherits" not in cur.queries[0][0]


def test_fetch_existing_relations_empty_schemas_returns_empty() -> None:
    from dataplat.services.db.orphans import fetch_existing_relations

    cur = MultiResultCursor([])
    result = fetch_existing_relations(cur, [], is_redshift=False)
    assert result == {}
    assert cur.queries == []


def test_diff_orphans_basic() -> None:
    from dataplat.services.db.orphans import diff_orphans

    live = {"public": {"stg_users", "fct_orders"}}
    existing = {"public": {"stg_users", "fct_orders", "old_table"}}
    assert diff_orphans(
        live=live,
        existing=existing,
        excluded_schemas=frozenset(),
        excluded_user_schemas=frozenset(),
        excluded_user_relations=frozenset(),
    ) == {"public": ["old_table"]}


def test_diff_orphans_skips_already_deprecated() -> None:
    from dataplat.services.db.orphans import diff_orphans

    live = {"public": {"fct_orders"}}
    existing = {"public": {"fct_orders", "old_deprecated"}}
    assert (
        diff_orphans(
            live=live,
            existing=existing,
            excluded_schemas=frozenset(),
            excluded_user_schemas=frozenset(),
            excluded_user_relations=frozenset(),
        )
        == {}
    )


def test_diff_orphans_honors_excluded_schemas() -> None:
    from dataplat.services.db.orphans import diff_orphans

    live = {"public": set()}
    existing = {"public": {"x"}, "raw": {"y"}}
    assert diff_orphans(
        live=live,
        existing=existing,
        excluded_schemas=frozenset({"raw"}),
        excluded_user_schemas=frozenset(),
        excluded_user_relations=frozenset(),
    ) == {"public": ["x"]}


def test_diff_orphans_honors_user_excluded_schemas() -> None:
    from dataplat.services.db.orphans import diff_orphans

    live = {"public": set(), "analytics": set()}
    existing = {"public": {"a"}, "analytics": {"b"}}
    assert diff_orphans(
        live=live,
        existing=existing,
        excluded_schemas=frozenset(),
        excluded_user_schemas=frozenset({"analytics"}),
        excluded_user_relations=frozenset(),
    ) == {"public": ["a"]}


def test_diff_orphans_honors_user_excluded_relations() -> None:
    from dataplat.services.db.orphans import diff_orphans

    live = {"public": set()}
    existing = {"public": {"keep", "drop"}}
    assert diff_orphans(
        live=live,
        existing=existing,
        excluded_schemas=frozenset(),
        excluded_user_schemas=frozenset(),
        excluded_user_relations=frozenset({("public", "keep")}),
    ) == {"public": ["drop"]}


def test_diff_orphans_returns_sorted_names() -> None:
    from dataplat.services.db.orphans import diff_orphans

    live = {"public": set()}
    existing = {"public": {"c", "a", "b"}}
    assert diff_orphans(
        live=live,
        existing=existing,
        excluded_schemas=frozenset(),
        excluded_user_schemas=frozenset(),
        excluded_user_relations=frozenset(),
    ) == {"public": ["a", "b", "c"]}


def test_diff_orphans_drops_schemas_with_no_orphans() -> None:
    from dataplat.services.db.orphans import diff_orphans

    live = {"public": {"a"}, "analytics": set()}
    existing = {"public": {"a"}, "analytics": {"b"}}
    assert diff_orphans(
        live=live,
        existing=existing,
        excluded_schemas=frozenset(),
        excluded_user_schemas=frozenset(),
        excluded_user_relations=frozenset(),
    ) == {"analytics": ["b"]}


def test_build_drop_statement_table() -> None:
    from dataplat.services.db.orphans import build_drop_statement

    stmt = build_drop_statement("public", "foo_deprecated", "table")
    assert stmt.as_string(None) == 'DROP TABLE "public"."foo_deprecated"'


def test_build_drop_statement_view() -> None:
    from dataplat.services.db.orphans import build_drop_statement

    stmt = build_drop_statement("public", "foo_deprecated", "view")
    assert stmt.as_string(None) == 'DROP VIEW "public"."foo_deprecated"'


def test_build_drop_statement_matview() -> None:
    from dataplat.services.db.orphans import build_drop_statement

    stmt = build_drop_statement("public", "foo_deprecated", "matview")
    assert stmt.as_string(None) == 'DROP MATERIALIZED VIEW "public"."foo_deprecated"'


def test_fetch_deprecated_objects_postgres_merges_tables_and_matviews() -> None:
    from dataplat.services.db.orphans import fetch_deprecated_objects

    cur = MultiResultCursor(
        [
            [
                ("public", "users_deprecated", "BASE TABLE"),
                ("public", "old_view_deprecated", "VIEW"),
                ("raw", "sensitive_deprecated", "BASE TABLE"),
            ],
            [("public", "mv_deprecated")],
        ]
    )
    result = fetch_deprecated_objects(
        cur,
        is_redshift=False,
        excluded_schemas=frozenset({"raw"}),
    )
    assert sorted(result) == [
        ("public", "mv_deprecated", "matview"),
        ("public", "old_view_deprecated", "view"),
        ("public", "users_deprecated", "table"),
    ]
    assert len(cur.queries) == 2
    assert "information_schema.tables" in cur.queries[0][0]
    assert "pg_inherits" in cur.queries[0][0]
    assert "pg_matviews" in cur.queries[1][0]


def test_fetch_deprecated_objects_redshift_skips_pg_matviews() -> None:
    from dataplat.services.db.orphans import fetch_deprecated_objects

    cur = MultiResultCursor([[("public", "users_deprecated", "BASE TABLE")]])
    result = fetch_deprecated_objects(
        cur,
        is_redshift=True,
        excluded_schemas=frozenset(),
    )
    assert result == [("public", "users_deprecated", "table")]
    assert len(cur.queries) == 1
    assert "pg_matviews" not in cur.queries[0][0]
    assert "pg_inherits" not in cur.queries[0][0]


def test_fetch_deprecated_objects_filters_excluded_schemas() -> None:
    from dataplat.services.db.orphans import fetch_deprecated_objects

    cur = MultiResultCursor(
        [
            [
                ("public", "a_deprecated", "BASE TABLE"),
                ("dbt_artifacts", "b_deprecated", "BASE TABLE"),
            ],
            [],
        ]
    )
    result = fetch_deprecated_objects(
        cur,
        is_redshift=False,
        excluded_schemas=frozenset({"dbt_artifacts"}),
    )
    assert result == [("public", "a_deprecated", "table")]
