"""``dp bi superset dashboards datasets`` -- filled in by Task 7."""

from __future__ import annotations

import typer

__all__ = ["datasets_command"]


def datasets_command(
    dashboard: str = typer.Argument(..., help="Dashboard id or slug."),
) -> None:
    """What a dashboard reads."""
    raise typer.Exit(code=1)
