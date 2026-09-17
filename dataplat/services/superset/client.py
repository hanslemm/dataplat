"""Superset API client helpers used by CLI adapters."""

from __future__ import annotations

import os
import weakref
from collections.abc import Iterable
from dataclasses import dataclass

import httpx

from dataplat.core.errors import AuthError, ConfigError
from dataplat.services._http import build_client, error_detail, service_error


@dataclass(frozen=True)
class SupersetAuthConfig:
    """Superset admin authentication configuration."""

    base_url: str
    username: str
    password: str


# Tracing and error wording live in the seam every service shares; this module
# keeps only what is specific to Superset. ``build_client`` is re-exported
# because commands import it from here, and because it is what makes tracing
# hold by construction.

__all__ = [
    "auth_headers",
    "build_client",
    "create_user",
    "delete_user",
    "extract_id_list",
    "get_auth_config_from_env",
    "iter_groups",
    "iter_roles",
    "iter_security_items",
    "iter_users",
    "login",
    "resolve_group_ids",
    "resolve_role_ids",
    "update_user",
    "user_group_ids",
    "user_role_ids",
    "write_headers",
]


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
        detail = error_detail(exc.response)
        raise AuthError(
            "Failed to login to Superset "
            f"({exc.response.status_code} {exc.response.reason_phrase})"
            + (f": {detail}" if detail else "")
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


# Keyed by the httpx.Client instance, not the access token: the token is tied
# to the session cookie a specific client holds (see ``_csrf_token`` below),
# so that is the only key that means anything. Weak so a client that a caller
# is done with is not kept alive by this cache alone.
_CSRF_TOKENS: weakref.WeakKeyDictionary[httpx.Client, str | None] = (
    weakref.WeakKeyDictionary()
)


def _csrf_token(client: httpx.Client, base_url: str, access_token: str) -> str | None:
    """Superset's CSRF token, which its write endpoints require and the
    Bearer token is not.

    Fetched with the caller's own client, deliberately: Superset ties the
    token to the session cookie that this request establishes, and httpx
    carries cookies per client instance. Fetching it through a second client
    yields a token for a session the write is not part of, which Superset
    rejects with the same error as sending none at all.

    Returns None when the endpoint is absent or unhappy, so an instance that
    does not enforce CSRF still works -- the write simply goes without it.
    """
    try:
        response = client.get(
            f"{base_url}/api/v1/security/csrf_token/",
            headers=auth_headers(access_token),
            timeout=60,
        )
    except httpx.HTTPError:
        return None
    if response.status_code >= 400:
        return None
    try:
        token = (response.json() or {}).get("result")
    except ValueError:
        return None
    return str(token) if token else None


def write_headers(
    client: httpx.Client, base_url: str, access_token: str
) -> dict[str, str]:
    """Headers for a request that CHANGES something in Superset.

    Superset enforces CSRF on its write endpoints, and the Bearer token alone
    does not satisfy it -- every POST and PUT below ``/api/v1/`` outside
    ``/security/`` comes back "The CSRF token is missing". The token is bound
    to the session cookie established by fetching it, and httpx keeps cookies
    per client, so it MUST be fetched with the SAME client that then writes --
    do not "tidy" this into a helper with its own client, that silently
    breaks every write.

    Falls back to plain auth headers when no token can be had, so an instance
    with CSRF disabled keeps working.

    Fetched once per client and cached here: the duplicate flow writes many
    times in one run against one client, and re-fetching on every call would
    be a GET this endpoint does not need to see again. A client that never
    yields a token (CSRF disabled, or the endpoint is absent) is cached too,
    for the same reason -- "no token" is as much an answer as a token is.
    """
    headers = auth_headers(access_token)
    if client in _CSRF_TOKENS:
        token = _CSRF_TOKENS[client]
    else:
        token = _csrf_token(client, base_url, access_token)
        _CSRF_TOKENS[client] = token
    if token:
        return {**headers, "X-CSRFToken": token, "Referer": base_url}
    return headers


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
            raise service_error(exc.response, "list Superset security items") from exc

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


def iter_roles(
    client: httpx.Client, base_url: str, access_token: str
) -> Iterable[dict]:
    """Iterate Superset roles."""
    return iter_security_items(client, base_url, access_token, "roles")


def iter_groups(
    client: httpx.Client,
    base_url: str,
    access_token: str,
) -> Iterable[dict]:
    """Iterate Superset groups."""
    return iter_security_items(client, base_url, access_token, "groups")


def iter_users(
    client: httpx.Client, base_url: str, access_token: str
) -> Iterable[dict]:
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
    response = client.post(
        url, json=payload, headers=auth_headers(access_token), timeout=60
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise service_error(exc.response, "create Superset user") from exc
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
    response = client.put(
        url, json=payload, headers=auth_headers(access_token), timeout=60
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise service_error(exc.response, "update Superset user") from exc
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
        raise service_error(exc.response, "delete Superset user") from exc
