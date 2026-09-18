"""dbt project command area."""

from __future__ import annotations

import typer

app = typer.Typer(
    name="dbt",
    help="Commands that operate on a named dbt project",
    no_args_is_help=True,
)


@app.callback()
def dbt() -> None:
    """Commands that operate on a named dbt project.

    The callback keeps this a group: Typer collapses a single-command app into
    that command, which would make `dp dbt orphans` unreachable once a second
    command lands.
    """


from dataplat.cli.dbt.orphans import app as orphans_app  # noqa: E402

app.add_typer(orphans_app, name="orphans")
