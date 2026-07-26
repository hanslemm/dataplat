"""Shared helpers for the aws command group."""

from __future__ import annotations

import os
from typing import Any

import typer
from rich import box
from rich.console import Console
from rich.table import Table

from dataplat.cli._render import esc
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


# ── profile aliases ─────────────────────────────────────────────────────────
def profile_aliases() -> dict[str, str]:
    """Short profile aliases from ``DP_AWS_PROFILE_ALIASES``.

    Format: ``alias=ProfileName,alias2=OtherProfile`` — e.g.
    ``prod=AdminAccess-Prod,qa=AdminAccess-QA``.
    """
    aliases: dict[str, str] = {}
    for chunk in os.getenv("DP_AWS_PROFILE_ALIASES", "").split(","):
        alias, sep, full = chunk.partition("=")
        if sep and alias.strip() and full.strip():
            aliases[alias.strip()] = full.strip()
    return aliases


def resolve_profile(name: str) -> str:
    """Resolve a short alias to the full AWS profile name."""
    return profile_aliases().get(name, name)


def resolve_profiles(profiles: list[str]) -> list[str]:
    """Resolve a list of profile names/aliases, expanding the special 'all' keyword."""
    resolved: list[str] = []
    for p in profiles:
        if p == "all":
            resolved.extend(profile_aliases().values())
        else:
            resolved.append(resolve_profile(p))
    # deduplicate while preserving order
    seen: set[str] = set()
    return [p for p in resolved if not (p in seen or seen.add(p))]  # type: ignore[func-returns-value]


def effective_profile(profile: str | None) -> str:
    """The profile a command really uses: the flag, alias-resolved, or the default.

    Every aws command routes ``--profile`` through here, so ``-p prod`` means the
    same thing for rds, redshift and secrets alike — which is exactly what
    :func:`profile_option`'s help text promises. ``DP_AWS_PROFILE`` is used
    verbatim: it is the configured default profile, not a shorthand someone typed
    on the command line.
    """
    return resolve_profile(profile) if profile else default_profile()


def cli_session(console: Console, profile: str | None, region: str | None):
    """Return a boto3 Session, converting auth failures into a clean exit.

    ``profile`` may be a ``DP_AWS_PROFILE_ALIASES`` alias; it is resolved here so
    every caller of this helper honours the aliases.
    """
    try:
        return get_session(
            profile=effective_profile(profile),
            region=region or default_region(),
            notify=lambda msg: console.print(f"[yellow]{esc(msg)}[/yellow]"),
        )
    except AuthError as exc:
        console.print(f"[red]{esc(exc)}[/red]")
        raise typer.Exit(code=1)


def make_table(title: str) -> Table:
    """The aws group's shared table style.

    ``title`` is rendered as markup, so callers must ``esc()`` any part of it
    that came from a flag or an API. No hardcoded row background: a fixed hex
    is legible in exactly one of light/dark terminals, and SIMPLE_HEAVY's
    header rule already separates the rows.
    """
    return Table(
        title=title,
        box=box.SIMPLE_HEAVY,
        header_style="bold bright_white",
        title_style="bold cyan",
        border_style="bright_black",
        show_lines=False,
        pad_edge=False,
    )


def profile_option(default: str | None = None) -> Any:
    """The single ``--profile`` spelling for the aws group."""
    return typer.Option(
        default,
        "--profile",
        "-p",
        help="AWS profile name or alias (see DP_AWS_PROFILE_ALIASES). "
        "Defaults to DP_AWS_PROFILE or 'default'.",
    )


def region_option(default: str | None = None) -> Any:
    """The single ``--region`` spelling for the aws group."""
    return typer.Option(
        default,
        "--region",
        "-r",
        help="AWS region. Defaults to DP_AWS_REGION/AWS_REGION or the "
        "profile's region.",
    )
