"""Superset's dataset catalogue.

Its own endpoint, outside ``/security/``, so the security paginator does not
serve it. Read for one question only: what would break if a schema went away.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx

from dataplat.services._http import raise_for_status
from dataplat.services.superset.client import auth_headers

__all__ = ["iter_datasets"]

_PAGE_SIZE = 100


def iter_datasets(
    client: httpx.Client, base_url: str, access_token: str
) -> Iterator[dict]:
    """Yield every dataset, a page at a time.

    Paged rather than asked for in one request: 842 datasets on a middling
    instance, each carrying its SQL, is not a response worth demanding whole.
    """
    page = 0
    headers = auth_headers(access_token)
    while True:
        response = client.get(
            f"{base_url}/api/v1/dataset/",
            params={"q": f"(page:{page},page_size:{_PAGE_SIZE})"},
            headers=headers,
            timeout=60,
        )
        raise_for_status(response, "list Superset datasets")

        rows = (response.json() or {}).get("result") or []
        if not rows:
            return
        yield from rows
        if len(rows) < _PAGE_SIZE:
            return
        page += 1
