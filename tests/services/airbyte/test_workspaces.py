"""Tests for dataplat.services.airbyte.workspaces."""

from __future__ import annotations

import json

import httpx
import pytest

from dataplat.core.errors import ServiceError
from dataplat.services.airbyte import workspaces


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


def test_list_workspaces_ok():
    client = _mock_client({"data": [{"workspaceId": "ws1"}, {"workspaceId": "ws2"}]})
    result = list(workspaces.list_workspaces(client, BASE))
    assert result == [{"workspaceId": "ws1"}, {"workspaceId": "ws2"}]
    assert client.last_url == f"{BASE}/api/public/v1/workspaces"


def test_list_workspaces_empty():
    client = _mock_client({"data": []})
    result = list(workspaces.list_workspaces(client, BASE))
    assert result == []


def test_get_workspace_ok():
    data = {"workspaceId": "ws1", "name": "Main Workspace"}
    client = _mock_client(data)
    result = workspaces.get_workspace(client, BASE, "ws1")
    assert result == data
    assert client.last_url == f"{BASE}/api/public/v1/workspaces/ws1"


def test_get_workspace_404():
    client = _mock_client({"message": "not found"}, status_code=404)
    with pytest.raises(ServiceError, match="Failed to get workspace"):
        workspaces.get_workspace(client, BASE, "missing")
