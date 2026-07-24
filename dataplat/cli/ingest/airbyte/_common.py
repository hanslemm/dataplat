"""Shared plumbing for airbyte CLI commands."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import typer
from rich.console import Console

from dataplat.core.errors import AuthError, ConfigError, ServiceError
from dataplat.services.airbyte.client import build_authenticated_client

console = Console()


@contextmanager
def airbyte_client() -> Iterator[tuple[httpx.Client, str]]:
    """Authenticated client with unified error handling and cleanup.

    Auth/config problems and ServiceErrors raised inside the block are
    printed and converted to exit code 1; the client is always closed.
    """
    try:
        client, base_url = build_authenticated_client()
    except (ConfigError, AuthError) as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(code=1)
    try:
        yield client, base_url
    except ServiceError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(code=1)
    finally:
        client.close()
