"""AWS command group."""

from __future__ import annotations

import typer

from dataplat.cli.cloud.aws import rds, redshift, secrets

app = typer.Typer(name="aws", help="AWS operations", no_args_is_help=True)
app.add_typer(secrets.app, name="secrets")
app.add_typer(rds.app, name="rds")
app.add_typer(redshift.app, name="redshift")
