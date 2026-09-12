"""Airbyte connector definition API helpers."""

from __future__ import annotations

import httpx

from dataplat.services._http import raise_for_status


def list_source_definitions(
    client: httpx.Client,
    base_url: str,
    workspace_id: str,
    limit: int = 100,
):
    """Paginated generator.

    GET /api/public/v1/workspaces/{workspace_id}/definitions/sources
    """
    offset = 0
    while True:
        response = client.get(
            f"{base_url}/api/public/v1/workspaces/{workspace_id}/definitions/sources",
            params={"limit": limit, "offset": offset},
        )
        raise_for_status(response, "list source definitions")

        payload = response.json() or {}
        data = payload.get("data") or []
        if not data:
            return
        yield from data
        offset += limit


def list_destination_definitions(
    client: httpx.Client,
    base_url: str,
    workspace_id: str,
    limit: int = 100,
):
    """Paginated generator.

    GET /api/public/v1/workspaces/{workspace_id}/definitions/destinations
    """
    offset = 0
    while True:
        response = client.get(
            f"{base_url}/api/public/v1/workspaces/{workspace_id}/definitions/destinations",
            params={"limit": limit, "offset": offset},
        )
        raise_for_status(response, "list destination definitions")

        payload = response.json() or {}
        data = payload.get("data") or []
        if not data:
            return
        yield from data
        offset += limit
