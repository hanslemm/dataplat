from __future__ import annotations

import httpx
import pytest

from dataplat.core.errors import ServiceError
from dataplat.services.airbyte.jobs import (
    cancel_job,
    get_job,
    list_jobs,
    trigger_job,
)
from tests._responses import response


class _FakeClient:
    def __init__(self, response: httpx.Response):
        self._response = response
        self.calls: list[tuple[str, str, dict]] = []

    def get(self, url, params=None, **kw):
        self.calls.append(("GET", url, params or {}))
        return self._response

    def post(self, url, json=None, **kw):
        self.calls.append(("POST", url, json or {}))
        return self._response

    def delete(self, url, **kw):
        self.calls.append(("DELETE", url, {}))
        return self._response


def test_list_jobs_passes_filters() -> None:
    client = _FakeClient(response({"data": [{"jobId": 1}]}))

    jobs = list_jobs(
        client,  # type: ignore[arg-type]
        "http://ab",
        connection_id="c1",
        status="failed",
        job_type="sync",
        limit=5,
    )

    assert jobs == [{"jobId": 1}]
    method, url, params = client.calls[0]
    assert url == "http://ab/api/public/v1/jobs"
    assert params["connectionId"] == "c1"
    assert params["status"] == "failed"
    assert params["jobType"] == "sync"
    assert params["limit"] == 5


def test_get_job() -> None:
    client = _FakeClient(response({"jobId": 7, "status": "running"}))

    job = get_job(client, "http://ab", "7")  # type: ignore[arg-type]

    assert job["status"] == "running"
    assert client.calls[0][1] == "http://ab/api/public/v1/jobs/7"


def test_cancel_job_uses_delete() -> None:
    client = _FakeClient(response({"jobId": 7, "status": "cancelled"}))

    cancel_job(client, "http://ab", "7")  # type: ignore[arg-type]

    assert client.calls[0][0] == "DELETE"


def test_trigger_job_posts_job_type() -> None:
    client = _FakeClient(response({"jobId": 9}))

    trigger_job(client, "http://ab", "c1", "reset")  # type: ignore[arg-type]

    method, url, payload = client.calls[0]
    assert method == "POST"
    assert payload == {"connectionId": "c1", "jobType": "reset"}


def test_error_raises_service_error() -> None:
    client = _FakeClient(response({"message": "boom"}, status_code=500))

    with pytest.raises(ServiceError, match="list jobs"):
        list_jobs(client, "http://ab")  # type: ignore[arg-type]
