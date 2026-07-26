"""The confirmation gate shared by every destructive command."""

from __future__ import annotations

import pytest
import typer
from rich.console import Console

from dataplat.cli import _prompt
from dataplat.cli._prompt import confirm_or_exit


class _Stdin:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.fixture
def console() -> Console:
    return Console(width=200, no_color=True, legacy_windows=False)


def _interactive(monkeypatch: pytest.MonkeyPatch, *, tty: bool) -> None:
    monkeypatch.setattr(_prompt.sys, "stdin", _Stdin(tty))


def test_yes_skips_every_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    _interactive(monkeypatch, tty=False)

    def _boom(*args: object, **kwargs: object) -> bool:
        raise AssertionError("must not prompt when yes=True")

    monkeypatch.setattr(typer, "confirm", _boom)
    confirm_or_exit("dropping everything", yes=True)


def test_accepted_confirmation_returns(
    monkeypatch: pytest.MonkeyPatch, console: Console
) -> None:
    _interactive(monkeypatch, tty=True)
    monkeypatch.setattr(typer, "confirm", lambda *a, **k: True)
    confirm_or_exit("summary", yes=False, console=console)


def test_declined_confirmation_exits_one(
    monkeypatch: pytest.MonkeyPatch, console: Console
) -> None:
    _interactive(monkeypatch, tty=True)
    monkeypatch.setattr(typer, "confirm", lambda *a, **k: False)
    with console.capture() as capture, pytest.raises(typer.Exit) as excinfo:
        confirm_or_exit("summary", yes=False, console=console)
    assert excinfo.value.exit_code == 1
    assert "Aborted." in capture.get()


def test_confirmation_defaults_to_no(
    monkeypatch: pytest.MonkeyPatch, console: Console
) -> None:
    seen: dict[str, object] = {}

    def _record(prompt: str, **kwargs: object) -> bool:
        seen.update(prompt=prompt, **kwargs)
        return True

    _interactive(monkeypatch, tty=True)
    monkeypatch.setattr(typer, "confirm", _record)
    confirm_or_exit("summary", yes=False, prompt="Drop it?", console=console)
    assert seen == {"prompt": "Drop it?", "default": False}


def test_default_yes_is_opt_in(
    monkeypatch: pytest.MonkeyPatch, console: Console
) -> None:
    """Reversible flows (dependency install) may make Enter mean yes."""
    seen: dict[str, object] = {}

    def _record(prompt: str, **kwargs: object) -> bool:
        seen.update(prompt=prompt, **kwargs)
        return True

    _interactive(monkeypatch, tty=True)
    monkeypatch.setattr(typer, "confirm", _record)
    confirm_or_exit("summary", yes=False, default=True, console=console)
    assert seen == {"prompt": "Proceed?", "default": True}


def test_default_yes_still_refuses_non_interactively(
    monkeypatch: pytest.MonkeyPatch, console: Console
) -> None:
    """A default of yes must never become "install silently" in a script."""
    _interactive(monkeypatch, tty=False)

    def _boom(*args: object, **kwargs: object) -> bool:
        raise AssertionError("must not prompt in a non-interactive session")

    monkeypatch.setattr(typer, "confirm", _boom)
    with pytest.raises(typer.Exit) as excinfo:
        confirm_or_exit("summary", yes=False, default=True, console=console)
    assert excinfo.value.exit_code == 1


def test_non_interactive_refuses_with_the_flag_that_works(
    monkeypatch: pytest.MonkeyPatch, console: Console
) -> None:
    _interactive(monkeypatch, tty=False)

    def _boom(*args: object, **kwargs: object) -> bool:
        raise AssertionError("must not block a non-interactive session")

    monkeypatch.setattr(typer, "confirm", _boom)
    with console.capture() as capture, pytest.raises(typer.Exit) as excinfo:
        confirm_or_exit("summary", yes=False, console=console)
    out = capture.get()
    assert excinfo.value.exit_code == 1
    assert "--yes" in out


def test_non_interactive_hint_is_customizable(
    monkeypatch: pytest.MonkeyPatch, console: Console
) -> None:
    _interactive(monkeypatch, tty=False)
    with console.capture() as capture, pytest.raises(typer.Exit):
        confirm_or_exit(
            "summary", yes=False, hint="Pass --write to run it.", console=console
        )
    assert "Pass --write to run it." in capture.get()


def test_summary_is_printed_before_prompting(
    monkeypatch: pytest.MonkeyPatch, console: Console
) -> None:
    _interactive(monkeypatch, tty=True)
    monkeypatch.setattr(typer, "confirm", lambda *a, **k: True)
    with console.capture() as capture:
        confirm_or_exit("DROP TABLE users", yes=False, console=console)
    assert "DROP TABLE users" in capture.get()


def test_empty_summary_prints_nothing(
    monkeypatch: pytest.MonkeyPatch, console: Console
) -> None:
    _interactive(monkeypatch, tty=True)
    monkeypatch.setattr(typer, "confirm", lambda *a, **k: True)
    with console.capture() as capture:
        confirm_or_exit(yes=False, console=console)
    assert capture.get() == ""
