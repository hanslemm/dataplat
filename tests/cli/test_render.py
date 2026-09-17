"""The markup-safety contract every renderer depends on."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from rich.console import Console
from rich.table import Table
from rich.text import Text

from dataplat.cli._render import cell, esc, shorten

DATAPLAT_ROOT = Path(__file__).resolve().parents[2] / "dataplat"

# A lint, not a parser: matches an f-string literal (any quote style, with or
# without an `r` prefix) that reaches `cell(` without a quote character in
# between. Every real call site so far has kept `cell(` on the line where the
# f-string opens, which is what `grep -rn 'f"[^"]*{cell(' dataplat` finds too.
_FSTRING_WRAPPING_CELL = re.compile(r"[rR]?[fF][rR]?[\"'][^\"']*\{cell\(")


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


def test_esc_survives_a_bracketed_value_that_looks_like_a_style_name() -> None:
    """The silent-swallow mode: cell() in an f-string drops `[bold]` clean out.

    ``str(cell("db_[bold]prod"))`` returns the plain text, which Rich then
    markup-parses -- consuming ``[bold]`` as a style instead of showing it.
    esc() must keep it as literal characters instead.
    """
    out = _render(f"user {esc('db_[bold]prod')}")
    assert "user db_[bold]prod" in out


def test_esc_does_not_crash_inside_our_own_markup() -> None:
    """The crash mode: a value shaped like a closing tag, inside our markup.

    ``cell("schema_[/dim]_x")`` in an f-string hands Rich a plain, unescaped
    ``[/dim]`` that doesn't match the real ``[dim]`` it's nested in, raising
    MarkupError. esc() must render it, not raise.
    """
    out = _render(f"[dim]{esc('schema_[/dim]_x')}[/dim]")
    assert "schema_[/dim]_x" in out


def test_no_call_site_wraps_cell_in_an_f_string() -> None:
    """cell() returns a Text whose protection dies inside an f-string.

    str(Text) hands back the plain, unescaped content, which Rich then
    markup-parses -- silently swallowing bracketed text, or raising
    MarkupError on anything shaped like a closing tag. esc() is the helper
    for interpolation; cell() is for values handed to a renderable.
    """
    offenders: list[str] = []
    for path in sorted(DATAPLAT_ROOT.rglob("*.py")):
        source = path.read_text()
        for match in _FSTRING_WRAPPING_CELL.finditer(source):
            line = source.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.relative_to(DATAPLAT_ROOT.parent)}:{line}")

    assert offenders == [], (
        "cell() loses its markup protection inside an f-string -- use esc() "
        "for interpolation instead: " + ", ".join(offenders)
    )


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
