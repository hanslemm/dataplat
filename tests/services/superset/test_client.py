from __future__ import annotations

import httpx
import pytest

from dataplat.core.errors import ConfigError
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
                json={"result": [{"id": 1}], "pagination": {"page": 0, "page_size": 1, "total_pages": 2}},
                request=request,
            )
        return httpx.Response(
            200,
            json={"result": [{"id": 2}], "pagination": {"page": 1, "page_size": 1, "total_pages": 2}},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        rows = list(client.iter_security_items(c, "https://superset.test", "tok", "roles"))

    assert [row["id"] for row in rows] == [1, 2]
    assert calls["count"] == 2
