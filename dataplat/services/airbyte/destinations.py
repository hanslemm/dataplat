"""Airbyte destination API helpers (thin wrappers over the generic resource CRUD)."""

from __future__ import annotations

import httpx

from dataplat.services.airbyte._resource import (
    create_resource,
    delete_resource,
    get_resource,
    list_resources,
    update_resource,
)


def list_destinations(
    client: httpx.Client,
    base_url: str,
    workspace_ids: list[str] | None = None,
    include_deleted: bool = False,
    limit: int = 100,
):
    """Paginated generator yielding destination dicts."""
    return list_resources(
        client,
        base_url,
        "destinations",
        workspace_ids=workspace_ids,
        include_deleted=include_deleted,
        limit=limit,
    )


def get_destination(
    client: httpx.Client, base_url: str, destination_id: str
) -> dict:
    return get_resource(client, base_url, "destinations", destination_id)


def create_destination(
    client: httpx.Client,
    base_url: str,
    name: str,
    workspace_id: str,
    definition_id: str,
    configuration: dict,
) -> dict:
    return create_resource(
        client,
        base_url,
        "destinations",
        name,
        workspace_id,
        definition_id,
        configuration,
    )


def update_destination(
    client: httpx.Client, base_url: str, destination_id: str, updates: dict
) -> dict:
    return update_resource(client, base_url, "destinations", destination_id, updates)


def delete_destination(
    client: httpx.Client, base_url: str, destination_id: str
) -> None:
    delete_resource(client, base_url, "destinations", destination_id)
