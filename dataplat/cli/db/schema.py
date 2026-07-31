"""Typer sub-app for ``dp db schema``.

Wiring only. Each subcommand's parsing and rendering lives in its own module, so
this file stays a one-glance map of the group.
"""

from __future__ import annotations

import typer

from dataplat.cli.db.schema_list import list_command

app = typer.Typer(
    name="schema",
    help="Inspect schemas and their object counts.",
    no_args_is_help=True,
)
app.command(
    "list",
    help="List schemas with owner and object counts.",
)(list_command)
