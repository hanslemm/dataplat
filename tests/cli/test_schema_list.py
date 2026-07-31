"""``dp db schema list`` at the CLI seam: filtering, rendering, JSON."""

from __future__ import annotations

import json

from dataplat.cli.db import schema_list as sl
from dataplat.services.db.schema_admin import SchemaSummary, translate_like_pattern


class _Cursor:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows
        self.executed: list[str] = []
        self.params: list[object] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def execute(self, sql_text, params=None) -> None:
        self.executed.append(str(sql_text))
        self.params.append(params)

    def fetchall(self) -> list[tuple]:
        return self._rows


class _Conn:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def cursor(self):
        return self._cursor


def _patch_session(monkeypatch, cursor: _Cursor) -> None:
    import contextlib

    @contextlib.contextmanager
    def _session(params):
        yield _Conn(cursor)

    monkeypatch.setattr(sl, "db_session", _session)


def _invoke(**overrides):
    kwargs: dict[str, object] = {
        "like": None,
        "include_system": False,
        "as_json": False,
        "target": None,
        "engine": None,
        "user": "u",
        "password": None,
        "database": "d0",
        "host": "h",
        "port": 5432,
        "sslmode": None,
        "env_prefix": "DEMO_PG",
    }
    kwargs.update(overrides)
    return sl.list_command(**kwargs)  # type: ignore[arg-type]


def test_rows_are_rendered_with_counts(monkeypatch, capsys) -> None:
    _patch_session(monkeypatch, _Cursor([("analytics", "alice", 3, 2, 0)]))

    _invoke()

    out = capsys.readouterr().out
    assert "analytics" in out
    assert "alice" in out
    assert "Total: 1 schema(s)" in out


def test_quota_columns_are_hidden_when_nothing_is_known(monkeypatch, capsys) -> None:
    """Two columns of "?" on every Postgres target would be pure noise."""
    _patch_session(monkeypatch, _Cursor([("analytics", "alice", 1, 0, 0)]))

    _invoke()

    out = capsys.readouterr().out
    assert "Quota" not in out
    assert "Used" not in out


def test_quota_columns_appear_when_the_engine_reports_them(capsys) -> None:
    """Rendered directly: the dialect, not the CLI, decides quotas exist."""
    from rich.console import Console

    sl._render(
        Console(),
        [
            SchemaSummary("analytics", "alice", 1, 0, quota_mb=51200, used_mb=1024),
            SchemaSummary("staging", "alice", 0, 0),
        ],
    )

    out = capsys.readouterr().out
    assert "Quota" in out
    assert "50.0 GB" in out  # 51200 MB rendered as GB
    assert "1024 MB" not in out  # used is also promoted to GB at >= 1024
    # The schema with no quota state renders unknown, never zero.
    assert "?" in out


def test_unknown_is_not_zero() -> None:
    assert sl._mb(None) == "?"
    assert sl._mb(0) == "0 MB"
    assert sl._mb(512) == "512 MB"
    assert sl._mb(2048) == "2.0 GB"


def test_a_glob_pattern_is_translated_before_it_reaches_sql(
    monkeypatch, capsys
) -> None:
    cursor = _Cursor([("dev_a", "alice", 0, 0, 0)])
    _patch_session(monkeypatch, cursor)

    _invoke(like="dev_*")

    assert cursor.params[0] == ("dev_%",)


def test_json_is_machine_readable_and_carries_unknown_as_null(
    monkeypatch, capsys
) -> None:
    _patch_session(monkeypatch, _Cursor([("analytics", "alice", 3, 2, 1)]))

    _invoke(as_json=True)

    payload = json.loads(capsys.readouterr().out)
    assert payload == [
        {
            "name": "analytics",
            "owner": "alice",
            "tables": 3,
            "views": 2,
            "quota_mb": None,
            "used_mb": None,
            "other": 1,
        }
    ]


def test_no_matches_says_so_rather_than_printing_an_empty_table(
    monkeypatch, capsys
) -> None:
    _patch_session(monkeypatch, _Cursor([]))

    _invoke(like="nothing_*")

    assert "No schemas match." in capsys.readouterr().out


def test_json_of_no_matches_is_an_empty_array(monkeypatch, capsys) -> None:
    """A script parsing --json must get valid JSON, not a prose message."""
    _patch_session(monkeypatch, _Cursor([]))

    _invoke(as_json=True)

    assert json.loads(capsys.readouterr().out) == []


def test_a_schema_name_containing_markup_is_not_interpreted(
    monkeypatch, capsys
) -> None:
    """A `[` in catalog data is data. Rich would otherwise raise or swallow it."""
    _patch_session(monkeypatch, _Cursor([("weird[/bold]name", "a[x]", 0, 0, 0)]))

    _invoke()

    out = capsys.readouterr().out
    assert "weird[/bold]name" in out
    assert "a[x]" in out


def test_glob_translation_leaves_underscores_alone() -> None:
    """`_` stays a LIKE wildcard: over-matching a read-only filter is harmless.

    Documented rather than incidental — a pattern that selects schemas to *drop*
    must escape instead, which is what like_escape is for.
    """
    assert translate_like_pattern("dev_*") == "dev_%"
    assert translate_like_pattern("a*b*c") == "a%b%c"
    assert translate_like_pattern("plain") == "plain"
