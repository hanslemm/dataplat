"""The shared HTTP seam every service client goes through.

Tracing and error reporting used to be copied per service: two identical sets
of event hooks, two ``_raise_for_status`` helpers, eighteen inline
``response.text[:500]`` snippets, and three different sentences for the same
failure. This module owns all of it, so a new service inherits the behaviour
instead of reimplementing two thirds of it.
"""

from __future__ import annotations

import httpx
import pytest

from dataplat.core import trace
from dataplat.core.errors import ServiceError
from dataplat.services import _http

BASE_URL = "https://service.test"


def _respond(status: int, **body: object) -> httpx.Response:
    request = httpx.Request("GET", f"{BASE_URL}/thing")
    return httpx.Response(status, request=request, **body)  # type: ignore[arg-type]


# --- error detail ---------------------------------------------------------


def test_a_status_alone_is_reported_when_the_body_says_nothing() -> None:
    error = _http.service_error(_respond(404, json={}), "list things")

    assert str(error) == "Failed to list things (404 Not Found)"


def test_the_servers_message_is_quoted() -> None:
    error = _http.service_error(
        _respond(404, json={"message": "workspace ws-1 not found"}), "list things"
    )

    assert (
        str(error) == "Failed to list things (404 Not Found): workspace ws-1 not found"
    )


def test_a_per_field_rejection_is_flattened() -> None:
    error = _http.service_error(
        _respond(
            422, json={"message": {"password": ["Must be at least 10 characters"]}}
        ),
        "create user",
    )

    assert "password: Must be at least 10 characters" in str(error)


def test_a_body_that_is_not_the_expected_envelope_is_still_shown() -> None:
    """A gateway answers with HTML, and that is the whole diagnosis."""
    error = _http.service_error(
        _respond(502, text="<html>\n  <body>gateway down</body>\n</html>"),
        "list things",
    )

    assert "gateway down" in str(error)
    # One line: Rich prints these, and a smeared error is unreadable.
    assert "\n" not in str(error)


def test_a_long_body_is_capped() -> None:
    error = _http.service_error(_respond(500, text="x" * 5_000), "list things")

    assert len(str(error)) < 700
    assert str(error).endswith("…")


def test_the_detail_never_carries_a_request_header() -> None:
    """Only the body is read, so a bearer token cannot reach an error message."""
    request = httpx.Request(
        "GET", f"{BASE_URL}/thing", headers={"Authorization": "Bearer s3cret-token"}
    )
    response = httpx.Response(403, request=request, json={"message": "nope"})

    assert "s3cret-token" not in str(_http.service_error(response, "list things"))


# --- raise_for_status -----------------------------------------------------


def test_raise_for_status_passes_a_success_through() -> None:
    _http.raise_for_status(_respond(200, json={"ok": True}), "list things")


def test_raise_for_status_raises_the_shared_error() -> None:
    with pytest.raises(ServiceError) as excinfo:
        _http.raise_for_status(_respond(409, json={"message": "conflict"}), "tag it")

    assert str(excinfo.value) == "Failed to tag it (409 Conflict): conflict"


# --- the traced client ----------------------------------------------------


def test_build_client_wires_the_trace_hooks() -> None:
    with _http.build_client() as client:
        assert client.event_hooks["request"]
        assert client.event_hooks["response"]


def test_build_client_passes_its_arguments_through() -> None:
    """Airbyte needs a timeout and a retrying transport; the seam must not eat them."""
    with _http.build_client(follow_redirects=False, timeout=httpx.Timeout(30.0)) as c:
        assert c.follow_redirects is False
        assert c.timeout.read == 30.0


def test_the_traced_client_reports_method_url_and_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={}, request=request)

    with trace.verbose():
        client = _http.build_client(transport=httpx.MockTransport(handler))
        client.get(f"{BASE_URL}/thing")
        client.close()

    err = capsys.readouterr().err
    assert "GET" in err
    assert f"{BASE_URL}/thing" in err
    assert "200" in err
