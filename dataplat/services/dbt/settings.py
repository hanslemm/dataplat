"""Settings a dbt-aware command needs, scoped to a project where one exists.

Every accessor takes an optional project. Passing one reads that project's
variables; passing ``None`` reads the legacy globals, which is what an
installation that has not adopted ``DP_DBT_PROJECTS`` still has set.
"""

from __future__ import annotations

import os

from dataplat.core.errors import ConfigError
from dataplat.services.dbt.projects import DbtProject

DBT_ARTIFACTS_SCHEMA = "dbt_artifacts"

DEFAULT_EXCLUDED_SCHEMAS: frozenset[str] = frozenset(
    {"raw", "_raw", DBT_ARTIFACTS_SCHEMA}
)


def _scoped(project: DbtProject | None, suffix: str, legacy: str) -> str | None:
    """Per-project variable when a project is given, else the legacy global.

    Returns None if unset at all levels, or the value as-is (with any whitespace
    preserved) if found at the project or legacy level. Callers strip as
    appropriate for their use case.
    """
    if project is not None:
        value = os.getenv(f"{project.env_prefix}_{suffix}")
        if value is not None:
            return value
    value = os.getenv(legacy)
    return value if value is not None else None


def node_prefix(project: DbtProject | None) -> str:
    """dbt node-id prefix, ``model.<project name>.``"""
    if project is not None:
        return f"model.{project.project_name}."
    legacy = os.getenv("DP_DBT_PROJECT", "").strip()
    if not legacy:
        raise ConfigError(
            "No dbt project to scan for. Set DP_DBT_PROJECTS and pass "
            "--project, or set the legacy DP_DBT_PROJECT."
        )
    return f"model.{legacy}."


def excluded_schemas(project: DbtProject | None) -> frozenset[str]:
    """Schemas never scanned for orphans.

    A comma-separated list replaces the default set rather than adding to it,
    which is how the legacy variable already behaved. Leading and trailing
    whitespace around the entire value and around individual schema names is
    stripped.
    """
    raw = _scoped(
        project, "DBT_ORPHANS_EXCLUDE_SCHEMAS", "DP_DBT_ORPHANS_EXCLUDE_SCHEMAS"
    )
    if raw is None:
        return DEFAULT_EXCLUDED_SCHEMAS
    raw = raw.strip()
    if not raw:
        return frozenset()
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


def invocation_command(project: DbtProject | None) -> str | None:
    """Optional dbt invocation-command filter, or None.

    An explicitly empty or whitespace-only value is treated as unset. Leading
    and trailing whitespace is stripped for normalization. This differs
    slightly from the legacy no-strip behavior, preferring to normalize
    whitespace-only (user error) to None rather than returning it as-is.
    """
    raw = _scoped(project, "DBT_INVOCATION_COMMAND", "DP_DBT_INVOCATION_COMMAND")
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped if stripped else None
