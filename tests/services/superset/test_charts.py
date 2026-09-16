"""Reading and writing individual charts."""

from __future__ import annotations

import json

import httpx

from dataplat.services.superset.charts import chart_data, get_chart, update_chart

BASE_URL = "https://superset.test"


def test_a_chart_is_read_by_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": {
                    "id": 7,
                    "datasource_id": 118,
                    "dashboards": [{"id": 42}],
                }
            },
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert get_chart(client, BASE_URL, "tok", 7)["datasource_id"] == 118


def test_an_update_sends_exactly_the_payload_given() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"result": {}}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        update_chart(client, BASE_URL, "tok", 7, {"datasource_id": 904})

    assert seen == [{"datasource_id": 904}]


def test_chart_data_returns_the_rows_of_the_first_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"result": [{"data": [{"a": 1}, {"a": 2}]}]},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert chart_data(client, BASE_URL, "tok", {"datasource": {"id": 904}}) == [
            {"a": 1},
            {"a": 2},
        ]
