"""Stub apps and installer flow for areas whose extras are not installed.

When an area's optional dependencies are absent, the root group mounts a stub
in its place so ``dp <area> ...`` explains what is missing, offers to install
the extra into dp's own environment, and re-runs the original command on
success.

Everything comes off the :class:`~dataplat.core.registry.AreaMount` the CLI is
mounting, never out of the ``AREAS`` global: an area supplied by a third party
carries its own contract and is not in that dict.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from typing import Any, NoReturn

import typer
from rich.console import Console
from typer.core import TyperGroup

from dataplat.cli._prompt import DEFAULT_HINT, confirm_or_exit
from dataplat.cli._render import esc
from dataplat.core.deps import (
    install_command,
    install_spec,
    manual_hint,
    missing_for,
)
from dataplat.core.registry import AreaMount, missing_extra_help

console = Console()


def run_install(extras: list[str], *, yes: bool, hint: str = DEFAULT_HINT) -> bool:
    """Install ``extras`` into dp's environment; True on success.

    Always shows the exact command first, then gates it: ``hint`` names the
    escape hatch for the caller's own flag, since not every caller has a
    ``--yes`` to offer. Declining or a non-interactive session exits through
    :func:`confirm_or_exit`; the ``False`` return is reserved for "there was
    nothing we could run" and "the install itself failed", so a caller cannot
    read a refusal as a failure.
    """
    cmd = install_command(extras)
    if cmd is None:
        console.print(f"[yellow]{esc(manual_hint(extras))}[/yellow]")
        return False
    # shlex.join, not " ".join: the spec contains brackets (and now an ==pin),
    # which a shell would glob. The point of showing the command is that the
    # user can run it themselves, so it has to be paste-able as printed.
    console.print(f"[bold]Will run:[/bold] {esc(shlex.join(cmd))}")
    # Enter means yes here, unlike every destructive gate: the user just ran a
    # command that needs this extra, and an install is reversible.
    confirm_or_exit(yes=yes, hint=hint, default=True, console=console)
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        console.print(f"[red]Install failed (exit {proc.returncode}).[/red]")
        return False
    return True


def reexec() -> NoReturn:
    """Re-run the original dp invocation against the updated environment."""
    os.execvp(sys.argv[0], sys.argv)


def build_missing_deps_app(mount: AreaMount) -> typer.Typer:
    """A stand-in Typer group for ``mount`` while its extra is missing."""
    spec = mount.deps
    # Only an area with a dependency contract can be missing one.
    assert spec is not None, f"area {mount.name!r} declares no dependencies"

    def handle() -> None:
        # Area name, module names and extra all come from a contract a third
        # party can supply, so they are escaped rather than trusted as markup.
        missing = ", ".join(missing_for(spec)) or spec.extra
        console.print(
            f"[yellow]`dp {esc(mount.name)}` needs {esc(missing)} — "
            f"the '{esc(spec.extra)}' extra "
            f"([bold]{esc(install_spec([spec.extra]))}[/bold]).[/yellow]"
        )
        # No --yes reaches this stub: every subcommand lands in handle(), so
        # running the printed command by hand is the only other way through.
        if run_install(
            [spec.extra],
            yes=False,
            hint="Run the command above yourself to install it.",
        ):
            console.print("[green]Installed. Re-running your command…[/green]")
            reexec()
        raise typer.Exit(code=1)

    class MissingDepsGroup(TyperGroup):
        """Subcommands resolve before the group callback runs, so a plain
        empty group would die with "No such command" instead of explaining
        the missing extra. Intercept resolution and run the handler for any
        subcommand. (No direct click import: typer >= 0.27 vendors click,
        so we only rely on the TyperGroup surface.)"""

        def list_commands(self, ctx: Any) -> list[str]:
            return []

        def resolve_command(self, ctx: Any, args: Any) -> Any:
            # Shell completion walks the command tree with resilient_parsing
            # set, and click's walker calls resolve_command as it descends.
            # Running handle() there wrote the install offer onto the stream the
            # shell evaluates — on zsh that text (backticks included) was
            # eval'd, and on bash the install itself executed from a keypress.
            # click stops descending when resolve_command reports no command,
            # which is the honest answer while the area is a stub.
            if ctx.resilient_parsing:
                return None, None, []
            handle()
            raise AssertionError("unreachable: handle() exits or re-execs")

    stub = typer.Typer(
        name=mount.name,
        cls=MissingDepsGroup,
        help=missing_extra_help(mount.help_text, spec),
        invoke_without_command=True,
        context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    )

    @stub.callback()
    def _missing_deps(ctx: typer.Context) -> None:
        if ctx.invoked_subcommand is None:
            handle()

    return stub
