"""Tests for create_connection and delete_connection in connections.py."""

from __future__ import annotations

import httpx
import pytest

from dataplat.core.errors import ServiceError
from dataplat.services.airbyte import connections
from dataplat.services.airbyte.connections import (
    get_connection_state,
    update_connection_state,
)
from tests._responses import response


def _mock_client(response_data, status_code=200):

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
                return response({"data": []}, 200)
            return response(response_data, status_code)

        def post(self, url, **kwargs):
            self.last_url = url
            self.last_json = kwargs.get("json")
            return response(response_data, status_code)

        def patch(self, url, **kwargs):
            self.last_url = url
            self.last_json = kwargs.get("json")
            return response(response_data, status_code)

        def delete(self, url, **kwargs):
            self.last_url = url
            return response(response_data, status_code)

    return FakeClient()


BASE = "http://airbyte.test"


def test_create_connection_minimal():
    data = {"connectionId": "conn1", "sourceId": "src1", "destinationId": "dst1"}
    client = _mock_client(data, status_code=200)
    result = connections.create_connection(
        client, BASE, source_id="src1", destination_id="dst1"
    )
    assert result == data
    assert client.last_url == f"{BASE}/api/public/v1/connections"
    assert client.last_json == {"sourceId": "src1", "destinationId": "dst1"}


def test_create_connection_full():
    data = {"connectionId": "conn2"}
    client = _mock_client(data, status_code=200)
    result = connections.create_connection(
        client,
        BASE,
        source_id="src1",
        destination_id="dst1",
        name="My Conn",
        schedule={"scheduleType": "manual"},
        namespace_definition="destination",
        status="active",
        configurations={"streams": []},
    )
    assert result == data
    assert client.last_json == {
        "sourceId": "src1",
        "destinationId": "dst1",
        "name": "My Conn",
        "schedule": {"scheduleType": "manual"},
        "namespaceDefinition": "destination",
        "status": "active",
        "configurations": {"streams": []},
    }


def test_create_connection_error():
    client = _mock_client({"message": "bad request"}, status_code=400)
    with pytest.raises(ServiceError, match="Failed to create connection"):
        connections.create_connection(
            client, BASE, source_id="src1", destination_id="dst1"
        )


def test_delete_connection_ok():
    client = _mock_client(None, status_code=204)
    # Should not raise
    connections.delete_connection(client, BASE, "conn1")
    assert client.last_url == f"{BASE}/api/public/v1/connections/conn1"


def test_delete_connection_error():
    client = _mock_client({"message": "not found"}, status_code=404)
    with pytest.raises(ServiceError, match="Failed to delete connection"):
        connections.delete_connection(client, BASE, "conn1")


# Tests for get_connection_state and update_connection_state


class _FakeClient:
    def __init__(self, response: httpx.Response):
        self._response = response
        self.calls: list[tuple[str, str, dict]] = []

    def post(self, url, json=None, **kw):
        self.calls.append(("POST", url, json or {}))
        return self._response


def test_get_connection_state_posts_connection_id() -> None:
    state = {"connectionId": "c1", "stateType": "stream", "streamState": []}
    client = _FakeClient(response(state))

    result = get_connection_state(client, "http://ab", "c1")  # type: ignore[arg-type]

    assert result == state
    method, url, payload = client.calls[0]
    assert method == "POST"
    assert url == "http://ab/api/v1/state/get"
    assert payload == {"connectionId": "c1"}


def test_update_connection_state_wraps_payload() -> None:
    new_state = {"connectionId": "c1", "stateType": "stream", "streamState": []}
    client = _FakeClient(response({"ok": True}))

    update_connection_state(client, "http://ab", "c1", new_state)  # type: ignore[arg-type]

    method, url, payload = client.calls[0]
    assert url == "http://ab/api/v1/state/create_or_update"
    assert payload == {"connectionId": "c1", "connectionState": new_state}


def test_get_connection_state_error_raises() -> None:
    client = _FakeClient(response({"message": "boom"}, status_code=500))

    with pytest.raises(ServiceError, match="connection state"):
        get_connection_state(client, "http://ab", "c1")  # type: ignore[arg-type]


def test_update_connection_state_error_raises() -> None:
    client = _FakeClient(response({"message": "boom"}, status_code=500))

    with pytest.raises(ServiceError, match="connection state"):
        update_connection_state(  # type: ignore[arg-type]
            client, "http://ab", "c1", {"stateType": "stream", "streamState": []}
        )
