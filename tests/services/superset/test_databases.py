"""Superset's database connections, and running SQL through one."""

from __future__ import annotations

import httpx
import pytest

from dataplat.core.errors import ConfigError, ServiceError
from dataplat.services.superset.databases import (
    _sqllab_detail,
    execute_sql,
    resolve_database_id,
    validation_query,
)

BASE_URL = "https://superset.test"

DATABASES = [
    {"id": 1, "database_name": "DataOcean"},
    {"id": 2, "database_name": "BetterData"},
]


def _serve(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _list_databases(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"result": DATABASES, "count": 2}, request=request)


def test_a_database_resolves_by_name_case_insensitively() -> None:
    with httpx.Client(transport=_serve(_list_databases)) as client:
        assert resolve_database_id(client, BASE_URL, "tok", "betterdata") == 2


def test_an_unknown_database_names_the_ones_that_exist() -> None:
    with (
        httpx.Client(transport=_serve(_list_databases)) as client,
        pytest.raises(ConfigError) as excinfo,
    ):
        resolve_database_id(client, BASE_URL, "tok", "Warehouse")

    message = str(excinfo.value)
    assert "Warehouse" in message
    assert "DataOcean" in message and "BetterData" in message


def test_the_validation_query_asks_for_no_rows() -> None:
    wrapped = validation_query("select 1 from t")
    assert "limit 0" in wrapped.lower()
    assert "select 1 from t" in wrapped


def test_a_trailing_semicolon_cannot_break_the_wrapper() -> None:
    # A dataset's SQL routinely ends with one, and it would close the
    # subquery early: `... FROM (select 1;) AS dp_validate`.
    assert ";" not in validation_query("select 1 from t;")


def test_a_rejected_statement_carries_the_engine_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"errors": [{"message": 'relation "borg.events" does not exist'}]},
            request=request,
        )

    with (
        httpx.Client(transport=_serve(handler)) as client,
        pytest.raises(ServiceError) as excinfo,
    ):
        execute_sql(client, BASE_URL, "tok", database_id=2, sql="select 1")

    assert 'relation "borg.events" does not exist' in str(excinfo.value)


def test_sqllab_detail_is_none_without_an_errors_envelope() -> None:
    """A body that is not SQL Lab's own shape (a generic gateway error, say)
    has nothing for this helper to extract; the shared fallback should get a
    turn instead."""
    request = httpx.Request("POST", f"{BASE_URL}/api/v1/sqllab/execute/")
    response = httpx.Response(
        500, json={"message": "database is unreachable"}, request=request
    )

    assert _sqllab_detail(response) is None


def test_a_failure_without_the_errors_envelope_still_surfaces_a_reason() -> None:
    """No ``errors[]`` in the body -- ``_sqllab_detail`` has nothing to give,
    so ``service_error``'s own fallback (``error_detail``) must engage and
    the caller still sees a reason, not a bare status line."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500, json={"message": "database is unreachable"}, request=request
        )

    with (
        httpx.Client(transport=_serve(handler)) as client,
        pytest.raises(ServiceError) as excinfo,
    ):
        execute_sql(client, BASE_URL, "tok", database_id=2, sql="select 1")

    assert "database is unreachable" in str(excinfo.value)


def test_a_statement_that_runs_returns_the_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": "success", "data": []}, request=request
        )

    with httpx.Client(transport=_serve(handler)) as client:
        assert (
            execute_sql(client, BASE_URL, "tok", database_id=2, sql="select 1")[
                "status"
            ]
            == "success"
        )
