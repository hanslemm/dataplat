"""``dp open`` — jump to the web UI for any system the CLI talks to."""

from __future__ import annotations

import os
import webbrowser
from urllib.parse import quote

import typer
from rich.console import Console

console = Console()

app = typer.Typer(
    name="open",
    help="Open a system's web UI in the browser",
    no_args_is_help=True,
)

def _console_region() -> str:
    return (
        os.getenv("DP_AWS_REGION")
        or os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
        or "us-east-1"
    )


def _strip_api_suffix(url: str) -> str:
    url = url.rstrip("/")
    for suffix in ("/api/public/v1", "/api/public", "/api/v1", "/api"):
        if url.endswith(suffix):
            return url[: -len(suffix)]
    return url


def _env_base_url(var: str) -> str:
    raw = os.getenv(var, "")
    if not raw:
        console.print(f"[red]Error: {var} is not set[/red]")
        raise typer.Exit(code=1)
    return _strip_api_suffix(raw)


def _launch(url: str, print_only: bool) -> None:
    console.print(url)
    if not print_only:
        webbrowser.open(url)


PrintOnlyOption = typer.Option(
    False, "--print-only", help="Print the URL without opening a browser."
)


@app.command("airbyte")
def open_airbyte(print_only: bool = PrintOnlyOption) -> None:
    """Open the Airbyte UI (from AIRBYTE_BASE_URL)."""
    _launch(_env_base_url("AIRBYTE_BASE_URL"), print_only)


@app.command("superset")
def open_superset(print_only: bool = PrintOnlyOption) -> None:
    """Open the Superset UI (from SUPERSET_BASE_URL)."""
    _launch(_env_base_url("SUPERSET_BASE_URL"), print_only)


@app.command("rds")
def open_rds(
    instance: str | None = typer.Argument(
        None, help="RDS instance identifier (defaults to DP_RDS_INSTANCE)."
    ),
    print_only: bool = PrintOnlyOption,
) -> None:
    """Open the AWS console page for an RDS instance."""
    name = instance or os.getenv("DP_RDS_INSTANCE")
    if not name:
        console.print(
            "[red]Error: no RDS instance given. Pass an identifier or set "
            "DP_RDS_INSTANCE.[/red]"
        )
        raise typer.Exit(code=1)
    url = (
        f"https://{_console_region()}.console.aws.amazon.com/rds/home"
        f"?region={_console_region()}#database:id={quote(name)};is-cluster=false"
    )
    _launch(url, print_only)


@app.command("redshift")
def open_redshift(print_only: bool = PrintOnlyOption) -> None:
    """Open the AWS console page for Redshift Serverless."""
    url = (
        f"https://{_console_region()}.console.aws.amazon.com/redshiftv2/home"
        f"?region={_console_region()}#serverless-dashboard"
    )
    _launch(url, print_only)


@app.command("secrets")
def open_secrets(
    name: str | None = typer.Argument(
        None, help="Secret name to open directly (otherwise the list view)."
    ),
    print_only: bool = PrintOnlyOption,
) -> None:
    """Open AWS Secrets Manager (optionally a specific secret)."""
    base = (
        f"https://{_console_region()}.console.aws.amazon.com/secretsmanager"
        f"/listsecrets?region={_console_region()}"
    )
    if name:
        base = (
            f"https://{_console_region()}.console.aws.amazon.com/secretsmanager"
            f"/secret?name={quote(name, safe='')}&region={_console_region()}"
        )
    _launch(base, print_only)
