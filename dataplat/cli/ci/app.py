"""CI command area."""

from __future__ import annotations

import typer

from dataplat.cli.ci.github.app import app as github_app

app = typer.Typer(
    name="ci",
    help="CI tools (GitHub Actions runners)",
    no_args_is_help=True,
)
app.add_typer(github_app, name="github")
