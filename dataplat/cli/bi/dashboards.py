"""Typer sub-app for ``dp bi superset dashboards``.

Wiring only. Each subcommand's parsing and rendering lives in its own module,
so this file stays a one-glance map of the group -- the shape
``dp db schema`` already uses.
"""

from __future__ import annotations

import typer

from dataplat.cli.bi.dashboards_datasets import datasets_command
from dataplat.cli.bi.dashboards_duplicate import duplicate_command
from dataplat.cli.bi.dashboards_list import list_command

__all__ = ["app"]

app = typer.Typer(
    name="dashboards",
    help="Inspect and duplicate Superset dashboards.",
    no_args_is_help=True,
)
app.command("list", help="List dashboards, with the databases they read.")(list_command)
app.command(
    "datasets",
    help="What a dashboard reads, and whether a target database has it.",
)(datasets_command)
app.command(
    "duplicate",
    help="Copy a dashboard and repoint the copy onto another database.",
)(duplicate_command)
