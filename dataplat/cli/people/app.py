"""People command area."""

from __future__ import annotations

import typer

from dataplat.cli.people.offboard import offboard
from dataplat.cli.people.onboard import onboard

app = typer.Typer(
    name="people",
    help="Manage a person's access across warehouses and BI",
    no_args_is_help=True,
)


@app.callback()
def people() -> None:
    """Manage a person's access across warehouses and BI.

    The callback is what keeps this a group: Typer collapses a single-command
    app into that command, so without it `dp people onboard ...` would read
    "onboard" as the email address.
    """


app.command("onboard")(onboard)
app.command("offboard")(offboard)
