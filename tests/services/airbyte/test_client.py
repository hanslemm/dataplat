from __future__ import annotations

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

    monkeypatch.setattr(client, "get_access_token", lambda *a, **k: (_ for _ in ()).throw(AuthError("x")))
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
