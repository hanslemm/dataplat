from __future__ import annotations

import re
from collections.abc import Callable

import httpx
import pytest

from dataplat.core import trace
from dataplat.core.errors import ConfigError, ServiceError
from dataplat.services.superset import client


def test_get_auth_config_from_env_requires_all(monkeypatch) -> None:
    monkeypatch.delenv("SUPERSET_BASE_URL", raising=False)
    monkeypatch.delenv("SUPERSET_ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("SUPERSET_ADMIN_PASSWORD", raising=False)

    with pytest.raises(ConfigError):
        client.get_auth_config_from_env()


def test_auth_headers() -> None:
    headers = client.auth_headers("token-123")
    assert headers["Authorization"] == "Bearer token-123"


def test_iter_security_items_paginates() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "0"))
        calls["count"] += 1
        if page == 0:
            return httpx.Response(
                200,
                json={
                    "result": [{"id": 1}],
                    "pagination": {"page": 0, "page_size": 1, "total_pages": 2},
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "result": [{"id": 2}],
                "pagination": {"page": 1, "page_size": 1, "total_pages": 2},
            },
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        rows = list(
            client.iter_security_items(c, "https://superset.test", "tok", "roles")
        )

    assert [row["id"] for row in rows] == [1, 2]
    assert calls["count"] == 2


# --- request tracing ------------------------------------------------------

BASE_URL = "https://superset.test"
ACCESS_TOKEN = "s3cret-access-token-value"


def test_build_client_wires_the_trace_hooks() -> None:
    """Every command goes through this factory, so the hooks cannot be forgotten.

    Asserted on the client rather than on output because that is the actual
    regression risk: a seventh command written as ``httpx.Client()`` would trace
    nothing and no other test would notice.
    """
    with client.build_client() as c:
        assert len(c.event_hooks["request"]) == 1
        assert len(c.event_hooks["response"]) == 1


def _traced_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.Client:
    """A client from the real factory, with a transport the test controls."""
    return client.build_client(transport=httpx.MockTransport(handler))


def _login_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/security/login"):
        return httpx.Response(200, json={"access_token": ACCESS_TOKEN})
    return httpx.Response(200, json={"result": [], "count": 0})


def test_traces_method_url_status_and_duration(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with trace.verbose(), _traced_client(_login_handler) as c:
        client.login(c, BASE_URL, "admin", "pa55word")
        list(client.iter_roles(c, BASE_URL, ACCESS_TOKEN))

    captured = capsys.readouterr()
    err = captured.err

    assert re.search(
        rf"\[dp:http\] POST {re.escape(BASE_URL)}/api/v1/security/login "
        r"\| -> 200 \| \d+\.\dms",
        err,
    )
    assert re.search(
        r"\[dp:http\] GET \S+/security/roles/\S* \| -> 200 \| \d+\.\dms", err
    )
    # Tracing is a stderr concern only: --json has to stay pipeable.
    assert captured.out == ""


def test_never_traces_the_password_or_the_access_token(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The login body holds the admin password and every later call a token."""
    with trace.verbose(), _traced_client(_login_handler) as c:
        client.login(c, BASE_URL, "admin", "pa55word")
        list(client.iter_roles(c, BASE_URL, ACCESS_TOKEN))

    err = capsys.readouterr().err
    assert "pa55word" not in err
    assert ACCESS_TOKEN not in err
    assert "Authorization" not in err
    assert "Bearer" not in err


def test_traces_nothing_when_not_enabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # monkeypatch rather than trace.disable(): the flag is process state, and a
    # bare disable() would leak into whatever runs next under DP_VERBOSE=1.
    monkeypatch.setattr(trace, "_enabled", False)

    with _traced_client(_login_handler) as c:
        client.login(c, BASE_URL, "admin", "pa55word")

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


# --- error detail ---------------------------------------------------------
# How a detail line is built is the shared seam's subject, and is tested in
# tests/services/test_http.py. What matters here is that every Superset call
# site goes through it: a status line alone ("422 Unprocessable Entity") is not
# diagnosable by the person who ran the command, which is the only audience
# these errors have.


def _failing(status: int, reason: bytes, **body: object) -> Callable:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            extensions={"reason_phrase": reason},
            request=request,
            **body,  # type: ignore[arg-type]
        )

    return handler


@pytest.mark.parametrize(
    ("call", "action"),
    [
        (
            lambda c: client.create_user(c, BASE_URL, ACCESS_TOKEN, {}),
            "create Superset user",
        ),
        (
            lambda c: client.update_user(c, BASE_URL, ACCESS_TOKEN, 7, {}),
            "update Superset user",
        ),
        (
            lambda c: client.delete_user(c, BASE_URL, ACCESS_TOKEN, 7),
            "delete Superset user",
        ),
        (
            lambda c: list(client.iter_roles(c, BASE_URL, ACCESS_TOKEN)),
            "list Superset security items",
        ),
    ],
    ids=["create", "update", "delete", "list"],
)
def test_every_call_reports_the_servers_reason(call: Callable, action: str) -> None:
    """One helper builds these errors, so no call site can drop the body again."""
    handler = _failing(403, b"Forbidden", json={"message": "sorry, no"})

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as c,
        pytest.raises(ServiceError) as excinfo,
    ):
        call(c)

    assert f"Failed to {action} (403 Forbidden): sorry, no" == str(excinfo.value)


# --- write_headers / CSRF --------------------------------------------------
# Every mutating Superset call (datasets, dashboards, charts, SQL Lab) goes
# through this one helper. Its own contract -- token fetched, both headers
# added, cached per client -- is tested here; each call site only needs to
# prove it actually calls this helper (tested alongside that call site).


def test_write_headers_carries_the_csrf_token_and_referer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/security/csrf_token/")
        return httpx.Response(200, json={"result": "csrf-xyz"}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        headers = client.write_headers(c, BASE_URL, ACCESS_TOKEN)

    assert headers["X-CSRFToken"] == "csrf-xyz"
    assert headers["Referer"] == BASE_URL
    # Still a valid auth header, not a replacement for it.
    assert headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"


def test_write_headers_falls_back_to_auth_headers_without_a_token() -> None:
    # An instance with CSRF disabled (or an older API surface): the token
    # endpoint 404s, and the caller must still get usable headers back.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        headers = client.write_headers(c, BASE_URL, ACCESS_TOKEN)

    assert "X-CSRFToken" not in headers
    assert "Referer" not in headers
    assert headers == client.auth_headers(ACCESS_TOKEN)


def test_write_headers_fetches_the_csrf_token_once_per_client() -> None:
    # The duplicate flow calls this many times against one client in a
    # single run; each call re-fetching the token would be a GET Superset
    # does not need to see again.
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, json={"result": "csrf-xyz"}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        first = client.write_headers(c, BASE_URL, ACCESS_TOKEN)
        second = client.write_headers(c, BASE_URL, ACCESS_TOKEN)

    assert calls["count"] == 1
    assert first["X-CSRFToken"] == second["X-CSRFToken"] == "csrf-xyz"


def test_write_headers_caches_the_no_token_case_too() -> None:
    # An instance without CSRF enforcement should not be re-asked on every
    # write either -- "no token" is cached exactly like a real one.
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(404, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        client.write_headers(c, BASE_URL, ACCESS_TOKEN)
        client.write_headers(c, BASE_URL, ACCESS_TOKEN)

    assert calls["count"] == 1


def test_write_headers_fetches_a_separate_token_per_client() -> None:
    # The cache is keyed by client, not by access token: a token is bound to
    # the session cookie of the client that fetched it, so a second client
    # must not reuse the first's.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": "csrf-xyz"}, request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as c1,
        httpx.Client(transport=httpx.MockTransport(handler)) as c2,
    ):
        client.write_headers(c1, BASE_URL, ACCESS_TOKEN)
        headers = client.write_headers(c2, BASE_URL, ACCESS_TOKEN)

    assert headers["X-CSRFToken"] == "csrf-xyz"
