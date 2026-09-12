"""Airbyte workspace API helpers."""

from __future__ import annotations

import httpx

from dataplat.services._http import raise_for_status


def list_workspaces(
    client: httpx.Client,
    base_url: str,
    limit: int = 100,
):
    """Paginated generator. GET /api/public/v1/workspaces"""
    offset = 0
    while True:
        response = client.get(
            f"{base_url}/api/public/v1/workspaces",
            params={"limit": limit, "offset": offset},
        )
        raise_for_status(response, "list workspaces")

        payload = response.json() or {}
        data = payload.get("data") or []
        if not data:
            return
        yield from data
        offset += limit


def get_workspace(client: httpx.Client, base_url: str, workspace_id: str) -> dict:
    """GET /api/public/v1/workspaces/{workspace_id}"""
    response = client.get(f"{base_url}/api/public/v1/workspaces/{workspace_id}")
    raise_for_status(response, "get workspace")
    return response.json()
