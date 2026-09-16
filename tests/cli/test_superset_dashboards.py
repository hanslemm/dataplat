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
from dataplat.core.redshift_ports import SOURCE_SYNCED

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


def test_list_filters_by_database(superset_env, patch_client) -> None:
    # The join Superset cannot do for us: a dashboard qualifies when one of
    # its charts reads a dataset on that connection. An inverted `any`, or a
    # comparison against the wrong field, passes every other test in this file.
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/security/login"):
            return httpx.Response(200, json={"access_token": "tok"}, request=request)
        if path.endswith("/database/"):
            return httpx.Response(
                200,
                json={"result": [{"id": 2, "database_name": "BetterData"}]},
                request=request,
            )
        if path.endswith("/dataset/"):
            return httpx.Response(200, json={"result": [{"id": 887}]}, request=request)
        if path.endswith("/dashboard/42/charts"):
            return httpx.Response(
                200, json={"result": [{"id": 7, "datasource_id": 887}]}, request=request
            )
        if path.endswith("/dashboard/43/charts"):
            return httpx.Response(
                200, json={"result": [{"id": 8, "datasource_id": 999}]}, request=request
            )
        if path.endswith("/dashboard/"):
            return httpx.Response(200, json={"result": DASHBOARDS}, request=request)
        return httpx.Response(404, json={"message": path}, request=request)

    patch_client(handler)

    result = runner.invoke(
        superset_cli.app, ["dashboards", "list", "--database", "BetterData"], env=WIDE
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "Revenue overview" in result.output  # its chart reads dataset 887
    assert "Ops daily" not in result.output  # its chart reads 999, not on this db


def test_an_unknown_database_exits_three(superset_env, patch_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/security/login"):
            return httpx.Response(200, json={"access_token": "tok"}, request=request)
        if path.endswith("/database/"):
            return httpx.Response(
                200,
                json={"result": [{"id": 2, "database_name": "BetterData"}]},
                request=request,
            )
        return httpx.Response(200, json={"result": DASHBOARDS}, request=request)

    patch_client(handler)

    result = runner.invoke(
        superset_cli.app, ["dashboards", "list", "--database", "Nope"], env=WIDE
    )

    assert result.exit_code == ExitCode.CONFIG, result.output
    assert "BetterData" in result.output  # names what does exist


CHARTS = [
    {
        "id": 7,
        "slice_name": "Revenue",
        "datasource_id": 118,
        "datasource_type": "table",
    },
    {
        "id": 8,
        "slice_name": "Signups",
        "datasource_id": 121,
        "datasource_type": "table",
    },
    {
        "id": 9,
        "slice_name": "Cohort",
        "datasource_id": 140,
        "datasource_type": "table",
    },
]

DATASET_118 = {
    "id": 118,
    "table_name": "orders",
    "schema": "public",
    "sql": None,
    "database": {"id": 1},
    "columns": [],
    "metrics": [],
}
DATASET_121 = {
    "id": 121,
    "table_name": "users",
    "schema": "public",
    "sql": None,
    "database": {"id": 1},
    "columns": [],
    "metrics": [],
}
# Virtual, and its SQL trips a real SYNTAX finding (DISTINCT ON is
# Postgres-only) -- the fixture that exercises the construct scan, which
# DATASET_118/121 (both physical, both `"sql": None`) cannot.
DATASET_140 = {
    "id": 140,
    "table_name": "cohort",
    "schema": "analytics",
    "sql": "select distinct on (case_id) case_id, x from t order by case_id",
    "database": {"id": 1},
    "columns": [],
    "metrics": [],
}
TARGET_DATASETS = [
    {"id": 887, "table_name": "users", "schema": "public", "database": {"id": 2}}
]
DATABASES = {
    "result": [
        {"id": 1, "database_name": "DataOcean"},
        {"id": 2, "database_name": "BetterData"},
    ]
}


def _dataset_routes(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/dataset/118"):
        return httpx.Response(200, json={"result": DATASET_118}, request=request)
    if path.endswith("/dataset/121"):
        return httpx.Response(200, json={"result": DATASET_121}, request=request)
    if path.endswith("/dataset/140"):
        return httpx.Response(200, json={"result": DATASET_140}, request=request)
    if path.endswith("/dataset/"):
        q = request.url.params.get("q", "")
        rows = TARGET_DATASETS if "2" in q else []
        return httpx.Response(200, json={"result": rows}, request=request)
    return httpx.Response(404, json={"message": path}, request=request)


def _coverage_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/security/login"):
        return httpx.Response(200, json={"access_token": "tok"}, request=request)
    if path.endswith("/dashboard/42/charts"):
        return httpx.Response(200, json={"result": CHARTS}, request=request)
    if path.endswith("/database/"):
        return httpx.Response(200, json=DATABASES, request=request)
    return _dataset_routes(request)


def test_datasets_reports_what_exists_and_what_is_missing(
    superset_env, patch_client
) -> None:
    patch_client(_coverage_handler)

    result = runner.invoke(
        superset_cli.app,
        ["dashboards", "datasets", "42", "--to-database", "BetterData"],
        env=WIDE,
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "public.users" in result.output
    assert "public.orders" in result.output
    # users exists on BetterData; orders does not.
    assert "887" in result.output
    assert "missing" in result.output.lower()


def test_datasets_without_a_target_just_lists_what_is_read(
    superset_env, patch_client
) -> None:
    patch_client(_coverage_handler)

    result = runner.invoke(superset_cli.app, ["dashboards", "datasets", "42"], env=WIDE)

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "public.orders" in result.output


def test_datasets_rejects_an_unknown_database_with_the_config_code(
    superset_env, patch_client
) -> None:
    patch_client(_coverage_handler)

    result = runner.invoke(
        superset_cli.app,
        ["dashboards", "datasets", "42", "--to-database", "Nope"],
        env=WIDE,
    )

    assert result.exit_code == ExitCode.CONFIG, result.output
    assert "BetterData" in result.output  # names what does exist


def test_a_virtual_dataset_is_scanned_for_redshift_constructs(
    superset_env, patch_client
) -> None:
    # The construct advisory is why this command is more than a coverage
    # count. With every other fixture physical, none of this path runs.
    patch_client(_coverage_handler)

    result = runner.invoke(
        superset_cli.app,
        ["dashboards", "datasets", "42", "--to-database", "BetterData"],
        env=WIDE,
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "DISTINCT ON" in result.output
    # A stale vendored table must announce its own age at the moment someone
    # leans on it.
    assert SOURCE_SYNCED in result.output


def test_scan_findings_reach_the_json_output(superset_env, patch_client) -> None:
    patch_client(_coverage_handler)

    result = runner.invoke(
        superset_cli.app,
        ["dashboards", "datasets", "42", "--to-database", "BetterData", "--json"],
        env=WIDE,
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    rows = json.loads(result.output)
    virtual = next(r for r in rows if r["source_id"] == 140)
    assert any(f["construct"] == "DISTINCT ON" for f in virtual["port_findings"])
