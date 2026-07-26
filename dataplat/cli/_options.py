"""Option objects shared by every area.

``--yes`` and ``--json`` were declared four and three times respectively, each
copy free to drift in spelling, shorthand, or help text. Declaring them once
keeps the surface identical everywhere; the factories exist for the handful of
commands that need wording specific to what they emit.
"""

from __future__ import annotations

from typing import Any

import typer

__all__ = ["JsonOption", "YesOption", "json_option", "yes_option"]


def yes_option(help_text: str = "Skip the confirmation prompt.") -> Any:
    """The shared ``--yes/-y`` spelling for destructive commands."""
    return typer.Option(False, "--yes", "-y", help=help_text)


def json_option(help_text: str = "Emit JSON instead of tables.") -> Any:
    """The shared ``--json`` spelling for machine-readable output."""
    return typer.Option(False, "--json", help=help_text)


YesOption = yes_option()
JsonOption = json_option()
