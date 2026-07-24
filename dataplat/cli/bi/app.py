"""BI command area."""

from __future__ import annotations

import typer

from dataplat.cli.bi import superset

app = typer.Typer(
    name="bi",
    help="Business-intelligence tools (Superset)",
    no_args_is_help=True,
)
app.add_typer(superset.app, name="superset")
