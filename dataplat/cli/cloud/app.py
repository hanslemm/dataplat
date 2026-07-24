"""Cloud command area."""

from __future__ import annotations

import typer

from dataplat.cli.cloud.aws.app import app as aws_app

app = typer.Typer(
    name="cloud",
    help="Cloud-provider tools (AWS)",
    no_args_is_help=True,
)
app.add_typer(aws_app, name="aws")
