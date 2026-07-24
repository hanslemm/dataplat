"""Stub apps and installer flow for areas whose extras are not installed.

When an area's optional dependencies are absent, ``main`` mounts a stub
group in its place so ``dp <area> ...`` explains what is missing, offers
to install the extra into dp's own environment, and re-runs the original
command on success.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any, NoReturn

import typer
from rich.console import Console
from rich.markup import escape
from typer.core import TyperGroup

from dataplat.core.deps import (
    AREAS,
    install_command,
    install_spec,
    manual_hint,
    missing_modules,
)

console = Console()


def run_install(extras: list[str], *, yes: bool) -> bool:
    """Install ``extras`` into dp's environment; True on success.

    Always shows the exact command first. Prompts unless ``yes``; in
    non-interactive sessions it prints the command and declines instead
    of installing silently.
    """
    cmd = install_command(extras)
    if cmd is None:
        console.print(f"[yellow]{escape(manual_hint(extras))}[/yellow]")
        return False
    console.print(f"[bold]Will run:[/bold] {escape(' '.join(cmd))}")
    if not yes:
        if not sys.stdin.isatty():
            console.print(
                "[yellow]Non-interactive session; run the command above "
                "yourself, or pass --yes.[/yellow]"
            )
            return False
        if not typer.confirm("Proceed?", default=True):
            return False
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        console.print(f"[red]Install failed (exit {proc.returncode}).[/red]")
        return False
    return True


def reexec() -> NoReturn:
    """Re-run the original dp invocation against the updated environment."""
    os.execvp(sys.argv[0], sys.argv)


def build_missing_deps_app(area: str, help_text: str) -> typer.Typer:
    """A stand-in Typer group for ``area`` while its extra is missing."""
    spec = AREAS[area]

    def handle() -> None:
        missing = missing_modules(area)
        console.print(
            f"[yellow]`dp {area}` needs {', '.join(missing) or spec.extra} — "
            f"the '{spec.extra}' extra "
            f"([bold]{escape(install_spec([spec.extra]))}[/bold]).[/yellow]"
        )
        if run_install([spec.extra], yes=False):
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
            handle()
            raise AssertionError("unreachable: handle() exits or re-execs")

    stub = typer.Typer(
        name=area,
        cls=MissingDepsGroup,
        help=f"{help_text} (needs extra: {spec.extra})",
        invoke_without_command=True,
        context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    )

    @stub.callback()
    def _missing_deps(ctx: typer.Context) -> None:
        if ctx.invoked_subcommand is None:
            handle()

    return stub
