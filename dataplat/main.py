"""Main CLI entry point for dataplat."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

import typer

from dataplat.core.envrc import load_envrc

# Load .envrc before the command modules import: option defaults (profiles,
# regions, DNS) read the environment at import time.
load_envrc()

from dataplat.cli._missing import build_missing_deps_app  # noqa: E402
from dataplat.cli.ci.app import app as ci_app  # noqa: E402
from dataplat.cli.config import app as config_app  # noqa: E402
from dataplat.cli.open import app as open_app  # noqa: E402
from dataplat.cli.status import app as status_app  # noqa: E402
from dataplat.core.deps import area_ready  # noqa: E402

# Areas with optional dependencies mount for real only when their extra is
# installed; otherwise a stub group explains and offers to install it.
if area_ready("db"):
    from dataplat.cli.db import app as db_app
else:
    db_app = build_missing_deps_app("db", "Database query commands")

if area_ready("ingest"):
    from dataplat.cli.ingest.app import app as ingest_app
else:
    ingest_app = build_missing_deps_app("ingest", "Data ingestion tools (Airbyte)")

if area_ready("bi"):
    from dataplat.cli.bi.app import app as bi_app
else:
    bi_app = build_missing_deps_app("bi", "Business-intelligence tools (Superset)")

if area_ready("cloud"):
    from dataplat.cli.cloud.app import app as cloud_app
else:
    cloud_app = build_missing_deps_app("cloud", "Cloud-provider tools (AWS)")

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
app.add_typer(db_app, name="db")
app.add_typer(ingest_app, name="ingest")
app.add_typer(bi_app, name="bi")
app.add_typer(cloud_app, name="cloud")
app.add_typer(ci_app, name="ci")
app.add_typer(status_app, name="status")
app.add_typer(open_app, name="open")


if __name__ == "__main__":
    app()
