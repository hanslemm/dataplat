"""Tests for dataplat.services.airbyte.sources."""
from __future__ import annotations

import json

import httpx
import pytest

from dataplat.core.errors import ServiceError
from dataplat.services.airbyte import sources


def _mock_client(response_data, status_code=200):
    class FakeResponse:
        def __init__(self, data, code):
            self.status_code = code
            self._data = data
            self.text = json.dumps(data) if data is not None else ""
            self.headers = {"content-type": "application/json"}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError(
                    "error",
                    request=httpx.Request("GET", "http://test"),
                    response=self,
                )

        def json(self):
            return self._data

    class FakeClient:
        def __init__(self):
            self.last_url = None
            self.last_json = None
            self.last_params = None
            self._get_call_count = 0

        def get(self, url, **kwargs):
            self.last_url = url
            self.last_params = kwargs.get("params")
            self._get_call_count += 1
            # Return empty data on subsequent calls to terminate pagination
            if self._get_call_count > 1:
                return FakeResponse({"data": []}, 200)
            return FakeResponse(response_data, status_code)

        def post(self, url, **kwargs):
            self.last_url = url
            self.last_json = kwargs.get("json")
            return FakeResponse(response_data, status_code)

        def patch(self, url, **kwargs):
            self.last_url = url
            self.last_json = kwargs.get("json")
            return FakeResponse(response_data, status_code)

        def delete(self, url, **kwargs):
            self.last_url = url
            return FakeResponse(response_data, status_code)

    return FakeClient()


BASE = "http://airbyte.test"


def test_list_sources_single_page():
    client = _mock_client({"data": [{"sourceId": "s1"}, {"sourceId": "s2"}]})
    result = list(sources.list_sources(client, BASE))
    assert result == [{"sourceId": "s1"}, {"sourceId": "s2"}]
    assert client.last_url == f"{BASE}/api/public/v1/sources"


def test_list_sources_empty():
    client = _mock_client({"data": []})
    result = list(sources.list_sources(client, BASE))
    assert result == []


def test_get_source_ok():
    data = {"sourceId": "abc", "name": "My Source"}
    client = _mock_client(data)
    result = sources.get_source(client, BASE, "abc")
    assert result == data
    assert client.last_url == f"{BASE}/api/public/v1/sources/abc"


def test_get_source_404():
    client = _mock_client({"message": "not found"}, status_code=404)
    with pytest.raises(ServiceError, match="Failed to get source"):
        sources.get_source(client, BASE, "missing")


def test_create_source_ok():
    data = {"sourceId": "new-src", "name": "Test"}
    client = _mock_client(data, status_code=200)
    result = sources.create_source(
        client, BASE, name="Test", workspace_id="ws1", definition_id="def1", configuration={"key": "val"}
    )
    assert result == data
    assert client.last_url == f"{BASE}/api/public/v1/sources"
    assert client.last_json == {
        "name": "Test",
        "workspaceId": "ws1",
        "definitionId": "def1",
        "configuration": {"key": "val"},
    }


def test_create_source_error():
    client = _mock_client({"message": "bad request"}, status_code=400)
    with pytest.raises(ServiceError, match="Failed to create source"):
        sources.create_source(
            client, BASE, name="Test", workspace_id="ws1", definition_id="def1", configuration={}
        )


def test_update_source_ok():
    data = {"sourceId": "s1", "name": "Updated"}
    client = _mock_client(data)
    result = sources.update_source(client, BASE, "s1", {"name": "Updated"})
    assert result == data
    assert client.last_url == f"{BASE}/api/public/v1/sources/s1"
    assert client.last_json == {"name": "Updated"}


def test_delete_source_ok():
    client = _mock_client(None, status_code=204)
    # Should not raise
    sources.delete_source(client, BASE, "s1")
    assert client.last_url == f"{BASE}/api/public/v1/sources/s1"


def test_delete_source_error():
    client = _mock_client({"message": "forbidden"}, status_code=403)
    with pytest.raises(ServiceError, match="Failed to delete source"):
        sources.delete_source(client, BASE, "s1")
