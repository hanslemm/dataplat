"""GitHub command group."""

from __future__ import annotations

import typer

from dataplat.cli.ci.github import runner

app = typer.Typer(name="github", help="GitHub operations", no_args_is_help=True)
app.add_typer(runner.app, name="runner")
