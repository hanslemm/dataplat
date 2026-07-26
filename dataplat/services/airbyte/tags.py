"""Airbyte tag service helpers."""

from __future__ import annotations

import httpx

from dataplat.core.errors import ServiceError


def list_tags(client: httpx.Client, base_url: str) -> list[dict]:
    """List available Airbyte tags."""
    response = client.get(f"{base_url}/api/public/v1/tags", timeout=60)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        snippet = (response.text or "").strip()[:500]
        raise ServiceError(
            "Failed to list tags "
            f"(status={response.status_code}, body={snippet or 'empty'})"
        ) from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise ServiceError("Failed to parse tag list response") from exc

    if isinstance(payload, dict):
        data = payload.get("data") or payload.get("tags") or []
        if isinstance(data, list):
            return data
    if isinstance(payload, list):
        return payload
    return []


def create_tag(
    client: httpx.Client,
    base_url: str,
    name: str,
    workspace_id: str | None = None,
    color: str | None = None,
) -> dict:
    """Create an Airbyte tag."""
    payload: dict[str, str] = {"name": name}
    if workspace_id:
        payload["workspaceId"] = workspace_id
    if color:
        payload["color"] = color

    response = client.post(f"{base_url}/api/public/v1/tags", json=payload, timeout=60)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        snippet = (response.text or "").strip()[:500]
        raise ServiceError(
            "Failed to create tag "
            f"(status={response.status_code}, body={snippet or 'empty'})"
        ) from exc
    return response.json()


def tag_id(tag: dict) -> str | None:
    """Return a tag's id across API shape variants."""
    return tag.get("tagId") or tag.get("id")


def normalize_tag(tag: dict) -> dict:
    """Ensure a tag dict carries the ``tagId`` key the update API expects."""
    if "tagId" not in tag and "id" in tag:
        return {**tag, "tagId": tag.get("id")}
    return tag


class TagResolver:
    """Resolve tag names to tag objects, creating missing tags on demand.

    The workspace tag list is fetched lazily once and cached for the
    lifetime of the resolver (one CLI invocation).
    """

    def __init__(self, client: httpx.Client, base_url: str) -> None:
        self._client = client
        self._base_url = base_url
        self._cache: dict[tuple[str | None, str], dict] = {}
        self._primed = False

    def _prime(self) -> None:
        if self._primed:
            return
        for tag in list_tags(self._client, self._base_url):
            name = tag.get("name")
            workspace = tag.get("workspaceId") or tag.get("workspace_id")
            if name:
                self._cache[(workspace, name)] = normalize_tag(tag)
        self._primed = True

    def ensure(
        self,
        name: str,
        workspace_id: str | None,
        color: str | None = None,
    ) -> dict:
        key = (workspace_id, name)
        if key in self._cache:
            return self._cache[key]
        self._prime()
        if key in self._cache:
            return self._cache[key]
        created = normalize_tag(
            create_tag(self._client, self._base_url, name, workspace_id, color)
        )
        self._cache[key] = created
        return created


def merge_tags(existing: list[dict], additional: list[dict]) -> list[dict]:
    """Union two tag lists by tag id, preserving first-seen order."""
    merged: dict[str, dict] = {}
    for tag in existing + additional:
        tag = normalize_tag(tag)
        tid = tag_id(tag)
        if tid:
            merged[tid] = tag
    return list(merged.values())
