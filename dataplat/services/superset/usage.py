"""How much a dashboard is actually looked at, read from Superset's own logs.

Every Superset instance records what happens to it in two tables that ship with
the product: ``logs`` (Flask-AppBuilder's action log, carrying ``action``,
``user_id``, ``dashboard_id`` and ``dttm``) and ``ab_user``. This module reads
*those* -- never a modelled copy of them -- which is what lets one command work
for anybody. A company that replicates its metadata database into a warehouse
points the two table variables at the replica; the column names are Superset's
either way, so there is nothing to map.

The action vocabulary is Superset's too, and it is what separates a human
opening a dashboard from the machinery around it. ``dashboard`` is the open;
``warm_up_cache``, ``ChartRestApi.warm_up_cache`` and ``ChartRestApi.thumbnail``
are the cache warmer and the thumbnail worker. Filtering on the action is
therefore not a local heuristic about who your service accounts are -- it holds
on any instance, before you know a single username. ``exclude_users`` remains
as a second net for an instance whose automation logs under a real account.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import cast

from dataplat.core.errors import ConfigError, ValidationError

__all__ = [
    "DEFAULT_LOGS_TABLE",
    "DEFAULT_USERS_TABLE",
    "DEFAULT_VIEW_ACTION",
    "TARGET_VAR",
    "ActionCount",
    "DashboardUsage",
    "UsageConfig",
    "action_rows",
    "actions_query",
    "load_usage_config",
    "usage_query",
    "usage_rows",
    "validate_table_name",
]

# Superset's own names, so the common case -- a target pointing straight at the
# metadata database -- needs no table configuration at all.
DEFAULT_LOGS_TABLE = "public.logs"
DEFAULT_USERS_TABLE = "public.ab_user"

# The action Superset writes when somebody opens a dashboard.
DEFAULT_VIEW_ACTION = "dashboard"

TARGET_VAR = "DP_SUPERSET_USAGE_TARGET"
LOGS_TABLE_VAR = "DP_SUPERSET_USAGE_LOGS_TABLE"
USERS_TABLE_VAR = "DP_SUPERSET_USAGE_USERS_TABLE"
EXCLUDE_USERS_VAR = "DP_SUPERSET_USAGE_EXCLUDE_USERS"

# One unquoted SQL identifier: a leading letter or underscore, then letters,
# digits, underscores or `$` (Redshift permits it). Deliberately strict --
# these values arrive from the environment and are interpolated as identifiers,
# which is the one position a placeholder cannot fill.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


@dataclass(frozen=True)
class UsageConfig:
    """Where this installation keeps Superset's log tables."""

    target: str
    logs_table: str = DEFAULT_LOGS_TABLE
    users_table: str = DEFAULT_USERS_TABLE
    exclude_users: tuple[str, ...] = ()


def validate_table_name(name: str, *, variable: str) -> str:
    """Return ``name`` if it is a bare or schema-qualified identifier.

    A table name cannot be passed as a query parameter, so it is interpolated
    into the SQL -- and anything interpolated has to be proved safe rather than
    trusted. ``.envrc`` is not user input in the argv sense, but it is read from
    disk and it is shared between people, so it gets the same treatment.
    """
    parts = name.split(".")
    if len(parts) > 2 or not all(_IDENTIFIER.match(part) for part in parts):
        raise ValidationError(
            f"{variable}={name!r} is not a table name. "
            "Expected `table` or `schema.table`, letters, digits, `_` and `$` only."
        )
    return name


def _table_from_env(variable: str, default: str) -> str:
    return validate_table_name(
        (os.getenv(variable) or "").strip() or default, variable=variable
    )


def load_usage_config() -> UsageConfig:
    """Build the usage configuration from the environment."""
    target = (os.getenv(TARGET_VAR) or "").strip()
    if not target:
        raise ConfigError(
            f"Set {TARGET_VAR} to the name of the DP_TARGETS entry holding "
            "Superset's log tables (its metadata database, or a replica of it)."
        )
    raw_excludes = (os.getenv(EXCLUDE_USERS_VAR) or "").split(",")
    return UsageConfig(
        target=target,
        logs_table=_table_from_env(LOGS_TABLE_VAR, DEFAULT_LOGS_TABLE),
        users_table=_table_from_env(USERS_TABLE_VAR, DEFAULT_USERS_TABLE),
        exclude_users=tuple(name.strip() for name in raw_excludes if name.strip()),
    )


def _cutoff(days: int) -> date:
    """The oldest ``dttm`` the window includes, as a bound value.

    Computed here rather than as ``current_date - %s`` in the SQL for two
    reasons, neither of which is portability -- that spelling is accepted by
    both Postgres and Redshift. First, the boundary becomes a value the command
    can print, so a report can say which window it actually covered. Second,
    two queries issued seconds apart cannot straddle midnight and disagree.

    UTC because that is what Superset writes into ``logs.dttm``; deriving the
    cutoff from the caller's local date would move the window by a day for
    anybody east or west of it.
    """
    if days < 1:
        raise ValidationError(f"--days must be at least 1, got {days}.")
    return datetime.now(UTC).date() - timedelta(days=days)


def _limit_clause(limit: int | None) -> str:
    """The ``LIMIT`` line, which is interpolated rather than bound.

    ``LIMIT %s`` is the one placeholder Redshift will not accept: it answers
    with ``Not implemented ... IsA(cons, Const)`` and no rows at all. So the cap
    goes into the SQL as text -- safely, because it is range-checked to an
    ``int`` first, and an ``int`` cannot carry SQL.

    ``None`` means every row. ``--unused`` needs that: it subtracts the
    dashboards with views from the dashboards that exist, and a capped left
    side would report everything past the cap as never opened.
    """
    if limit is None:
        return ""
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValidationError(f"--limit must be a whole number, got {limit!r}.")
    if limit < 1:
        raise ValidationError(f"--limit must be at least 1, got {limit}.")
    return f"\n LIMIT {limit}"


def usage_query(
    config: UsageConfig,
    *,
    days: int,
    view_actions: Sequence[str],
    limit: int | None,
) -> tuple[str, list[object]]:
    """Views per dashboard over a window, as ``(sql, params)``.

    Everything that could carry SQL is bound: the actions, the excluded
    usernames, the window. Exactly two things are interpolated, and both are
    proved safe first rather than trusted -- the table names, which
    :func:`validate_table_name` restricts to identifiers, and the row cap,
    which :func:`_limit_clause` reduces to an ``int`` because Redshift will not
    bind a ``LIMIT``. The tests assert that split by hunting for the values in
    the SQL, rather than by taking this docstring's word for it.
    """
    if not view_actions:
        raise ValidationError(
            "At least one --view-action is required; counting every action "
            "would count cache warm-ups and thumbnails as people."
        )
    params: list[object] = [_cutoff(days), *view_actions]
    action_slots = ", ".join(["%s"] * len(view_actions))

    # An empty NOT IN list is a syntax error, so the clause is built only when
    # there is something to exclude. The `is null` arm keeps rows whose user has
    # since been deleted, and anonymous views of a public dashboard: both are
    # somebody looking, and dropping them would undercount.
    exclusion = ""
    if config.exclude_users:
        user_slots = ", ".join(["%s"] * len(config.exclude_users))
        exclusion = f"\n   AND (u.username IS NULL OR u.username NOT IN ({user_slots}))"
        params.extend(config.exclude_users)

    limit_clause = _limit_clause(limit)
    sql = (
        "SELECT l.dashboard_id,\n"
        "       COUNT(*) AS views,\n"
        "       COUNT(DISTINCT l.user_id) AS viewers,\n"
        "       MAX(l.dttm) AS last_viewed\n"
        f"  FROM {config.logs_table} l\n"
        f"  LEFT JOIN {config.users_table} u ON u.id = l.user_id\n"
        " WHERE l.dttm >= %s\n"
        f"   AND l.action IN ({action_slots})\n"
        "   AND l.dashboard_id IS NOT NULL"
        f"{exclusion}\n"
        " GROUP BY l.dashboard_id\n"
        " ORDER BY views DESC, l.dashboard_id"
        f"{limit_clause}"
    )
    return sql, params


def actions_query(
    config: UsageConfig, *, days: int, limit: int | None
) -> tuple[str, list[object]]:
    """Every action this instance logged in the window, most frequent first.

    Deliberately unfiltered. The point of ``--actions`` is to show which
    vocabulary *your* Superset uses, so that somebody whose version does not
    spell a dashboard open ``dashboard`` can find out what it does spell it --
    and see the cache-warmer and thumbnail actions sitting next to it.
    """
    limit_clause = _limit_clause(limit)
    sql = (
        "SELECT l.action,\n"
        "       COUNT(*) AS events,\n"
        "       COUNT(l.dashboard_id) AS with_dashboard,\n"
        "       MAX(l.dttm) AS last_seen\n"
        f"  FROM {config.logs_table} l\n"
        " WHERE l.dttm >= %s\n"
        " GROUP BY l.action\n"
        " ORDER BY events DESC"
        f"{limit_clause}"
    )
    return sql, [_cutoff(days)]


@dataclass(frozen=True)
class DashboardUsage:
    """One dashboard's usage over the window."""

    dashboard_id: int
    views: int
    viewers: int
    last_viewed: datetime | None
    title: str = ""


@dataclass(frozen=True)
class ActionCount:
    """One action name and how often the instance logged it."""

    action: str
    events: int
    with_dashboard: int
    last_seen: datetime | None


def usage_rows(rows: Iterable[Sequence[object]]) -> list[DashboardUsage]:
    """Shape raw driver rows into :class:`DashboardUsage`."""
    return [
        DashboardUsage(
            dashboard_id=int(cast(int, row[0])),
            views=int(cast(int, row[1])),
            viewers=int(cast(int, row[2])),
            last_viewed=cast("datetime | None", row[3]),
        )
        for row in rows
    ]


def action_rows(rows: Iterable[Sequence[object]]) -> list[ActionCount]:
    """Shape raw driver rows into :class:`ActionCount`."""
    return [
        ActionCount(
            action=str(row[0]),
            events=int(cast(int, row[1])),
            with_dashboard=int(cast(int, row[2])),
            last_seen=cast("datetime | None", row[3]),
        )
        for row in rows
    ]
