"""Superset's dashboards: reading them, and asking Superset to copy one.

The copy endpoint is Superset's own, and it is used rather than rebuilt for
one reason: cloning the charts means rewriting every ``chartId`` inside the
layout tree, and Superset already does that correctly. What it does NOT do is
move the charts to different datasets -- the clones point at the originals'.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx

from dataplat.core.errors import ServiceError
from dataplat.services._http import raise_for_status
from dataplat.services.superset.client import auth_headers

__all__ = [
    "copy_dashboard",
    "dashboard_charts",
    "get_dashboard",
    "iter_dashboards",
    "update_dashboard",
]

_PAGE_SIZE = 100


def iter_dashboards(
    client: httpx.Client, base_url: str, access_token: str
) -> Iterator[dict]:
    """Yield every dashboard, a page at a time."""
    page = 0
    headers = auth_headers(access_token)
    while True:
        response = client.get(
            f"{base_url}/api/v1/dashboard/",
            params={"q": f"(page:{page},page_size:{_PAGE_SIZE})"},
            headers=headers,
            timeout=60,
        )
        raise_for_status(response, "list Superset dashboards")
        rows = (response.json() or {}).get("result") or []
        if not rows:
            return
        yield from rows
        if len(rows) < _PAGE_SIZE:
            return
        page += 1


def get_dashboard(
    client: httpx.Client, base_url: str, access_token: str, dashboard: str | int
) -> dict:
    """One dashboard in full.

    Takes an id or a slug because Superset's own URLs do, and the id is not
    what anyone has in front of them when they are looking at a dashboard.
    """
    response = client.get(
        f"{base_url}/api/v1/dashboard/{dashboard}",
        headers=auth_headers(access_token),
        timeout=60,
    )
    raise_for_status(response, "read Superset dashboard")
    result = (response.json() or {}).get("result")
    return dict(result) if isinstance(result, dict) else {}


def dashboard_charts(
    client: httpx.Client, base_url: str, access_token: str, dashboard: str | int
) -> list[dict]:
    """The charts on a dashboard.

    Carries ``form_data`` but not ``query_context``; a caller that needs the
    latter reads each chart with ``charts.get_chart``.
    """
    response = client.get(
        f"{base_url}/api/v1/dashboard/{dashboard}/charts",
        headers=auth_headers(access_token),
        timeout=60,
    )
    raise_for_status(response, "read Superset dashboard charts")
    return list((response.json() or {}).get("result") or [])


def copy_dashboard(
    client: httpx.Client,
    base_url: str,
    access_token: str,
    dashboard_id: int,
    payload: dict,
) -> int:
    """Copy a dashboard and return the new id.

    ``payload`` must carry ``json_metadata`` with ``positions`` merged in --
    see ``repoint.copy_metadata``. Superset reads the layout out of the
    metadata it is SENT in order to remap ``chartId`` to the cloned charts, so
    metadata passed through as received produces a copy still wired to the
    original's charts.
    """
    response = client.post(
        f"{base_url}/api/v1/dashboard/{dashboard_id}/copy/",
        json=payload,
        headers=auth_headers(access_token),
        timeout=300,
    )
    raise_for_status(response, "copy Superset dashboard")
    result = (response.json() or {}).get("result") or {}
    new_id = result.get("id")
    if new_id is None:
        raise ServiceError("copy Superset dashboard: response missing id")
    return int(new_id)


def update_dashboard(
    client: httpx.Client,
    base_url: str,
    access_token: str,
    dashboard_id: int,
    payload: dict,
) -> dict:
    """Update a dashboard."""
    response = client.put(
        f"{base_url}/api/v1/dashboard/{dashboard_id}",
        json=payload,
        headers=auth_headers(access_token),
        timeout=120,
    )
    raise_for_status(response, "update Superset dashboard")
    return response.json() if response.text else {}
