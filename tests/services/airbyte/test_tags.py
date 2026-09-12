"""Tag resolution and merging.

A note on what these prove. There is no Airbyte to run against here — unlike the
database tiers, which execute real SQL — so every response below is a fake, and
a fake cannot tell you what Airbyte actually returns. What it *can* prove is what
**this code** does with a given shape, and that is the whole subject: these
functions exist because the API's tag payloads vary (``tagId`` or ``id``,
``workspaceId`` or ``workspace_id``, a list or a dict wrapping one), and every
branch handling that variance was previously unexecuted.

Where a test documents a behaviour that looks wrong rather than one that is, it
says so and pins the current answer instead of changing it. Guessing at an API
nobody here can observe is how the Redshift bugs in this repo happened.
"""

from __future__ import annotations

import httpx
import pytest

from dataplat.core.errors import ServiceError
from dataplat.services.airbyte.tags import (
    TagResolver,
    create_tag,
    list_tags,
    merge_tags,
    normalize_tag,
    tag_id,
)
from tests._responses import response


class _FakeClient:
    def __init__(self, *responses: httpx.Response):
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict | None]] = []

    def _next(self) -> httpx.Response:
        return (
            self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        )

    def get(self, url, **kw):
        self.calls.append(("GET", url, None))
        return self._next()

    def post(self, url, json=None, **kw):
        self.calls.append(("POST", url, json))
        return self._next()


# ---------------------------------------------------------------------------
# tag_id / normalize_tag — the shape variance everything else rests on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ({"tagId": "t1"}, "t1"),
        ({"id": "t1"}, "t1"),
        # tagId wins when both are present, because that is the key the update
        # API reads back.
        ({"tagId": "t1", "id": "other"}, "t1"),
        ({}, None),
        ({"name": "prod"}, None),
    ],
)
def test_tag_id_reads_either_spelling(tag: dict, expected: str | None) -> None:
    assert tag_id(tag) == expected


def test_normalize_tag_adds_the_key_the_update_api_expects() -> None:
    assert normalize_tag({"id": "t1", "name": "prod"}) == {
        "id": "t1",
        "name": "prod",
        "tagId": "t1",
    }


def test_normalize_tag_leaves_an_already_normal_tag_alone() -> None:
    tag = {"tagId": "t1", "name": "prod"}

    assert normalize_tag(tag) is tag


def test_normalize_tag_does_not_mutate_its_input() -> None:
    """It is called on tags that came from a response the caller still holds."""
    original = {"id": "t1"}

    normalize_tag(original)

    assert original == {"id": "t1"}


def test_normalize_tag_passes_through_a_tag_with_no_id_at_all() -> None:
    assert normalize_tag({"name": "prod"}) == {"name": "prod"}


# ---------------------------------------------------------------------------
# list_tags — three payload shapes and two failure modes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"data": [{"tagId": "t1"}]},
        {"tags": [{"tagId": "t1"}]},
        [{"tagId": "t1"}],
    ],
)
def test_list_tags_unwraps_every_shape_the_api_uses(payload) -> None:
    client = _FakeClient(response(payload))

    assert list_tags(client, "http://ab") == [{"tagId": "t1"}]  # type: ignore[arg-type]


def test_list_tags_prefers_data_over_tags() -> None:
    client = _FakeClient(response({"data": [{"tagId": "a"}], "tags": []}))

    assert list_tags(client, "http://ab") == [{"tagId": "a"}]  # type: ignore[arg-type]


@pytest.mark.parametrize("payload", [{}, {"data": None}, {"other": 1}, "a string", 7])
def test_an_unrecognised_payload_yields_no_tags_rather_than_raising(payload) -> None:
    """A listing that cannot be understood is empty, not an error.

    The caller (``TagResolver._prime``) treats an empty list as "nothing cached
    yet" and goes on to create, which is recoverable. Raising here would fail an
    otherwise-fine connection update over a response shape.
    """
    client = _FakeClient(response(payload))

    assert list_tags(client, "http://ab") == []  # type: ignore[arg-type]


def test_a_http_error_names_the_status_and_quotes_the_body() -> None:
    client = _FakeClient(response(None, status_code=403, text="forbidden"))

    with pytest.raises(ServiceError) as exc:
        list_tags(client, "http://ab")  # type: ignore[arg-type]

    assert str(exc.value) == "Failed to list tags (403 Forbidden): forbidden"


def test_an_empty_error_body_leaves_the_status_line_to_speak() -> None:
    """Nothing to add, so nothing is appended -- no dangling separator."""
    client = _FakeClient(response(None, status_code=500, text="   "))

    with pytest.raises(ServiceError) as exc:
        list_tags(client, "http://ab")  # type: ignore[arg-type]

    assert str(exc.value) == "Failed to list tags (500 Internal Server Error)"


def test_a_long_error_body_is_truncated() -> None:
    """A stack trace or an HTML error page must not become the whole message."""
    client = _FakeClient(response(None, status_code=500, text="x" * 5000))

    with pytest.raises(ServiceError) as exc:
        list_tags(client, "http://ab")  # type: ignore[arg-type]

    assert len(str(exc.value)) < 700


def test_an_unparseable_body_is_reported_as_such() -> None:
    client = _FakeClient(response(text="not json"))

    with pytest.raises(ServiceError, match="parse"):
        list_tags(client, "http://ab")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# create_tag
# ---------------------------------------------------------------------------


def test_create_tag_sends_only_the_fields_it_was_given() -> None:
    """Optional fields are omitted, not sent as null — the API rejects nulls for
    workspaceId, and a colour nobody asked for would be chosen by the server."""
    client = _FakeClient(response({"tagId": "t1"}))

    create_tag(client, "http://ab", "prod")  # type: ignore[arg-type]

    _, url, payload = client.calls[0]
    assert url == "http://ab/api/public/v1/tags"
    assert payload == {"name": "prod"}


def test_create_tag_includes_workspace_and_colour_when_present() -> None:
    client = _FakeClient(response({"tagId": "t1"}))

    create_tag(client, "http://ab", "prod", "ws-1", "#fff")  # type: ignore[arg-type]

    assert client.calls[0][2] == {
        "name": "prod",
        "workspaceId": "ws-1",
        "color": "#fff",
    }


def test_create_tag_reports_a_rejected_creation() -> None:
    client = _FakeClient(response(None, status_code=422, text="duplicate name"))

    with pytest.raises(ServiceError) as exc:
        create_tag(client, "http://ab", "prod")  # type: ignore[arg-type]

    assert str(exc.value) == (
        "Failed to create tag (422 Unprocessable Entity): duplicate name"
    )
    assert "duplicate name" in str(exc.value)


# ---------------------------------------------------------------------------
# TagResolver
# ---------------------------------------------------------------------------


def test_an_existing_tag_is_reused_rather_than_recreated() -> None:
    client = _FakeClient(response({"data": [{"id": "t1", "name": "prod"}]}))

    resolved = TagResolver(client, "http://ab").ensure("prod", None)  # type: ignore[arg-type]

    assert resolved == {"id": "t1", "name": "prod", "tagId": "t1"}
    # Listed, never created.
    assert [m for m, _, _ in client.calls] == ["GET"]


def test_the_listing_is_fetched_once_for_the_whole_invocation() -> None:
    """The cache is the point: a connection update resolving five tags must not
    list the workspace five times."""
    client = _FakeClient(
        response({"data": [{"id": "t1", "name": "a"}, {"id": "t2", "name": "b"}]})
    )
    resolver = TagResolver(client, "http://ab")  # type: ignore[arg-type]

    resolver.ensure("a", None)
    resolver.ensure("b", None)
    resolver.ensure("a", None)

    assert [m for m, _, _ in client.calls].count("GET") == 1


def test_a_missing_tag_is_created_and_then_cached() -> None:
    client = _FakeClient(
        response({"data": []}),
        response({"id": "new", "name": "prod"}),
    )
    resolver = TagResolver(client, "http://ab")  # type: ignore[arg-type]

    first = resolver.ensure("prod", None)
    second = resolver.ensure("prod", None)

    assert first == {"id": "new", "name": "prod", "tagId": "new"}
    assert second == first
    # Created once, despite two calls.
    assert [m for m, _, _ in client.calls].count("POST") == 1


def test_a_tag_without_a_name_is_not_cached() -> None:
    """It could never be looked up by name, and caching it under None would
    collide with every other nameless tag."""
    client = _FakeClient(
        response({"data": [{"id": "t1"}]}),
        response({"id": "new", "name": "prod"}),
    )

    TagResolver(client, "http://ab").ensure("prod", None)  # type: ignore[arg-type]

    assert [m for m, _, _ in client.calls].count("POST") == 1


def test_the_workspace_is_read_from_either_spelling() -> None:
    client = _FakeClient(
        response({"data": [{"id": "t1", "name": "prod", "workspace_id": "ws-1"}]})
    )

    resolved = TagResolver(client, "http://ab").ensure("prod", "ws-1")  # type: ignore[arg-type]

    assert resolved["tagId"] == "t1"
    assert [m for m, _, _ in client.calls] == ["GET"]  # reused, not created


def test_a_listing_that_omits_the_workspace_recreates_a_tag_that_exists() -> None:
    """Documents a real weakness rather than asserting it is correct.

    ``_prime`` caches under the workspace found *in the payload*; ``ensure``
    looks up the workspace it was *asked* about. When the listing omits
    ``workspaceId`` — which this code clearly anticipates, since it reads two
    spellings and falls back to None — the two keys never meet, and a tag that
    already exists is created again.

    Pinned, not fixed — and now with the evidence that says why it can stay
    pinned. Probed against the production Airbyte on 2026-09-12: all 27 tags
    came back carrying ``workspaceId``, the key set being exactly
    ``{tagId, name, color, workspaceId}``, and no two tags shared a name. So
    this branch is defensive, not live, and the duplicate it would create is
    unreachable there.

    Making the lookup fall back to the None-keyed entry would reuse a tag
    across workspaces, which is worse than creating a duplicate and stays
    unverifiable: the fallback only ever runs on a listing this Airbyte does
    not produce. If a future version drops the field, this test is what will
    say so.
    """
    client = _FakeClient(
        response({"data": [{"id": "t1", "name": "prod"}]}),
        response({"id": "dupe", "name": "prod"}),
    )

    resolved = TagResolver(client, "http://ab").ensure("prod", "ws-1")  # type: ignore[arg-type]

    assert resolved["tagId"] == "dupe"
    assert [m for m, _, _ in client.calls].count("POST") == 1


# ---------------------------------------------------------------------------
# merge_tags — the one that writes back to the server
# ---------------------------------------------------------------------------


def test_merge_keeps_first_seen_order() -> None:
    merged = merge_tags([{"tagId": "a"}], [{"tagId": "b"}])

    assert [t["tagId"] for t in merged] == ["a", "b"]


def test_merge_deduplicates_by_id_across_both_spellings() -> None:
    """The same tag arriving as `id` from one endpoint and `tagId` from another
    must not be added twice to a connection."""
    merged = merge_tags([{"id": "a", "name": "prod"}], [{"tagId": "a"}])

    assert len(merged) == 1
    assert merged[0]["tagId"] == "a"


def test_merge_normalizes_everything_it_returns() -> None:
    """The result is PUT back, and the update API reads `tagId`."""
    merged = merge_tags([{"id": "a"}], [])

    assert all("tagId" in tag for tag in merged)


def test_merging_nothing_into_nothing_is_empty() -> None:
    assert merge_tags([], []) == []


def test_re_adding_an_existing_tag_changes_nothing() -> None:
    existing = [{"tagId": "a", "name": "prod"}]

    assert merge_tags(existing, [{"tagId": "a", "name": "prod"}]) == existing


def test_a_tag_carrying_no_id_is_kept_rather_than_deleted() -> None:
    """The merged list is a connection's *complete* tag set, so dropping deletes.

    ``connections.py`` writes `"tags": merge_tags(existing_tags, new_tags)` back
    as the whole set, and ``existing_tags`` is whatever the connection payload
    held. An entry this code cannot identify used to be dropped, which did not
    merely fail to merge it — it removed the tag from the connection, so adding
    one tag silently deleted another.

    Keeping it is the safe half of the trade: if Airbyte rejects an id-less
    entry the update fails loudly and says why, and a loud failure beats a
    silent deletion. Whether Airbyte accepts it is exactly what cannot be
    settled without one, and does not need to be: either outcome is better
    than losing the tag.
    """
    merged = merge_tags([{"name": "orphan"}, {"tagId": "a"}], [{"tagId": "b"}])

    assert [t.get("name") for t in merged if not t.get("tagId")] == ["orphan"]
    assert [t.get("tagId") for t in merged if t.get("tagId")] == ["a", "b"]


def test_an_unidentifiable_tag_keeps_its_first_seen_position() -> None:
    """Order is the contract the identified entries already had."""
    merged = merge_tags([{"name": "orphan"}, {"tagId": "a"}], [])

    assert [t.get("tagId") or t.get("name") for t in merged] == ["orphan", "a"]


def test_the_same_unidentifiable_tag_is_not_added_twice() -> None:
    """Name is the only handle these have; two of one name is one tag."""
    merged = merge_tags([{"name": "orphan"}], [{"name": "orphan"}])

    assert len(merged) == 1


def test_a_tag_with_neither_id_nor_name_is_still_kept() -> None:
    """Pathological, and still not ours to delete.

    Two of them are two entries: with no handle to compare them by, collapsing
    them would be the same silent loss under a different name.
    """
    merged = merge_tags([{"color": "red"}, {"color": "blue"}], [])

    assert len(merged) == 2
