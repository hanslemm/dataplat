"""Textual TUI for browsing airbyte connections (``connections list --tui``).

``textual`` costs ~58 ms to import and is only needed once the TUI actually
opens, so this module is imported lazily by its caller — importing it here at
class-definition time is unavoidable (``ConnectionsApp`` subclasses ``App``),
which is precisely why nothing may import this module at its own module scope.
"""

from __future__ import annotations

import json

from textual.app import App as TextualApp

from dataplat.cli._render import cell, esc


class ConnectionsApp(TextualApp):
    CSS = """
    Screen { layout: vertical; }
    #search { dock: top; }
    DataTable { height: 1fr; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("/", "focus_search", "Search"),
        ("escape", "focus_list", "List"),
        ("tab", "focus_list", "List"),
        ("c", "copy_row", "Copy row"),
        ("e", "export_rows", "Export"),
    ]

    def __init__(
        self,
        columns: list[str],
        rows_data: list[list[str]],
        export_path: str,
    ):
        super().__init__()
        self.columns = columns
        self.all_rows = rows_data
        self.filtered_rows = rows_data
        self.export_path = export_path

    def compose(self):
        from textual.widgets import (
            DataTable,
            Footer,
            Header,
            Input,
        )

        yield Header()
        yield Input(placeholder="Search...", id="search")
        yield DataTable()
        yield Footer()

    def on_mount(self):
        from textual.widgets import DataTable

        table = self.query_one(DataTable)
        # With --all-columns these labels are raw Airbyte JSON keys, and
        # DataTable.add_column runs Text.from_markup over any str label, so an
        # unbalanced tag in a key would abort the mount just as it would a cell.
        table.add_columns(*[cell(col) for col in self.columns])
        self._render_rows(self.filtered_rows)
        table.cursor_type = "row"
        table.focus()

    def _render_rows(self, rows: list[list[str]]):
        from textual.widgets import DataTable

        table = self.query_one(DataTable)
        table.clear()
        for row in rows:
            # A str cell goes through Text.from_markup inside textual, so a
            # connection name containing "[/x]" would abort the render.
            table.add_row(*[cell(v) for v in row])

    def action_focus_search(self):
        from textual.widgets import Input

        self.query_one("#search", Input).focus()

    def action_focus_list(self):
        from textual.widgets import DataTable

        self.query_one(DataTable).focus()

    def on_input_changed(self, event):
        try:
            input_id = event.input.id
        except Exception:
            return
        if input_id != "search":
            return

        query = (event.value or "").strip().lower()
        if not query:
            self.filtered_rows = self.all_rows
        else:
            self.filtered_rows = [
                row
                for row in self.all_rows
                if any(query in str(cell).lower() for cell in row)
            ]
        self._render_rows(self.filtered_rows)

    def action_copy_row(self):
        from textual.widgets import DataTable

        table = self.query_one(DataTable)
        row_index = table.cursor_row
        if row_index is None or row_index < 0 or row_index >= len(self.filtered_rows):
            self.notify("No row selected", severity="warning")
            return
        row = self.filtered_rows[row_index]
        text = "\t".join(str(v) for v in row)
        self.copy_to_clipboard(text)
        self.notify("Row copied to clipboard")

    def action_export_rows(self):
        data = [
            dict(zip(self.columns, row, strict=False)) for row in self.filtered_rows
        ]
        with open(self.export_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        # Toast bodies are rendered as markup, so the user's path is escaped.
        self.notify(
            f"Exported {len(self.filtered_rows)} rows to {esc(self.export_path)}"
        )
