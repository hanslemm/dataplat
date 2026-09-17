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
        if request.url.path.endswith("/security/csrf_token/"):
            return httpx.Response(200, json={"result": "csrf-abc"}, request=request)
        assert request.method == "PUT"
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"result": {}}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        update_chart(client, BASE_URL, "tok", 7, {"datasource_id": 904})

    assert seen == [{"datasource_id": 904}]


def test_an_update_sends_the_csrf_token() -> None:
    # Measured live: PUT /chart/{id} without it is refused.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/security/csrf_token/"):
            return httpx.Response(200, json={"result": "csrf-abc"}, request=request)
        assert request.headers["x-csrftoken"] == "csrf-abc"
        return httpx.Response(200, json={"result": {}}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        update_chart(client, BASE_URL, "tok", 7, {"datasource_id": 904})


def test_chart_data_returns_the_rows_of_the_first_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": [
                    {"data": [{"a": 1}, {"a": 2}]},
                    {"data": [{"a": 99}]},
                ]
            },
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        # The second result's rows must NOT appear: a concatenating
        # implementation passes a single-result mock identically.
        assert chart_data(client, BASE_URL, "tok", {"datasource": {"id": 904}}) == [
            {"a": 1},
            {"a": 2},
        ]


def test_chart_data_does_not_fetch_a_csrf_token() -> None:
    # Verified live: /chart/data returned 6,765 rows with only a Bearer
    # token, no CSRF. It also runs once per chart during `--compare`, so a
    # token fetch added here would be a second request on every one of them.
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"result": [{"data": []}]}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        chart_data(client, BASE_URL, "tok", {"datasource": {"id": 904}})

    assert paths == ["/api/v1/chart/data"]
