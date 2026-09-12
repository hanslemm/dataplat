"""JSON output must survive the terminal it is printed to.

``--json`` and ``--format json`` exist to be piped. Rich, however, renders to a
console width and folds a token longer than that width *mid-token* — which puts
a newline inside a JSON string literal. The result looks plausible on screen and
is unparseable the moment it reaches ``jq``:

    "name":
    "https://example.com/very/long/path/that/exceeds/the/width
    ?token=abc"

Twelve Airbyte commands printed JSON through a Rich console this way, while the
rest of the CLI used ``typer.echo``. The behavioural proof lives in
``test_airbyte_commands.py``; this module pins the rule itself, because the next
command to be written is the one that would reintroduce it — and the failure is
invisible to any test that does not force a narrow width.
"""

from __future__ import annotations

import re
from pathlib import Path

CLI_ROOT = Path(__file__).resolve().parents[2] / "dataplat" / "cli"

# Matches the call across line breaks: ruff's formatter splits a long one.
RENDERED_JSON = re.compile(r"console\.print\(\s*(?:cell\(\s*)?json\.dumps")


def test_no_command_renders_json_through_a_console() -> None:
    offenders: list[str] = []
    for path in sorted(CLI_ROOT.rglob("*.py")):
        source = path.read_text()
        for match in RENDERED_JSON.finditer(source):
            line = source.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.relative_to(CLI_ROOT.parent.parent)}:{line}")

    assert offenders == [], (
        "JSON must be written with typer.echo, not printed through a Rich "
        "console, which wraps it at the console width: " + ", ".join(offenders)
    )
