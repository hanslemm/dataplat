"""Generic CRUD helpers for airbyte sources/destinations.

The public API treats sources and destinations identically apart from the
path segment and the id field name, so both service modules delegate here.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx

from dataplat.core.errors import ServiceError


def _raise_for_status(response: httpx.Response, action: str) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        snippet = (response.text or "").strip()[:500]
        raise ServiceError(
            f"Failed to {action} (status={response.status_code}, body={snippet})"
        ) from exc


def list_resources(
    client: httpx.Client,
    base_url: str,
    resource: str,
    workspace_ids: list[str] | None = None,
    include_deleted: bool = False,
    limit: int = 100,
) -> Iterator[dict]:
    """Paginated generator yielding resource dicts."""
    offset = 0
    while True:
        params: dict = {
            "limit": limit,
            "offset": offset,
            "includeDeleted": str(include_deleted).lower(),
        }
        if workspace_ids:
            params["workspaceIds"] = ",".join(workspace_ids)

        response = client.get(
            f"{base_url}/api/public/v1/{resource}",
            params=params,
        )
        _raise_for_status(response, f"list {resource}")

        payload = response.json() or {}
        data = payload.get("data") or []
        if not data:
            return
        yield from data
        offset += limit


def get_resource(
    client: httpx.Client, base_url: str, resource: str, resource_id: str
) -> dict:
    """GET /api/public/v1/{resource}/{id}"""
    response = client.get(f"{base_url}/api/public/v1/{resource}/{resource_id}")
    _raise_for_status(response, f"get {resource[:-1]}")
    return response.json()


def create_resource(
    client: httpx.Client,
    base_url: str,
    resource: str,
    name: str,
    workspace_id: str,
    definition_id: str,
    configuration: dict,
) -> dict:
    """POST /api/public/v1/{resource}"""
    response = client.post(
        f"{base_url}/api/public/v1/{resource}",
        json={
            "name": name,
            "workspaceId": workspace_id,
            "definitionId": definition_id,
            "configuration": configuration,
        },
    )
    _raise_for_status(response, f"create {resource[:-1]}")
    return response.json()


def update_resource(
    client: httpx.Client,
    base_url: str,
    resource: str,
    resource_id: str,
    updates: dict,
) -> dict:
    """PATCH /api/public/v1/{resource}/{id}"""
    response = client.patch(
        f"{base_url}/api/public/v1/{resource}/{resource_id}",
        json=updates,
    )
    _raise_for_status(response, f"update {resource[:-1]}")
    return response.json()


def delete_resource(
    client: httpx.Client, base_url: str, resource: str, resource_id: str
) -> None:
    """DELETE /api/public/v1/{resource}/{id}, expect 204"""
    response = client.delete(f"{base_url}/api/public/v1/{resource}/{resource_id}")
    if response.status_code != 204:
        _raise_for_status(response, f"delete {resource[:-1]}")
