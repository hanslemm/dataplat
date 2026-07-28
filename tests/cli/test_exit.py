"""The single catch-site helper: right message, right exit code, every time."""

from __future__ import annotations

import pytest
import typer
from rich.console import Console

from dataplat.cli._exit import exit_code_for, fail
from dataplat.core.errors import (
    AuthError,
    ConfigError,
    DataplatError,
    ExitCode,
    ServiceError,
    ValidationError,
)

TYPED: list[tuple[type[DataplatError], int]] = [
    (DataplatError, 1),
    (ValidationError, 2),
    (ConfigError, 3),
    (AuthError, 4),
    (ServiceError, 5),
]


@pytest.fixture
def console() -> Console:
    return Console(width=200, no_color=True, legacy_windows=False)


def _fail(exc: DataplatError, console: Console) -> tuple[int, str]:
    with console.capture() as capture, pytest.raises(typer.Exit) as excinfo:
        fail(exc, console=console)
    return excinfo.value.exit_code, capture.get()


@pytest.mark.parametrize(("error_class", "code"), TYPED)
def test_exits_with_the_exceptions_own_code(
    error_class: type[DataplatError], code: int, console: Console
) -> None:
    exit_code, out = _fail(error_class("boom"), console)
    assert exit_code == code
    assert "Error: boom" in out


def test_the_code_is_not_hardcoded(console: Console) -> None:
    """The defect this replaces: every failure exited 1, so nothing was told
    apart. Two different causes must produce two different codes."""
    assert (
        _fail(ValidationError("bad"), console)[0]
        != _fail(ServiceError("down"), console)[0]
    )


def test_message_is_escaped_not_interpreted(console: Console) -> None:
    """Exception text quotes warehouse rows and API bodies verbatim.

    A `[/x]` in one used to raise MarkupError mid-render, turning a handled
    error into a traceback -- so the message must survive as characters.
    """
    exit_code, out = _fail(ServiceError("relation [/x] and [bold]kept[/bold]"), console)
    assert exit_code == ExitCode.SERVICE
    assert "relation [/x] and [bold]kept[/bold]" in out


def test_prints_in_red() -> None:
    """The red is the signal a human reads before the words. Keep it.

    ``no_color=False`` overrides the suite's NO_COLOR, which exists so every
    other assertion can match plain substrings.
    """
    styled = Console(
        width=200, force_terminal=True, no_color=False, legacy_windows=False
    )
    with styled.capture() as capture, pytest.raises(typer.Exit):
        fail(ConfigError("no config"), console=styled)
    out = capture.get()
    assert "\x1b[31m" in out
    assert "no config" in out


def test_falls_back_to_the_module_console(capsys: pytest.CaptureFixture[str]) -> None:
    """Errors go to stdout, as every hand-rolled site in this tree already did.

    Pinned deliberately: `dataplat.core.trace` owns stderr, and moving error
    output there would silently change what a few dozen CliRunner assertions
    see.
    """
    with pytest.raises(typer.Exit) as excinfo:
        fail(AuthError("denied"))
    captured = capsys.readouterr()
    assert excinfo.value.exit_code == ExitCode.AUTH
    assert "Error: denied" in captured.out
    assert captured.err == ""


@pytest.mark.parametrize(("error_class", "code"), TYPED)
def test_exit_code_for_typed_error(error_class: type[DataplatError], code: int) -> None:
    assert exit_code_for(error_class("boom")) == code


def test_exit_code_for_untyped_error_is_failure() -> None:
    """Anything we did not classify is 1, never a plausible-looking 5."""
    assert exit_code_for(ValueError("not ours")) is ExitCode.FAILURE
    assert exit_code_for(KeyboardInterrupt()) is ExitCode.FAILURE


def test_exit_code_for_does_not_exit() -> None:
    """It exists for callers that want the number and their own control flow."""
    assert exit_code_for(ConfigError("x")) is ExitCode.CONFIG


def test_fail_always_raises(console: Console) -> None:
    """Annotated NoReturn, so a caller may write code after it and be right."""
    with pytest.raises(typer.Exit):
        fail(DataplatError("boom"), console=console)


def test_subclass_of_a_typed_error_inherits_its_code(console: Console) -> None:
    """An area-specific error gets the right code without touching the helper."""

    class AirbyteDown(ServiceError):
        pass

    exit_code, out = _fail(AirbyteDown("502 from the API"), console)
    assert exit_code == ExitCode.SERVICE
    assert "502 from the API" in out
