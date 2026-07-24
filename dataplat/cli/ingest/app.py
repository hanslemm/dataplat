"""Ingestion command area."""

from __future__ import annotations

import typer

from dataplat.cli.ingest import airbyte

app = typer.Typer(
    name="ingest",
    help="Data ingestion tools (Airbyte)",
    no_args_is_help=True,
)
app.add_typer(airbyte.app, name="airbyte")
