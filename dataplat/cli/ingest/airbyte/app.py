"""Airbyte top-level command group."""

from __future__ import annotations

import typer

from dataplat.cli.ingest.airbyte import (
    connections,
    definitions,
    destinations,
    jobs,
    sources,
    tags,
    templates,
    workspaces,
)

app = typer.Typer(
    name="airbyte",
    help="Manage Airbyte connections and configurations",
    no_args_is_help=True,
)

app.add_typer(connections.app, name="connections")
app.add_typer(sources.app, name="sources")
app.add_typer(destinations.app, name="destinations")
app.add_typer(definitions.app, name="definitions")
app.add_typer(workspaces.app, name="workspaces")
app.add_typer(tags.app, name="tags")
app.add_typer(templates.app, name="templates")
app.add_typer(jobs.app, name="jobs")
