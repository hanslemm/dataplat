"""Typer sub-app for ``dp db schema``.

Wiring only. Each subcommand's parsing and rendering lives in its own module, so
this file stays a one-glance map of the group.
"""

from __future__ import annotations

import typer

from dataplat.cli.db.schema_alter import alter_command
from dataplat.cli.db.schema_create import create_command
from dataplat.cli.db.schema_drop import drop_command
from dataplat.cli.db.schema_grant import grant_command, revoke_command
from dataplat.cli.db.schema_list import list_command

app = typer.Typer(
    name="schema",
    help="Inspect, create, drop, and manage privileges on schemas.",
    no_args_is_help=True,
)
app.command(
    "list",
    help="List schemas with owner and object counts.",
)(list_command)
app.command(
    "create",
    help="Create one or more schemas, with an optional owner and quota.",
)(create_command)
app.command(
    "drop",
    help="Drop one or more schemas, showing owner and object counts first.",
)(drop_command)
app.command(
    "grant",
    help="Grant schema privileges to users, groups, or roles.",
)(grant_command)
app.command(
    "revoke",
    help="Revoke schema privileges from users, groups, or roles.",
)(revoke_command)
app.command(
    "alter",
    help="Change a schema's owner, quota, or name.",
)(alter_command)
