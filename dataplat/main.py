"""Main CLI entry point for dataplat."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

import typer

from dataplat.core.envrc import load_envrc

# Load .envrc before the command modules import: option defaults (profiles,
# regions, DNS) read the environment at import time.
load_envrc()

from dataplat.cli._missing import build_missing_deps_app  # noqa: E402
from dataplat.cli.config import app as config_app  # noqa: E402
from dataplat.cli.open import app as open_app  # noqa: E402
from dataplat.cli.status import app as status_app  # noqa: E402
from dataplat.core.deps import area_ready  # noqa: E402
from dataplat.core.registry import all_areas, load_app  # noqa: E402

app = typer.Typer(
    name="dp",
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
    """Load environment variables once at CLI startup."""
    load_envrc()


app.add_typer(config_app, name="config")

# Areas mount through the registry: for real when their dependencies are
# installed, otherwise as a stub that offers to install the missing extra.
for _mount in all_areas():
    if _mount.deps is None or area_ready(_mount.name):
        app.add_typer(load_app(_mount), name=_mount.name)
    else:
        app.add_typer(
            build_missing_deps_app(_mount.name, _mount.help_text),
            name=_mount.name,
        )

app.add_typer(status_app, name="status")
app.add_typer(open_app, name="open")


if __name__ == "__main__":
    app()
