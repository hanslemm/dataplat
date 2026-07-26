"""The markup-safety contract every renderer depends on."""

from __future__ import annotations

import pytest
from rich.console import Console
from rich.table import Table
from rich.text import Text

from dataplat.cli._render import cell, esc, shorten


def _render(renderable: object) -> str:
    console = Console(width=200, no_color=True, legacy_windows=False)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


def test_unbalanced_closing_tag_crashes_plain_strings() -> None:
    """The bug the helpers exist to prevent — proof the danger is real."""
    from rich.errors import MarkupError

    with pytest.raises(MarkupError):
        _render("closes [/issue] 42")


def test_cell_renders_unbalanced_tag_verbatim() -> None:
    assert "closes [/issue] 42" in _render(cell("closes [/issue] 42"))


def test_cell_keeps_style_names_visible() -> None:
    """A real style name must survive as characters, not be consumed."""
    assert "value [bold]x[/bold] end" in _render(cell("value [bold]x[/bold] end"))


def test_cell_none_is_empty() -> None:
    assert _render(cell(None)).strip() == ""


def test_cell_stringifies_non_strings() -> None:
    assert "42" in _render(cell(42))


def test_cell_clips_to_max_length() -> None:
    assert cell("abcdefghij", max_length=5).plain == "abcd…"


def test_cell_max_length_zero_keeps_everything() -> None:
    assert cell("abcdefghij", max_length=0).plain == "abcdefghij"


def test_cell_carries_style() -> None:
    assert cell("x", style="cyan").style == "cyan"


def test_cell_is_text() -> None:
    assert isinstance(cell("x"), Text)


def test_table_row_with_hostile_data_renders() -> None:
    table = Table()
    table.add_column("name", style="cyan")
    table.add_column("data")
    table.add_row(cell("plain"), cell("[/x] and [bold]kept[/bold]"))
    out = _render(table)
    assert "[/x]" in out
    assert "[bold]kept[/bold]" in out


def test_esc_neutralizes_tags_inside_our_markup() -> None:
    out = _render(f"[red]Error: {esc('relation [/x] missing')}[/red]")
    assert "relation [/x] missing" in out


def test_esc_none_is_empty_string() -> None:
    assert esc(None) == ""


def test_esc_stringifies_exceptions() -> None:
    assert esc(ValueError("[bold]boom")) == r"\[bold]boom"


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        ("abc", 0, "abc"),
        ("abc", -1, "abc"),
        ("abc", 3, "abc"),
        ("abcd", 3, "ab…"),
        ("abcd", 1, "…"),
    ],
)
def test_shorten(text: str, limit: int, expected: str) -> None:
    assert shorten(text, limit) == expected
