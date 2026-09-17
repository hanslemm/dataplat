"""Superset's database connections, and running a statement through one.

``execute_sql`` exists for one question: would this SQL run on the other
warehouse? Superset already holds the credentials for every connection it
manages, so asking it is the only way to find out without `dp` growing a
second set of warehouse credentials it does not otherwise need.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx

from dataplat.core.errors import ConfigError, ValidationError
from dataplat.core.sql_text import strip_comments, strip_literals
from dataplat.services._http import raise_for_status, service_error
from dataplat.services.superset.client import auth_headers, write_headers

__all__ = ["execute_sql", "iter_databases", "resolve_database_id", "validation_query"]

_PAGE_SIZE = 100


def iter_databases(
    client: httpx.Client, base_url: str, access_token: str
) -> Iterator[dict]:
    """Yield every database connection, a page at a time."""
    page = 0
    headers = auth_headers(access_token)
    while True:
        response = client.get(
            f"{base_url}/api/v1/database/",
            params={"q": f"(page:{page},page_size:{_PAGE_SIZE})"},
            headers=headers,
            timeout=60,
        )
        raise_for_status(response, "list Superset databases")
        rows = (response.json() or {}).get("result") or []
        if not rows:
            return
        yield from rows
        if len(rows) < _PAGE_SIZE:
            return
        page += 1


def resolve_database_id(
    client: httpx.Client, base_url: str, access_token: str, name: str
) -> int:
    """Resolve a connection name to its id.

    Names the available connections on failure, the way ``resolve_role_ids``
    does: "unknown database" without the list sends someone to the UI to read
    a name they could have been told.
    """
    databases = list(iter_databases(client, base_url, access_token))
    for database in databases:
        if str(database.get("database_name", "")).lower() == name.lower():
            return int(database["id"])

    available = ", ".join(
        sorted(
            str(d.get("database_name", "")) for d in databases if d.get("database_name")
        )
    )
    raise ConfigError(f"Unknown database: {name}. Available databases: {available}")


def validation_query(sql: str) -> str:
    """Wrap ``sql`` so it is parsed and planned but returns nothing.

    Refuses input carrying a statement separator. The wrapper is string
    interpolation into SQL, and this Superset executes multi-statement input,
    returning the last statement's result -- so a dataset whose SQL closes the
    wrapper's paren and opens a statement of its own would run that statement
    on the target warehouse under the operator's credentials. Measured, not
    theorised: sending the dataset's SQL to a live instance let a planted
    second statement's result come back instead of the first's. The separator
    is looked for with comments and string literals removed, so a ``;`` inside
    a literal is not mistaken for one.

    The trailing semicolon is still stripped: dataset SQL routinely ends with
    one and it is not an escape -- there is nothing after it for a second
    statement to be.
    """
    body = sql.strip().rstrip(";")
    if ";" in strip_literals(strip_comments(body)):
        raise ValidationError(
            "This dataset's SQL contains a statement separator (';') and is "
            "refused: the validation wrapper is string interpolation, and this "
            "Superset runs multi-statement SQL, so anything after the "
            "separator would execute on the target warehouse under the "
            "operator's credentials. Rewrite the dataset's SQL as a single "
            "statement."
        )
    return f"SELECT * FROM (\n{body}\n) AS dp_validate LIMIT 0"


def execute_sql(
    client: httpx.Client,
    base_url: str,
    access_token: str,
    *,
    database_id: int,
    sql: str,
    schema: str | None = None,
) -> dict:
    """Run ``sql`` synchronously through SQL Lab and return the payload.

    The engine's own message is what reaches the caller. ``type "jsonb" does
    not exist`` is more useful than any category this could sort it into, and
    a classifier here would be a third hand-maintained list to keep correct.

    SQL Lab enforces CSRF where ``chart/data`` does not, so this is the one
    endpoint in this module that needs the extra header pair -- a Bearer
    token alone gets ``400: The CSRF token is missing`` before the SQL is
    even looked at.
    """
    payload: dict[str, object] = {
        "database_id": database_id,
        "sql": sql,
        "runAsync": False,
        "select_as_cta": False,
    }
    if schema:
        payload["schema"] = schema

    headers = write_headers(client, base_url, access_token)

    response = client.post(
        f"{base_url}/api/v1/sqllab/execute/",
        json=payload,
        headers=headers,
        timeout=300,
    )
    if response.status_code >= 400:
        raise service_error(
            response,
            "run SQL on the target database",
            detail=_sqllab_detail(response),
        )
    return response.json() if response.text else {}


def _sqllab_detail(response: httpx.Response) -> str | None:
    """SQL Lab's own message, which it nests under ``errors[]``.

    ``error_detail`` knows the ``{"message": ...}`` envelope and not this
    one, and against a body it does not recognise it falls back to the raw
    text -- where the engine's message arrives with its quotes escaped.
    Returning None when there is nothing here lets the shared extraction
    have the last word.
    """
    try:
        body = response.json() or {}
    except ValueError:
        return None
    errors = body.get("errors")
    if not isinstance(errors, list) or not errors:
        return None
    message = (errors[0] or {}).get("message")
    return str(message) if message else None
