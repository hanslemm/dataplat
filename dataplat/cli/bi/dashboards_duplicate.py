"""``dp bi superset dashboards duplicate`` -- filled in by Task 8."""

from __future__ import annotations

import typer

__all__ = ["duplicate_command"]


def duplicate_command(
    dashboard: str = typer.Argument(..., help="Dashboard id or slug."),
) -> None:
    """Copy a dashboard onto another database."""
    raise typer.Exit(code=1)
