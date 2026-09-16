"""Reading Superset's dataset catalogue.

Its own endpoint and its own pagination -- `/api/v1/dataset/` is not under
`/security/`, so the security paginator does not reach it.
"""

from __future__ import annotations

import json

import httpx
import pytest

from dataplat.core.errors import ServiceError
from dataplat.services.superset.datasets import (
    create_dataset,
    get_dataset,
    iter_datasets,
    update_dataset,
)

BASE_URL = "https://superset.test"


def _serve(pages: list[list[dict]], status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        if status >= 400:
            return httpx.Response(status, json={"message": "nope"}, request=request)
        q = request.url.params.get("q", "")
        page = 0
        if "page:" in q:
            page = int(q.split("page:")[1].split(",")[0].rstrip(")"))
        rows = pages[page] if page < len(pages) else []
        return httpx.Response(
            200,
            json={"result": rows, "count": sum(len(p) for p in pages)},
            request=request,
        )

    return httpx.MockTransport(handler)


def test_every_page_is_read() -> None:
    pages = [[{"id": n} for n in range(100)], [{"id": 100}]]

    with httpx.Client(transport=_serve(pages)) as client:
        rows = list(iter_datasets(client, BASE_URL, "tok"))

    assert [r["id"] for r in rows] == list(range(101))


def test_an_empty_catalogue_yields_nothing() -> None:
    with httpx.Client(transport=_serve([[]])) as client:
        assert list(iter_datasets(client, BASE_URL, "tok")) == []


def test_a_failure_reports_the_reason_superset_gave() -> None:
    with (
        httpx.Client(transport=_serve([], status=403)) as client,
        pytest.raises(ServiceError) as excinfo,
    ):
        list(iter_datasets(client, BASE_URL, "tok"))

    assert "403 Forbidden" in str(excinfo.value)
    assert "nope" in str(excinfo.value)


def test_a_database_filter_is_sent_to_superset() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("q", ""))
        return httpx.Response(200, json={"result": []}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        list(iter_datasets(client, BASE_URL, "tok", database_id=2))

    # Filtering server-side, not in Python: an instance with 842 datasets
    # should not be paged through whole to find the dozen on one connection.
    assert "database" in seen[0]
    assert "2" in seen[0]


def test_creation_returns_the_new_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": 904}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert create_dataset(client, BASE_URL, "tok", {"table_name": "orders"}) == 904


def test_a_creation_that_returns_no_id_fails_here_rather_than_later() -> None:
    # A bogus id does not fail at the point of creation -- it fails three
    # calls later, against an id nobody chose.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={}, request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ServiceError) as excinfo,
    ):
        create_dataset(client, BASE_URL, "tok", {"table_name": "orders"})

    assert "no id" in str(excinfo.value)


def test_a_refused_creation_says_what_superset_said() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422, json={"message": "Dataset already exists"}, request=request
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ServiceError) as excinfo,
    ):
        create_dataset(client, BASE_URL, "tok", {"table_name": "orders"})

    assert "already exists" in str(excinfo.value)


def test_a_dataset_is_read_back_by_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/904")
        return httpx.Response(
            200, json={"result": {"id": 904, "table_name": "orders"}}, request=request
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert get_dataset(client, BASE_URL, "tok", 904)["table_name"] == "orders"


def test_an_update_puts_the_payload() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"result": {}}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        update_dataset(
            client, BASE_URL, "tok", 904, {"metrics": [{"metric_name": "r"}]}
        )

    assert seen[0]["metrics"][0]["metric_name"] == "r"
