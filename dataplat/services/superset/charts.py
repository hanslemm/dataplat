"""Individual Superset charts, and running the query behind one.

``chart_data`` is how this area answers "does it actually work" -- and, with
both dashboards live at once, "do the two engines agree". A chart that renders
is not evidence; a chart that returns the same rows is.
"""

from __future__ import annotations

import httpx

from dataplat.services._http import raise_for_status
from dataplat.services.superset.client import auth_headers

__all__ = ["chart_data", "get_chart", "update_chart"]


def get_chart(
    client: httpx.Client, base_url: str, access_token: str, chart_id: int
) -> dict:
    """One chart in full, including ``query_context`` and ``dashboards``.

    ``dashboards`` is what the clone guard reads: a chart that belongs to more
    than the new dashboard was never cloned, and writing to it would edit the
    original.
    """
    response = client.get(
        f"{base_url}/api/v1/chart/{chart_id}",
        headers=auth_headers(access_token),
        timeout=60,
    )
    raise_for_status(response, "read Superset chart")
    result = (response.json() or {}).get("result")
    return dict(result) if isinstance(result, dict) else {}


def update_chart(
    client: httpx.Client,
    base_url: str,
    access_token: str,
    chart_id: int,
    payload: dict,
) -> dict:
    """Update a chart."""
    response = client.put(
        f"{base_url}/api/v1/chart/{chart_id}",
        json=payload,
        headers=auth_headers(access_token),
        timeout=120,
    )
    raise_for_status(response, "update Superset chart")
    return response.json() if response.text else {}


def chart_data(
    client: httpx.Client, base_url: str, access_token: str, query_context: dict
) -> list[dict]:
    """Run a query context and return the rows of its first result."""
    response = client.post(
        f"{base_url}/api/v1/chart/data",
        json=query_context,
        headers=auth_headers(access_token),
        timeout=300,
    )
    raise_for_status(response, "run Superset chart query")
    results = (response.json() or {}).get("result") or []
    if not results:
        return []
    return list((results[0] or {}).get("data") or [])
