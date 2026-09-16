"""CLI coverage for `dp bi superset dashboards`.

Runs against a fake Superset over httpx.MockTransport, so the real client, the
real Typer wiring and the real Rich render path are all exercised -- the only
way a markup regression is caught.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from dataplat.cli.bi import superset as superset_cli
from dataplat.core.errors import ExitCode

runner = CliRunner()
WIDE = {"COLUMNS": "200"}

DASHBOARDS: list[dict[str, Any]] = [
    {
        "id": 42,
        "dashboard_title": "Revenue overview",
        "status": "published",
        "owners": [{"first_name": "Ada", "last_name": "Lovelace"}],
    },
    {
        "id": 43,
        "dashboard_title": "Ops daily",
        "status": "draft",
        "owners": [],
    },
]


@pytest.fixture
def superset_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPERSET_BASE_URL", "https://superset.test")
    monkeypatch.setenv("SUPERSET_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("SUPERSET_ADMIN_PASSWORD", "secret")


def serve(routes: dict[str, Any]):
    """Answer by path suffix; unrouted paths fail loudly rather than silently."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/security/login"):
            return httpx.Response(200, json={"access_token": "tok"}, request=request)
        for suffix, payload in routes.items():
            if request.url.path.endswith(suffix):
                if callable(payload):
                    return payload(request)
                return httpx.Response(200, json=payload, request=request)
        return httpx.Response(404, json={"message": request.url.path}, request=request)

    return handler


@pytest.fixture
def patch_client(monkeypatch: pytest.MonkeyPatch):
    def install(handler) -> None:
        def build(**kwargs: Any) -> httpx.Client:
            return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

        for module in (
            "dataplat.cli.bi.dashboards_list",
            "dataplat.cli.bi.dashboards_datasets",
            "dataplat.cli.bi.dashboards_duplicate",
        ):
            with contextlib.suppress(AttributeError):
                monkeypatch.setattr(f"{module}.build_client", build)

    return install


def test_list_shows_every_dashboard(superset_env, patch_client) -> None:
    patch_client(serve({"/api/v1/dashboard/": {"result": DASHBOARDS}}))

    result = runner.invoke(superset_cli.app, ["dashboards", "list"], env=WIDE)

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "Revenue overview" in result.output
    assert "Ops daily" in result.output
    assert "42" in result.output


def test_list_filters_by_title(superset_env, patch_client) -> None:
    patch_client(serve({"/api/v1/dashboard/": {"result": DASHBOARDS}}))

    result = runner.invoke(
        superset_cli.app, ["dashboards", "list", "--title", "revenue"], env=WIDE
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "Revenue overview" in result.output
    assert "Ops daily" not in result.output


def test_list_emits_json(superset_env, patch_client) -> None:
    patch_client(serve({"/api/v1/dashboard/": {"result": DASHBOARDS}}))

    result = runner.invoke(superset_cli.app, ["dashboards", "list", "--json"], env=WIDE)

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert [row["id"] for row in json.loads(result.output)] == [42, 43]


def test_a_title_containing_markup_does_not_kill_the_render(
    superset_env, patch_client
) -> None:
    # Rich interprets [...] in every str it renders; an unescaped title turns
    # an ordinary row into a MarkupError traceback.
    hostile = [{"id": 1, "dashboard_title": "Q3 [/bold] review", "owners": []}]
    patch_client(serve({"/api/v1/dashboard/": {"result": hostile}}))

    result = runner.invoke(superset_cli.app, ["dashboards", "list"], env=WIDE)

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "[/bold]" in result.output


def test_a_rejected_login_exits_four(superset_env, patch_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"}, request=request)

    patch_client(handler)

    result = runner.invoke(superset_cli.app, ["dashboards", "list"], env=WIDE)

    assert result.exit_code == ExitCode.AUTH, result.output
