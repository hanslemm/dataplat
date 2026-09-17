"""CLI coverage for ``dp bi superset dashboards usage``.

The command straddles two areas -- it reads dashboard titles from the Superset
API and the view counts from a ``dp`` database target -- so both seams are
faked here and the real Typer wiring and Rich render path run in between.
"""

from __future__ import annotations

import contextlib
import json
import re
from datetime import datetime
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from dataplat.cli.bi import superset as superset_cli
from dataplat.core.errors import ExitCode

runner = CliRunner()
WIDE = {"COLUMNS": "220"}

DASHBOARDS: list[dict[str, Any]] = [
    {"id": 42, "dashboard_title": "Revenue overview", "status": "published"},
    {"id": 43, "dashboard_title": "Ops daily", "status": "draft"},
    {"id": 44, "dashboard_title": "Abandoned experiment", "status": "draft"},
]

# (dashboard_id, views, viewers, last_viewed) -- 44 is deliberately absent.
USAGE_ROWS = [
    (42, 519, 36, datetime(2026, 9, 16, 8, 30)),
    (43, 12, 2, datetime(2026, 8, 1, 9, 0)),
]

ACTION_ROWS = [
    ("warm_up_cache", 5562780, 5562780, datetime(2026, 9, 17, 6, 0)),
    ("dashboard", 421458, 421458, datetime(2026, 9, 17, 11, 0)),
    ("ChartRestApi.thumbnail", 207712, 0, datetime(2026, 9, 17, 5, 0)),
]


class _Cursor:
    """Answers whichever query it is given, recording what it was asked.

    It honours a trailing ``LIMIT n`` rather than ignoring the SQL. That one
    detail is what lets a test tell a capped query from an uncapped one -- a
    cursor that always returned every row would pass whether or not the
    command respected the cap, which is the difference between testing the
    code and testing the fake.
    """

    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows
        self.executed: list[str] = []
        self.params: list[Any] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def execute(self, sql_text, params=None) -> None:
        self.executed.append(str(sql_text))
        self.params.append(params)

    def fetchall(self) -> list[tuple]:
        match = re.search(r"LIMIT\s+(\d+)\s*$", self.executed[-1], re.IGNORECASE)
        return self._rows[: int(match.group(1))] if match else self._rows


class _Conn:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def cursor(self):
        return self._cursor


@pytest.fixture
def usage_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPERSET_BASE_URL", "https://superset.test")
    monkeypatch.setenv("SUPERSET_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("SUPERSET_ADMIN_PASSWORD", "secret")
    monkeypatch.setenv("DP_SUPERSET_USAGE_TARGET", "warehouse")
    monkeypatch.delenv("DP_SUPERSET_USAGE_LOGS_TABLE", raising=False)
    monkeypatch.delenv("DP_SUPERSET_USAGE_USERS_TABLE", raising=False)
    monkeypatch.delenv("DP_SUPERSET_USAGE_EXCLUDE_USERS", raising=False)


def serve(routes: dict[str, Any]):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/security/login"):
            return httpx.Response(200, json={"access_token": "tok"}, request=request)
        for suffix, payload in routes.items():
            if request.url.path.endswith(suffix):
                return httpx.Response(200, json=payload, request=request)
        return httpx.Response(404, json={"message": request.url.path}, request=request)

    return handler


@pytest.fixture
def patch_seams(monkeypatch: pytest.MonkeyPatch):
    """Fake both halves: the Superset API and the warehouse connection."""

    def install(rows: list[tuple], handler=None) -> _Cursor:
        cursor = _Cursor(rows)

        @contextlib.contextmanager
        def _session(params):
            yield _Conn(cursor)

        monkeypatch.setattr(
            "dataplat.cli.bi.dashboards_usage.db_session", _session, raising=False
        )
        monkeypatch.setattr(
            "dataplat.cli.bi.dashboards_usage.resolve_usage_params",
            lambda target: object(),
            raising=False,
        )
        served = handler or serve({"/api/v1/dashboard/": {"result": DASHBOARDS}})

        def build(**kwargs: Any) -> httpx.Client:
            return httpx.Client(transport=httpx.MockTransport(served), **kwargs)

        monkeypatch.setattr(
            "dataplat.cli.bi.dashboards_usage.build_client", build, raising=False
        )
        return cursor

    return install


def test_usage_ranks_dashboards_and_names_them(usage_env, patch_seams) -> None:
    """The warehouse knows ids; the titles have to come from the API."""
    patch_seams(USAGE_ROWS)

    result = runner.invoke(superset_cli.app, ["dashboards", "usage"], env=WIDE)

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "Revenue overview" in result.output
    assert "519" in result.output
    assert "36" in result.output


def test_usage_emits_json(usage_env, patch_seams) -> None:
    patch_seams(USAGE_ROWS)

    result = runner.invoke(
        superset_cli.app, ["dashboards", "usage", "--json"], env=WIDE
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    payload = json.loads(result.output)
    assert [row["dashboard_id"] for row in payload] == [42, 43]
    assert payload[0]["title"] == "Revenue overview"
    assert payload[0]["views"] == 519


def test_actions_reports_the_vocabulary_including_the_machine_traffic(
    usage_env, patch_seams
) -> None:
    """The whole point: see what your instance calls things before filtering."""
    cursor = patch_seams(ACTION_ROWS)

    result = runner.invoke(
        superset_cli.app, ["dashboards", "usage", "--actions"], env=WIDE
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "warm_up_cache" in result.output
    assert "ChartRestApi.thumbnail" in result.output
    # Discovery must not pre-filter to the view action, or it could never
    # show you the two rows above.
    assert all("l.action IN" not in sql for sql in cursor.executed)


def test_unused_lists_what_nobody_opened(usage_env, patch_seams) -> None:
    """Dashboard 44 has no usage row at all, which is the point of --unused."""
    patch_seams(USAGE_ROWS)

    result = runner.invoke(
        superset_cli.app, ["dashboards", "usage", "--unused"], env=WIDE
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "Abandoned experiment" in result.output
    assert "Revenue overview" not in result.output


def test_unused_says_so_when_the_dashboard_list_is_partial(
    usage_env, patch_seams
) -> None:
    """Superset's dashboard list is filtered by what the account may see.

    A non-admin gets a short list, so `--unused` would quietly mean "of the
    ones I can see" and could be read as "of all of them". When the logs name
    a dashboard the listing does not, that is proof the listing is partial,
    and the report has to say so rather than let the number stand alone.
    """
    patch_seams([*USAGE_ROWS, (99, 4, 1, datetime(2026, 9, 10, 8, 0))])

    result = runner.invoke(
        superset_cli.app, ["dashboards", "usage", "--unused"], env=WIDE
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "1" in result.output
    assert "not in this account" in result.output.lower()


def test_unused_ignores_the_row_cap(usage_env, patch_seams) -> None:
    """With `--limit 1`, a capped scan would see only dashboard 42.

    Dashboard 43 would then look unopened, which is the exact way this command
    could libel a dashboard into being deleted. The cap must not reach the
    query on this path.
    """
    patch_seams(USAGE_ROWS)

    result = runner.invoke(
        superset_cli.app,
        ["dashboards", "usage", "--unused", "--limit", "1"],
        env=WIDE,
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "Abandoned experiment" in result.output
    assert "Ops daily" not in result.output


def test_actions_and_unused_together_are_refused(usage_env, patch_seams) -> None:
    patch_seams(USAGE_ROWS)

    result = runner.invoke(
        superset_cli.app, ["dashboards", "usage", "--actions", "--unused"], env=WIDE
    )

    assert result.exit_code == ExitCode.INVALID_INPUT, result.output


def test_list_by_usage_shows_the_counts_and_ranks_by_them(
    usage_env, patch_seams, monkeypatch
) -> None:
    """The retirement question: of these dashboards, which does nobody open?"""
    cursor = _Cursor(USAGE_ROWS)

    @contextlib.contextmanager
    def _session(params):
        yield _Conn(cursor)

    monkeypatch.setattr("dataplat.cli.bi.dashboards_list.db_session", _session)
    monkeypatch.setattr(
        "dataplat.cli.bi.dashboards_list.resolve_usage_params", lambda t: object()
    )
    handler = serve({"/api/v1/dashboard/": {"result": DASHBOARDS}})

    def build(**kwargs: Any) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("dataplat.cli.bi.dashboards_list.build_client", build)

    result = runner.invoke(
        superset_cli.app, ["dashboards", "list", "--by-usage", "--json"], env=WIDE
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    payload = json.loads(result.output)
    # Most-viewed first, and the dashboard nobody opened is present with a
    # zero rather than missing -- it is the row the whole flag is for.
    assert [row["id"] for row in payload] == [42, 43, 44]
    assert payload[0]["views"] == 519
    assert payload[2]["views"] == 0


def test_a_missing_target_exits_config_not_failure(
    usage_env, patch_seams, monkeypatch
) -> None:
    patch_seams(USAGE_ROWS)
    monkeypatch.delenv("DP_SUPERSET_USAGE_TARGET", raising=False)

    result = runner.invoke(superset_cli.app, ["dashboards", "usage"], env=WIDE)

    assert result.exit_code == ExitCode.CONFIG, result.output
    assert "DP_SUPERSET_USAGE_TARGET" in result.output


def test_a_nonsense_window_exits_validation(usage_env, patch_seams) -> None:
    patch_seams(USAGE_ROWS)

    result = runner.invoke(
        superset_cli.app, ["dashboards", "usage", "--days", "0"], env=WIDE
    )

    assert result.exit_code == ExitCode.INVALID_INPUT, result.output


def test_a_custom_view_action_reaches_the_query(usage_env, patch_seams) -> None:
    """`--actions` is only useful if what it reveals can then be applied."""
    cursor = patch_seams(USAGE_ROWS)

    result = runner.invoke(
        superset_cli.app,
        ["dashboards", "usage", "--view-action", "dashboard.view"],
        env=WIDE,
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert any("dashboard.view" in (p or []) for p in cursor.params)
