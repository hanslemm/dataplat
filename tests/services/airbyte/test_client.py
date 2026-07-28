from __future__ import annotations

import re
from collections.abc import Callable, Iterator

import httpx
import pytest

from dataplat.core import trace
from dataplat.core.errors import AuthError
from dataplat.services.airbyte import client


def test_build_auth_headers_defaults(monkeypatch) -> None:
    monkeypatch.delenv("AIRBYTE_AUTH_HEADER", raising=False)
    monkeypatch.delenv("AIRBYTE_AUTH_VALUE", raising=False)
    monkeypatch.delenv("AIRBYTE_AUTH_SCHEME", raising=False)

    headers = client.build_auth_headers("abc")

    assert headers["Authorization"] == "Bearer abc"
    assert headers["Accept"] == "application/json"


def test_build_authenticated_client_cloud_fallbacks_to_oss(monkeypatch) -> None:
    monkeypatch.setenv("AIRBYTE_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("AIRBYTE_CLIENT_ID", "cid")
    monkeypatch.setenv("AIRBYTE_CLIENT_SECRET", "sec")
    monkeypatch.setenv("AIRBYTE_EMAIL", "mail@example.com")
    monkeypatch.setenv("AIRBYTE_PASSWORD", "pw")

    monkeypatch.setattr(
        client,
        "get_access_token",
        lambda *a, **k: (_ for _ in ()).throw(AuthError("x")),
    )
    monkeypatch.setattr(client, "login_airbyte_oss", lambda *a, **k: "fallback-token")

    c, base_url = client.build_authenticated_client()
    try:
        assert base_url == "https://example.invalid"
        assert c.headers["Authorization"] == "Bearer fallback-token"
    finally:
        c.close()


def test_split_cron_timezone() -> None:
    expr, tz = client.split_cron_timezone("0 0 12 ? * * Europe/Berlin")
    assert expr == "0 0 12 ? * *"
    assert tz == "Europe/Berlin"


# --- request tracing ------------------------------------------------------
#
# `--verbose` exists to answer "what did we actually send", and this client is
# the one that handles credentials, so every test below asserts both halves:
# the request is described, and the credential is not.

BASE_URL = "https://airbyte.test"
SECRET = "sup3r-s3cret-jwt-value"
TOKEN_URL = f"{BASE_URL}/api/public/v1/applications/token"

# What trace_http emits for a completed request: `-> 200` and a duration.
# Pieces are joined with " | " so a URL path ending in /token cannot make the
# redactor eat the arrow (see dataplat.core.trace.trace_http).
COMPLETED = re.compile(r"\| -> \d{3} \|")


@pytest.fixture(autouse=True)
def _isolate_token_cache() -> Iterator[None]:
    """The token cache is process state; a test must not leave one behind.

    Without this, a cached token from one test makes the next one report a
    cache hit and never issue the request it is asserting about.
    """
    saved = dict(client._TOKEN_CACHE)
    client._TOKEN_CACHE.update({"token": None, "expires_at": 0.0})
    try:
        yield
    finally:
        client._TOKEN_CACHE.clear()
        client._TOKEN_CACHE.update(saved)


@pytest.fixture
def cloud_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIRBYTE_BASE_URL", BASE_URL)
    monkeypatch.setenv("AIRBYTE_CLIENT_ID", "cid")
    monkeypatch.setenv("AIRBYTE_CLIENT_SECRET", "cs3cret")
    monkeypatch.delenv("AIRBYTE_EMAIL", raising=False)
    monkeypatch.delenv("AIRBYTE_PASSWORD", raising=False)
    monkeypatch.delenv("AIRBYTE_AUTH_HEADER", raising=False)
    monkeypatch.delenv("AIRBYTE_AUTH_VALUE", raising=False)
    monkeypatch.delenv("AIRBYTE_AUTH_SCHEME", raising=False)


def _serve(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """Route the real client's transport to ``handler``.

    ``build_authenticated_client`` constructs ``httpx.HTTPTransport`` itself, so
    replacing that name is what lets the test drive the genuine client — real
    event hooks, real auth flow — instead of a stand-in that proves nothing
    about the wiring.
    """
    monkeypatch.setattr(
        httpx, "HTTPTransport", lambda *a, **k: httpx.MockTransport(handler)
    )


def _cloud_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/applications/token"):
        return httpx.Response(200, json={"access_token": SECRET, "expires_in": 900})
    return httpx.Response(200, json={"data": []})


def test_traces_every_request_with_status_and_duration(
    cloud_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _serve(monkeypatch, _cloud_handler)

    with trace.verbose():
        c, _ = client.build_authenticated_client()
        try:
            c.get(f"{BASE_URL}/api/public/v1/workspaces")
        finally:
            c.close()

    captured = capsys.readouterr()
    lines = [ln for ln in captured.err.splitlines() if ln.startswith("[dp:http]")]

    # Pre-flight then outcome, for both the token exchange and the GET: the
    # pre-flight line is the only trace a request that never returns leaves.
    assert sum(1 for ln in lines if "/workspaces" in ln) == 2
    outcome = next(ln for ln in lines if "GET" in ln and COMPLETED.search(ln))
    assert f"GET {BASE_URL}/api/public/v1/workspaces" in outcome

    # --json and --format csv are readable only if nothing lands on stdout.
    assert captured.out == ""


def test_never_traces_the_token_or_the_client_secret(
    cloud_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The credential must be absent because it was never passed, not masked."""
    _serve(monkeypatch, _cloud_handler)

    with trace.verbose():
        c, _ = client.build_authenticated_client()
        try:
            # The Authorization header is set on the client by now, so this
            # request carries the token; the hooks must still not see it.
            c.get(f"{BASE_URL}/api/public/v1/workspaces")
        finally:
            c.close()

    err = capsys.readouterr().err
    assert SECRET not in err
    assert "cs3cret" not in err
    assert "Authorization" not in err
    assert "Bearer" not in err


def test_traces_that_a_token_was_obtained_and_when_it_expires(
    cloud_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _serve(monkeypatch, _cloud_handler)

    with trace.verbose():
        client.build_authenticated_client()[0].close()

    err = capsys.readouterr().err
    assert "airbyte auth mode=cloud" in err
    assert "access_token acquired" in err
    assert re.search(r"expires_at=\d{9,}", err)
    assert "from=expires_in" in err
    assert SECRET not in err

    # The token exchange is traced like any other request, arrow included. It
    # briefly was not: joined with spaces, redact() read this URL's `/token`
    # path as a bearer-scheme prefix and masked the `->` after it. trace_http
    # now joins with " | ", which ends that pattern's match, so the endpoint
    # whose name triggers the redactor is exactly the one this pins.
    assert re.search(rf"POST {re.escape(TOKEN_URL)} \| -> 200 \| \d+\.\dms", err)


def test_traces_a_cache_hit_instead_of_a_second_request(
    cloud_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 401 mid-run means something different if the token was reused."""
    _serve(monkeypatch, _cloud_handler)

    with trace.verbose():
        client.build_authenticated_client()[0].close()
        capsys.readouterr()
        client.build_authenticated_client()[0].close()

    err = capsys.readouterr().err
    assert "reusing cached access_token" in err
    assert TOKEN_URL not in err
    assert SECRET not in err


def test_traces_the_oss_jwt_expiry_and_the_fallback_reason(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A successful fallback discards the Cloud error; the trace is its record.

    The JWT is decoded for its ``exp`` claim, so the expiry can be reported
    without the token — the one thing that must never appear.
    """
    monkeypatch.setenv("AIRBYTE_BASE_URL", BASE_URL)
    monkeypatch.setenv("AIRBYTE_CLIENT_ID", "cid")
    monkeypatch.setenv("AIRBYTE_CLIENT_SECRET", "cs3cret")
    monkeypatch.setenv("AIRBYTE_EMAIL", "admin@example.com")
    monkeypatch.setenv("AIRBYTE_PASSWORD", "pa55word")
    monkeypatch.delenv("AIRBYTE_AUTH_HEADER", raising=False)
    monkeypatch.delenv("AIRBYTE_AUTH_VALUE", raising=False)
    monkeypatch.delenv("AIRBYTE_AUTH_SCHEME", raising=False)

    # exp claim 1900000000, unsigned -- parse_jwt_exp does not verify.
    jwt = (
        "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjE5MDAwMDAwMDB9.c2lnbmF0dXJlLXdlLW5ldmVyLWNoZWNr"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/applications/token"):
            return httpx.Response(401, json={"message": "client is disabled"})
        if request.url.path.endswith("/users/login"):
            return httpx.Response(200, json={"token": jwt})
        raise AssertionError(f"unexpected: {request.url}")

    _serve(monkeypatch, handler)

    with trace.verbose():
        client.build_authenticated_client()[0].close()

    err = capsys.readouterr().err
    assert "airbyte cloud auth failed" in err
    assert "falling back to OSS login" in err
    assert "jwt acquired, exp=1900000000" in err
    assert jwt not in err
    assert "pa55word" not in err


def test_traces_nothing_at_all_when_not_enabled(
    cloud_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Off is off: no buffering, no cost, no stderr."""
    _serve(monkeypatch, _cloud_handler)
    # monkeypatch rather than trace.disable(): the flag is process state, and a
    # bare disable() would leak into whatever runs next under DP_VERBOSE=1.
    monkeypatch.setattr(trace, "_enabled", False)

    c, _ = client.build_authenticated_client()
    try:
        c.get(f"{BASE_URL}/api/public/v1/workspaces")
    finally:
        c.close()

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""
