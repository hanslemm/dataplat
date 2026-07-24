"""Shared Rich rendering helpers for the db command group's report style.

Used by ``describe``, ``role``, and ``top-tables`` so all printed reports
share one visual language: numbered sections with captions, a rounded title
card, HORIZONTALS tables, and consistent size/row formatting.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from rich import box as _box
from rich.align import Align
from rich.console import Console
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table

DASH = "[dim]—[/dim]"


def dim(text: str) -> str:
    return f"[dim]{text}[/dim]"


def green(text: str) -> str:
    return f"[green]{text}[/green]"


def fmt_size(n: int | None, *, colored: bool = True) -> str:
    """Human-friendly byte count; wrapped in [green] when colored."""
    if n is None:
        return DASH if colored else "—"
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024:
            text = f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            return green(text) if colored else text
        size /= 1024
    text = f"{size:.1f} PiB"
    return green(text) if colored else text


def fmt_size_plain(n: int | None) -> str:
    """Uncolored byte size — for use inside already-styled strings."""
    return fmt_size(n, colored=False)


def fmt_rows(n: int | None, *, colored: bool = True) -> str:
    if n is None or n < 0:
        return DASH if colored else "—"
    text = f"{n:,}"
    return green(text) if colored else text


def indent(renderable: Any, cols: int = 2) -> Padding:
    """Indent a renderable by the body gutter."""
    return Padding(renderable, (0, 0, 0, cols))


class SectionCounter:
    """Monotonically increasing section number, reset per report."""

    __slots__ = ("_n",)

    def __init__(self) -> None:
        self._n = 0

    def next(self) -> int:
        self._n += 1
        return self._n


def print_section_heading(
    console: Console, counter: SectionCounter, title: str, caption: str
) -> None:
    """Print `  N. Title` + caption below it, with the report spacing rules.

    Caller is responsible for printing a blank line between caption and body
    (the body itself may begin with a blank line if desired).
    """
    n = counter.next()
    if n > 1:
        # Two blank lines between end-of-section body and next heading.
        console.print()
        console.print()
    console.print(f"  [bold cyan]{n}.[/bold cyan] [bold]{title}[/bold]")
    console.print(f"    [dim italic]{caption}[/dim italic]")
    console.print()


def metadata_grid(pairs: Iterable[tuple[str, str]], *, two_column: bool) -> Table:
    """Label/value grid used inside the title card.

    Labels are right-aligned and dim; values default color.
    """
    grid = Table.grid(expand=False)
    items = [(k, v) for k, v in pairs if v]
    if not items:
        grid.add_column()
        return grid

    if two_column and len(items) > 1:
        # Two logical columns: [label, gap, value, wide gap, label, gap, value]
        grid.add_column(justify="right", style="dim", no_wrap=True)
        grid.add_column(width=2)
        grid.add_column(justify="left")
        grid.add_column(width=6)
        grid.add_column(justify="right", style="dim", no_wrap=True)
        grid.add_column(width=2)
        grid.add_column(justify="left")
        # Split items into left/right columns (roughly balanced).
        mid = (len(items) + 1) // 2
        left = items[:mid]
        right = items[mid:]
        for i in range(mid):
            lk, lv = left[i]
            if i < len(right):
                rk, rv = right[i]
                grid.add_row(lk, "", lv, "", rk, "", rv)
            else:
                grid.add_row(lk, "", lv, "", "", "", "")
    else:
        grid.add_column(justify="right", style="dim", no_wrap=True)
        grid.add_column(width=2)
        grid.add_column(justify="left")
        for k, v in items:
            grid.add_row(k, "", v)
    return grid


def title_card(
    console: Console,
    *,
    title: str,
    subtitle: str,
    metadata: list[tuple[str, str]],
) -> Align | Panel:
    """Build the cover card renderable."""
    width = console.size.width or 80
    two_column = width >= 100

    inner = Table.grid(expand=True)
    inner.add_column(justify="center")
    inner.add_row(f"[bold]{title}[/bold]")
    inner.add_row(f"[dim italic]{subtitle}[/dim italic]")
    if metadata:
        inner.add_row("")
        grid = metadata_grid(metadata, two_column=two_column)
        inner.add_row(Align.center(grid))

    panel_width: int | None = None
    if width >= 120:
        panel_width = 120
    panel = Panel(
        inner,
        box=_box.ROUNDED,
        border_style="cyan",
        padding=(1, 4),
        expand=(panel_width is None),
        width=panel_width,
    )
    if panel_width is not None:
        return Align.center(panel)
    return panel


def report_table(*, zebra: bool = False) -> Table:
    """Base Table configured with the report's paper feel."""
    row_styles = ["", "on grey11"] if zebra else None
    return Table(
        box=_box.HORIZONTALS,
        show_header=True,
        header_style="bold",
        padding=(0, 2),
        pad_edge=False,
        row_styles=row_styles,
        show_edge=False,
    )
