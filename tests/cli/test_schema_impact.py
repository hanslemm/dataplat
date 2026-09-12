"""`dp db schema impact` — what breaks if this schema goes.

The command answers from the systems that know: Superset's datasets and
Airbyte's destinations. It opens no database connection, because what a schema
*contains* is already `schema list`'s job and is not the half that surprises
anyone.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from dataplat.cli.db import schema_impact as cli
from dataplat.cli.db.schema import app as schema_app

runner = CliRunner()
WIDE = {"COLUMNS": "200"}

DATASETS = [
    {
        "id": 1,
        "table_name": "orders",
        "schema": "analytics",
        "sql": None,
        "database": {"database_name": "Dataocean Production"},
    },
    {
        "id": 2,
        "table_name": "virtual_summary",
        "schema": None,
        "sql": "SELECT * FROM analytics.orders",
        "database": {"database_name": "Dataocean Production"},
    },
    {
        "id": 3,
        "table_name": "unrelated",
        "schema": "md_analytics",
        "sql": None,
        "database": {"database_name": "Dataocean Production"},
    },
]

DESTINATIONS = [
    {
        "destinationId": "d1",
        "name": "Dataocean analytics",
        "configuration": {"schema": "analytics", "database": "dataocean"},
    }
]

CONNECTIONS = [
    {
        "connectionId": "c1",
        "name": "sheets sync",
        "destinationId": "d1",
        "namespaceDefinition": "destination",
        "status": "active",
    }
]


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPERSET_BASE_URL", "https://superset.test")
    monkeypatch.setenv("SUPERSET_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("SUPERSET_ADMIN_PASSWORD", "secret")
    monkeypatch.setenv("AIRBYTE_BASE_URL", "https://airbyte.test")
    monkeypatch.setenv("AIRBYTE_CLIENT_ID", "id")
    monkeypatch.setenv("AIRBYTE_CLIENT_SECRET", "secret")


def _superset(monkeypatch: pytest.MonkeyPatch, datasets: list[dict[str, Any]]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/security/login"):
            return httpx.Response(200, json={"access_token": "tok"})
        if "/dataset/" in request.url.path:
            q = request.url.params.get("q", "")
            page = int(q.split("page:")[1].split(",")[0]) if "page:" in q else 0
            rows = datasets if page == 0 else []
            return httpx.Response(200, json={"result": rows, "count": len(datasets)})
        raise AssertionError(f"unexpected {request.url}")

    transport = httpx.MockTransport(handler)
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: real(transport=transport))


def _airbyte(
    monkeypatch: pytest.MonkeyPatch,
    destinations: list[dict[str, Any]],
    connections: list[dict[str, Any]],
) -> None:
    monkeypatch.setattr(
        cli, "build_authenticated_client", lambda: (_FakeAirbyte(), "https://airbyte.test")
    )
    monkeypatch.setattr(cli, "list_destinations", lambda c, b: iter(destinations))
    monkeypatch.setattr(cli, "list_connections", lambda c, b: iter(connections))


class _FakeAirbyte:
    def close(self) -> None:
        return None


def test_a_dataset_is_reported_however_it_names_the_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _superset(monkeypatch, DATASETS)
    _airbyte(monkeypatch, [], [])

    result = runner.invoke(schema_app, ["impact", "analytics"], env=WIDE)

    assert result.exit_code == 0, result.output
    assert "orders" in result.output
    assert "virtual_summary" in result.output
    assert "unrelated" not in result.output


def test_the_ingestion_that_lands_there_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _superset(monkeypatch, [])
    _airbyte(monkeypatch, DESTINATIONS, CONNECTIONS)

    result = runner.invoke(schema_app, ["impact", "analytics"], env=WIDE)

    assert result.exit_code == 0, result.output
    assert "Dataocean analytics" in result.output
    assert "sheets sync" in result.output


def test_a_schema_nothing_points_at_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    _superset(monkeypatch, DATASETS)
    _airbyte(monkeypatch, DESTINATIONS, CONNECTIONS)

    result = runner.invoke(schema_app, ["impact", "untouched"], env=WIDE)

    assert result.exit_code == 0, result.output
    assert "Nothing found" in result.output


def test_a_system_that_is_not_configured_is_skipped_not_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A platform without Airbyte still wants the Superset half of the answer."""
    monkeypatch.delenv("AIRBYTE_BASE_URL")
    _superset(monkeypatch, DATASETS)

    result = runner.invoke(schema_app, ["impact", "analytics"], env=WIDE)

    assert result.exit_code == 0, result.output
    assert "orders" in result.output
    assert "airbyte" in result.output.lower()


def test_json_is_machine_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    _superset(monkeypatch, DATASETS)
    _airbyte(monkeypatch, DESTINATIONS, CONNECTIONS)

    result = runner.invoke(schema_app, ["impact", "analytics", "--json"], env=WIDE)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["schema"] == "analytics"
    assert {d["name"] for d in payload["datasets"]} == {"orders", "virtual_summary"}
    assert payload["destinations"][0]["name"] == "Dataocean analytics"
    assert payload["connections"][0]["name"] == "sheets sync"
