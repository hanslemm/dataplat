"""Tests for dataplat.services.airbyte.definitions."""

from __future__ import annotations

import json

import httpx
import pytest

from dataplat.core.errors import ServiceError
from dataplat.services.airbyte import definitions


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
WS = "workspace-1"


def test_list_source_definitions_ok():
    client = _mock_client(
        {"data": [{"definitionId": "def1"}, {"definitionId": "def2"}]}
    )
    result = list(definitions.list_source_definitions(client, BASE, WS))
    assert result == [{"definitionId": "def1"}, {"definitionId": "def2"}]
    assert (
        client.last_url == f"{BASE}/api/public/v1/workspaces/{WS}/definitions/sources"
    )


def test_list_source_definitions_empty():
    client = _mock_client({"data": []})
    result = list(definitions.list_source_definitions(client, BASE, WS))
    assert result == []


def test_list_source_definitions_error():
    client = _mock_client({"message": "server error"}, status_code=500)
    with pytest.raises(ServiceError, match="Failed to list source definitions"):
        list(definitions.list_source_definitions(client, BASE, WS))


def test_list_destination_definitions_ok():
    client = _mock_client({"data": [{"definitionId": "dst-def1"}]})
    result = list(definitions.list_destination_definitions(client, BASE, WS))
    assert result == [{"definitionId": "dst-def1"}]
    assert (
        client.last_url
        == f"{BASE}/api/public/v1/workspaces/{WS}/definitions/destinations"
    )


def test_list_destination_definitions_error():
    client = _mock_client({"message": "server error"}, status_code=500)
    with pytest.raises(ServiceError, match="Failed to list destination definitions"):
        list(definitions.list_destination_definitions(client, BASE, WS))
