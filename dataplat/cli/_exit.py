"""The one way a command reports a typed error and stops.

Every catch site in the tree had grown the same two lines — print red with an
``Error: `` prefix, then ``raise typer.Exit(code=1)`` — and the code was the
part that was wrong: a missing config file, a rejected password and an
unreachable warehouse all exited 1, so a script could tell that something broke
and nothing else. :func:`fail` keeps the printing byte-for-byte and takes the
number from the exception, which is the only thing that knows what happened.

It does not branch on the error type, deliberately. A ``type -> code`` chain
here would be a second source of truth that a new error class could be added
without, and the first symptom would be a wrong exit code in someone's CI. The
class attribute in :mod:`dataplat.core.errors` is the whole mechanism; this
function is the plumbing.

Two details that are load-bearing:

- the message goes through :func:`dataplat.cli._render.esc`, because exception
  text quotes warehouse rows and API response bodies verbatim, and a stray
  ``[/x]`` in either one raises ``MarkupError`` mid-render — turning a handled
  error into a traceback;
- output goes to *stdout*, where this codebase's errors have always gone. That
  is parity, not a claim that it is right: moving diagnostics to stderr is what
  :mod:`dataplat.core.trace` does for tracing, and doing the same for errors
  would change what every existing CliRunner assertion sees. If it happens, it
  happens here, once, on purpose.
"""

from __future__ import annotations

from typing import NoReturn

import typer
from rich.console import Console

from dataplat.cli._render import esc
from dataplat.core.errors import DataplatError, ExitCode

__all__ = ["exit_code_for", "fail"]

_console = Console()


def exit_code_for(exc: BaseException) -> ExitCode:
    """Return the exit code ``exc`` should produce, without exiting.

    A :class:`~dataplat.core.errors.DataplatError` answers for itself; anything
    else is unclassified and therefore :attr:`~dataplat.core.errors.ExitCode.
    FAILURE`. Split out from :func:`fail` for the callers that need the number
    but not the exit — logging it, recording it in a summary table, or exiting
    through their own path (a TUI cannot raise ``typer.Exit`` at an arbitrary
    point and expect it to mean anything).
    """
    if isinstance(exc, DataplatError):
        return exc.exit_code
    return ExitCode.FAILURE


def fail(exc: DataplatError, *, console: Console | None = None) -> NoReturn:
    """Print ``exc`` as an error and exit with the code it declares.

    Pass ``console`` where the command already owns one, so the error lands in
    the same stream the rest of its output did.
    """
    (console or _console).print(f"[red]Error: {esc(exc)}[/red]")
    # exit_code_for rather than exc.exit_code: identical for every typed error,
    # and total for the untyped one a caller slips past the annotation.
    raise typer.Exit(code=exit_code_for(exc))
