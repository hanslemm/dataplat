"""Reading dashboards, and asking Superset to copy one."""

from __future__ import annotations

import json

import httpx
import pytest

from dataplat.core.errors import ServiceError
from dataplat.services.superset.dashboards import (
    copy_dashboard,
    dashboard_charts,
    get_dashboard,
    iter_dashboards,
    update_dashboard,
)

BASE_URL = "https://superset.test"


def test_every_page_of_dashboards_is_read() -> None:
    pages = [[{"id": n} for n in range(100)], [{"id": 100}]]

    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("q", "")
        page = int(q.split("page:")[1].split(",")[0]) if "page:" in q else 0
        rows = pages[page] if page < len(pages) else []
        return httpx.Response(200, json={"result": rows}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert len(list(iter_dashboards(client, BASE_URL, "tok"))) == 101


def test_a_dashboard_is_fetched_by_slug_as_well_as_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/revenue-overview")
        return httpx.Response(
            200,
            json={"result": {"id": 42, "dashboard_title": "Revenue"}},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert get_dashboard(client, BASE_URL, "tok", "revenue-overview")["id"] == 42


def test_the_charts_of_a_dashboard_are_returned() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/42/charts")
        return httpx.Response(
            200, json={"result": [{"id": 7, "slice_name": "Revenue"}]}, request=request
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert dashboard_charts(client, BASE_URL, "tok", 42)[0]["id"] == 7


def test_a_copy_returns_the_new_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith("/42/copy/")
        assert json.loads(request.content)["duplicate_slices"] is True
        return httpx.Response(201, json={"result": {"id": 318}}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        new_id = copy_dashboard(
            client,
            BASE_URL,
            "tok",
            42,
            {
                "dashboard_title": "Revenue (BetterData)",
                "duplicate_slices": True,
                "json_metadata": "{}",
            },
        )

    assert new_id == 318


def test_a_refused_copy_says_why() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Forbidden"}, request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ServiceError) as excinfo,
    ):
        copy_dashboard(client, BASE_URL, "tok", 42, {"json_metadata": "{}"})

    assert "Forbidden" in str(excinfo.value)


def test_an_update_sends_exactly_the_payload_given() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path.endswith("/dashboard/318")
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"result": {}}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        update_dashboard(client, BASE_URL, "tok", 318, {"json_metadata": "{}"})

    assert seen == [{"json_metadata": "{}"}]


def test_a_copy_with_non_int_id_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Superset responds 2xx but with id as string instead of int
        return httpx.Response(201, json={"result": {"id": "abc"}}, request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ServiceError) as excinfo,
    ):
        copy_dashboard(client, BASE_URL, "tok", 42, {"json_metadata": "{}"})

    assert "no usable id" in str(excinfo.value)
