"""Options and resolution shared by every dbt command."""

from __future__ import annotations

import typer

from dataplat.core.errors import ValidationError
from dataplat.services.db.targets import DbTarget, resolve_target
from dataplat.services.dbt.projects import (
    DbtProject,
    default_project_name,
    resolve_projects,
)

ProjectOption = typer.Option(
    None,
    "--project",
    "-p",
    help=(
        "Named dbt project from DP_DBT_PROJECTS, or all "
        "(default: DP_DBT_DEFAULT_PROJECT)."
    ),
)

TargetFilterOption = typer.Option(
    None,
    "--target",
    "-t",
    help=(
        "Narrow to one of the project's own targets. "
        "Defaults to every target the project declares."
    ),
)


def projects_and_targets(
    project: str | None, target: str | None
) -> list[tuple[DbtProject, list[DbTarget]]]:
    """Resolve ``--project``/``--target`` into projects and their targets.

    A project declares the targets it builds into, so ``--project`` alone fans
    out over all of them and ``--target`` narrows. Narrowing to a target the
    project does not declare is rejected rather than silently accepted: the
    command would otherwise scan a warehouse this project never writes to and
    report everything in it as an orphan.
    """
    name = project or default_project_name()
    if name is None:
        raise ValidationError(
            "No dbt project given and none configured. Pass --project or set "
            "DP_DBT_PROJECTS."
        )
    pairs: list[tuple[DbtProject, list[DbTarget]]] = []
    for resolved in resolve_projects(name):
        declared = resolved.target_names
        if not declared:
            raise ValidationError(
                f"Project '{resolved.name}' declares no targets. Set "
                f"{resolved.env_prefix}_DBT_TARGETS."
            )
        if target is not None:
            wanted = target.strip().lower()
            if wanted not in declared:
                raise ValidationError(
                    f"Project '{resolved.name}' does not build into '{wanted}'. "
                    f"It declares: {', '.join(declared)}."
                )
            names = [wanted]
        else:
            names = list(declared)
        pairs.append((resolved, [resolve_target(n) for n in names]))
    return pairs
