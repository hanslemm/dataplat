"""Mount areas by name; import one only when it is actually run.

``main`` used to call :func:`~dataplat.core.registry.load_app` for every area
at import time, so ``dp --version`` paid for psycopg, httpx and croniter
(~250 ms of a ~280 ms run) just to print one line. Here an area is mounted as
an empty placeholder group carrying nothing but the name and help text the
registry already knows, and :class:`LazyRootGroup` swaps in the real Typer app
— or the missing-deps stub — when click resolves that area, and only then.

Placeholders are mounted through ``add_typer`` rather than injected into the
click group, so command order, ``--help``, "did you mean" suggestions and
top-level completion keep coming out of Typer's own machinery instead of a
reimplementation of it, and nothing else in the CLI has to know that areas are
lazy. Those paths are all import-free (verified: ``dp --help`` and ``dp <TAB>``
leave psycopg unimported). Completing *inside* an area — ``dp db <TAB>`` — does
import it, because the subcommand list it has to offer is the area's own; that
is the one non-dispatch path that pays, and there is no way around it.

No direct click import here either — typer vendors click, so only the
``TyperGroup`` surface is safe to rely on (see :mod:`dataplat.cli._missing`).
"""

from __future__ import annotations

from typing import Any

import typer
from typer.core import TyperGroup
from typer.main import get_group

from dataplat.core.deps import ready
from dataplat.core.registry import AreaMount, area_by_name, load_app, mount_help

__all__ = [
    "AreaPlaceholderGroup",
    "LazyRootGroup",
    "area_command",
    "area_placeholder",
]


class AreaPlaceholderGroup(TyperGroup):
    """Stands in for an area nobody has imported yet.

    Deliberately empty: it is only ever asked to describe itself (name, help
    text, hidden), and :meth:`LazyRootGroup.resolve_command` replaces it before
    anything can be invoked through it.
    """


def area_placeholder(mount: AreaMount) -> typer.Typer:
    """A command-less Typer app holding just ``mount``'s name and help line."""
    return typer.Typer(
        name=mount.name,
        help=mount_help(mount),
        cls=AreaPlaceholderGroup,
    )


def area_command(mount: AreaMount) -> typer.Typer:
    """``mount``'s real Typer app, or the stub that offers to install its extra.

    Readiness is decided here rather than at mount time, so an area whose extra
    is missing costs nothing until someone runs it — and it is decided from
    ``mount.deps``, so an area that is not one of the built-ins still works.
    """
    if mount.deps is None or ready(mount.deps):
        return load_app(mount)
    # Imported on this branch only: the stub pulls in subprocess, which costs
    # more to import (~5 ms) than every readiness check in the CLI together.
    from dataplat.cli._missing import build_missing_deps_app

    return build_missing_deps_app(mount)


class LazyRootGroup(TyperGroup):
    """Root group that resolves an area's real app the first time it is used."""

    def resolve_command(self, ctx: Any, args: Any) -> Any:
        """Import the area behind a placeholder, now that it is needed.

        Resolution is what "the user is reaching into this area" looks like to
        click. ``--help``, typo suggestions and top-level completion never come
        through here — they read the placeholder and stay import-free. Shell
        completion *descending* into an area does come through here, with
        ``ctx.resilient_parsing`` set, and must still import: the completions it
        owes the shell are the area's own subcommands.
        """
        name, cmd, rest = super().resolve_command(ctx, args)
        if isinstance(cmd, AreaPlaceholderGroup) and name is not None:
            cmd = self._load_area(name)
        return name, cmd, rest

    def _load_area(self, name: str) -> Any:
        mount = area_by_name(name)
        # The placeholder exists because a mount was registered under this name.
        assert mount is not None, f"no registered area named {name!r}"
        # get_group, not get_command: the latter bolts the completion options
        # onto whatever it builds, which add_typer never did for an area.
        group = get_group(area_command(mount))
        # add_typer(name=...) used to name the mounted group; get_group takes
        # the name off the area's own Typer, so keep the mount authoritative —
        # a third-party area may well name its app something else.
        group.name = name
        # Cached in place: replacing the placeholder keeps command order intact
        # and means one process imports an area at most once.
        self.commands[name] = group
        return group
