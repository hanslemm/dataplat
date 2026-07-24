"""Superset API client helpers used by CLI adapters."""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass

import httpx

from dataplat.core.errors import AuthError, ConfigError, ServiceError


@dataclass(frozen=True)
class SupersetAuthConfig:
    """Superset admin authentication configuration."""

    base_url: str
    username: str
    password: str


def get_auth_config_from_env() -> SupersetAuthConfig:
    """Load Superset auth configuration from environment variables."""
    base_url = (os.getenv("SUPERSET_BASE_URL") or "").rstrip("/")
    username = os.getenv("SUPERSET_ADMIN_USERNAME") or ""
    password = os.getenv("SUPERSET_ADMIN_PASSWORD") or ""

    if not base_url or not username or not password:
        raise ConfigError(
            "Set SUPERSET_BASE_URL, SUPERSET_ADMIN_USERNAME, SUPERSET_ADMIN_PASSWORD"
        )

    return SupersetAuthConfig(base_url=base_url, username=username, password=password)


def login(
    client: httpx.Client,
    base_url: str,
    username: str,
    password: str,
) -> str:
    """Authenticate against Superset and return access token."""
    login_url = f"{base_url}/api/v1/security/login"
    try:
        response = client.post(
            login_url,
            json={"username": username, "password": password, "provider": "db"},
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json() or {}
        access_token = payload.get("access_token")
        if not access_token:
            raise AuthError("No access_token in Superset login response")
        return access_token
    except httpx.HTTPStatusError as exc:
        raise AuthError(
            "Failed to login to Superset "
            f"({exc.response.status_code} {exc.response.reason_phrase})"
        ) from exc
    except (httpx.ConnectError, httpx.RemoteProtocolError) as exc:
        raise AuthError(f"Failed to connect to Superset login endpoint: {exc}") from exc
    except ValueError as exc:
        raise AuthError("Failed to parse Superset login response") from exc


def auth_headers(access_token: str) -> dict[str, str]:
    """Return default headers for Superset API calls."""
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def extract_id_list(items: object) -> list[int]:
    """Extract integer IDs from Superset API list payload fields."""
    if not isinstance(items, list):
        return []

    ids: list[int] = []
    for item in items:
        if isinstance(item, dict):
            item_id = item.get("id")
            if isinstance(item_id, int):
                ids.append(int(item_id))
        elif isinstance(item, int):
            ids.append(int(item))
    return ids


def user_role_ids(user: dict) -> list[int]:
    """Extract role IDs from a Superset user object."""
    return extract_id_list(user.get("roles"))


def user_group_ids(user: dict) -> list[int]:
    """Extract group IDs from a Superset user object."""
    return extract_id_list(user.get("groups"))


def _extract_results(payload: dict) -> tuple[list[dict], dict]:
    meta: dict = {}
    results: list[dict] = []

    if isinstance(payload.get("pagination"), dict):
        meta = {**payload.get("pagination", {})}

    raw = payload.get("result")
    if isinstance(raw, dict):
        results = raw.get("data") or raw.get("result") or []
        meta = {**raw, **meta}
    elif isinstance(raw, list):
        results = raw
    elif isinstance(payload.get("data"), list):
        results = payload.get("data") or []

    if "count" not in meta and isinstance(payload.get("count"), int):
        meta["count"] = payload.get("count")
    if "total" not in meta and isinstance(payload.get("total"), int):
        meta["total"] = payload.get("total")
    if "page" not in meta and isinstance(payload.get("page"), int):
        meta["page"] = payload.get("page")
    if "page_size" not in meta and isinstance(payload.get("page_size"), int):
        meta["page_size"] = payload.get("page_size")

    return results, meta


def _has_more(meta: dict, current_page: int, current_page_size: int) -> bool:
    total_pages = meta.get("total_pages")
    if isinstance(total_pages, int):
        page_idx = meta.get("page", current_page)
        return page_idx + 1 < total_pages

    count = meta.get("count")
    total = meta.get("total")
    total_items = (
        count if isinstance(count, int) else total if isinstance(total, int) else None
    )
    page_idx = meta.get("page", current_page)
    size = meta.get("page_size", current_page_size)

    if isinstance(total_items, int) and isinstance(size, int):
        return (page_idx + 1) * size < total_items

    return False


def iter_security_items(
    client: httpx.Client,
    base_url: str,
    access_token: str,
    resource_path: str,
) -> Iterable[dict]:
    """Iterate paginated security resources."""
    url = f"{base_url}/api/v1/security/{resource_path}/"
    page = 0
    page_size = 100
    headers = auth_headers(access_token)

    while True:
        response = client.get(
            url,
            params={
                "page": page,
                "page_size": page_size,
                "q": f"(page:{page},page_size:{page_size})",
            },
            headers=headers,
            timeout=60,
        )

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ServiceError(
                "Failed to list Superset security items "
                f"({exc.response.status_code} {exc.response.reason_phrase})"
            ) from exc

        payload = response.json() or {}
        results, meta = _extract_results(payload)
        if not results:
            return

        yield from results

        if _has_more(meta, page, page_size):
            page = int(meta.get("page", page)) + 1
            page_size = int(meta.get("page_size", page_size))
            continue

        if any(k in meta for k in ("total_pages", "count", "total")):
            return

        if len(results) < page_size:
            return

        page += 1


def iter_roles(client: httpx.Client, base_url: str, access_token: str) -> Iterable[dict]:
    """Iterate Superset roles."""
    return iter_security_items(client, base_url, access_token, "roles")


def iter_groups(
    client: httpx.Client,
    base_url: str,
    access_token: str,
) -> Iterable[dict]:
    """Iterate Superset groups."""
    return iter_security_items(client, base_url, access_token, "groups")


def iter_users(client: httpx.Client, base_url: str, access_token: str) -> Iterable[dict]:
    """Iterate Superset users."""
    return iter_security_items(client, base_url, access_token, "users")


def resolve_role_ids(
    client: httpx.Client,
    base_url: str,
    access_token: str,
    role_names: list[str],
) -> list[int]:
    """Resolve role names to IDs."""
    roles = list(iter_roles(client, base_url, access_token))
    role_map = {str(role.get("name")).lower(): role for role in roles}

    missing = [name for name in role_names if name.lower() not in role_map]
    if missing:
        available = ", ".join(sorted(r.get("name", "") for r in roles if r.get("name")))
        raise ConfigError(
            f"Unknown role(s): {', '.join(missing)}. Available roles: {available}"
        )

    return [int(role_map[name.lower()]["id"]) for name in role_names]


def resolve_group_ids(
    client: httpx.Client,
    base_url: str,
    access_token: str,
    group_names: list[str],
) -> list[int]:
    """Resolve group names to IDs."""
    groups = list(iter_groups(client, base_url, access_token))

    group_map = {str(group.get("name")).lower(): group for group in groups}
    missing = [name for name in group_names if name.lower() not in group_map]
    if missing:
        available = ", ".join(
            sorted(g.get("name", "") for g in groups if g.get("name"))
        )
        raise ConfigError(
            f"Unknown group(s): {', '.join(missing)}. Available groups: {available}"
        )

    return [int(group_map[name.lower()]["id"]) for name in group_names]


def create_user(
    client: httpx.Client,
    base_url: str,
    access_token: str,
    payload: dict,
) -> dict:
    """Create a Superset user."""
    url = f"{base_url}/api/v1/security/users/"
    response = client.post(url, json=payload, headers=auth_headers(access_token), timeout=60)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ServiceError(
            "Failed to create Superset user "
            f"({exc.response.status_code} {exc.response.reason_phrase})"
        ) from exc
    return response.json() if response.text else {}


def update_user(
    client: httpx.Client,
    base_url: str,
    access_token: str,
    user_id: int,
    payload: dict,
) -> dict:
    """Update a Superset user."""
    url = f"{base_url}/api/v1/security/users/{user_id}"
    response = client.put(url, json=payload, headers=auth_headers(access_token), timeout=60)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ServiceError(
            "Failed to update Superset user "
            f"({exc.response.status_code} {exc.response.reason_phrase})"
        ) from exc
    return response.json() if response.text else {}


def delete_user(
    client: httpx.Client,
    base_url: str,
    access_token: str,
    user_id: int,
) -> None:
    """Delete a Superset user."""
    url = f"{base_url}/api/v1/security/users/{user_id}"
    response = client.delete(url, headers=auth_headers(access_token), timeout=60)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ServiceError(
            "Failed to delete Superset user "
            f"({exc.response.status_code} {exc.response.reason_phrase})"
        ) from exc
