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
    """Per-project variable when a project is given, else the legacy global."""
    if project is not None:
        value = os.getenv(f"{project.env_prefix}_{suffix}", "").strip()
        if value:
            return value
    value = os.getenv(legacy, "").strip()
    return value or None


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
    which is how the legacy variable already behaved.
    """
    raw = _scoped(
        project, "DBT_ORPHANS_EXCLUDE_SCHEMAS", "DP_DBT_ORPHANS_EXCLUDE_SCHEMAS"
    )
    if raw is None:
        return DEFAULT_EXCLUDED_SCHEMAS
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


def invocation_command(project: DbtProject | None) -> str | None:
    """Optional dbt invocation-command filter, or None."""
    return _scoped(project, "DBT_INVOCATION_COMMAND", "DP_DBT_INVOCATION_COMMAND")
