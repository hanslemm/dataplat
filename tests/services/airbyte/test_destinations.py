"""Tests for dataplat.services.airbyte.destinations."""
from __future__ import annotations

import json

import httpx
import pytest

from dataplat.core.errors import ServiceError
from dataplat.services.airbyte import destinations


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


def test_list_destinations_single_page():
    client = _mock_client({"data": [{"destinationId": "d1"}, {"destinationId": "d2"}]})
    result = list(destinations.list_destinations(client, BASE))
    assert result == [{"destinationId": "d1"}, {"destinationId": "d2"}]
    assert client.last_url == f"{BASE}/api/public/v1/destinations"


def test_list_destinations_empty():
    client = _mock_client({"data": []})
    result = list(destinations.list_destinations(client, BASE))
    assert result == []


def test_get_destination_ok():
    data = {"destinationId": "abc", "name": "My Destination"}
    client = _mock_client(data)
    result = destinations.get_destination(client, BASE, "abc")
    assert result == data
    assert client.last_url == f"{BASE}/api/public/v1/destinations/abc"


def test_get_destination_404():
    client = _mock_client({"message": "not found"}, status_code=404)
    with pytest.raises(ServiceError, match="Failed to get destination"):
        destinations.get_destination(client, BASE, "missing")


def test_create_destination_ok():
    data = {"destinationId": "new-dst", "name": "Test"}
    client = _mock_client(data, status_code=200)
    result = destinations.create_destination(
        client, BASE, name="Test", workspace_id="ws1", definition_id="def1", configuration={"key": "val"}
    )
    assert result == data
    assert client.last_url == f"{BASE}/api/public/v1/destinations"
    assert client.last_json == {
        "name": "Test",
        "workspaceId": "ws1",
        "definitionId": "def1",
        "configuration": {"key": "val"},
    }


def test_create_destination_error():
    client = _mock_client({"message": "bad request"}, status_code=400)
    with pytest.raises(ServiceError, match="Failed to create destination"):
        destinations.create_destination(
            client, BASE, name="Test", workspace_id="ws1", definition_id="def1", configuration={}
        )


def test_update_destination_ok():
    data = {"destinationId": "d1", "name": "Updated"}
    client = _mock_client(data)
    result = destinations.update_destination(client, BASE, "d1", {"name": "Updated"})
    assert result == data
    assert client.last_url == f"{BASE}/api/public/v1/destinations/d1"
    assert client.last_json == {"name": "Updated"}


def test_delete_destination_ok():
    client = _mock_client(None, status_code=204)
    # Should not raise
    destinations.delete_destination(client, BASE, "d1")
    assert client.last_url == f"{BASE}/api/public/v1/destinations/d1"


def test_delete_destination_error():
    client = _mock_client({"message": "forbidden"}, status_code=403)
    with pytest.raises(ServiceError, match="Failed to delete destination"):
        destinations.delete_destination(client, BASE, "d1")
