"""Connection listing, paging, and the "has any stream selected" predicate.

Same caveat as ``test_tags.py``: there is no Airbyte here, so these fakes prove
what *this* code does with a given response, not what Airbyte sends. That is
still the subject — every branch below exists to absorb a shape or a failure the
API is known to produce, and none of them had ever executed.

The three guards in ``list_connections`` are the interesting part. Each turns a
response that is not what was asked for into a sentence naming what arrived, and
each was written for a real failure: a gateway answering a redirect, an auth
proxy answering HTML, and a body that is neither.
"""

from __future__ import annotations

import json

import httpx
import pytest

from dataplat.core.errors import ServiceError
from dataplat.services.airbyte.connections import (
    connection_has_active_streams,
    get_connection,
    list_connections,
    patch_connection,
)


class _FakeResponse:
    def __init__(
        self,
        data=None,
        status_code=200,
        *,
        text=None,
        headers=None,
        bad_json=False,
    ):
        self.status_code = status_code
        self._data = data
        self._bad_json = bad_json
        self.headers = {"content-type": "application/json", **(headers or {})}
        self.text = text if text is not None else json.dumps(data or {})

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error",
                request=httpx.Request("GET", "http://test"),
                response=self,  # type: ignore[arg-type]
            )

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._data


class _FakeClient:
    """Returns each queued response once, then repeats the last."""

    def __init__(self, *responses: _FakeResponse):
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict | None]] = []

    def _next(self):
        return (
            self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        )

    def get(self, url, params=None, **kw):
        self.calls.append(("GET", url, params))
        return self._next()

    def patch(self, url, json=None, **kw):
        self.calls.append(("PATCH", url, json))
        return self._next()


def _page(*ids: str) -> _FakeResponse:
    return _FakeResponse({"data": [{"connectionId": i} for i in ids]})


# ---------------------------------------------------------------------------
# list_connections — paging
# ---------------------------------------------------------------------------


def test_pages_until_the_server_runs_out() -> None:
    client = _FakeClient(_page("a", "b"), _page("c"), _FakeResponse({"data": []}))

    found = [c["connectionId"] for c in list_connections(client, "http://ab", limit=2)]  # type: ignore[arg-type]

    assert found == ["a", "b", "c"]


def test_the_offset_advances_by_the_page_size() -> None:
    """A fixed offset would re-request page one forever."""
    client = _FakeClient(_page("a"), _page("b"), _FakeResponse({"data": []}))

    list(list_connections(client, "http://ab", limit=1))  # type: ignore[arg-type]

    offsets = [params["offset"] for _, _, params in client.calls]
    assert offsets == [0, 1, 2]


def test_deleted_connections_are_excluded_by_the_request() -> None:
    client = _FakeClient(_FakeResponse({"data": []}))

    list(list_connections(client, "http://ab"))  # type: ignore[arg-type]

    assert client.calls[0][2]["includeDeleted"] == "false"


def test_an_empty_first_page_yields_nothing_and_stops() -> None:
    client = _FakeClient(_FakeResponse({"data": []}))

    assert list(list_connections(client, "http://ab")) == []  # type: ignore[arg-type]
    assert len(client.calls) == 1


@pytest.mark.parametrize("payload", [{}, {"data": None}, {"other": []}])
def test_a_payload_without_data_stops_paging(payload) -> None:
    client = _FakeClient(_FakeResponse(payload))

    assert list(list_connections(client, "http://ab")) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# list_connections — the three guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_gateway_redirect_is_named_rather_than_followed(status: int) -> None:
    """An SSO or ingress redirect answers 200-with-a-login-page if followed.

    Reported as a redirect, with the destination, because the fix is a URL or a
    proxy setting and neither is guessable from "unexpected response".
    """
    client = _FakeClient(
        _FakeResponse(status_code=status, headers={"location": "https://sso/login"})
    )

    with pytest.raises(ServiceError) as exc:
        list(list_connections(client, "http://ab"))  # type: ignore[arg-type]

    assert f"status={status}" in str(exc.value)
    assert "https://sso/login" in str(exc.value)


def test_a_redirect_without_a_location_still_reports_the_status() -> None:
    client = _FakeClient(_FakeResponse(status_code=302, headers={"location": ""}))

    with pytest.raises(ServiceError, match="location=unknown"):
        list(list_connections(client, "http://ab"))  # type: ignore[arg-type]


def test_an_html_body_is_refused_before_it_is_parsed() -> None:
    """A proxy's login page is valid HTTP 200 and not a connection listing."""
    client = _FakeClient(
        _FakeResponse(
            text="<html>Sign in</html>",
            headers={"content-type": "text/html; charset=utf-8"},
        )
    )

    with pytest.raises(ServiceError) as exc:
        list(list_connections(client, "http://ab"))  # type: ignore[arg-type]

    assert "content-type=text/html" in str(exc.value)
    assert "Sign in" in str(exc.value)


def test_a_missing_content_type_is_reported_as_unknown() -> None:
    client = _FakeClient(_FakeResponse(text="junk", headers={"content-type": ""}))

    with pytest.raises(ServiceError, match="content-type=unknown"):
        list(list_connections(client, "http://ab"))  # type: ignore[arg-type]


def test_json_content_type_with_a_charset_is_accepted() -> None:
    """`application/json; charset=utf-8` is the common spelling and must pass."""
    client = _FakeClient(
        _FakeResponse(
            {"data": []}, headers={"content-type": "application/json; charset=utf-8"}
        )
    )

    assert list(list_connections(client, "http://ab")) == []  # type: ignore[arg-type]


def test_a_body_that_is_not_json_after_all_is_reported() -> None:
    client = _FakeClient(_FakeResponse(bad_json=True, text="garbage"))

    with pytest.raises(ServiceError) as exc:
        list(list_connections(client, "http://ab"))  # type: ignore[arg-type]

    assert "parse" in str(exc.value)
    assert "garbage" in str(exc.value)


def test_an_http_error_names_the_status() -> None:
    client = _FakeClient(_FakeResponse(status_code=401, text="unauthorized"))

    with pytest.raises(ServiceError) as exc:
        list(list_connections(client, "http://ab"))  # type: ignore[arg-type]

    assert "status=401" in str(exc.value)
    assert "unauthorized" in str(exc.value)


# ---------------------------------------------------------------------------
# get / patch
# ---------------------------------------------------------------------------


def test_get_connection_addresses_the_connection_by_id() -> None:
    client = _FakeClient(_FakeResponse({"connectionId": "c1"}))

    assert get_connection(client, "http://ab", "c1")["connectionId"] == "c1"  # type: ignore[arg-type]
    assert client.calls[0][1] == "http://ab/api/public/v1/connections/c1"


def test_get_connection_reports_a_missing_connection() -> None:
    client = _FakeClient(_FakeResponse(status_code=404, text="not found"))

    with pytest.raises(ServiceError) as exc:
        get_connection(client, "http://ab", "gone")  # type: ignore[arg-type]

    assert "status=404" in str(exc.value)


def test_patch_sends_only_the_updates_it_was_given() -> None:
    client = _FakeClient(_FakeResponse({"connectionId": "c1"}))

    patch_connection(client, "http://ab", "c1", {"name": "new"})  # type: ignore[arg-type]

    method, url, payload = client.calls[0]
    assert method == "PATCH"
    assert url == "http://ab/api/public/v1/connections/c1"
    assert payload == {"name": "new"}


def test_patch_reports_a_rejected_update() -> None:
    client = _FakeClient(_FakeResponse(status_code=422, text="bad cron"))

    with pytest.raises(ServiceError) as exc:
        patch_connection(client, "http://ab", "c1", {"schedule": "nonsense"})  # type: ignore[arg-type]

    assert "status=422" in str(exc.value)
    assert "bad cron" in str(exc.value)


def test_patch_reports_an_empty_error_body_as_empty() -> None:
    client = _FakeClient(_FakeResponse(status_code=500, text=""))

    with pytest.raises(ServiceError, match="body=empty"):
        patch_connection(client, "http://ab", "c1", {})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# connection_has_active_streams — three places "selected" can live
# ---------------------------------------------------------------------------


def test_selected_on_the_stream_config() -> None:
    detail = {"syncCatalog": {"streams": [{"config": {"selected": True}}]}}

    assert connection_has_active_streams(detail) is True


def test_selected_on_the_entry_itself() -> None:
    detail = {"syncCatalog": {"streams": [{"selected": True}]}}

    assert connection_has_active_streams(detail) is True


def test_selected_on_the_nested_stream() -> None:
    detail = {"syncCatalog": {"streams": [{"stream": {"selected": True}}]}}

    assert connection_has_active_streams(detail) is True


def test_the_catalog_key_has_two_spellings() -> None:
    assert (
        connection_has_active_streams(
            {"catalog": {"streams": [{"config": {"selected": True}}]}}
        )
        is True
    )


def test_one_selected_stream_among_many_is_enough() -> None:
    detail = {
        "syncCatalog": {
            "streams": [
                {"config": {"selected": False}},
                {"config": {"selected": True}},
            ]
        }
    }

    assert connection_has_active_streams(detail) is True


def test_all_streams_deselected_is_false() -> None:
    detail = {
        "syncCatalog": {"streams": [{"config": {"selected": False}}, {"config": {}}]}
    }

    assert connection_has_active_streams(detail) is False


@pytest.mark.parametrize(
    "detail",
    [
        {},
        {"syncCatalog": None},
        {"syncCatalog": "not a dict"},
        {"syncCatalog": {}},
        {"syncCatalog": {"streams": None}},
        {"syncCatalog": {"streams": "not a list"}},
        {"syncCatalog": {"streams": []}},
    ],
)
def test_a_malformed_catalog_is_false_rather_than_an_exception(detail) -> None:
    """This is a listing filter, so a shape it cannot read must not abort the
    listing — every connection after it would be lost too."""
    assert connection_has_active_streams(detail) is False


def test_a_non_dict_stream_entry_is_skipped_not_fatal() -> None:
    detail = {"syncCatalog": {"streams": ["junk", {"config": {"selected": True}}]}}

    assert connection_has_active_streams(detail) is True


def test_selected_must_be_true_not_merely_truthy() -> None:
    """`is True`, so a string or a 1 from a loose serializer does not count."""
    for value in ("true", 1, "yes"):
        detail = {"syncCatalog": {"streams": [{"config": {"selected": value}}]}}
        assert connection_has_active_streams(detail) is False
