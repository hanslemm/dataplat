"""Airbyte connector definition API helpers."""
from __future__ import annotations

import httpx

from dataplat.core.errors import ServiceError


def list_source_definitions(
    client: httpx.Client,
    base_url: str,
    workspace_id: str,
    limit: int = 100,
):
    """Paginated generator. GET /api/public/v1/workspaces/{workspace_id}/definitions/sources"""
    offset = 0
    while True:
        response = client.get(
            f"{base_url}/api/public/v1/workspaces/{workspace_id}/definitions/sources",
            params={"limit": limit, "offset": offset},
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            snippet = (response.text or "").strip()[:500]
            raise ServiceError(
                f"Failed to list source definitions (status={response.status_code}, body={snippet})"
            ) from exc

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
    """Paginated generator. GET /api/public/v1/workspaces/{workspace_id}/definitions/destinations"""
    offset = 0
    while True:
        response = client.get(
            f"{base_url}/api/public/v1/workspaces/{workspace_id}/definitions/destinations",
            params={"limit": limit, "offset": offset},
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            snippet = (response.text or "").strip()[:500]
            raise ServiceError(
                f"Failed to list destination definitions (status={response.status_code}, body={snippet})"
            ) from exc

        payload = response.json() or {}
        data = payload.get("data") or []
        if not data:
            return
        yield from data
        offset += limit
