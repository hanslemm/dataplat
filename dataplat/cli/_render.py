"""Markup-safe rendering of data dataplat did not author.

Rich interprets ``[...]`` in every ``str`` it renders, so warehouse rows, API
payloads, secret values, and driver error messages must never reach a Console
as plain strings:

- ``[/anything]`` raises :class:`rich.errors.MarkupError` mid-render, so an
  ordinary row of data kills the command with a traceback;
- a real style name such as ``[bold]`` is silently consumed, so the output
  misrepresents the value it claims to show.

Two helpers cover every case:

- :func:`cell` for table cells and whole values printed on their own. The
  returned :class:`~rich.text.Text` renders verbatim — no markup, and no repr
  highlighting either — while still inheriting the column's style.
- :func:`esc` for a value interpolated into one of *our* markup strings, as in
  ``console.print(f"[red]Error: {esc(exc)}[/red]")``.
"""

from __future__ import annotations

from rich.markup import escape
from rich.text import Text

__all__ = ["cell", "esc", "shorten"]

# Truncation marker; one character so it never widens a column.
ELLIPSIS = "…"


def esc(value: object) -> str:
    """Escape ``value`` for interpolation into a Rich markup string."""
    return escape("" if value is None else str(value))


def shorten(text: str, max_length: int) -> str:
    """Clip ``text`` to ``max_length`` characters, ellipsis included.

    ``max_length <= 0`` disables clipping.
    """
    if max_length <= 0 or len(text) <= max_length:
        return text
    if max_length == 1:
        return ELLIPSIS
    return text[: max_length - 1] + ELLIPSIS


def cell(value: object, *, style: str = "", max_length: int = 0) -> Text:
    """Render ``value`` as literal text, safe for any table or console.

    ``None`` becomes an empty cell. ``max_length`` clips long values the way
    :func:`shorten` does.
    """
    text = "" if value is None else str(value)
    return Text(shorten(text, max_length), style=style)
