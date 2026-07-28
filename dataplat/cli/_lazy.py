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

Plugin areas are mounted lazily too, one level up: ``main`` mounts only the
built-ins (their names and help text are constants), and :class:`LazyRootGroup`
adds a placeholder per plugin area the first time click asks for the command
surface. That keeps the entry-point scan off ``dp --version``, which never looks
at the command list, and off ``dp db …``, whose name a plugin cannot claim.

No direct click import here either — typer vendors click, so only the
``TyperGroup`` surface is safe to rely on (see :mod:`dataplat.cli._missing`).
"""

from __future__ import annotations

from typing import Any

import typer
from typer.core import TyperGroup
from typer.main import get_group

from dataplat.core.deps import ready
from dataplat.core.errors import ExitCode
from dataplat.core.registry import (
    AreaMount,
    area_by_name,
    is_builtin,
    load_app,
    mount_help,
    plugin_areas,
    warn_plugin,
    warn_plugin_failed,
)

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


def _load_or_diagnose(mount: AreaMount) -> Any:
    """:func:`load_app`, but a third-party area's import failure is news.

    A built-in that will not import is a bug in dp, and the traceback is the bug
    report — swallowing it would hide it behind a sentence. A plugin that raises
    on import is a fact about the user's environment, and the useful output is
    which area, from which target, failed how. Either way the blast radius is one
    area: laziness means nothing else in the CLI has imported it, so ``dp
    --help`` and every other area keep working.

    ``Exception``, not ``BaseException``: a plugin that calls ``sys.exit()`` or is
    interrupted mid-import is asking to stop the process, not to be diagnosed.
    """
    if is_builtin(mount):
        # Unguarded on purpose — see above.
        return load_app(mount)
    try:
        return load_app(mount)
    except Exception as exc:
        warn_plugin_failed(mount, exc)
        # Unclassified failure: a broken third-party package is not invalid
        # input, not our configuration, and not a service that answered badly.
        raise typer.Exit(code=ExitCode.FAILURE)


def area_command(mount: AreaMount) -> typer.Typer:
    """``mount``'s real Typer app, or the stub that offers to install its extra.

    Readiness is decided here rather than at mount time, so an area whose extra
    is missing costs nothing until someone runs it — and it is decided from
    ``mount.deps``, so an area that is not one of the built-ins still works.
    """
    if mount.deps is None or ready(mount.deps):
        return _load_or_diagnose(mount)
    # Imported on this branch only: the stub pulls in subprocess, which costs
    # more to import (~5 ms) than every readiness check in the CLI together.
    from dataplat.cli._missing import build_missing_deps_app

    return build_missing_deps_app(mount)


class LazyRootGroup(TyperGroup):
    """Root group that resolves an area's real app the first time it is used.

    It also owns *when* plugin areas appear: ``main`` mounts the built-ins, and
    the two overrides below add the plugin placeholders the moment click needs a
    command surface that could contain one.
    """

    # Class-level default, set per instance on first use: an instance attribute
    # avoids overriding TyperGroup.__init__ just to hold one bool.
    _plugins_mounted: bool = False

    def _mount_plugins(self) -> None:
        """Mount a placeholder for every plugin area, once per group.

        The flag is set *before* discovery, not after: a scan that warned about a
        broken plugin must not repeat itself on the next lookup, and several
        lookups happen in one invocation.
        """
        if self._plugins_mounted:
            return
        self._plugins_mounted = True
        for mount in plugin_areas():
            if mount.name in self.commands:
                # The registry already refuses a built-in *area*'s name; what is
                # left is the rest of the root surface (config, status, open),
                # which only the group knows about. Refused for the same reason:
                # `dp status` must keep meaning `dp status`.
                warn_plugin(
                    f"ignoring plugin area {mount.name!r}: "
                    f"dp {mount.name} is already a command"
                )
                continue
            # Same construction as a built-in placeholder, so plugin areas are
            # indistinguishable downstream — including in --help, "did you mean"
            # and completion, which read the group's commands and nothing else.
            self.commands[mount.name] = get_group(area_placeholder(mount))

    def list_commands(self, ctx: Any) -> list[str]:
        """Every command name, plugin areas included.

        --help, "did you mean" and top-level completion all come through here, so
        this is where the scan is genuinely owed: the answer is a list of names,
        and a plugin area's name belongs in it.
        """
        self._mount_plugins()
        return super().list_commands(ctx)

    def get_command(self, ctx: Any, name: str) -> Any:
        """Look up one command, discovering plugins only if nothing claims it.

        A name the built-ins already answer is returned without a scan, and that
        is sound rather than a shortcut: a plugin cannot claim a built-in area's
        name (the registry refuses it) nor an existing root command's (above), so
        discovery could not change this answer. It is what keeps ``dp db query``
        as cheap as it was before plugins existed.
        """
        command = super().get_command(ctx, name)
        if command is not None:
            return command
        self._mount_plugins()
        return super().get_command(ctx, name)

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
