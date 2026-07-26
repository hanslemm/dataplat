"""Markup safety for ``dp ingest airbyte connections list --tui``.

Nothing else in the suite touches ``tui.py``: textual is imported lazily so a
plain ``dp`` run never pays for it, which also means every regression in this
module is invisible to the rest of the tests. So it is driven end to end here
through textual's own headless pilot, letting ``on_mount``, ``on_input_changed``
and the export binding run exactly as they do for a user.

``asyncio.run`` instead of an async test function: ``run_test()`` needs a loop,
not a fixture, and the suite deliberately carries no async pytest plugin.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

from textual.content import Content
from textual.widgets import DataTable, Input

from dataplat.cli.ingest.airbyte.tui import ConnectionsApp

# The two shapes of hostile value, as in test_airbyte_commands.py: an unbalanced
# closing tag aborts a markup parse outright, while a real style name is
# silently consumed so the table lies about the data it claims to show.
CLOSING_TAG = "closes [/issue] 42"
STYLE_TAG = "[bold]not-styled[/bold]"


def _drive[T](app: ConnectionsApp, probe: Callable[..., Awaitable[T]]) -> T:
    """Mount ``app`` headlessly and return what ``probe(pilot)`` observed.

    Exceptions raised inside textual's message pump surface when the pilot
    context exits, so a crash during ``on_mount`` fails the calling test.
    """

    async def run() -> T:
        async with app.run_test() as pilot:
            return await probe(pilot)

    return asyncio.run(run())


def _labels(table: DataTable) -> list[str]:
    """Header labels after textual has finished interpreting them."""
    return [str(column.label) for column in table.columns.values()]


def _rows(table: DataTable) -> list[list[str]]:
    """Cell values as the table holds them, in display order."""
    return [
        [str(value) for value in table.get_row_at(index)]
        for index in range(table.row_count)
    ]


def test_column_labels_render_markup_literally() -> None:
    """``--all-columns`` hands raw Airbyte JSON keys in as column labels.

    ``DataTable.add_column`` runs ``Text.from_markup`` over any ``str`` label,
    so before the fix a key holding ``[/issue]`` aborted the mount with
    MarkupError and one holding ``[bold]`` lost it.
    """
    app = ConnectionsApp([CLOSING_TAG, STYLE_TAG], [["v1", "v2"]], "unused.json")

    async def probe(pilot):
        return _labels(pilot.app.query_one(DataTable))

    assert _drive(app, probe) == [CLOSING_TAG, STYLE_TAG]


def test_row_cells_render_markup_literally() -> None:
    """Connection names and cron strings reach the table verbatim."""
    app = ConnectionsApp(
        ["Name", "Schedule"], [[CLOSING_TAG, STYLE_TAG]], "unused.json"
    )

    async def probe(pilot):
        return _rows(pilot.app.query_one(DataTable))

    assert _drive(app, probe) == [[CLOSING_TAG, STYLE_TAG]]


def test_search_rerenders_surviving_rows_literally() -> None:
    """Filtering re-runs the row render, so the hostile values pass through twice."""
    app = ConnectionsApp(
        ["Name", "Schedule"],
        [[CLOSING_TAG, "0 0 * * *"], ["quiet-conn", STYLE_TAG]],
        "unused.json",
    )

    async def probe(pilot):
        pilot.app.query_one("#search", Input).value = "closes"
        await pilot.pause()
        return _rows(pilot.app.query_one(DataTable))

    assert _drive(app, probe) == [[CLOSING_TAG, "0 0 * * *"]]


def test_export_keeps_markup_bytes_and_escapes_the_path(monkeypatch, tmp_path) -> None:
    """Export writes JSON verbatim; the toast naming the path is markup.

    A closing tag cannot occur in a real filename (it needs a ``/``), so the
    reachable hazard for the path is the silently-swallowed style tag.
    """
    export_path = tmp_path / "[bold]connections.json"
    notified: list[str] = []
    monkeypatch.setattr(
        ConnectionsApp,
        "notify",
        lambda self, message, **kwargs: notified.append(message),
    )
    app = ConnectionsApp(["Name"], [[CLOSING_TAG], [STYLE_TAG]], str(export_path))

    async def probe(pilot):
        await pilot.press("e")
        await pilot.pause()

    _drive(app, probe)

    assert json.loads(export_path.read_text(encoding="utf-8")) == [
        {"Name": CLOSING_TAG},
        {"Name": STYLE_TAG},
    ]
    # Parsed the way textual parses a toast body: the path must come back whole.
    assert Content.from_markup(notified[0]).plain == (
        f"Exported 2 rows to {export_path}"
    )
