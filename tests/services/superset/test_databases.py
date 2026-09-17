"""Superset's database connections, and running SQL through one."""

from __future__ import annotations

import httpx
import pytest

from dataplat.core.errors import ConfigError, ServiceError, ValidationError
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


def test_the_live_exploit_payload_is_refused() -> None:
    # Reproduced live: this wraps to two syntactically complete statements,
    # and this Superset accepted it and returned the SECOND statement's
    # result (columns came back as ['pwned']) -- not a theoretical injection.
    # Pinned verbatim.
    sql = "select 1) AS x; SELECT 424242 AS pwned FROM (select 1"
    with pytest.raises(ValidationError):
        validation_query(sql)


def test_an_embedded_separator_is_refused() -> None:
    with pytest.raises(ValidationError):
        validation_query("select 1; select 2")


def test_a_trailing_semicolon_alone_is_still_accepted() -> None:
    # The common real case: dataset SQL ending with one must not regress
    # into a refusal.
    wrapped = validation_query("select 1 from t;")
    assert "select 1 from t" in wrapped


def test_a_semicolon_inside_a_string_literal_is_not_a_separator() -> None:
    # Without stripping literals first, this would be a false refusal that
    # breaks a legitimate dataset whose label text contains a semicolon.
    wrapped = validation_query("select 'a;b' as x from t")
    assert "'a;b'" in wrapped


def test_a_semicolon_inside_a_comment_is_not_a_separator() -> None:
    wrapped = validation_query("select 1 -- note; here\nfrom t")
    assert "select 1" in wrapped


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


def test_execute_sql_sends_the_csrf_token_and_referer() -> None:
    # SQL Lab enforces CSRF where /chart/data does not (verified live); the
    # POST is rejected without both of these, not just the token.
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/security/csrf_token/"):
            return httpx.Response(200, json={"result": "csrf-abc"}, request=request)
        captured["post"] = request
        return httpx.Response(
            200, json={"status": "success", "data": []}, request=request
        )

    with httpx.Client(transport=_serve(handler)) as client:
        execute_sql(client, BASE_URL, "tok", database_id=2, sql="select 1")

    post = captured["post"]
    assert post.headers["x-csrftoken"] == "csrf-abc"
    assert post.headers["referer"] == BASE_URL


def test_the_csrf_token_is_fetched_before_the_sqllab_post() -> None:
    # The token is bound to the session cookie the GET establishes; fetched
    # out of order (or through a different client) it would be a token for a
    # session the POST is not part of.
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/security/csrf_token/"):
            return httpx.Response(200, json={"result": "csrf-abc"}, request=request)
        return httpx.Response(
            200, json={"status": "success", "data": []}, request=request
        )

    with httpx.Client(transport=_serve(handler)) as client:
        execute_sql(client, BASE_URL, "tok", database_id=2, sql="select 1")

    assert calls == ["/api/v1/security/csrf_token/", "/api/v1/sqllab/execute/"]


def test_a_missing_csrf_endpoint_does_not_break_execute_sql() -> None:
    # An instance with CSRF disabled (or an older API surface) still works:
    # the POST goes out without the header instead of the call failing.
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/security/csrf_token/"):
            return httpx.Response(404, request=request)
        captured["post"] = request
        return httpx.Response(
            200, json={"status": "success", "data": []}, request=request
        )

    with httpx.Client(transport=_serve(handler)) as client:
        result = execute_sql(client, BASE_URL, "tok", database_id=2, sql="select 1")

    assert result["status"] == "success"
    assert "x-csrftoken" not in captured["post"].headers


def test_a_500_with_the_errors_envelope_still_surfaces_the_engine_message() -> None:
    # Live behaviour against Redshift: a rejected statement comes back as
    # 500, not 400, but SQL Lab nests the message the same way either time.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/security/csrf_token/"):
            return httpx.Response(200, json={"result": "csrf-abc"}, request=request)
        return httpx.Response(
            500,
            json={
                "errors": [
                    {
                        "message": (
                            'redshift error: relation "definitely_not_a_table_xyz" '
                            "does not exist"
                        )
                    }
                ]
            },
            request=request,
        )

    with (
        httpx.Client(transport=_serve(handler)) as client,
        pytest.raises(ServiceError) as excinfo,
    ):
        execute_sql(client, BASE_URL, "tok", database_id=2, sql="select 1")

    assert "definitely_not_a_table_xyz" in str(excinfo.value)
