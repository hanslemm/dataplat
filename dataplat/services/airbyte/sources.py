"""Airbyte source API helpers (thin wrappers over the generic resource CRUD)."""

from __future__ import annotations

import httpx

from dataplat.services.airbyte._resource import (
    create_resource,
    delete_resource,
    get_resource,
    list_resources,
    update_resource,
)


def list_sources(
    client: httpx.Client,
    base_url: str,
    workspace_ids: list[str] | None = None,
    include_deleted: bool = False,
    limit: int = 100,
):
    """Paginated generator yielding source dicts."""
    return list_resources(
        client,
        base_url,
        "sources",
        workspace_ids=workspace_ids,
        include_deleted=include_deleted,
        limit=limit,
    )


def get_source(client: httpx.Client, base_url: str, source_id: str) -> dict:
    return get_resource(client, base_url, "sources", source_id)


def create_source(
    client: httpx.Client,
    base_url: str,
    name: str,
    workspace_id: str,
    definition_id: str,
    configuration: dict,
) -> dict:
    return create_resource(
        client, base_url, "sources", name, workspace_id, definition_id, configuration
    )


def update_source(
    client: httpx.Client, base_url: str, source_id: str, updates: dict
) -> dict:
    return update_resource(client, base_url, "sources", source_id, updates)


def delete_source(client: httpx.Client, base_url: str, source_id: str) -> None:
    delete_resource(client, base_url, "sources", source_id)
