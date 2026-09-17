"""Superset's dataset catalogue.

Its own endpoint, outside ``/security/``, so the security paginator does not
serve it. Read for one question only: what would break if a schema went away.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx

from dataplat.core.errors import ServiceError
from dataplat.services._http import raise_for_status
from dataplat.services.superset.client import auth_headers, write_headers

__all__ = [
    "create_dataset",
    "get_dataset",
    "iter_datasets",
    "update_dataset",
]

_PAGE_SIZE = 100


def iter_datasets(
    client: httpx.Client,
    base_url: str,
    access_token: str,
    *,
    database_id: int | None = None,
) -> Iterator[dict]:
    """Yield every dataset, a page at a time, optionally on one database.

    Paged rather than asked for in one request: 842 datasets on a middling
    instance, each carrying its SQL, is not a response worth demanding whole.
    The filter is sent to Superset rather than applied here for the same
    reason -- the dozen datasets on one connection should not cost 842 rows.
    """
    page = 0
    headers = auth_headers(access_token)
    while True:
        query = f"(page:{page},page_size:{_PAGE_SIZE}"
        if database_id is not None:
            query += f",filters:!((col:database,opr:rel_o_m,value:{database_id}))"
        query += ")"
        response = client.get(
            f"{base_url}/api/v1/dataset/",
            params={"q": query},
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


def get_dataset(
    client: httpx.Client, base_url: str, access_token: str, dataset_id: int
) -> dict:
    """One dataset in full -- columns, metrics and SQL included.

    The list endpoint does not carry columns or metrics, and those are exactly
    what has to travel when a dataset is cloned onto another database.
    """
    response = client.get(
        f"{base_url}/api/v1/dataset/{dataset_id}",
        headers=auth_headers(access_token),
        timeout=60,
    )
    raise_for_status(response, "read Superset dataset")
    payload = response.json() or {}
    result = payload.get("result")
    return dict(result) if isinstance(result, dict) else {}


def create_dataset(
    client: httpx.Client, base_url: str, access_token: str, payload: dict
) -> int:
    """Create a dataset and return its id.

    POST accepts only database, catalog, schema, table_name and sql -- columns
    and metrics exist solely on the PUT schema, which is why creating a usable
    dataset is two calls and not one.

    Superset requires CSRF on this endpoint (measured live: a Bearer token
    alone gets ``400: The CSRF token is missing``), hence ``write_headers``
    rather than ``auth_headers``.
    """
    response = client.post(
        f"{base_url}/api/v1/dataset/",
        json=payload,
        headers=write_headers(client, base_url, access_token),
        timeout=120,
    )
    raise_for_status(response, "create Superset dataset")
    dataset_id = (response.json() or {}).get("id")
    if not isinstance(dataset_id, int):
        # A 2xx with no id is not a dataset we can use, and returning a
        # plausible-looking 0 would surface three calls later as a confusing
        # "not found" against an id nobody chose.
        raise ServiceError(
            "Superset accepted the dataset but returned no id "
            f"({response.status_code} {response.reason_phrase})"
        )
    return dataset_id


def update_dataset(
    client: httpx.Client,
    base_url: str,
    access_token: str,
    dataset_id: int,
    payload: dict,
) -> dict:
    """Update a dataset.

    The ``columns`` list is a FULL REPLACEMENT keyed by id: entries with an id
    update, entries without are created, and anything omitted is DELETED. The
    caller must send the whole list, which is why the creation path reads the
    synced columns back before writing.

    CSRF-enforced like ``create_dataset``, hence ``write_headers``.
    """
    response = client.put(
        f"{base_url}/api/v1/dataset/{dataset_id}",
        json=payload,
        headers=write_headers(client, base_url, access_token),
        timeout=120,
    )
    raise_for_status(response, "update Superset dataset")
    return response.json() if response.text else {}
