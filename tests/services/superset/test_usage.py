"""Reading dashboard usage out of Superset's own log tables.

The tables these queries read are Superset's, not any one company's: ``logs``
and ``ab_user`` ship with every instance. That is what makes the command
portable, and it is why the defaults here are Superset's own names.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from dataplat.core.errors import ConfigError, ValidationError
from dataplat.services.superset.usage import (
    DEFAULT_LOGS_TABLE,
    DEFAULT_USERS_TABLE,
    DEFAULT_VIEW_ACTION,
    UsageConfig,
    actions_query,
    load_usage_config,
    usage_query,
)


def test_a_missing_target_names_the_variable_to_set(monkeypatch) -> None:
    monkeypatch.delenv("DP_SUPERSET_USAGE_TARGET", raising=False)

    with pytest.raises(ConfigError) as excinfo:
        load_usage_config()

    assert "DP_SUPERSET_USAGE_TARGET" in str(excinfo.value)


def test_the_tables_default_to_supersets_own_names(monkeypatch) -> None:
    """One variable is enough when the target *is* the Superset metadata DB."""
    monkeypatch.setenv("DP_SUPERSET_USAGE_TARGET", "meta")
    monkeypatch.delenv("DP_SUPERSET_USAGE_LOGS_TABLE", raising=False)
    monkeypatch.delenv("DP_SUPERSET_USAGE_USERS_TABLE", raising=False)

    config = load_usage_config()

    assert config.target == "meta"
    assert config.logs_table == DEFAULT_LOGS_TABLE == "public.logs"
    assert config.users_table == DEFAULT_USERS_TABLE == "public.ab_user"
    assert config.exclude_users == ()


def test_a_replica_under_other_names_is_configured_not_modelled(monkeypatch) -> None:
    monkeypatch.setenv("DP_SUPERSET_USAGE_TARGET", "betterdata")
    monkeypatch.setenv("DP_SUPERSET_USAGE_LOGS_TABLE", "_raw.raw_superset__logs")
    monkeypatch.setenv("DP_SUPERSET_USAGE_USERS_TABLE", "_raw.raw_superset__ab_user")
    monkeypatch.setenv("DP_SUPERSET_USAGE_EXCLUDE_USERS", "admin, thumbnail_worker ,")

    config = load_usage_config()

    assert config.logs_table == "_raw.raw_superset__logs"
    assert config.users_table == "_raw.raw_superset__ab_user"
    # Split, trimmed, and empties dropped -- a trailing comma is not a username.
    assert config.exclude_users == ("admin", "thumbnail_worker")


def test_a_table_name_that_is_not_an_identifier_is_refused(monkeypatch) -> None:
    """Env vars reach the SQL as identifiers, which cannot be parameterized."""
    monkeypatch.setenv("DP_SUPERSET_USAGE_TARGET", "meta")
    monkeypatch.setenv(
        "DP_SUPERSET_USAGE_LOGS_TABLE", "public.logs; DROP TABLE ab_user--"
    )

    with pytest.raises(ValidationError):
        load_usage_config()


def test_the_default_view_action_is_supersets_dashboard_open() -> None:
    assert DEFAULT_VIEW_ACTION == "dashboard"


# --- query building -----------------------------------------------------


def _config(**kwargs) -> UsageConfig:
    base = {
        "target": "meta",
        "logs_table": "_raw.raw_superset__logs",
        "users_table": "_raw.raw_superset__ab_user",
        "exclude_users": (),
    }
    return UsageConfig(**{**base, **kwargs})


def test_the_configured_tables_are_the_ones_queried() -> None:
    sql, _ = usage_query(_config(), days=90, view_actions=("dashboard",), limit=10)

    assert "_raw.raw_superset__logs" in sql
    assert "_raw.raw_superset__ab_user" in sql
    # The defaults must not leak in alongside the configured names.
    assert DEFAULT_LOGS_TABLE not in sql


def test_the_view_action_travels_as_a_parameter_not_as_sql() -> None:
    """The one assertion that fails if an action is ever interpolated.

    The sentinel matters: ``dashboard`` itself appears in the SQL legitimately,
    as the ``dashboard_id`` column, so asserting on it would pass either way.
    """
    sql, params = usage_query(
        _config(), days=90, view_actions=("dashboard", "sentinel_action"), limit=10
    )

    assert "sentinel_action" not in sql
    assert "dashboard" in params
    assert "sentinel_action" in params


def test_excluded_usernames_travel_as_parameters_not_as_sql() -> None:
    quoted = "o" + chr(39) + "brien"
    sql, params = usage_query(
        _config(exclude_users=("admin", quoted)),
        days=90,
        view_actions=("dashboard",),
        limit=10,
    )

    assert "admin" not in sql
    assert quoted not in sql
    assert "admin" in params
    assert quoted in params


def test_no_exclusions_means_no_exclusion_clause() -> None:
    """`not in ()` is a syntax error, so the clause has to disappear entirely."""
    sql, _ = usage_query(
        _config(exclude_users=()), days=90, view_actions=("dashboard",), limit=10
    )

    assert "not in" not in sql.lower()
    assert "()" not in sql.replace(" ", "")


def test_a_user_with_no_ab_user_row_is_still_counted() -> None:
    """A deleted account, or an anonymous view, is still somebody looking."""
    sql, _ = usage_query(
        _config(exclude_users=("admin",)),
        days=90,
        view_actions=("dashboard",),
        limit=10,
    )

    assert "left join" in sql.lower()
    assert "is null" in sql.lower()


def test_the_window_is_a_bound_date_so_the_report_can_name_it() -> None:
    _, params = usage_query(_config(), days=30, view_actions=("dashboard",), limit=7)

    assert datetime.now(UTC).date() - timedelta(days=30) in params


def test_the_row_cap_is_a_literal_because_redshift_refuses_a_bound_limit() -> None:
    """Live Redshift answers `LIMIT %s` with `Not implemented ... IsA(cons, Const)`.

    Verified against a real cluster, where every other placeholder in this query
    was accepted and this one alone was not.
    """
    sql, params = usage_query(_config(), days=30, view_actions=("dashboard",), limit=7)

    assert sql.rstrip().endswith("LIMIT 7")
    assert "LIMIT %s" not in sql
    assert 7 not in params


def test_asking_for_every_row_omits_the_limit_clause() -> None:
    """`--unused` subtracts the viewed from the existing, so it cannot be capped.

    A capped list would silently report every dashboard past the cap as never
    opened -- the one answer this command must not get wrong.
    """
    sql, _ = usage_query(_config(), days=90, view_actions=("dashboard",), limit=None)

    assert "limit" not in sql.lower()


@pytest.mark.parametrize("limit", [0, -5])
def test_a_cap_that_returns_nothing_is_refused(limit: int) -> None:
    with pytest.raises(ValidationError):
        usage_query(_config(), days=30, view_actions=("dashboard",), limit=limit)


def test_a_cap_that_is_not_a_number_never_reaches_the_sql() -> None:
    """The cap is interpolated, so `int` is the whole of its safety."""
    with pytest.raises(ValidationError):
        usage_query(
            _config(),
            days=30,
            view_actions=("dashboard",),
            limit="1; DROP TABLE logs",  # type: ignore[arg-type]
        )


def test_no_engine_specific_date_arithmetic_reaches_the_sql() -> None:
    sql, _ = usage_query(_config(), days=90, view_actions=("dashboard",), limit=10)

    lowered = sql.lower()
    assert "current_date" not in lowered
    assert "dateadd" not in lowered
    assert "interval" not in lowered


def test_the_action_report_bounds_its_window_the_same_way() -> None:
    _, params = actions_query(_config(), days=7, limit=25)

    assert datetime.now(UTC).date() - timedelta(days=7) in params


@pytest.mark.parametrize("days", [0, -1])
def test_a_window_that_is_not_a_window_is_refused(days: int) -> None:
    with pytest.raises(ValidationError):
        usage_query(_config(), days=days, view_actions=("dashboard",), limit=10)


def test_counting_views_without_an_action_would_count_everything() -> None:
    with pytest.raises(ValidationError):
        usage_query(_config(), days=90, view_actions=(), limit=10)


def test_the_action_report_counts_every_action_not_just_views() -> None:
    """`--actions` exists to reveal the vocabulary, so it must not pre-filter.

    A report that already filtered to ``dashboard`` could never show you
    ``warm_up_cache`` -- and the whole point is to find out what your instance
    calls things.
    """
    sql, params = actions_query(_config(), days=90, limit=25)

    assert "group by" in sql.lower()
    assert "action" in sql.lower()
    assert "'dashboard'" not in sql
    assert DEFAULT_VIEW_ACTION not in params
