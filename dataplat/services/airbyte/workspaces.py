"""Airbyte workspace API helpers."""

from __future__ import annotations

import httpx

from dataplat.core.errors import ServiceError


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
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            snippet = (response.text or "").strip()[:500]
            raise ServiceError(
                "Failed to list workspaces "
                f"(status={response.status_code}, body={snippet})"
            ) from exc

        payload = response.json() or {}
        data = payload.get("data") or []
        if not data:
            return
        yield from data
        offset += limit


def get_workspace(client: httpx.Client, base_url: str, workspace_id: str) -> dict:
    """GET /api/public/v1/workspaces/{workspace_id}"""
    response = client.get(f"{base_url}/api/public/v1/workspaces/{workspace_id}")
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        snippet = (response.text or "").strip()[:500]
        raise ServiceError(
            f"Failed to get workspace (status={response.status_code}, body={snippet})"
        ) from exc
    return response.json()
