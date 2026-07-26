"""One confirmation gate for every destructive command.

Three idioms used to coexist — a bare ``typer.confirm(abort=True)``, a
local ``_confirm_or_abort``, and a hand-rolled non-interactive branch — and
only the last one told the user how to proceed when stdin was not a TTY.
:func:`confirm_or_exit` is that behavior, once:

- ``yes`` short-circuits, so ``--yes/-y`` stays the scriptable path;
- a non-interactive session never blocks and never guesses: it prints the
  flag that would have worked and exits 1;
- declining exits 1 as well, so a caller can never mistake "no" for "go".

Callers own the ``summary``: it is rendered as markup, so any value coming
from the user or a service must be passed through
:func:`dataplat.cli._render.esc` first.
"""

from __future__ import annotations

import sys

import typer
from rich.console import Console

_console = Console()

DEFAULT_HINT = "Pass --yes/-y to proceed non-interactively."


def confirm_or_exit(
    summary: str = "",
    *,
    yes: bool,
    prompt: str = "Proceed?",
    hint: str = DEFAULT_HINT,
    default: bool = False,
    console: Console | None = None,
) -> None:
    """Require an explicit go-ahead before a destructive action.

    Returns only when the action is authorized — by ``yes`` or by an
    interactive confirmation. Every other path raises ``typer.Exit(1)``.

    ``default`` is what a bare Enter means. It stays ``False`` for anything
    that destroys data, so a stray keypress can never drop a table; pass
    ``True`` only where proceeding is the obviously wanted outcome and is
    reversible — installing a dependency the user's own command needs, say.
    """
    if yes:
        return

    out = console or _console
    if summary:
        out.print(summary)

    if not sys.stdin.isatty():
        out.print(
            f"[red]Error: refusing to continue without confirmation. {hint}[/red]"
        )
        raise typer.Exit(code=1)

    if typer.confirm(prompt, default=default):
        return

    out.print("[yellow]Aborted.[/yellow]")
    raise typer.Exit(code=1)
