"""Textual TUI for browsing airbyte connections (``connections list --tui``)."""

from __future__ import annotations

import json

from textual.app import App as TextualApp

# textual is a hard dependency (see pyproject), so the TUI is always available.
TEXTUAL_AVAILABLE = True


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
        table.add_columns(*self.columns)
        self._render_rows(self.filtered_rows)
        table.cursor_type = "row"
        table.focus()

    def _render_rows(self, rows: list[list[str]]):
        from textual.widgets import DataTable

        table = self.query_one(DataTable)
        table.clear()
        for row in rows:
            table.add_row(*[str(v) for v in row])

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
        if (
            row_index is None
            or row_index < 0
            or row_index >= len(self.filtered_rows)
        ):
            self.notify("No row selected", severity="warning")
            return
        row = self.filtered_rows[row_index]
        text = "\t".join(str(v) for v in row)
        self.copy_to_clipboard(text)
        self.notify("Row copied to clipboard")

    def action_export_rows(self):
        data = [dict(zip(self.columns, row, strict=False)) for row in self.filtered_rows]
        with open(self.export_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        self.notify(
            f"Exported {len(self.filtered_rows)} rows to {self.export_path}"
        )
