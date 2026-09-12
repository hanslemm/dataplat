"""Reading Superset's dataset catalogue.

Its own endpoint and its own pagination -- `/api/v1/dataset/` is not under
`/security/`, so the security paginator does not reach it.
"""

from __future__ import annotations

import httpx
import pytest

from dataplat.core.errors import ServiceError
from dataplat.services.superset.datasets import iter_datasets

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
