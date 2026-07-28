"""Shared plumbing for airbyte CLI commands."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import httpx
from rich.console import Console

from dataplat.cli._exit import fail
from dataplat.core.errors import AuthError, ConfigError, ServiceError
from dataplat.services.airbyte.client import build_authenticated_client

console = Console()


@contextmanager
def airbyte_client() -> Iterator[tuple[httpx.Client, str]]:
    """Authenticated client with unified error handling and cleanup.

    Auth/config problems and ServiceErrors raised inside the block are printed
    and turned into the exit code the exception declares — 3, 4 and 5
    respectively, where all three used to be 1 — and the client is always
    closed. Being the funnel is the point: every command that opens it gets the
    contract without knowing it exists.
    """
    try:
        client, base_url = build_authenticated_client()
    except (ConfigError, AuthError) as exc:
        fail(exc, console=console)
    try:
        yield client, base_url
    except ServiceError as exc:
        # ServiceError carries the API's response body verbatim, so it can
        # contain anything a warehouse or connector chose to put there.
        fail(exc, console=console)
    finally:
        client.close()
