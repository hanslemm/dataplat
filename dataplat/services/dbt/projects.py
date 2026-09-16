"""Named dbt projects, declared through the environment.

A project bundles what a dbt-aware command needs: where the project lives on
disk, which profiles directory it uses, the dbt project name that node ids are
prefixed with, and the database targets it builds into.

Projects are fully user-defined, mirroring ``DP_TARGETS``:

- ``DP_DBT_PROJECTS`` -- comma-separated project names.
- Per project ``<NAME>_DBT_PATH`` -- the project directory. Required.
- Per project ``<NAME>_DBT_PROFILES_DIR`` -- defaults to ``_DBT_PATH``.
- Per project ``<NAME>_DBT_NAME`` -- the dbt project name; defaults to the
  ``name:`` in the project's ``dbt_project.yml``.
- Per project ``<NAME>_DBT_TARGETS`` -- comma-separated ``DP_TARGETS`` names.
- ``DP_DBT_DEFAULT_PROJECT`` -- used when ``--project`` is omitted (defaults to
  the first name in ``DP_DBT_PROJECTS``).

``<NAME>`` is the project name uppercased with ``-`` mapped to ``_``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dataplat.core.errors import ConfigError, ValidationError


@dataclass(frozen=True)
class DbtProject:
    """A named dbt project the CLI can reason about."""

    name: str
    env_prefix: str
    path: Path
    profiles_dir: Path
    project_name: str
    target_names: tuple[str, ...]


ALL_PROJECTS = "all"

_PROJECTS_VAR = "DP_DBT_PROJECTS"
_DEFAULT_PROJECT_VAR = "DP_DBT_DEFAULT_PROJECT"


def _prefix_for(name: str) -> str:
    return name.strip().upper().replace("-", "_")


def _require_path(prefix: str) -> Path:
    raw = os.getenv(f"{prefix}_DBT_PATH", "").strip()
    if not raw:
        raise ConfigError(
            f"{prefix}_DBT_PATH must be set to the dbt project directory."
        )
    path = Path(raw).expanduser()
    if not (path / "dbt_project.yml").is_file():
        raise ConfigError(
            f"{prefix}_DBT_PATH='{path}' has no dbt_project.yml. "
            "Point it at the directory containing that file."
        )
    return path


def _project_name(prefix: str, path: Path) -> str:
    explicit = os.getenv(f"{prefix}_DBT_NAME", "").strip()
    if explicit:
        return explicit
    # Imported here, not at module scope: this is the only place in the module
    # that touches PyYAML, and PyYAML ships only under the `dbt` extra. Every
    # other function in this module (load_projects, default_project_name,
    # resolve_project/resolve_projects) has to work with only DP_TARGETS-style
    # env vars for a caller that sets <NAME>_DBT_NAME explicitly and never
    # needs its dbt_project.yml read -- a module-scope `import yaml` made that
    # impossible: any importer of this module (dataplat.cli.dbt._common, and
    # through it dataplat.cli.dbt.orphans, and through *that* the `dp db
    # dbt-orphans` backward-compat mount in dataplat.cli.db) required PyYAML
    # just to be imported, even for a dataplat[db]-only install that never
    # resolves a project's name from its dbt_project.yml at all.
    #
    # Reading it beats making the operator restate it: two sources of the same
    # fact drift, and the file is authoritative.
    import yaml

    with (path / "dbt_project.yml").open(encoding="utf-8") as handle:
        parsed = yaml.safe_load(handle) or {}
    name = str(parsed.get("name", "")).strip()
    if not name:
        raise ConfigError(
            f"{path / 'dbt_project.yml'} has no top-level 'name'. "
            f"Set {prefix}_DBT_NAME instead."
        )
    return name


def _target_names(prefix: str) -> tuple[str, ...]:
    raw = os.getenv(f"{prefix}_DBT_TARGETS", "")
    return tuple(chunk.strip().lower() for chunk in raw.split(",") if chunk.strip())


def _build_project(name: str) -> DbtProject:
    prefix = _prefix_for(name)
    path = _require_path(prefix)
    profiles_raw = os.getenv(f"{prefix}_DBT_PROFILES_DIR", "").strip()
    profiles_dir = Path(profiles_raw).expanduser() if profiles_raw else path
    return DbtProject(
        name=name,
        env_prefix=prefix,
        path=path,
        profiles_dir=profiles_dir,
        project_name=_project_name(prefix, path),
        target_names=_target_names(prefix),
    )


def load_projects() -> dict[str, DbtProject]:
    """Build the project registry from ``DP_DBT_PROJECTS``.

    Returns an empty dict when none are configured; callers then fall back to
    the legacy single-project variables.
    """
    raw = os.getenv(_PROJECTS_VAR, "")
    projects: dict[str, DbtProject] = {}
    for chunk in raw.split(","):
        name = chunk.strip().lower()
        if not name:
            continue
        if name == ALL_PROJECTS:
            raise ConfigError(f"'{ALL_PROJECTS}' is a reserved project name.")
        projects[name] = _build_project(name)
    return projects


def default_project_name() -> str | None:
    """The project used when ``--project`` is omitted, if any."""
    explicit = os.getenv(_DEFAULT_PROJECT_VAR, "").strip().lower()
    projects = load_projects()
    if explicit:
        if explicit not in projects:
            known = ", ".join(projects) or "none configured"
            raise ConfigError(
                f"{_DEFAULT_PROJECT_VAR}='{explicit}' is not in {_PROJECTS_VAR} "
                f"(known projects: {known})."
            )
        return explicit
    return next(iter(projects), None)


def resolve_project(name: str) -> DbtProject:
    """Return the project for ``name``; raise ValidationError if unknown."""
    projects = load_projects()
    project = projects.get(name.strip().lower())
    if project is None:
        if not projects:
            raise ValidationError(
                f"No dbt projects configured. Set {_PROJECTS_VAR} (e.g. "
                f"{_PROJECTS_VAR}=betterdoc) plus <NAME>_DBT_PATH."
            )
        known = ", ".join(projects)
        raise ValidationError(f"Unknown dbt project '{name}'. Known projects: {known}.")
    return project


def resolve_projects(name: str) -> list[DbtProject]:
    """Like resolve_project, but ``all`` expands to every configured project."""
    if name.strip().lower() == ALL_PROJECTS:
        projects = load_projects()
        if not projects:
            raise ValidationError(
                f"No dbt projects configured. Set {_PROJECTS_VAR} to use "
                f"'{ALL_PROJECTS}'."
            )
        return list(projects.values())
    return [resolve_project(name)]
