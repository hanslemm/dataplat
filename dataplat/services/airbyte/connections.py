"""Airbyte connection service helpers."""

from __future__ import annotations

import httpx

from dataplat.core.errors import ServiceError


def list_connections(client: httpx.Client, base_url: str, limit: int = 100):
    """List Airbyte connections with pagination."""
    offset = 0
    while True:
        response = client.get(
            f"{base_url}/api/public/v1/connections",
            params={"limit": limit, "offset": offset, "includeDeleted": "false"},
            timeout=60,
        )
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location", "")
            raise ServiceError(
                "Connections request redirected by gateway "
                f"(status={response.status_code}, location={location or 'unknown'})"
            )

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            snippet = (response.text or "").strip()[:500]
            raise ServiceError(
                "Failed to list connections "
                f"(status={response.status_code}, body={snippet})"
            ) from exc

        content_type = response.headers.get("content-type", "").lower()
        if "application/json" not in content_type:
            snippet = (response.text or "").strip()[:200]
            raise ServiceError(
                "Unexpected response from connections endpoint "
                f"(content-type={content_type or 'unknown'}, body={snippet or 'empty'})"
            )

        try:
            payload = response.json() or {}
        except ValueError as exc:
            snippet = (response.text or "").strip()[:200]
            raise ServiceError(
                f"Failed to parse connections payload (body={snippet or 'empty'})"
            ) from exc

        data = payload.get("data") or []
        if not data:
            return

        yield from data
        offset += limit


def get_connection(client: httpx.Client, base_url: str, connection_id: str) -> dict:
    """Get a single connection by ID."""
    response = client.get(
        f"{base_url}/api/public/v1/connections/{connection_id}",
        timeout=60,
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        snippet = (response.text or "").strip()[:500]
        raise ServiceError(
            f"Failed to get connection (status={response.status_code}, body={snippet})"
        ) from exc
    return response.json()


def connection_has_active_streams(connection_detail: dict) -> bool:
    """Return True when a connection has at least one selected stream."""
    catalog = connection_detail.get("syncCatalog") or connection_detail.get("catalog")
    if not isinstance(catalog, dict):
        return False

    streams = catalog.get("streams") or []
    if not isinstance(streams, list):
        return False

    for stream_entry in streams:
        if not isinstance(stream_entry, dict):
            continue

        config = stream_entry.get("config") or {}
        if isinstance(config, dict) and config.get("selected") is True:
            return True

        if stream_entry.get("selected") is True:
            return True

        stream = stream_entry.get("stream")
        if isinstance(stream, dict) and stream.get("selected") is True:
            return True

    return False


def patch_connection(
    client: httpx.Client,
    base_url: str,
    connection_id: str,
    updates: dict,
) -> dict:
    """Patch a connection using public API endpoint."""
    response = client.patch(
        f"{base_url}/api/public/v1/connections/{connection_id}",
        json=updates,
        timeout=60,
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        snippet = (response.text or "").strip()[:500]
        raise ServiceError(
            "Failed to update connection "
            f"(status={response.status_code}, body={snippet or 'empty'})"
        ) from exc
    return response.json()


def build_web_backend_updates(updates: dict) -> dict:
    """Map public update fields to web_backend update payload."""
    mapped: dict = {}

    for key in (
        "name",
        "status",
        "dataResidency",
        "namespaceDefinition",
        "namespaceFormat",
        "prefix",
        "nonBreakingSchemaUpdatesBehavior",
        "tags",
    ):
        if key in updates:
            mapped[key] = updates[key]

    schedule = updates.get("schedule") or {}
    schedule_type = schedule.get("scheduleType")
    if schedule_type:
        mapped["scheduleType"] = schedule_type
        if schedule_type == "cron":
            cron_payload = {}
            if schedule.get("cronExpression"):
                cron_payload["cronExpression"] = schedule["cronExpression"]
            if schedule.get("cronTimeZone"):
                cron_payload["cronTimeZone"] = schedule["cronTimeZone"]
            if cron_payload:
                mapped["scheduleData"] = {"cron": cron_payload}
                mapped["cron"] = cron_payload
        elif schedule_type == "manual":
            mapped["scheduleData"] = {}

    return mapped


def update_connection_web_backend(
    client: httpx.Client,
    base_url: str,
    connection_id: str,
    updates: dict,
) -> dict:
    """Update a connection via web_backend endpoint."""
    payload = {"connectionId": connection_id, **updates}
    response = client.post(
        f"{base_url}/api/v1/web_backend/connections/update",
        json=payload,
        timeout=60,
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        snippet = (response.text or "").strip()[:500]
        raise ServiceError(
            "Failed to update connection via web_backend "
            f"(status={response.status_code}, body={snippet or 'empty'})"
        ) from exc
    return response.json()


def get_connection_state(
    client: httpx.Client,
    base_url: str,
    connection_id: str,
) -> dict:
    """Read a connection's saved sync state (config API /v1/state/get)."""
    response = client.post(
        f"{base_url}/api/v1/state/get",
        json={"connectionId": connection_id},
        timeout=60,
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        snippet = (response.text or "").strip()[:500]
        raise ServiceError(
            "Failed to get connection state "
            f"(status={response.status_code}, body={snippet or 'empty'})"
        ) from exc
    return response.json()


def update_connection_state(
    client: httpx.Client,
    base_url: str,
    connection_id: str,
    connection_state: dict,
) -> dict:
    """Write a connection's sync state (config API /v1/state/create_or_update)."""
    payload = {"connectionId": connection_id, "connectionState": connection_state}
    response = client.post(
        f"{base_url}/api/v1/state/create_or_update",
        json=payload,
        timeout=60,
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        snippet = (response.text or "").strip()[:500]
        raise ServiceError(
            "Failed to update connection state "
            f"(status={response.status_code}, body={snippet or 'empty'})"
        ) from exc
    return response.json()


def trigger_sync_job(client: httpx.Client, base_url: str, connection_id: str) -> dict:
    """Trigger an Airbyte sync job."""
    response = client.post(
        f"{base_url}/api/public/v1/jobs",
        json={"connectionId": connection_id, "jobType": "sync"},
        timeout=60,
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        snippet = (response.text or "").strip()[:500]
        raise ServiceError(
            "Failed to trigger sync job "
            f"(status={response.status_code}, body={snippet or 'empty'})"
        ) from exc
    return response.json()


def get_job(client: httpx.Client, base_url: str, job_id: str) -> dict:
    """Get Airbyte job details by ID."""
    response = client.get(f"{base_url}/api/public/v1/jobs/{job_id}", timeout=60)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        snippet = (response.text or "").strip()[:500]
        raise ServiceError(
            "Failed to get job status "
            f"(status={response.status_code}, body={snippet or 'empty'})"
        ) from exc
    return response.json()


def create_connection(
    client: httpx.Client,
    base_url: str,
    source_id: str,
    destination_id: str,
    name: str | None = None,
    schedule: dict | None = None,
    namespace_definition: str | None = None,
    status: str | None = None,
    configurations: dict | None = None,
) -> dict:
    """POST /api/public/v1/connections.

    Payload: {sourceId, destinationId, ...optional fields}
    """
    payload: dict = {"sourceId": source_id, "destinationId": destination_id}
    if name is not None:
        payload["name"] = name
    if schedule is not None:
        payload["schedule"] = schedule
    if namespace_definition is not None:
        payload["namespaceDefinition"] = namespace_definition
    if status is not None:
        payload["status"] = status
    if configurations is not None:
        payload["configurations"] = configurations

    response = client.post(
        f"{base_url}/api/public/v1/connections",
        json=payload,
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        snippet = (response.text or "").strip()[:500]
        raise ServiceError(
            "Failed to create connection "
            f"(status={response.status_code}, body={snippet})"
        ) from exc
    return response.json()


def delete_connection(client: httpx.Client, base_url: str, connection_id: str) -> None:
    """DELETE /api/public/v1/connections/{connection_id}, expect 204"""
    response = client.delete(f"{base_url}/api/public/v1/connections/{connection_id}")
    if response.status_code != 204:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            snippet = (response.text or "").strip()[:500]
            raise ServiceError(
                "Failed to delete connection "
                f"(status={response.status_code}, body={snippet})"
            ) from exc
