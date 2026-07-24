"""Shared helpers for the aws command group."""

from __future__ import annotations

import os
from typing import Any

import typer
from rich import box
from rich.console import Console
from rich.table import Table

from dataplat.core.errors import AuthError
from dataplat.services.aws.auth import get_session


def default_profile() -> str:
    """AWS profile used when --profile is omitted."""
    return os.getenv("DP_AWS_PROFILE") or "default"


def default_region() -> str | None:
    """AWS region used when --region is omitted (None → profile default)."""
    return (
        os.getenv("DP_AWS_REGION")
        or os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
    )


def cli_session(console: Console, profile: str | None, region: str | None):
    """Return a boto3 Session, converting auth failures into a clean exit."""
    try:
        return get_session(
            profile=profile or default_profile(),
            region=region or default_region(),
            notify=lambda msg: console.print(f"[yellow]{msg}[/yellow]"),
        )
    except AuthError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)


def make_table(title: str) -> Table:
    """The aws group's shared table style."""
    return Table(
        title=title,
        box=box.SIMPLE_HEAVY,
        header_style="bold bright_white",
        title_style="bold cyan",
        border_style="bright_black",
        row_styles=["", "on #2a2a2a"],
        show_lines=False,
        pad_edge=False,
    )


def profile_option(default: str | None = None) -> Any:
    return typer.Option(
        default,
        "--profile",
        "-p",
        help="AWS profile name. Defaults to DP_AWS_PROFILE or 'default'.",
    )


def region_option(default: str | None = None) -> Any:
    return typer.Option(
        default,
        "--region",
        "-r",
        help="AWS region. Defaults to DP_AWS_REGION/AWS_REGION or the "
        "profile's region.",
    )


JsonOption = typer.Option(False, "--json", help="Emit JSON instead of a table.")
