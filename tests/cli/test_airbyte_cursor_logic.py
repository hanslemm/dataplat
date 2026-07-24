"""Unit tests for pure Airbyte cursor classification/rewrite logic."""
from __future__ import annotations

from datetime import date

import pytest

from dataplat.cli.ingest.airbyte._cursor import (
    classify_cursor_value,
    parse_target_date,
    plan_cursor_rewrites,
    rewrite_date,
    rewrite_xmin,
)


def _xmin_stream(xid: int, *, raw: int | None = None, wraparound: int = 0) -> dict:
    inner = {
        "state_type": "xmin",
        "version": 2,
        "xmin_xid_value": xid,
        "xmin_raw_value": xid if raw is None else raw,
        "num_wraparound": wraparound,
    }
    return {
        "streamDescriptor": {"name": "orders", "namespace": "public"},
        "streamState": inner,
    }


@pytest.mark.parametrize(
    "value",
    [
        "2024-06-01T00:00:00Z",
        "2024-06-01T12:30:00+02:00",
        "2024-06-01T00:00:00",
        "2024-06-01T00:00:00.123456Z",
        "2024-06-01",
    ],
)
def test_classify_date_values(value: str) -> None:
    assert classify_cursor_value(value) == "date"


@pytest.mark.parametrize(
    "value",
    [
        1711753326,            # epoch int -> opaque (conservative)
        123,
        12.5,
        "12345",               # numeric string, not ISO
        "not-a-date",
        "2024",                # year only, not a full date
        "",
        None,
        {"lsn": 123456},       # CDC/global object
        ["2024-06-01"],
        True,
    ],
)
def test_classify_opaque_values(value: object) -> None:
    assert classify_cursor_value(value) == "opaque"


def test_parse_target_date_accepts_date_and_timestamp() -> None:
    assert parse_target_date("2024-01-01") == date(2024, 1, 1)
    assert parse_target_date("2024-01-01T09:15:00Z") == date(2024, 1, 1)
    assert parse_target_date("garbage") is None


@pytest.mark.parametrize(
    ("old", "expected"),
    [
        ("2024-06-01T00:00:00Z", "2024-01-01T00:00:00Z"),
        ("2024-06-15T12:30:45+02:00", "2024-01-01T12:30:45+02:00"),
        ("2024-06-01T00:00:00", "2024-01-01T00:00:00"),
        ("2024-06-01T00:00:00.500000Z", "2024-01-01T00:00:00.500000Z"),
        ("2024-06-01", "2024-01-01"),
        ("2024-06-01T00:00:00.123Z", "2024-01-01T00:00:00.123Z"),
        ("2024-06-15T12:30:45+0200", "2024-01-01T12:30:45+0200"),
        ("2024-06-01 12:30:00", "2024-01-01 12:30:00"),
        ("20240601", "20240101"),
    ],
)
def test_rewrite_date_preserves_format(old: str, expected: str) -> None:
    assert rewrite_date(old, date(2024, 1, 1)) == expected


def test_plan_rewrites_only_date_cursors() -> None:
    state = {
        "connectionId": "c1",
        "stateType": "stream",
        "streamState": [
            {
                "streamDescriptor": {"name": "orders", "namespace": "public"},
                "streamState": {"updated_at": "2024-06-01T00:00:00Z"},
            },
            {
                "streamDescriptor": {"name": "events", "namespace": "public"},
                "streamState": {"id": 987654},
            },
        ],
    }
    new_state, actions = plan_cursor_rewrites(state, date(2024, 1, 1))

    # original object untouched (deep copy)
    assert state["streamState"][0]["streamState"]["updated_at"] == "2024-06-01T00:00:00Z"
    # date cursor rewritten in the new state
    assert new_state["streamState"][0]["streamState"]["updated_at"] == "2024-01-01T00:00:00Z"
    # opaque cursor untouched in the new state
    assert new_state["streamState"][1]["streamState"]["id"] == 987654

    rewrite = [a for a in actions if a["action"] == "rewrite"]
    skipped = [a for a in actions if a["action"] == "skip:opaque"]
    assert rewrite == [
        {"stream": "orders", "namespace": "public", "key": "updated_at",
         "old": "2024-06-01T00:00:00Z", "new": "2024-01-01T00:00:00Z", "action": "rewrite"}
    ]
    assert skipped == [
        {"stream": "events", "namespace": "public", "key": "id",
         "old": 987654, "new": 987654, "action": "skip:opaque"}
    ]


def test_plan_rewrites_global_state_is_noop() -> None:
    state = {"connectionId": "c1", "stateType": "global", "globalState": {"lsn": 42}}
    new_state, actions = plan_cursor_rewrites(state, date(2024, 1, 1))
    assert actions == []
    assert new_state == state


# --- xmin -----------------------------------------------------------------


def test_rewrite_xmin_absolute_ignores_old() -> None:
    assert rewrite_xmin(999, xmin_value=0) == 0
    assert rewrite_xmin("garbage", xmin_value=42) == 42


@pytest.mark.parametrize(
    ("old", "factor", "expected"),
    [
        (1000, 0.1, 100),
        (12345, 0.1, 1234),   # round(1234.5) -> 1234 (banker's)
        (1000, 0.0, 0),
        (1000, 2.0, 2000),
        ("500", 0.5, 250),    # numeric string coerces
    ],
)
def test_rewrite_xmin_factor(old: object, factor: float, expected: int) -> None:
    assert rewrite_xmin(old, xmin_factor=factor) == expected


def test_plan_xmin_factor_scales_both_fields() -> None:
    state = {"stateType": "stream", "streamState": [_xmin_stream(1000, raw=1000)]}
    new_state, actions = plan_cursor_rewrites(state, xmin_factor=0.1)
    inner = new_state["streamState"][0]["streamState"]
    assert inner["xmin_xid_value"] == 100
    assert inner["xmin_raw_value"] == 100
    assert inner["version"] == 2            # untouched
    assert inner["num_wraparound"] == 0     # untouched
    assert actions == [
        {"stream": "orders", "namespace": "public", "key": "xmin",
         "old": 1000, "new": 100, "action": "rewrite:xmin"}
    ]


def test_plan_xmin_absolute_zero_resets() -> None:
    state = {"stateType": "stream", "streamState": [_xmin_stream(777)]}
    new_state, actions = plan_cursor_rewrites(state, xmin_value=0)
    inner = new_state["streamState"][0]["streamState"]
    assert inner["xmin_xid_value"] == 0
    assert inner["xmin_raw_value"] == 0
    assert actions[0]["action"] == "rewrite:xmin"


def test_plan_xmin_skipped_when_no_xmin_op() -> None:
    state = {"stateType": "stream", "streamState": [_xmin_stream(555)]}
    new_state, actions = plan_cursor_rewrites(state, date(2024, 1, 1))
    # only --to was given: the xmin stream is left alone
    assert new_state["streamState"][0]["streamState"]["xmin_xid_value"] == 555
    assert actions == [
        {"stream": "orders", "namespace": "public", "key": "xmin",
         "old": 555, "new": 555, "action": "skip:xmin"}
    ]


def test_plan_date_skipped_when_no_target() -> None:
    state = {
        "stateType": "stream",
        "streamState": [
            {"streamDescriptor": {"name": "orders", "namespace": "public"},
             "streamState": {"updated_at": "2024-06-01T00:00:00Z"}},
        ],
    }
    new_state, actions = plan_cursor_rewrites(state, xmin_value=0)
    # only an xmin op was given: date cursor untouched, reported as skip:date
    assert new_state["streamState"][0]["streamState"]["updated_at"] == "2024-06-01T00:00:00Z"
    assert actions[0]["action"] == "skip:date"


def test_plan_combined_date_and_xmin_in_one_pass() -> None:
    state = {
        "stateType": "stream",
        "streamState": [
            {"streamDescriptor": {"name": "orders", "namespace": "public"},
             "streamState": {"updated_at": "2024-06-01T00:00:00Z"}},
            _xmin_stream(1000),
        ],
    }
    new_state, actions = plan_cursor_rewrites(
        state, date(2024, 1, 1), xmin_factor=0.1
    )
    streams = new_state["streamState"]
    assert streams[0]["streamState"]["updated_at"] == "2024-01-01T00:00:00Z"
    assert streams[1]["streamState"]["xmin_xid_value"] == 100
    kinds = {a["action"] for a in actions}
    assert kinds == {"rewrite", "rewrite:xmin"}


def _cursor_based_stream(cursor: str, *, count: int = 1) -> dict:
    return {
        "streamDescriptor": {"name": "orders", "namespace": "public"},
        "streamState": {
            "cursor": cursor, "version": 2, "state_type": "cursor_based",
            "stream_name": "orders", "cursor_field": ["updated_at"],
            "stream_namespace": "public", "cursor_record_count": count,
        },
    }


def test_plan_cursor_based_rewrites_cursor_and_resets_count() -> None:
    state = {
        "stateType": "stream",
        "streamState": [_cursor_based_stream("2012-11-26T08:18:52.438210", count=1904)],
    }
    new_state, actions = plan_cursor_rewrites(state, date(2024, 1, 1))
    inner = new_state["streamState"][0]["streamState"]
    # date prefix swapped, time-of-day preserved
    assert inner["cursor"] == "2024-01-01T08:18:52.438210"
    # boundary-dedup counter reset
    assert inner["cursor_record_count"] == 0
    # metadata preserved
    assert inner["state_type"] == "cursor_based"
    assert inner["cursor_field"] == ["updated_at"]
    kinds = {(a["key"], a["action"]) for a in actions}
    assert ("cursor", "rewrite") in kinds
    assert ("cursor_record_count", "reset:count") in kinds


def test_plan_cursor_based_count_untouched_when_cursor_not_rewritten() -> None:
    # xmin-only op (target=None): the date cursor is skipped, so the count stays
    state = {
        "stateType": "stream",
        "streamState": [_cursor_based_stream("2012-11-26T08:18:52.438210", count=1904)],
    }
    new_state, actions = plan_cursor_rewrites(state, xmin_value=0)
    inner = new_state["streamState"][0]["streamState"]
    assert inner["cursor"] == "2012-11-26T08:18:52.438210"
    assert inner["cursor_record_count"] == 1904
    assert not any(a["action"] == "reset:count" for a in actions)


def test_plan_cursor_based_count_zero_not_reported_as_reset() -> None:
    state = {
        "stateType": "stream",
        "streamState": [_cursor_based_stream("2012-11-26T08:18:52.438210", count=0)],
    }
    _, actions = plan_cursor_rewrites(state, date(2024, 1, 1))
    # already 0: no reset action, reported as a plain skip
    assert not any(a["action"] == "reset:count" for a in actions)
    count_rows = [a for a in actions if a["key"] == "cursor_record_count"]
    assert count_rows and count_rows[0]["action"] == "skip:opaque"


def test_plan_xmin_non_numeric_payload_is_skipped() -> None:
    stream = _xmin_stream(0)
    stream["streamState"]["xmin_xid_value"] = "not-a-number"
    stream["streamState"]["xmin_raw_value"] = "not-a-number"
    state = {"stateType": "stream", "streamState": [stream]}
    new_state, actions = plan_cursor_rewrites(state, xmin_factor=0.1)
    assert new_state["streamState"][0]["streamState"]["xmin_xid_value"] == "not-a-number"
    assert actions[0]["action"] == "skip:xmin"


# --- only_rewind ------------------------------------------------------------


def _date_stream(name: str, value: str, **extra) -> dict:
    return {
        "streamDescriptor": {"name": name, "namespace": "public"},
        "streamState": {"updated_at": value, **extra},
    }


def test_only_rewind_skips_date_cursor_behind_target() -> None:
    state = {
        "stateType": "stream",
        "streamState": [
            _date_stream("ahead", "2024-06-01T00:00:00Z"),   # after target: rewind
            _date_stream("behind", "2023-01-01T00:00:00Z"),  # before target: skip
        ],
    }
    new_state, actions = plan_cursor_rewrites(
        state, date(2024, 1, 1), only_rewind=True
    )
    assert new_state["streamState"][0]["streamState"]["updated_at"] == (
        "2024-01-01T00:00:00Z"
    )
    # the behind stream's cursor must not move forward
    assert new_state["streamState"][1]["streamState"]["updated_at"] == (
        "2023-01-01T00:00:00Z"
    )
    by_stream = {a["stream"]: a for a in actions}
    assert by_stream["ahead"]["action"] == "rewrite"
    assert by_stream["behind"] == {
        "stream": "behind", "namespace": "public", "key": "updated_at",
        "old": "2023-01-01T00:00:00Z", "new": "2023-01-01T00:00:00Z",
        "action": "skip:advance",
    }


def test_only_rewind_same_day_still_rewrites() -> None:
    # equal calendar date is not an advance; the (no-op) rewrite is kept
    state = {"stateType": "stream",
             "streamState": [_date_stream("orders", "2024-01-01T12:00:00Z")]}
    _, actions = plan_cursor_rewrites(state, date(2024, 1, 1), only_rewind=True)
    assert actions[0]["action"] == "rewrite"
    assert actions[0]["new"] == "2024-01-01T12:00:00Z"


def test_without_only_rewind_forward_move_still_happens() -> None:
    # regression guard: default behavior is unchanged
    state = {"stateType": "stream",
             "streamState": [_date_stream("behind", "2023-01-01T00:00:00Z")]}
    new_state, actions = plan_cursor_rewrites(state, date(2024, 1, 1))
    assert actions[0]["action"] == "rewrite"
    assert new_state["streamState"][0]["streamState"]["updated_at"] == (
        "2024-01-01T00:00:00Z"
    )


def test_only_rewind_skip_leaves_cursor_record_count_alone() -> None:
    state = {"stateType": "stream",
             "streamState": [_date_stream(
                 "behind", "2023-01-01T00:00:00Z", cursor_record_count=7)]}
    new_state, actions = plan_cursor_rewrites(
        state, date(2024, 1, 1), only_rewind=True
    )
    assert new_state["streamState"][0]["streamState"]["cursor_record_count"] == 7
    assert not any(a["action"] == "reset:count" for a in actions)


def test_only_rewind_skips_xmin_increase_absolute() -> None:
    state = {"stateType": "stream", "streamState": [_xmin_stream(100)]}
    new_state, actions = plan_cursor_rewrites(
        state, xmin_value=500, only_rewind=True
    )
    inner = new_state["streamState"][0]["streamState"]
    assert inner["xmin_xid_value"] == 100
    assert inner["xmin_raw_value"] == 100
    assert actions == [
        {"stream": "orders", "namespace": "public", "key": "xmin",
         "old": 100, "new": 100, "action": "skip:advance"}
    ]


def test_only_rewind_skips_xmin_increase_factor() -> None:
    state = {"stateType": "stream", "streamState": [_xmin_stream(100)]}
    _, actions = plan_cursor_rewrites(state, xmin_factor=1.5, only_rewind=True)
    assert actions[0]["action"] == "skip:advance"


def test_only_rewind_allows_xmin_decrease() -> None:
    state = {"stateType": "stream", "streamState": [_xmin_stream(1000)]}
    new_state, actions = plan_cursor_rewrites(
        state, xmin_factor=0.1, only_rewind=True
    )
    assert new_state["streamState"][0]["streamState"]["xmin_xid_value"] == 100
    assert actions[0]["action"] == "rewrite:xmin"


def test_only_rewind_allows_equal_xmin() -> None:
    # setting the same xid is not an advance
    state = {"stateType": "stream", "streamState": [_xmin_stream(100)]}
    _, actions = plan_cursor_rewrites(state, xmin_value=100, only_rewind=True)
    assert actions[0]["action"] == "rewrite:xmin"
