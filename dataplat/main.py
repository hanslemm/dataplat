"""Main CLI entry point for dataplat."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

import typer

from dataplat.core.envrc import load_envrc

# Load .envrc here, at module scope, and nowhere else: Typer evaluates an
# area's option defaults (profiles, regions, DNS) while that area module is
# being imported, and click resolves — and therefore imports — the subcommand
# *before* the root callback runs. So the environment has to be in place before
# any area import, which module scope is the only point that guarantees.
# Lazy mounting moves those imports later, never earlier, so it does not change
# this: loading from the callback would still be too late.
load_envrc()

from dataplat.cli._lazy import LazyRootGroup, area_placeholder  # noqa: E402
from dataplat.cli.config import app as config_app  # noqa: E402
from dataplat.cli.open import app as open_app  # noqa: E402
from dataplat.cli.status import app as status_app  # noqa: E402
from dataplat.core.registry import all_areas  # noqa: E402

app = typer.Typer(
    name="dp",
    cls=LazyRootGroup,
    help="dataplat — one command to manage any shape of data platform",
    no_args_is_help=True,
)


def _version_callback(value: bool) -> None:
    if not value:
        return
    try:
        pkg_version = version("dataplat")
    except PackageNotFoundError:
        pkg_version = "unknown"
    typer.echo(f"dp {pkg_version}")
    raise typer.Exit()


@app.callback()
def bootstrap(
    show_version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the CLI version and exit.",
    ),
) -> None:
    """Declare the root options; there is nothing left to do here.

    Environment loading used to be repeated in this body. It was already a
    no-op (loading is ``setdefault``-based) and it could never have been
    anything else: click has imported and parsed the subcommand by the time it
    calls this.
    """


app.add_typer(config_app, name="config")

# Areas mount as placeholders in registry order: the name and help text come
# from the registry, and LazyRootGroup imports the area (or its missing-deps
# stub) only when a command inside it is resolved.
for _mount in all_areas():
    app.add_typer(area_placeholder(_mount), name=_mount.name)

app.add_typer(status_app, name="status")
app.add_typer(open_app, name="open")


if __name__ == "__main__":
    app()
